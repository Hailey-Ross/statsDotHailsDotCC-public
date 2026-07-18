#!/usr/bin/env python3
# The VPS performance collector: a resident sampler for the Performance page.
#
#   python3 hails-perf-collect.py            (run by hails-perf.service, no arguments)
#
# Samples /proc plus service and HTTP probes, and writes fixed size binary rings, perf_meta.json and
# perf_svc.json under /var/lib/hails-stats. hails-perf.py renders what it collects. Nothing here has
# a history to read from: every /proc counter is cumulative since boot, so this daemon is the only
# source of the past.
#
# Stdlib only, single threaded, rings allocated once and never grown.
#
# Tiers, and the window each one serves:
#   raw 10s, 3 hours    : 1 min, 5 min, 15 min, 30 min, hour
#   5 min, 7 days       : day, week
#   1 hour, 1 year      : month
#   1 day, 5 years      : year
# Rollups cascade: raw closes into 5 min, 5 min into hourly, hourly into daily.
#
# Hardware is discovered at startup, so a resized box picks up new cores, disks, volumes and NICs on
# the next restart. Each entity owns its own ring files, which is what makes that safe: adding a core
# creates one new set of files and leaves every existing ring untouched. Sharing one wide record
# would mean the record width changed, and a width change wipes every tier.
#
# Configuration comes from the environment, normally /etc/hails-stats/config.env.
#
# No dash punctuation in text: commas and colons only.
import os, sys, time, json, struct, subprocess, zlib, urllib.request, urllib.error

DIR = os.environ.get("HAILS_PERF_DIR", "/var/lib/hails-stats")
INTERVAL = float(os.environ.get("HAILS_PERF_INTERVAL", "10"))     # sample cadence, seconds
HTTP_EVERY = float(os.environ.get("HAILS_PERF_HTTP", "300"))      # HTTP probe cadence
LOCAL_EVERY = float(os.environ.get("HAILS_PERF_LOCAL", "300"))    # systemd and docker cadence
SVC_FLUSH = 300.0                                                 # service json flush cadence
SCHEMA = 2

META = os.path.join(DIR, "perf_meta.json")
SVCF = os.path.join(DIR, "perf_svc.json")

# Metrics held per entity in each group. Widths here are fixed, only the number of entities varies.
GROUPS = {
    "sys":  ["load1", "load5", "load15", "cpu", "steal", "iowait",
             "mem_pct", "mem_used", "mem_total", "swap_used", "swap_total"],
    "cpu":  ["busy"],
    "disk": ["read", "write", "iops", "util"],
    "fs":   ["pct", "used", "total", "avail"],
    "net":  ["rx", "tx"],
}

# (tier key, slot count, bucket seconds). 2100 five minute slots not 2016: a week is exactly 2016,
# so the exact figure would leave the week window resting on the single oldest record.
TIERS = [("raw", 1080, 0), ("5m", 2100, 300), ("1h", 8760, 3600), ("1d", 1825, 86400)]


def log(msg):
    sys.stderr.write("hails-perf: %s\n" % msg)
    sys.stderr.flush()


def safe(name):
    return "".join(c if c.isalnum() else "_" for c in name).strip("_") or "root"


def read(path):
    with open(path, "r") as fh:
        return fh.read()


# Hardware discovery. Runs once at startup, so new hardware appears after a restart.
def list_cores():
    out = []
    try:
        for line in read("/proc/stat").splitlines():
            if line.startswith("cpu") and line[3:4].isdigit():
                out.append(line.split()[0])
    except Exception:
        pass
    return out or ["cpu0"]


def list_disks():
    """Whole disks worth charting, skipping loop, ram, device mapper, optical and removable.
    HAILS_PERF_DISK pins the list by hand if discovery ever picks wrong."""
    pin = os.environ.get("HAILS_PERF_DISK", "").strip()
    if pin:
        return [d.strip() for d in pin.split(",") if d.strip()]
    out = []
    try:
        for name in sorted(os.listdir("/sys/block")):
            if name.startswith(("loop", "ram", "dm-", "sr", "zram", "md")):
                continue
            try:
                if read("/sys/block/%s/removable" % name).strip() == "1":
                    continue
            except Exception:
                pass
            out.append(name)
    except Exception:
        pass
    return out


SKIP_FS = {"tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs", "cgroup", "cgroup2",
           "devpts", "debugfs", "tracefs", "securityfs", "pstore", "bpf", "configfs", "fusectl",
           "hugetlbfs", "mqueue", "autofs", "binfmt_misc", "efivarfs", "ramfs", "nsfs", "rpc_pipefs"}


def list_filesystems():
    """Real on disk filesystems as (mount, device), deduplicated by device so bind mounts and
    container layers do not appear several times."""
    out = []
    seen = set()
    try:
        for line in read("/proc/mounts").splitlines():
            f = line.split()
            if len(f) < 3:
                continue
            dev, mount, kind = f[0], f[1], f[2]
            if kind in SKIP_FS or not dev.startswith("/dev/"):
                continue
            if dev in seen:
                continue
            seen.add(dev)
            out.append((mount.replace("\\040", " "), dev))
    except Exception:
        pass
    return out


def list_nics():
    """Physical interfaces only. A NIC is kept when it has a device symlink, which selects the real
    hardware and drops lo, docker0, every br- bridge and every veth. Those veth pairs are created and
    destroyed with each container, so charting them would fill the store with dead files.
    HAILS_PERF_NIC pins the list by hand if discovery ever picks wrong."""
    pin = os.environ.get("HAILS_PERF_NIC", "").strip()
    if pin:
        return [n.strip() for n in pin.split(",") if n.strip()]
    out = []
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if os.path.exists("/sys/class/net/%s/device" % name):
                out.append(name)
    except Exception:
        pass
    return out or ["lo"]


# Ring files
class Ring(object):
    """A fixed size ring of packed records. Allocated once, written one slot at a time."""

    def __init__(self, path, recsz, slots):
        self.path, self.recsz, self.slots = path, recsz, slots
        self.head = 0
        self.count = 0
        want = recsz * slots
        # Decide create versus open on the size ALONE. Anything else that can go wrong, a short read
        # or a bad unpack, must not reach the branch that zero fills the file, or a transient IO
        # error would cost the five year ring.
        try:
            usable = os.path.getsize(path) == want
        except Exception:
            usable = False

        if usable:
            self.fh = open(path, "r+b")
            try:
                self.scan()
            except Exception as e:
                # Unreadable content, but the file is the right size, so keep it and append from the
                # start rather than destroying records that may still be perfectly good.
                log("could not scan %s (%s), appending from slot 0" % (os.path.basename(path), e))
                self.head = 0
                self.count = 0
            return

        # Wrong size means the metric list for this group changed, so the old records cannot be read
        # back. Move them aside and say so rather than truncating in place. Hardware changes do NOT
        # come through here: each entity owns a file of its own fixed width.
        if os.path.exists(path):
            try:
                os.replace(path, path + ".old")
                log("layout changed for %s, previous data kept as %s.old"
                    % (os.path.basename(path), os.path.basename(path)))
            except Exception:
                pass
        self.fh = open(path, "w+b")
        self.fh.write(b"\0" * want)
        self.fh.flush()
        self.head = 0
        self.count = 0

    def scan(self):
        """Recover head and count from the file itself by finding the newest timestamp.

        The alternative is trusting the pointers in perf_meta.json, which are only flushed once a
        minute, so a restart would rewind head and overwrite the most recent records. Every record
        starts with its own timestamp, and an unwritten slot is zeroed, so the file can always say
        where it got to."""
        self.fh.seek(0)
        buf = self.fh.read()
        newest = 0
        newest_at = -1
        used = 0
        for i in range(self.slots):
            ts = struct.unpack_from("<I", buf, i * self.recsz)[0]
            if not ts:
                continue
            used += 1
            if ts >= newest:
                newest = ts
                newest_at = i
        self.count = used
        self.head = 0 if newest_at < 0 else (newest_at + 1) % self.slots

    def append(self, blob):
        self.fh.seek(self.head * self.recsz)
        self.fh.write(blob)
        self.fh.flush()
        self.head = (self.head + 1) % self.slots
        if self.count < self.slots:
            self.count += 1


class Bucket(object):
    """Running n, sum, min, max per metric for one time bucket of one entity."""

    def __init__(self, bid, nm):
        self.bid = bid
        self.nm = nm
        self.n = 0
        self.sum = [0.0] * nm
        self.min = [0.0] * nm
        self.max = [0.0] * nm

    def add_sample(self, v):
        if self.n == 0:
            self.min = list(v)
            self.max = list(v)
        else:
            for i in range(self.nm):
                if v[i] < self.min[i]:
                    self.min[i] = v[i]
                if v[i] > self.max[i]:
                    self.max[i] = v[i]
        for i in range(self.nm):
            self.sum[i] += v[i]
        self.n += 1

    def add_bucket(self, b):
        """Fold a lower tier bucket in, so hourly is built from the 5 min buckets, not from raw."""
        if b.n == 0:
            return
        if self.n == 0:
            self.min = list(b.min)
            self.max = list(b.max)
        else:
            for i in range(self.nm):
                if b.min[i] < self.min[i]:
                    self.min[i] = b.min[i]
                if b.max[i] > self.max[i]:
                    self.max[i] = b.max[i]
        for i in range(self.nm):
            self.sum[i] += b.sum[i]
        self.n += b.n

    def pack(self, fmt):
        return struct.pack(fmt, self.bid, min(self.n, 65535), *(self.sum + self.min + self.max))

    def to_dict(self):
        return {"bid": self.bid, "n": self.n, "sum": self.sum, "min": self.min, "max": self.max}

    @staticmethod
    def from_dict(d, nm):
        try:
            if len(d["sum"]) != nm or len(d["min"]) != nm or len(d["max"]) != nm:
                return None
            b = Bucket(int(d["bid"]), nm)
            b.n = int(d["n"])
            b.sum = [float(x) for x in d["sum"]]
            b.min = [float(x) for x in d["min"]]
            b.max = [float(x) for x in d["max"]]
            return b
        except Exception:
            return None


class Series(object):
    """One entity's four tier ring set. Adding an entity creates new files and disturbs nothing."""

    def __init__(self, group, entity, metrics, key=None):
        self.group, self.entity, self.metrics = group, entity, metrics
        self.nm = len(metrics)
        self.raw_fmt = "<I" + "f" * self.nm
        self.agg_fmt = "<IH" + "f" * (self.nm * 3)
        raw_sz = struct.calcsize(self.raw_fmt)
        agg_sz = struct.calcsize(self.agg_fmt)
        self.key = key or (group if entity is None else "%s_%s" % (group, safe(entity)))
        self.rings = {}
        self.open_b = {}
        for tier, slots, secs in TIERS:
            path = os.path.join(DIR, "perf_%s_%s.bin" % (self.key, tier))
            self.rings[tier] = Ring(path, raw_sz if tier == "raw" else agg_sz, slots)
            if secs:
                self.open_b[tier] = None

    def add(self, now, v):
        self.rings["raw"].append(struct.pack(self.raw_fmt, int(now), *v))
        # Each tier is keyed on the timestamp of the item being folded in, never on now: 5 min
        # boundaries align with hour boundaries, so at the top of an hour the closing 5 min bucket
        # still belongs to the hour that just ended.
        carry = None
        for tier, _, secs in TIERS:
            if not secs:
                continue
            if tier == "5m":
                item_ts, item = now, v
            else:
                if carry is None:
                    break
                item_ts, item = carry.bid, carry
            bid = int(item_ts // secs) * secs
            b = self.open_b.get(tier)
            closed = None
            if b is not None and b.bid != bid:
                self.rings[tier].append(b.pack(self.agg_fmt))
                closed = b
                b = None
            if b is None:
                b = Bucket(bid, self.nm)
                self.open_b[tier] = b
            if tier == "5m":
                b.add_sample(item)
            else:
                b.add_bucket(item)
            carry = closed


# /proc readers
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
        v = [int(x) for x in f[1:]]
        while len(v) < 8:
            v.append(0)
        total = sum(v[:8])
        out[f[0]] = (total - v[3] - v[4], total, v[7], v[4])
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
    return total, total - avail, swtot - m.get("SwapFree", 0), swtot


def proc_diskstats():
    """{device: (reads, sectors read, writes, sectors written, io_ticks)}."""
    out = {}
    for line in read("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) > 13:
            out[f[2]] = (int(f[3]), int(f[5]), int(f[7]), int(f[9]), int(f[12]))
    return out


def proc_netdev():
    """{interface: (rx bytes, tx bytes)}."""
    out = {}
    for line in read("/proc/net/dev").splitlines():
        name, _, rest = line.partition(":")
        f = rest.split()
        if len(f) > 8:
            out[name.strip()] = (int(f[0]), int(f[8]))
    return out


def proc_uptime():
    return float(read("/proc/uptime").split()[0])


def fs_usage(mount):
    """Total, used, available and used percent, following df: the percent is used over used plus
    available, which excludes the blocks reserved for root."""
    st = os.statvfs(mount)
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    usable = used + avail
    return total, used, avail, (used * 100.0 / usable) if usable else 0.0


def delta(cur, prev):
    """Counter delta, or None if it went backwards, which means a reboot or a vanished device."""
    if cur is None or prev is None:
        return None
    d = cur - prev
    return None if d < 0 else d


class Sampler(object):
    """Reads /proc once per tick and returns {(group, entity): [values]}."""

    def __init__(self, cores, disks, fs, nics):
        self.cores, self.disks, self.fs, self.nics = cores, disks, fs, nics
        self.prev = None

    def take(self):
        now = time.time()
        cpu, dsk, net = proc_stat(), proc_diskstats(), proc_netdev()
        prev, self.prev = self.prev, (now, cpu, dsk, net)
        if prev is None:
            return now, None
        pt, pcpu, pdsk, pnet = prev
        dt = now - pt
        if dt <= 0:
            return now, None

        # A counter going backwards means the machine rebooted between samples, so the whole cycle is
        # dropped rather than zero filled: a zero would drag every mean containing it down.
        regressed = [False]

        def d(c, p):
            out = delta(c, p)
            if out is None:
                regressed[0] = True
            return out

        def cpu_pct(key):
            c, p = cpu.get(key), pcpu.get(key)
            if not c or not p:
                return 0.0, 0.0, 0.0
            dtot = d(c[1], p[1])
            if not dtot:
                return 0.0, 0.0, 0.0
            return ((d(c[0], p[0]) or 0) * 100.0 / dtot, (d(c[2], p[2]) or 0) * 100.0 / dtot,
                    (d(c[3], p[3]) or 0) * 100.0 / dtot)

        out = {}
        l1, l5, l15 = proc_loadavg()
        agg, steal, iowait = cpu_pct("cpu")
        mtot, mused, swused, swtot = proc_meminfo()
        out[("sys", None)] = [l1, l5, l15, agg, steal, iowait,
                              (mused * 100.0 / mtot) if mtot else 0.0,
                              float(mused), float(mtot), float(swused), float(swtot)]

        for c in self.cores:
            out[("cpu", c)] = [cpu_pct(c)[0]]

        for name in self.disks:
            c, p = dsk.get(name), pdsk.get(name)
            if not c or not p:
                continue
            drd, dwr = d(c[1], p[1]), d(c[3], p[3])
            dio, dio2, dtick = d(c[0], p[0]), d(c[2], p[2]), d(c[4], p[4])
            out[("disk", name)] = [
                (drd * 512.0 / dt) if drd is not None else 0.0,
                (dwr * 512.0 / dt) if dwr is not None else 0.0,
                ((dio + dio2) / dt) if (dio is not None and dio2 is not None) else 0.0,
                min(100.0, dtick / (dt * 1000.0) * 100.0) if dtick is not None else 0.0]

        for name in self.nics:
            c, p = net.get(name), pnet.get(name)
            if not c or not p:
                continue
            drx, dtx = d(c[0], p[0]), d(c[1], p[1])
            out[("net", name)] = [(drx / dt) if drx is not None else 0.0,
                                  (dtx / dt) if dtx is not None else 0.0]

        if regressed[0]:
            return now, None

        for mount, _dev in self.fs:
            try:
                ftot, fused, favail, fpct = fs_usage(mount)
            except Exception:
                continue
            out[("fs", mount)] = [fpct, float(fused), float(ftot), float(favail)]
        return now, out


# Service probes
def parse_units(raw):
    return [u.strip() if u.strip().endswith(".service") else u.strip() + ".service"
            for u in raw.split(",") if u.strip()]


def parse_targets(raw):
    """HAILS_PERF_TARGETS is name|url|codes entries separated by semicolons. The codes are the
    statuses that count as healthy, which matters because plenty of healthy hosts answer a redirect
    or an auth challenge on their root."""
    out = []
    for chunk in raw.split(";"):
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        codes = (200,)
        if len(parts) > 2 and parts[2]:
            try:
                codes = tuple(int(c) for c in parts[2].split(",") if c.strip()) or (200,)
            except ValueError:
                codes = (200,)
        out.append((parts[0], parts[1], codes))
    return out


SYSTEMD_UNITS = parse_units(os.environ.get("HAILS_PERF_UNITS", "caddy.service,docker.service"))
HTTP_TARGETS = parse_targets(os.environ.get("HAILS_PERF_TARGETS", ""))

UA = "hails-perf-probe"     # must stay in sync with PROBE_UA in hails-stats-pre.py, which drops these


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # A 301 or 302 is the healthy answer for some hosts and must be seen, not followed.
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
        return e.code
    except Exception:
        return 0


def http_probe(url):
    """Return (status, latency ms). Status 0 means unreachable. HEAD first, falling back to GET,
    since some apps answer 405 to HEAD and would look broken."""
    t0 = time.monotonic()
    code = one_request(url, "HEAD")
    if code in (405, 501):
        code = one_request(url, "GET")
    return code, (time.monotonic() - t0) * 1000.0


def systemd_states():
    """One subprocess for all units, not one per unit."""
    out = {}
    if not SYSTEMD_UNITS:
        return out
    try:
        p = subprocess.run(["systemctl", "show", "--no-pager", "-p", "Id", "-p", "ActiveState",
                            "-p", "SubState", "-p", "ActiveEnterTimestampMonotonic",
                            "-p", "MemoryCurrent"] + SYSTEMD_UNITS,
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
        return out
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
    """Current state plus availability history. Hourly resolution inside 48 hours, daily beyond."""

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
        """Forget services that stopped being probed, so a deleted container does not keep rendering
        a green row on the strength of a check that last happened months ago."""
        # Must allow for the SLOWER of the two probe cadences, or raising HAILS_PERF_HTTP past the
        # cutoff would prune every http row each pass and take its availability history with it.
        cutoff = now - max(3 * LOCAL_EVERY, 3 * HTTP_EVERY, 900)
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


# Main loop
def load_meta():
    try:
        with open(META, "r", encoding="utf-8") as fh:
            m = json.load(fh)
        if m.get("schema") == SCHEMA:
            return m
    except Exception:
        pass
    return {"schema": SCHEMA, "since": int(time.time()), "heads": {}, "open": {}}


def save_meta(meta, series):
    meta["heads"] = {}
    meta["open"] = {}
    for s in series.values():
        for tier, r in s.rings.items():
            meta["heads"]["%s_%s" % (s.key, tier)] = [r.head, r.count]
        for tier, b in s.open_b.items():
            if b is not None and b.n:
                meta["open"]["%s_%s" % (s.key, tier)] = b.to_dict()
    meta["updated"] = int(time.time())
    meta["interval"] = INTERVAL
    # The renderer builds its columns from this, so it follows a hardware change automatically.
    meta["groups"] = {g: {"metrics": GROUPS[g],
                          "entities": sorted({s.entity for s in series.values()
                                              if s.group == g and s.entity is not None})}
                      for g in GROUPS}
    # The resolved ring name per entity, so the renderer never has to recompute it and stays right
    # even when a name clash forced a suffix.
    meta["keys"] = {"%s|%s" % (s.group, "" if s.entity is None else s.entity): s.key
                    for s in series.values()}
    tmp = META + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, separators=(",", ":"))
        os.replace(tmp, META)
    except Exception as e:
        log("meta flush failed: %s" % e)


def build_series(meta):
    """One Series per entity found on the box, restoring head pointers and open buckets from meta."""
    cores, disks, fs, nics = list_cores(), list_disks(), list_filesystems(), list_nics()
    wanted = [("sys", None)]
    wanted += [("cpu", c) for c in cores]
    wanted += [("disk", d) for d in disks]
    wanted += [("fs", m) for m, _d in fs]
    wanted += [("net", n) for n in nics]

    # Two entities can sanitise to the same filename, for example / and /root both becoming "root",
    # or /mnt/data and /mnt-data both becoming "mnt_data". Sharing a ring file would interleave two
    # histories and corrupt both, so a clash gets a suffix from a crc of the full name. crc32 and not
    # hash(), because hash() is salted per process and the suffix has to be the same every restart.
    series = {}
    used = set()
    for group, entity in wanted:
        key = group if entity is None else "%s_%s" % (group, safe(entity))
        if key in used:
            key = "%s_%08x" % (key, zlib.crc32(entity.encode("utf-8")) & 0xffffffff)
            log("ring name clash for %s, using %s" % (entity, key))
        used.add(key)
        s = Series(group, entity, GROUPS[group], key)
        for tier, _slots, secs in TIERS:
            # head and count come from Ring.scan, not from meta, so a restart never rewinds.
            if secs:
                d = meta.get("open", {}).get("%s_%s" % (s.key, tier))
                if isinstance(d, dict):
                    s.open_b[tier] = Bucket.from_dict(d, s.nm)
        series[(group, entity)] = s
    return series, cores, disks, fs, nics


def cycle(sampler, series, svcs, meta, last):
    now, vals = sampler.take()
    if vals:
        for key, v in vals.items():
            s = series.get(key)
            if s is not None and len(v) == s.nm:
                s.add(now, v)

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
            since = 0
            try:
                # Monotonic, so a clock step does not move the reported start time.
                mono = int(st.get("ActiveEnterTimestampMonotonic", "0"))
                if mono > 0:
                    since = int(boot + mono / 1000000.0)
            except Exception:
                pass
            try:
                mem = int(st.get("MemoryCurrent", "0"))
            except Exception:
                mem = 0
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
        # Persists the buckets still filling: a daily bucket closes only when the first hour of the
        # next day closes, so a restart in that window would otherwise drop a whole day.
        save_meta(meta, series)


def main():
    os.makedirs(DIR, exist_ok=True)
    meta = load_meta()
    series, cores, disks, fs, nics = build_series(meta)
    sampler = Sampler(cores, disks, fs, nics)
    svcs = Services()
    last = {"http": 0.0, "local": 0.0, "svc": 0.0, "meta": 0.0}
    tick = time.monotonic()

    log("started, dir=%s interval=%.0fs" % (DIR, INTERVAL))
    log("hardware: %d cores, disks=%s, filesystems=%s, nics=%s"
        % (len(cores), ",".join(disks) or "none",
           ",".join(m for m, _d in fs) or "none", ",".join(nics)))

    while True:
        try:
            cycle(sampler, series, svcs, meta, last)
        except Exception as e:
            # Never die on one bad cycle: with Restart=always a persistent fault would respawn the
            # process every few seconds and reprobe every target each time.
            log("cycle failed: %s" % e)
        tick += INTERVAL
        gap = tick - time.monotonic()
        if gap < 0:
            tick = time.monotonic()
            gap = 0
        time.sleep(gap)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
