#!/usr/bin/env python3
# The Performance page:  python3 hails-perf.py "<TITLE>" "<OUTDIR>"
# Reads the rings written by hails-perf-collect.py, never the access log, and writes perf.html into
# OUTDIR. Machine wide, so it is generated into the aggregate scope only.
# The shape of the page follows perf_meta.json, so a resized box grows a column or a card on its own.
import sys, os, json, html, time, struct, calendar

FONT_BASE = os.environ.get("HAILS_FONT_BASE", "/fonts").rstrip("/")
FONT_CSS = (
    "@font-face{font-family:'Roboto';font-style:normal;font-weight:100 900;font-display:swap;src:url(%s/roboto-latin-ext.woff2) format('woff2');unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}"
    "@font-face{font-family:'Roboto';font-style:normal;font-weight:100 900;font-display:swap;src:url(%s/roboto-latin.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}"
) % (FONT_BASE, FONT_BASE)

TITLE = sys.argv[1] if len(sys.argv) > 1 else "All domains (aggregate)"
OUTDIR = sys.argv[2] if len(sys.argv) > 2 else "."
DIR = os.environ.get("HAILS_PERF_DIR", "/var/lib/hails-stats")

META = os.path.join(DIR, "perf_meta.json")
SVCF = os.path.join(DIR, "perf_svc.json")

# Must match TIERS in hails-perf-collect.py. Slot counts only pick a tier, read_ring derives the
# real record count from the file size.
TIERS = {"raw": 0, "5m": 300, "1h": 3600, "1d": 86400}


def esc(s):
    return html.escape(str(s)) if s not in ("", None) else "(none)"


def hb(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (int(n), unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "0 B"


def rate(n):
    return hb(n) + "/s"


def pct(n):
    return "%.1f%%" % n


def bar(p):
    return "<span class=bar style='width:%d%%'></span>" % max(0, min(100, int(p)))


def dur(seconds):
    """Humanize a span, coarsest two units: 55 days, 19 hours."""
    s = int(seconds)
    if s <= 0:
        return "unknown"
    d, s2 = divmod(s, 86400)
    h, s3 = divmod(s2, 3600)
    m = s3 // 60
    if d:
        return "%d day%s, %d hour%s" % (d, "" if d == 1 else "s", h, "" if h == 1 else "s")
    if h:
        return "%d hour%s, %d min" % (h, "" if h == 1 else "s", m)
    return "%d min" % m


def stamp(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "unknown"


def safe(name):
    return "".join(c if c.isalnum() else "_" for c in name).strip("_") or "root"


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


MET = load_json(META, {})
SVC = load_json(SVCF, {})
SVC_SINCE = SVC.get("since") or 0
SINCE = MET.get("since") or 0
UPDATED = MET.get("updated") or 0
GROUPS = MET.get("groups") or {}
NOW = time.time()


def entities(group):
    return (GROUPS.get(group) or {}).get("entities") or []


def metrics(group):
    return (GROUPS.get(group) or {}).get("metrics") or []


KEYS = MET.get("keys") or {}


def ring_key(group, entity):
    """The collector records the resolved ring name, so a name clash that forced a suffix still
    reads correctly here. The computed form is only a fallback for an older meta file."""
    k = KEYS.get("%s|%s" % (group, "" if entity is None else entity))
    return k or (group if entity is None else "%s_%s" % (group, safe(entity)))


_CACHE = {}


def read_ring(group, entity, tier):
    """Records oldest first as (ts, n, sum[], min[], max[]). Raw records come back with n of 1.

    Every slot is scanned and sorted by its own timestamp rather than trusting a head pointer, so a
    stale or missing meta file cannot corrupt the page. Unwritten slots have ts 0 and drop out."""
    ck = (group, entity, tier)
    if ck in _CACHE:
        return _CACHE[ck]
    nm = len(metrics(group))
    out = []
    if nm:
        raw_fmt = "<I" + "f" * nm
        agg_fmt = "<IH" + "f" * (nm * 3)
        fmt = raw_fmt if tier == "raw" else agg_fmt
        recsz = struct.calcsize(fmt)
        path = os.path.join(DIR, "perf_%s_%s.bin" % (ring_key(group, entity), tier))
        try:
            with open(path, "rb") as fh:
                buf = fh.read()
        except Exception:
            buf = b""
        for i in range(len(buf) // recsz):
            try:
                f = struct.unpack(fmt, buf[i * recsz:(i + 1) * recsz])
            except Exception:
                continue
            if not f[0]:
                continue
            if tier == "raw":
                vals = list(f[1:])
                out.append((f[0], 1, vals, vals, vals))
            else:
                if not f[1]:
                    continue
                body = f[2:]
                out.append((f[0], f[1], list(body[0:nm]), list(body[nm:nm * 2]),
                            list(body[nm * 2:nm * 3])))
        out.sort(key=lambda r: r[0])
    _CACHE[ck] = out
    return out


if not SINCE:
    raw = read_ring("sys", None, "raw")
    SINCE = raw[0][0] if raw else 0

# name: (seconds, tier). Each tier is the coarsest one that still resolves the window.
WIN = {"1 min": (60, "raw"), "5 min": (300, "raw"), "15 min": (900, "raw"),
       "30 min": (1800, "raw"), "hour": (3600, "raw"),
       "day": (86400, "5m"), "week": (604800, "5m"),
       "month": (2592000, "1h"), "year": (31536000, "1d")}


def covered_since(group, entity):
    """When this entity's own history starts, which is not the same as when the collector first ran.

    A disk or core added months in has empty rings, and judging it by the global start would let a
    year window average an hour of data and present it as whole. Since hardware can now appear at any
    time, the guard has to be per entity."""
    ck = ("since", group, entity)
    if ck not in _CACHE:
        oldest = [r[0][0] for r in
                  (read_ring(group, entity, t) for t in ("1d", "1h", "5m", "raw")) if r]
        _CACHE[ck] = max(SINCE, min(oldest)) if oldest else 0
    return _CACHE[ck]


def agg(group, entity, metric, wname):
    """Mean, min and max of one metric over one window, or None when the window is not covered yet.

    None means collecting: the page says so rather than averaging a partial span and presenting it
    as if it were whole."""
    names = metrics(group)
    if metric not in names:
        return None
    secs, tier = WIN[wname]
    start = NOW - secs
    since = covered_since(group, entity)
    if not since or since > start + 1:
        return None
    i = names.index(metric)
    tot_n = 0
    tot_sum = 0.0
    lo = hi = None
    for ts, n, sm, mn, mx in read_ring(group, entity, tier):
        if ts < start or not n:
            continue
        tot_n += n
        tot_sum += sm[i]
        lo = mn[i] if lo is None or mn[i] < lo else lo
        hi = mx[i] if hi is None or mx[i] > hi else hi
    if not tot_n:
        return None
    return (tot_sum / tot_n, lo, hi)


def growth(group, entity, metric, wname):
    """Signed change across the window, oldest record against newest."""
    names = metrics(group)
    if metric not in names:
        return None
    secs, tier = WIN[wname]
    since = covered_since(group, entity)
    if not since or since > NOW - secs + 1:
        return None
    i = names.index(metric)
    recs = [r for r in read_ring(group, entity, tier) if r[0] >= NOW - secs and r[1]]
    if len(recs) < 2:
        return None
    return recs[-1][2][i] / recs[-1][1] - recs[0][2][i] / recs[0][1]


def cur(group, entity, metric):
    """Newest raw reading, or 0.0 for an unknown metric so a layout change degrades one tile rather
    than raising and leaving no page at all."""
    names = metrics(group)
    if metric not in names:
        return 0.0
    recs = read_ring(group, entity, "raw")
    return recs[-1][2][names.index(metric)] if recs else 0.0


def tile(k, val, sub=""):
    return ("<div class=tile><div class=k>%s</div><div class=val>%s</div>"
            "<div class=d>%s</div></div>" % (esc(k), val, sub))


def section(title, note, head, rows, sub=None):
    if not rows:
        return ""
    h = ["<h2>%s</h2>" % esc(title)]
    if sub:
        h.append("<p class=sub2>%s</p>" % esc(sub))
    if note:
        h.append("<p class=note>%s</p>" % note)
    h.append("<div class=card><div class=tw><table><tr>")
    h.append("".join("<th>%s</th>" % esc(c) for c in head))
    h.append("</tr>")
    h.extend(rows)
    h.append("</table></div></div>")
    return "".join(h)


def win_rows(group, entity, wins, cols, barmetric=None):
    """One row per window, one column per (metric, formatter)."""
    top = 1
    if barmetric:
        vals = [agg(group, entity, barmetric, w) for w in wins]
        top = max([v[0] for v in vals if v] or [0]) or 1
    rows = []
    for w in wins:
        b = ""
        if barmetric:
            a = agg(group, entity, barmetric, w)
            if a:
                b = bar(a[0] * 100.0 / top)
        cells = []
        for metric, fmt, which in cols:
            a = agg(group, entity, metric, w)
            if a is None:
                cells.append("<td class=nd>collecting</td>")
            else:
                cells.append("<td>%s</td>" % fmt(a[which]))
        rows.append("<tr><td class=lbl>%s%s</td>%s</tr>" % (esc(w), b, "".join(cells)))
    return rows


W4 = ["1 min", "5 min", "15 min", "30 min"]
W8 = ["1 min", "5 min", "15 min", "30 min", "hour", "day", "week", "month"]
W7 = ["5 min", "15 min", "30 min", "hour", "day", "week", "month"]
W4L = ["day", "week", "month", "year"]

MEAN, MIN, MAX = 0, 1, 2


def build():
    out = []
    cores = entities("cpu")
    disks = entities("disk")
    fses = entities("fs")
    nics = entities("net")
    ncore = max(1, len(cores))

    root = "/" if "/" in fses else (fses[0] if fses else None)
    uptime = SVC.get("uptime") or 0
    tiles = [tile("VPS uptime", esc(dur(uptime)), "since %s" % esc(stamp(SVC.get("boot"))))]
    tiles.append(tile("Load", "%.2f" % cur("sys", None, "load1"), "1 min, %d cores" % ncore))
    tiles.append(tile("CPU", pct(cur("sys", None, "cpu")), "all cores, now"))
    tiles.append(tile("Memory", pct(cur("sys", None, "mem_pct")),
                      "%s of %s" % (hb(cur("sys", None, "mem_used")),
                                    hb(cur("sys", None, "mem_total")))))
    swt = cur("sys", None, "swap_total")
    tiles.append(tile("Swap", hb(cur("sys", None, "swap_used")),
                      ("of %s" % hb(swt)) if swt else "none configured"))
    if root:
        tiles.append(tile("Disk", pct(cur("fs", root, "pct")),
                          "%s of %s on %s" % (hb(cur("fs", root, "used")),
                                              hb(cur("fs", root, "total")), esc(root))))
    out.append("<div class=tiles>%s</div>" % "".join(tiles))

    f2 = lambda v: "%.2f" % v

    # Load. The kernel keeps 1, 5 and 15 minute averages, so the 30 minute row is our own mean.
    lrows = []
    for w in W4:
        a = agg("sys", None, "load1", w)
        if a is None:
            lrows.append("<tr><td class=lbl>%s</td><td class=nd colspan=4>collecting</td></tr>" % esc(w))
            continue
        lrows.append("<tr><td class=lbl>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (esc(w), bar(a[0] * 100.0 / ncore), f2(a[0]), f2(a[1]), f2(a[2]),
                        f2(a[0] / ncore)))
    out.append(section(
        "Load average",
        "Load is runnable plus uninterruptible tasks, so on %d cores a figure of %d means fully "
        "busy. The kernel keeps only 1, 5 and 15 minute averages, so the 30 minute row is a mean "
        "over our own samples." % (ncore, ncore),
        ["Window", "Average", "Minimum", "Peak", "Per core"], lrows))

    out.append(section(
        "Memory",
        "Used is total minus MemAvailable, so cache and reclaimable buffers do not count as used. "
        "Swap is shown separately because it fills for different reasons.",
        ["Window", "Used", "Used bytes", "Peak used", "Swap used"],
        win_rows("sys", None, W4,
                 [("mem_pct", pct, MEAN), ("mem_used", hb, MEAN),
                  ("mem_pct", pct, MAX), ("swap_used", hb, MEAN)],
                 barmetric="mem_pct")))

    head = ["Window", "All cores"] + [c.replace("cpu", "Core ") for c in cores] + ["Steal", "IO wait"]
    crows = []
    for w in W8:
        cells = []
        a = agg("sys", None, "cpu", w)
        cells.append("<td class=nd>collecting</td>" if a is None else "<td>%s</td>" % pct(a[0]))
        for c in cores:
            ca = agg("cpu", c, "busy", w)
            cells.append("<td class=nd>collecting</td>" if ca is None else "<td>%s</td>" % pct(ca[0]))
        for m in ("steal", "iowait"):
            sa = agg("sys", None, m, w)
            cells.append("<td class=nd>collecting</td>" if sa is None else "<td>%s</td>" % pct(sa[0]))
        b = bar(a[0]) if a else ""
        crows.append("<tr><td class=lbl>%s%s</td>%s</tr>" % (esc(w), b, "".join(cells)))
    out.append(section(
        "CPU usage",
        "Steal is time the hypervisor gave to another tenant, IO wait is time blocked on the disk. "
        "Both are counted out of the all cores figure, not into it. Each core keeps its own history, "
        "so adding cores does not disturb the rest of this page.",
        head, crows))

    for dsk in disks:
        out.append(section(
            "Disk IO", "Utilisation is the share of wall clock time the disk had at least one "
            "request in flight.",
            ["Window", "Read", "Write", "IOPS", "Utilisation", "Peak read", "Peak write"],
            win_rows("disk", dsk, W7,
                     [("read", rate, MEAN), ("write", rate, MEAN),
                      ("iops", lambda v: "%.1f" % v, MEAN), ("util", pct, MEAN),
                      ("read", rate, MAX), ("write", rate, MAX)],
                     barmetric="util"),
            sub=dsk))

    for nic in nics:
        out.append(section(
            "Network IO", "The real wire total in both directions, so it reads higher than the "
            "Bandwidth page, which counts response bodies only.",
            ["Window", "Inbound", "Outbound", "Peak in", "Peak out"],
            win_rows("net", nic, W7,
                     [("rx", rate, MEAN), ("tx", rate, MEAN), ("rx", rate, MAX), ("tx", rate, MAX)],
                     barmetric="tx"),
            sub=nic))

    for mount in fses:
        grows = []
        for w in W4L:
            a = agg("fs", mount, "used", w)
            p = agg("fs", mount, "pct", w)
            if a is None or p is None:
                grows.append("<tr><td class=lbl>%s</td><td class=nd colspan=4>collecting</td></tr>"
                             % esc(w))
                continue
            g = growth("fs", mount, "used", w)
            gtxt = "collecting" if g is None else ("+" if g >= 0 else "") + hb(abs(g))
            grows.append("<tr><td class=lbl>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                         % (esc(w), bar(p[0]), pct(p[0]), hb(a[0]), hb(a[2]), gtxt))
        out.append(section(
            "Storage", "Growth is the change across the window, first reading against last, so a "
            "negative figure means space was reclaimed.",
            ["Window", "Average used", "Average bytes", "Peak bytes", "Growth"], grows, sub=mount))

    out.append(services())
    return "".join(out)


def bucket_time(key, fmt):
    """Parse a bucket key back to epoch seconds. The collector keys in UTC, so timegm not mktime."""
    try:
        return calendar.timegm(time.strptime(key, fmt))
    except Exception:
        return None


def walk(tier, name, fmt, seconds):
    """Buckets for one service that overlap the span, so only ones ending before it are dropped."""
    start = NOW - seconds
    width = 3600 if tier == "hourly" else 86400
    for k, b in ((SVC.get(tier) or {}).get(name) or {}).items():
        t = bucket_time(k, fmt)
        if t is None or t + width < start:
            continue
        if isinstance(b, list) and len(b) >= 4:
            yield b


def avail(name, seconds):
    """Availability percent over a span, or None when the span is not covered yet. Without that
    guard a collector up for a minute would report a flat 100 percent over 30 days."""
    if not SVC_SINCE or SVC_SINCE > NOW - seconds + 1:
        return None
    tier, fmt = ("hourly", "%Y-%m-%dT%H") if seconds <= 172800 else ("daily", "%Y-%m-%d")
    n = ok = 0
    for b in walk(tier, name, fmt, seconds):
        n += b[0]
        ok += b[1]
    return (ok * 100.0 / n) if n else None


def latency(name):
    tot = 0.0
    n = 0
    for b in walk("hourly", name, "%Y-%m-%dT%H", 86400):
        tot += b[2]
        n += b[1]
    return (tot / n) if n else None


def services():
    curd = SVC.get("cur") or {}
    if not curd:
        return ("<h2>Service uptime</h2><div class=card><p class=empty>No service probes on record "
                "yet.</p></div>")
    order = {"systemd": 0, "docker": 1, "http": 2}
    names = sorted(curd.keys(), key=lambda n: (order.get(curd[n].get("kind"), 9), n))
    rows = []
    for n in names:
        c = curd[n]
        kind = c.get("kind", "")
        ok = bool(c.get("ok"))
        state = c.get("state") or ("up" if ok else "down")
        if kind == "http":
            state = "%s (%s)" % ("responding" if ok else "unexpected", c.get("code") or "no answer")
        elif kind == "docker" and "unhealthy" in (c.get("status") or ""):
            state = "running, unhealthy"
        since = c.get("since") or 0
        run = dur(NOW - since) if since else ("see status" if kind == "docker" else "unknown")
        if kind == "docker" and c.get("status"):
            run = c["status"].replace("Up ", "").split(" (")[0]
        cells = []
        for span in (86400, 604800, 2592000):
            a = avail(n, span)
            cells.append("<td class=nd>collecting</td>" if a is None else
                         "<td class=%s>%s</td>" % ("ok" if a >= 99.5 else "bad", "%.2f%%" % a))
        lat = latency(n) if kind == "http" else None
        rows.append("<tr><td class=lbl>%s</td><td>%s</td><td class=%s>%s</td><td>%s</td><td>%s</td>"
                    "%s<td>%s</td></tr>"
                    % (esc(n), esc(kind), "ok" if ok else "bad", esc(state),
                       esc(stamp(since)) if since else "", esc(run),
                       "".join(cells), ("%.0f ms" % lat) if lat is not None else ""))
    return section(
        "Service uptime",
        "systemd and docker report exactly when each service last started. The http rows probe each "
        "site over the network against the status it is expected to return, so a redirect or an auth "
        "challenge counts as healthy. Both are shown because a proxy can answer while the app behind "
        "it is down.",
        ["Service", "Source", "State", "Started", "Running", "24 hours", "7 days", "30 days",
         "Avg latency"], rows)


BODY = build()

foot = ("Sampled every %s seconds straight from /proc. Averages are true means over the window, not "
        "point readings, and Peak columns are the highest single sample seen in that window."
        % esc(int(MET.get("interval") or 10)))
foot += ("<br>Hardware is discovered when the collector starts, so new cores, disks, volumes and "
         "interfaces appear here after a restart. Each one keeps its own history, so an expansion "
         "does not disturb what is already recorded.")
if SINCE:
    foot += ("<br>Collection began %s. Windows longer than that read collecting until enough history "
             "exists, and fill in on their own." % esc(stamp(SINCE)))
else:
    foot += ("<br>No samples on record yet. Check that hails-perf.service is running: "
             "<code>systemctl status hails-perf</code>.")
if UPDATED:
    foot += "<br>Collector last wrote %s." % esc(stamp(UPDATED))
    if NOW - UPDATED > 300:
        foot += " That is more than 5 minutes ago, so the collector may have stopped."

FAVICON = ('<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,AAABAAEAEBAQAAEABAAo'
           'AQAAFgAAACgAAAAQAAAAIAAAAAEABAAAAAAAgAAAAAAAAAAAAAAAEAAAAAAAAADGxsYAWFhYABwcHABfAP8A/9df'
           'AADXrwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIiIiIiIiIiIjMlUkQgAiIiIiIiIi'
           'IiIiIzJVJEIAAAIiIiIiIiIiIiMyVSRCAAIiIiIiIiIiIiIRERERERERERERERERERERIiIiIiIiIiIgACVVUiIiIi'
           'IiIiIiIiIiIAAlVVIiIiIiIiIiIiIiIhEREREREREREREREREREREAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
           'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">')

STYLE = """<style>
""" + FONT_CSS + """
*{box-sizing:border-box}
body{font-family:'Roboto',system-ui,sans-serif;min-height:100vh;margin:0;padding:4.4rem 1.2rem 3rem;color:#e8e8f0;background:radial-gradient(1100px 600px at 50% -10%,rgba(111,76,255,.16),transparent 60%),radial-gradient(900px 500px at 88% 6%,rgba(1,110,218,.12),transparent 55%),radial-gradient(800px 480px at 10% 16%,rgba(217,0,192,.08),transparent 55%),#04050c}
.wrap{max-width:1120px;margin:0 auto}
h1{font-weight:900;font-size:1.5rem;margin:0 0 1rem}
h2{font-weight:700;font-size:1.05rem;margin:1.4rem 0 .2rem;color:#cfe0ff}
a{color:#7aa2f7}
code{background:rgba(255,255,255,.08);border-radius:4px;padding:1px 5px;font-size:.92em}
.card{background:rgba(13,14,33,.85);border:1px solid rgba(255,255,255,.09);border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.45);padding:1rem 1.2rem;margin:0 0 1rem}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th{text-align:right;color:rgba(255,255,255,.5);font-weight:500;padding:.5rem .6rem;border-bottom:1px solid rgba(255,255,255,.12);white-space:nowrap}
th:first-child{text-align:left}
td{text-align:right;padding:.4rem .6rem;border-bottom:1px solid rgba(255,255,255,.06);white-space:nowrap}
td.lbl{text-align:left;position:relative;max-width:520px;overflow:hidden;text-overflow:ellipsis;color:#fff}
.bar{position:absolute;left:0;bottom:1px;height:3px;border-radius:2px;background:linear-gradient(90deg,#016eda,#d900c0);opacity:.85}
td.nd{color:rgba(255,255,255,.3);font-style:italic}
td.ok{color:#5ce08a}
td.bad{color:#ff6b8a}
tr:hover td{background:rgba(111,76,255,.06)}
.empty{color:rgba(255,255,255,.4);padding:.6rem}
.note{color:rgba(255,255,255,.42);font-size:.78rem;margin:0 0 .6rem;line-height:1.5}
.sub2{font:600 .85rem Roboto,system-ui,sans-serif;color:#fff;margin:.1rem 0 .4rem}
.foot{color:rgba(255,255,255,.42);font-size:.78rem;margin-top:1.2rem;line-height:1.6}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem;margin:0 0 1.2rem}
.tile{background:rgba(13,14,33,.85);border:1px solid rgba(255,255,255,.09);border-radius:14px;padding:.85rem 1rem}
.tile .k{font:600 11px Roboto,system-ui,sans-serif;color:rgba(255,255,255,.55);text-transform:uppercase;letter-spacing:.06em}
.tile .val{font:900 1.5rem/1.2 Roboto,system-ui,sans-serif;color:#fff;margin:.15rem 0}
.tile .d{font-size:.8rem;color:rgba(255,255,255,.45)}
</style>"""

SUB = TITLE + " : Performance"
OUT = ("<!doctype html><meta charset=utf-8><meta name=viewport content=\"width=device-width,"
       "initial-scale=1\">\n<title>%s</title>\n%s\n%s\n<div class=wrap>\n<h1>%s</h1>\n"
       "%s\n<div class=foot>%s</div>\n</div>\n<script src=\"/nav.js\"></script>\n"
       % (esc(SUB), FAVICON, STYLE, esc(SUB), BODY, foot))

os.makedirs(OUTDIR, exist_ok=True)
tmp = os.path.join(OUTDIR, ".perf.html")
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(OUT)
os.replace(tmp, os.path.join(OUTDIR, "perf.html"))
