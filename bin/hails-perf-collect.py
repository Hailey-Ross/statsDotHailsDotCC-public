#!/usr/bin/env python3
# The VPS performance collector: a resident sampler for the Performance page.
#
#   python3 hails-perf-collect.py            (run by hails-perf.service, no arguments)
#
# Samples /proc plus service and HTTP probes, and writes fixed size binary rings, perf_meta.json and
# perf_svc.json under /var/lib/hails-stats. hails-perf.py renders what it collects.
# Stdlib only, single threaded, rings allocated once and never grown.
#
# Tiers, and the window each one serves:
#   raw 10s, 3 hours    : 1 min, 5 min, 15 min, 30 min, hour
#   5 min, 7 days       : day, week
#   1 hour, 1 year      : month
#   1 day, 5 years      : year
# Rollups cascade: raw closes into 5 min, 5 min into hourly, hourly into daily.
#
# Configuration comes from the environment, normally /etc/hails-stats/config.env.
import os, sys, time, json, struct, subprocess, socket, urllib.request, urllib.error

DIR = os.environ.get("HAILS_PERF_DIR", "/var/lib/hails-stats")
INTERVAL = float(os.environ.get("HAILS_PERF_INTERVAL", "10"))     # sample cadence, seconds
HTTP_EVERY = float(os.environ.get("HAILS_PERF_HTTP", "300"))      # HTTP probe cadence
LOCAL_EVERY = float(os.environ.get("HAILS_PERF_LOCAL", "300"))    # systemd and docker cadence
SVC_FLUSH = 300.0                                                 # service json flush cadence
SCHEMA = 1


def detect_disk():
    """Return the first non removable whole disk name in /sys/block."""
    try:
        for name in sorted(os.listdir("/sys/block")):
            if name.startswith(("loop", "ram", "dm-", "sr", "zram")):
                continue
            try:
                with open("/sys/block/%s/removable" % name) as fh:
                    if fh.read().strip() == "1":
                        continue
            except Exception:
                pass
            return name
    except Exception:
        pass
    return "sda"


def detect_nic():
    """Return the interface holding the default route."""
    try:
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                f = line.split()
                if len(f) > 1 and f[1] == "00000000":     # destination 0.0.0.0 is the default route
                    return f[0]
    except Exception:
        pass
    return "eth0"


def core_count():
    n = 0
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("cpu") and line[3:4].isdigit():
                    n += 1
    except Exception:
        pass
    return n or 1


DISK = os.environ.get("HAILS_PERF_DISK") or detect_disk()         # the disk to chart, see lsblk
NIC = os.environ.get("HAILS_PERF_NIC") or detect_nic()            # the WAN NIC, not docker0 or veth
CORES = core_count()

# The metric vector. This is the on disk order: appending is safe, reordering is not, so bump SCHEMA
# if you reorder and the rings will be rebuilt rather than misread.
# NAMES goes into perf_meta.json and hails-perf.py builds its CPU columns from it. A change in core
# count changes the record width, so the rings are reallocated and the old history is lost.
NAMES = (["load1", "load5", "load15", "cpu"]
         + ["cpu%d" % i for i in range(CORES)]
         + ["steal", "iowait",
            "mem_pct", "mem_used", "swap_used", "mem_total", "swap_total",
            "dsk_read", "dsk_write", "dsk_iops", "dsk_util",
            "net_rx", "net_tx",
            "fs_pct", "fs_used", "fs_total", "fs_avail"])
NM = len(NAMES)
IX = {n: i for i, n in enumerate(NAMES)}

RAW_FMT = "<I" + "f" * NM                 # ts, then one float per metric
RAW_SZ = struct.calcsize(RAW_FMT)
AGG_FMT = "<IH" + "f" * (NM * 3)          # ts, n, then sum[], min[], max[]
AGG_SZ = struct.calcsize(AGG_FMT)

# (key, filename, record size, slot count, bucket seconds).
TIERS = [("raw", "perf_raw.bin", RAW_SZ, 1080, 0),
         # 2100 slots not 2016: a week is exactly 2016 buckets, so the extra slots give margin.
         ("5m", "perf_5m.bin", AGG_SZ, 2100, 300),
         ("1h", "perf_1h.bin", AGG_SZ, 8760, 3600),
         ("1d", "perf_1d.bin", AGG_SZ, 1825, 86400)]

META = os.path.join(DIR, "perf_meta.json")
SVCF = os.path.join(DIR, "perf_svc.json")


def log(msg):
    sys.stderr.write("hails-perf: %s\n" % msg)
    sys.stderr.flush()


class Ring(object):
    """A fixed size ring of packed records. Allocated once, written one slot at a time."""

    def __init__(self, path, recsz, slots):
        self.path, self.recsz, self.slots = path, recsz, slots
        self.head = 0          # next slot to write
        self.count = 0         # records written, capped at slots
        want = recsz * slots
        try:
            if os.path.getsize(path) != want:
                raise OSError("size mismatch")
            self.fh = open(path, "r+b")
        except Exception:
            # Allocate the whole ring up front so the on disk footprint is fixed.
            self.fh = open(path, "w+b")
            self.fh.write(b"\0" * want)
            self.fh.flush()
            self.head = 0
            self.count = 0

    def append(self, blob):
        self.fh.seek(self.head * self.recsz)
        self.fh.write(blob)
        self.fh.flush()
        self.head = (self.head + 1) % self.slots
        if self.count < self.slots:
            self.count += 1

    def read_all(self):
        """Return every record as packed bytes, oldest first."""
        if not self.count:
            return []
        self.fh.seek(0)
        buf = self.fh.read()
        out = []
        start = (self.head - self.count) % self.slots
        for i in range(self.count):
            s = ((start + i) % self.slots) * self.recsz
            out.append(buf[s:s + self.recsz])
        return out


# /proc readers.
def read(path):
    with open(path, "r") as fh:
        return fh.read()


def proc_loadavg():
    p = read("/proc/loadavg").split()
    return float(p[0]), float(p[1]), float(p[2])


def proc_stat():
    """Cumulative jiffies per cpu: {key: (busy, total, steal, iowait)}."""
    out = {}
    for line in read("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            continue
        f = line.split()
        key = f[0]
        v = [int(x) for x in f[1:]]
        while len(v) < 8:
            v.append(0)
        user, nice, sysj, idle, iowait, irq, softirq, steal = v[:8]
        total = sum(v[:8])
        busy = total - idle - iowait
        out[key] = (busy, total, steal, iowait)
    return out


def proc_meminfo():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, _, rest = line.partition(":")
        try:
            m[k] = int(rest.split()[0]) * 1024
        except Exception:
            pass
    total = m.get("MemTotal", 0)
    avail = m.get("MemAvailable", m.get("MemFree", 0))
    swtot = m.get("SwapTotal", 0)
    swfree = m.get("SwapFree", 0)
    return total, total - avail, swtot - swfree, swtot


def proc_diskstats():
    for line in read("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) > 13 and f[2] == DISK:
            # reads, sectors read, writes, sectors written, io_ticks (ms the disk was busy)
            return int(f[3]), int(f[5]), int(f[7]), int(f[9]), int(f[12])
    return None


def proc_netdev():
    for line in read("/proc/net/dev").splitlines():
        name, _, rest = line.partition(":")
        if name.strip() == NIC:
            f = rest.split()
            return int(f[0]), int(f[8])       # rx bytes, tx bytes
    return None


def proc_uptime():
    return float(read("/proc/uptime").split()[0])


def fs_usage():
    """Return total, used, available and used percent, following df's convention.

    Use% is computed against used plus available, which excludes the root reserved blocks."""
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    usable = used + avail
    return total, used, avail, (used * 100.0 / usable) if usable else 0.0


def delta(cur, prev):
    """Return the counter delta, or None if the counter went backwards (a reboot or a lost device)."""
    if cur is None or prev is None:
        return None
    d = cur - prev
    return None if d < 0 else d


class Sampler(object):
    def __init__(self):
        self.prev = None
        self.prev_t = None

    def take(self):
        """Return a list of NM floats, or None if this is the first sample (no deltas yet)."""
        now = time.time()
        cpu = proc_stat()
        dsk = proc_diskstats()
        net = proc_netdev()
        cur = (now, cpu, dsk, net)
        prev, self.prev = self.prev, cur
        if prev is None:
            return None
        pt, pcpu, pdsk, pnet = prev
        dt = now - pt
        if dt <= 0:
            return None

        v = [0.0] * NM
        v[IX["load1"]], v[IX["load5"]], v[IX["load15"]] = proc_loadavg()

        # A counter going backwards means a reboot between samples, so the whole vector is dropped
        # rather than zero filled.
        regressed = [False]

        def d(cur_v, prev_v):
            out = delta(cur_v, prev_v)
            if out is None:
                regressed[0] = True
            return out

        # CPU busy percent, aggregate and per core.
        def cpu_pct(key):
            c, p = cpu.get(key), pcpu.get(key)
            if not c or not p:
                return 0.0, 0.0, 0.0
            dtot = d(c[1], p[1])
            if not dtot:
                return 0.0, 0.0, 0.0
            dbusy = d(c[0], p[0]) or 0
            dsteal = d(c[2], p[2]) or 0
            dio = d(c[3], p[3]) or 0
            return (dbusy * 100.0 / dtot, dsteal * 100.0 / dtot, dio * 100.0 / dtot)

        v[IX["cpu"]], v[IX["steal"]], v[IX["iowait"]] = cpu_pct("cpu")
        for i in range(CORES):
            k = "cpu%d" % i
            if k in IX:
                v[IX[k]] = cpu_pct(k)[0]

        mtot, mused, swused, swtot = proc_meminfo()
        v[IX["mem_used"]] = float(mused)
        v[IX["swap_used"]] = float(swused)
        v[IX["mem_total"]] = float(mtot)
        v[IX["swap_total"]] = float(swtot)
        v[IX["mem_pct"]] = (mused * 100.0 / mtot) if mtot else 0.0

        if dsk and pdsk:
            drd = d(dsk[1], pdsk[1])
            dwr = d(dsk[3], pdsk[3])
            dio = d(dsk[0], pdsk[0])
            dio2 = d(dsk[2], pdsk[2])
            dtick = d(dsk[4], pdsk[4])
            if drd is not None:
                v[IX["dsk_read"]] = drd * 512.0 / dt
            if dwr is not None:
                v[IX["dsk_write"]] = dwr * 512.0 / dt
            if dio is not None and dio2 is not None:
                v[IX["dsk_iops"]] = (dio + dio2) / dt
            if dtick is not None:
                v[IX["dsk_util"]] = min(100.0, dtick / (dt * 1000.0) * 100.0)

        if net and pnet:
            drx = d(net[0], pnet[0])
            dtx = d(net[1], pnet[1])
            if drx is not None:
                v[IX["net_rx"]] = drx / dt
            if dtx is not None:
                v[IX["net_tx"]] = dtx / dt

        if regressed[0]:
            return None                   # the machine rebooted between samples, drop this vector

        ftot, fused, favail, fpct = fs_usage()
        v[IX["fs_total"]] = float(ftot)
        v[IX["fs_used"]] = float(fused)
        v[IX["fs_avail"]] = float(favail)
        v[IX["fs_pct"]] = fpct
        return v


class Bucket(object):
    """Running n, sum, min, max per metric for one time bucket."""

    def __init__(self, bid):
        self.bid = bid
        self.n = 0
        self.sum = [0.0] * NM
        self.min = [0.0] * NM
        self.max = [0.0] * NM

    def add_sample(self, v):
        if self.n == 0:
            self.min = list(v)
            self.max = list(v)
        else:
            for i in range(NM):
                if v[i] < self.min[i]:
                    self.min[i] = v[i]
                if v[i] > self.max[i]:
                    self.max[i] = v[i]
        for i in range(NM):
            self.sum[i] += v[i]
        self.n += 1

    def add_bucket(self, b):
        """Fold a lower tier bucket in, so each tier is built from the one below it."""
        if b.n == 0:
            return
        if self.n == 0:
            self.min = list(b.min)
            self.max = list(b.max)
        else:
            for i in range(NM):
                if b.min[i] < self.min[i]:
                    self.min[i] = b.min[i]
                if b.max[i] > self.max[i]:
                    self.max[i] = b.max[i]
        for i in range(NM):
            self.sum[i] += b.sum[i]
        self.n += b.n

    def pack(self):
        return struct.pack(AGG_FMT, self.bid, min(self.n, 65535),
                           *(self.sum + self.min + self.max))

    def to_dict(self):
        return {"bid": self.bid, "n": self.n, "sum": self.sum, "min": self.min, "max": self.max}

    @staticmethod
    def from_dict(d):
        try:
            if len(d["sum"]) != NM or len(d["min"]) != NM or len(d["max"]) != NM:
                return None
            b = Bucket(int(d["bid"]))
            b.n = int(d["n"])
            b.sum = [float(x) for x in d["sum"]]
            b.min = [float(x) for x in d["min"]]
            b.max = [float(x) for x in d["max"]]
            return b
        except Exception:
            return None


def parse_units(raw):
    return [u.strip() if u.strip().endswith(".service") else u.strip() + ".service"
            for u in raw.split(",") if u.strip()]


def parse_targets(raw):
    """Parse HAILS_PERF_TARGETS, a semicolon separated list of name|url|codes, into tuples.

        site|https://example.com/|200 ; shortener|https://l.example.com/|200,301

    Codes are the statuses that count as healthy for that host, since a redirect or an auth
    challenge is the correct answer for some of them."""
    out = []
    for chunk in raw.split(";"):
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        codes = (200,)
        if len(parts) > 2 and parts[2]:
            try:
                codes = tuple(int(c) for c in parts[2].split(",") if c.strip())
            except ValueError:
                codes = (200,)
        out.append((parts[0], parts[1], codes or (200,)))
    return out


# Which local units to report, set via HAILS_PERF_UNITS.
SYSTEMD_UNITS = parse_units(os.environ.get("HAILS_PERF_UNITS", "caddy.service,docker.service"))

# Which sites to probe over the network. Empty by default, so a fresh install probes nothing.
HTTP_TARGETS = parse_targets(os.environ.get("HAILS_PERF_TARGETS", ""))

UA = "hails-perf-probe"     # must stay in sync with PROBE_UA in hails-stats-pre.py, which drops these lines


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Do not follow redirects: a 301 or 302 is the healthy answer for some hosts and must be seen.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def one_request(url, method):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", UA)
    try:
        r = OPENER.open(req, timeout=5)
        code = r.getcode()
        r.close()
        return code
    except urllib.error.HTTPError as e:
        try:
            e.close()
        except Exception:
            pass
        return e.code                     # a real answer, just not a 2xx
    except Exception:
        return 0                          # DNS failure, refused, timeout, TLS error


def http_probe(url):
    """Return (status, latency_ms). status is 0 when the host could not be reached at all.

    HEAD first to skip the body, falling back to GET when the server rejects the method."""
    t0 = time.monotonic()
    code = one_request(url, "HEAD")
    if code in (405, 501):
        code = one_request(url, "GET")
    return code, (time.monotonic() - t0) * 1000.0


def systemd_states():
    """Return {unit id: systemctl property dict} using one subprocess for all units."""
    out = {}
    try:
        p = subprocess.run(["systemctl", "show", "--no-pager",
                            "-p", "Id", "-p", "ActiveState", "-p", "SubState",
                            "-p", "ActiveEnterTimestampMonotonic", "-p", "MemoryCurrent"]
                           + SYSTEMD_UNITS,
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        log("systemctl probe failed: %s" % e)
        return out
    cur = {}
    for line in p.stdout.splitlines():
        if not line.strip():
            if cur.get("Id"):
                out[cur["Id"]] = cur
            cur = {}
            continue
        k, _, val = line.partition("=")
        cur[k] = val
    if cur.get("Id"):
        out[cur["Id"]] = cur
    return out


def docker_states():
    out = {}
    try:
        p = subprocess.run(["docker", "ps", "--no-trunc",
                            "--format", "{{.Names}}\t{{.Status}}\t{{.State}}"],
                           capture_output=True, text=True, timeout=20)
    except Exception:
        return out                        # docker absent or busy, not fatal
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) >= 3:
            out[f[0]] = {"status": f[1], "state": f[2]}
    return out


def hour_key(ts):
    return time.strftime("%Y-%m-%dT%H", time.gmtime(ts))


def day_key(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class Services(object):
    """Current service state plus hourly and daily availability history, stored in perf_svc.json."""

    def __init__(self):
        self.doc = {"since": int(time.time()), "updated": 0, "cur": {}, "hourly": {}, "daily": {}}
        try:
            with open(SVCF, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and "cur" in loaded:
                self.doc = loaded
                self.doc.setdefault("hourly", {})
                self.doc.setdefault("daily", {})
        except Exception:
            pass

    def record(self, name, ok, latency_ms):
        now = time.time()
        for tier, key, keep in (("hourly", hour_key(now), 48), ("daily", day_key(now), 400)):
            svc = self.doc[tier].setdefault(name, {})
            b = svc.setdefault(key, [0, 0, 0.0, 0.0])
            b[0] += 1
            if ok:
                b[1] += 1
                b[2] += latency_ms
                if latency_ms > b[3]:
                    b[3] = latency_ms
            if len(svc) > keep:
                for k in sorted(svc.keys())[:-keep]:
                    del svc[k]

    def prune(self, now):
        """Forget services that have stopped being probed, so removed units stop rendering as up.

        Three local cycles of grace, so one slow or failed probe round never evicts a live service."""
        cutoff = now - max(3 * LOCAL_EVERY, 900)
        for name in [n for n, c in self.doc["cur"].items() if c.get("checked", 0) < cutoff]:
            del self.doc["cur"][name]
            for tier in ("hourly", "daily"):
                self.doc.get(tier, {}).pop(name, None)

    def flush(self):
        self.doc["updated"] = int(time.time())
        tmp = SVCF + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.doc, fh, separators=(",", ":"))
            os.replace(tmp, SVCF)
        except Exception as e:
            log("service flush failed: %s" % e)


def load_meta():
    try:
        with open(META, "r", encoding="utf-8") as fh:
            m = json.load(fh)
        if m.get("schema") == SCHEMA:
            return m
    except Exception:
        pass
    return {"schema": SCHEMA, "since": int(time.time()), "heads": {}}


def save_meta(meta, rings, open_b=None):
    meta["heads"] = {k: [r.head, r.count] for k, r in rings.items()}
    meta["updated"] = int(time.time())
    meta["names"] = NAMES
    meta["interval"] = INTERVAL
    # Persist the buckets still filling, or a restart loses them: a daily bucket closes only when the
    # first hour of the next day closes, so a restart in that window would drop a whole day.
    if open_b is not None:
        meta["open"] = {k: b.to_dict() for k, b in open_b.items() if b is not None and b.n}
    tmp = META + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, separators=(",", ":"))
        os.replace(tmp, META)
    except Exception as e:
        log("meta flush failed: %s" % e)


def main():
    os.makedirs(DIR, exist_ok=True)
    meta = load_meta()

    rings = {}
    for key, fname, recsz, slots, _ in TIERS:
        r = Ring(os.path.join(DIR, fname), recsz, slots)
        h = meta.get("heads", {}).get(key)
        if h and isinstance(h, list) and len(h) == 2:
            r.head = h[0] % slots
            r.count = min(h[1], slots)
        rings[key] = r

    sampler = Sampler()
    svcs = Services()
    # One open bucket per rollup tier, restored from the meta file so a restart resumes them.
    open_b = {key: None for key, _, _, _, secs in TIERS if secs}
    for key, d in (meta.get("open") or {}).items():
        if key in open_b and isinstance(d, dict):
            b = Bucket.from_dict(d)
            if b is not None:
                open_b[key] = b
                log("resumed open %s bucket, n=%d" % (key, b.n))

    last = {"http": 0.0, "local": 0.0, "svc": 0.0, "meta": 0.0}
    tick = time.monotonic()

    log("started, dir=%s interval=%.0fs disk=%s nic=%s" % (DIR, INTERVAL, DISK, NIC))

    while True:
        try:
            cycle(sampler, svcs, rings, open_b, meta, last)
        except Exception as e:
            # Never die on one bad cycle: with Restart=always a persistent fault would respawn the
            # process every few seconds and reprobe every target each time.
            log("cycle failed: %s" % e)
        # Sleep to the next tick on an absolute schedule so the cadence does not drift with the work.
        tick += INTERVAL
        gap = tick - time.monotonic()
        if gap < 0:
            tick = time.monotonic()
            gap = 0
        time.sleep(gap)


def cycle(sampler, svcs, rings, open_b, meta, last):
    """Take one sample and run whichever slower probe jobs are due."""
    now = time.time()
    if True:
        v = sampler.take()

        if v is not None:
            rings["raw"].append(struct.pack(RAW_FMT, int(now), *v))
            # Cascade the sample up the tiers. Each bucket is keyed on the timestamp of the item
            # being folded in, never on now, or every hour loses its own last 5 minutes.
            carry = None
            for key, _, _, _, secs in TIERS:
                if not secs:
                    continue
                if key == "5m":
                    item_ts, item = now, v
                else:
                    if carry is None:
                        break                 # nothing closed below, so nothing to fold up
                    item_ts, item = carry.bid, carry
                bid = int(item_ts // secs) * secs
                b = open_b.get(key)
                closed = None
                if b is not None and b.bid != bid:
                    rings[key].append(b.pack())
                    closed = b
                    b = None
                if b is None:
                    b = Bucket(bid)
                    open_b[key] = b
                if key == "5m":
                    b.add_sample(item)
                else:
                    b.add_bucket(item)
                carry = closed

        # Probes, on their own slower cadences.
        if now - last["http"] >= HTTP_EVERY:
            last["http"] = now
            for name, url, expect in HTTP_TARGETS:
                try:
                    code, ms = http_probe(url)
                except Exception:
                    code, ms = 0, 0.0
                ok = code in expect
                svcs.record(name, ok, ms)
                cur = svcs.doc["cur"].setdefault(name, {})
                cur.update({"kind": "http", "ok": bool(ok), "code": code,
                            "ms": round(ms, 1), "checked": int(now)})
                if ok:
                    cur["last_ok"] = int(now)

        if now - last["local"] >= LOCAL_EVERY:
            last["local"] = now
            boot = now - proc_uptime()
            for unit, st in systemd_states().items():
                name = unit.replace(".service", "")
                active = st.get("ActiveState", "") == "active"
                # ActiveEnterTimestampMonotonic is microseconds since boot, unaffected by clock steps.
                since = 0
                try:
                    mono = int(st.get("ActiveEnterTimestampMonotonic", "0"))
                    if mono > 0:
                        since = int(boot + mono / 1000000.0)
                except Exception:
                    pass
                mem = 0
                try:
                    mem = int(st.get("MemoryCurrent", "0"))
                except Exception:
                    pass
                svcs.doc["cur"][name] = {"kind": "systemd", "ok": active,
                                         "state": st.get("SubState", st.get("ActiveState", "")),
                                         "since": since, "mem": mem, "checked": int(now)}
                svcs.record(name, active, 0.0)
            for cname, st in docker_states().items():
                status = st.get("status", "")
                ok = st.get("state", "") == "running" and "unhealthy" not in status
                svcs.doc["cur"][cname] = {"kind": "docker", "ok": ok, "state": st.get("state", ""),
                                          "status": status, "checked": int(now)}
                svcs.record(cname, ok, 0.0)
            svcs.doc["uptime"] = int(proc_uptime())
            svcs.doc["boot"] = int(boot)
            svcs.prune(now)

        if now - last["svc"] >= SVC_FLUSH:
            last["svc"] = now
            svcs.flush()
        if now - last["meta"] >= 60:
            last["meta"] = now
            save_meta(meta, rings, open_b)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
