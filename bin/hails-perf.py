#!/usr/bin/env python3
# The Performance page: htop like statistics for the VPS itself.
#
#   python3 hails-perf.py "<TITLE>" "<OUTDIR>"
#
# Reads the rings written by hails-perf-collect.py, not the access log and nothing on stdin, and
# writes perf.html into OUTDIR. Machine wide, so it is generated into the aggregate scope only.
#
# This page has no Daily/Weekly/Monthly switcher and writes no localStorage key: every metric shows
# all its windows at once as columns. That also keeps it clear of the shared hailsTimeView key.
#
# Self contained by repo convention: it carries its own copies of STYLE, FAVICON and hb.
import sys, os, json, html, time, struct, calendar

TITLE = sys.argv[1] if len(sys.argv) > 1 else "All domains (aggregate)"
OUTDIR = sys.argv[2] if len(sys.argv) > 2 else "."
DIR = os.environ.get("HAILS_PERF_DIR", "/var/lib/hails-stats")

META = os.path.join(DIR, "perf_meta.json")
SVCF = os.path.join(DIR, "perf_svc.json")


# Format helpers.
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


def num(n):
    return "{:,}".format(int(n))


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


# Load the store written by the collector.
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

NAMES = MET.get("names") or []
NM = len(NAMES)
IX = {n: i for i, n in enumerate(NAMES)}
SINCE = MET.get("since") or 0
UPDATED = MET.get("updated") or 0

RAW_FMT = "<I" + "f" * NM
RAW_SZ = struct.calcsize(RAW_FMT) if NM else 0
AGG_FMT = "<IH" + "f" * (NM * 3)
AGG_SZ = struct.calcsize(AGG_FMT) if NM else 0

# Must match TIERS in hails-perf-collect.py. The slot counts are only used to pick a tier, since
# read_ring derives the real count from the file size.
TIERS = [("raw", "perf_raw.bin", RAW_SZ, 1080, 0),
         ("5m", "perf_5m.bin", AGG_SZ, 2100, 300),
         ("1h", "perf_1h.bin", AGG_SZ, 8760, 3600),
         ("1d", "perf_1d.bin", AGG_SZ, 1825, 86400)]


def read_ring(key):
    """Return records oldest first as (ts, n, sum[], min[], max[]). Raw records become n=1.

    Every slot is scanned and sorted by its own timestamp rather than trusting the meta head
    pointer, so a stale or missing meta file cannot corrupt the page."""
    for k, fname, recsz, slots, _ in TIERS:
        if k != key or not recsz:
            continue
        try:
            with open(os.path.join(DIR, fname), "rb") as fh:
                buf = fh.read()
        except Exception:
            return []
        out = []
        for i in range(len(buf) // recsz):
            rec = buf[i * recsz:(i + 1) * recsz]
            try:
                if key == "raw":
                    f = struct.unpack(RAW_FMT, rec)
                    if not f[0]:
                        continue
                    vals = list(f[1:])
                    out.append((f[0], 1, vals, vals, vals))
                else:
                    f = struct.unpack(AGG_FMT, rec)
                    if not f[0] or not f[1]:
                        continue
                    body = f[2:]
                    out.append((f[0], f[1], list(body[0:NM]), list(body[NM:NM * 2]),
                                list(body[NM * 2:NM * 3])))
            except Exception:
                continue
        out.sort(key=lambda r: r[0])
        return out
    return []


RINGS = {k: read_ring(k) for k, _, _, _, _ in TIERS}
NOW = time.time()

# Fall back to the oldest raw sample, not a rollup record: rollups are stamped with their bucket
# start, which would claim coverage the data does not have and defeat the cold start guard in agg().
if not SINCE:
    SINCE = RINGS["raw"][0][0] if RINGS.get("raw") else 0

# The newest raw sample drives the "right now" tiles.
LATEST = RINGS["raw"][-1] if RINGS["raw"] else None


# name: (seconds, tier). Tier is the coarsest ring that still covers the window.
WIN = {"1 min": (60, "raw"), "5 min": (300, "raw"), "15 min": (900, "raw"),
       "30 min": (1800, "raw"), "hour": (3600, "raw"),
       "day": (86400, "5m"), "week": (604800, "5m"),
       "month": (2592000, "1h"), "year": (31536000, "1d")}


def agg(metric, wname):
    """Return (mean, min, max) of one metric over one window, or None if the window is not covered.

    None renders as collecting rather than averaging a partial span."""
    if metric not in IX:
        return None
    secs, tier = WIN[wname]
    i = IX[metric]
    start = NOW - secs
    # Cold start: refuse the window if collection began after it started.
    if not SINCE or SINCE > start + 1:
        return None
    recs = RINGS.get(tier) or []
    tot_n = 0
    tot_sum = 0.0
    lo = None
    hi = None
    for ts, n, sm, mn, mx in recs:
        if ts < start or not n:
            continue
        tot_n += n
        tot_sum += sm[i]
        lo = mn[i] if lo is None or mn[i] < lo else lo
        hi = mx[i] if hi is None or mx[i] > hi else hi
    if not tot_n:
        return None
    return (tot_sum / tot_n, lo, hi)


def cell(metric, wname, fmt):
    a = agg(metric, wname)
    if a is None:
        return "<td class=nd>collecting</td>"
    return "<td>%s</td>" % fmt(a[0])


def peak_cell(metric, wname, fmt):
    a = agg(metric, wname)
    if a is None:
        return "<td class=nd>collecting</td>"
    return "<td>%s</td>" % fmt(a[2])


def growth(metric, wname):
    """Return the signed change across the window, oldest record mean against newest, or None."""
    if metric not in IX:
        return None
    secs, tier = WIN[wname]
    if not SINCE or SINCE > NOW - secs + 1:
        return None
    i = IX[metric]
    recs = [r for r in (RINGS.get(tier) or []) if r[0] >= NOW - secs and r[1]]
    if len(recs) < 2:
        return None
    return recs[-1][2][i] / recs[-1][1] - recs[0][2][i] / recs[0][1]


def cur(metric):
    """Return the newest raw reading, or 0.0 for an unknown metric so schema drift degrades one tile
    instead of raising and leaving no page at all."""
    if not LATEST or metric not in IX:
        return 0.0
    return LATEST[2][IX[metric]]


# Page sections.
def tile(k, val, sub=""):
    return ("<div class=tile><div class=k>%s</div><div class=val>%s</div>"
            "<div class=d>%s</div></div>" % (esc(k), val, sub))


def section(title, note, head, rows):
    if not rows:
        return ""
    h = ["<h2>%s</h2>" % esc(title)]
    if note:
        h.append("<p class=note>%s</p>" % note)
    h.append("<div class=card><div class=tw><table><tr>")
    h.append("".join("<th>%s</th>" % esc(c) for c in head))
    h.append("</tr>")
    h.extend(rows)
    h.append("</table></div></div>")
    return "".join(h)


def win_rows(wins, cols, barmetric=None):
    """One row per window, one column per (metric, formatter)."""
    tops = {}
    if barmetric:
        vals = [agg(barmetric, w) for w in wins]
        tops = max([v[0] for v in vals if v] or [0]) or 1
    rows = []
    for w in wins:
        b = ""
        if barmetric:
            a = agg(barmetric, w)
            if a:
                b = bar(a[0] * 100.0 / tops)
        cells = "".join(f(m, w) for m, f in cols)
        rows.append("<tr><td class=lbl>%s%s</td>%s</tr>" % (esc(w), b, cells))
    return rows


def cpu_cores():
    return [n for n in NAMES if n.startswith("cpu") and n != "cpu"]


def build():
    out = []

    # KPI tiles, the right now numbers from the newest sample.
    uptime = SVC.get("uptime") or 0
    tiles = [tile("VPS uptime", esc(dur(uptime)), "since %s" % esc(stamp(SVC.get("boot"))))]
    ncore = max(1, len(cpu_cores()))
    if LATEST:
        tiles.append(tile("Load", "%.2f" % cur("load1"), "1 min, %d cores" % ncore))
        tiles.append(tile("CPU", pct(cur("cpu")), "all cores, now"))
        tiles.append(tile("Memory", pct(cur("mem_pct")),
                          "%s of %s" % (hb(cur("mem_used")), hb(cur("mem_total") or 0))))
        swt = cur("swap_total") or 0
        tiles.append(tile("Swap", hb(cur("swap_used")),
                          ("of %s" % hb(swt)) if swt else "none configured"))
        tiles.append(tile("Disk", pct(cur("fs_pct")),
                          "%s of %s" % (hb(cur("fs_used")), hb(cur("fs_total")))))
    else:
        tiles.append(tile("Collector", "no data", "waiting for the first sample"))
    out.append("<div class=tiles>%s</div>" % "".join(tiles))

    W4 = ["1 min", "5 min", "15 min", "30 min"]
    W8 = ["1 min", "5 min", "15 min", "30 min", "hour", "day", "week", "month"]
    W7 = ["5 min", "15 min", "30 min", "hour", "day", "week", "month"]
    W4L = ["day", "week", "month", "year"]

    f2 = lambda v: "%.2f" % v

    # Load.
    lrows = []
    for w in W4:
        a = agg("load1", w)
        if a is None:
            lrows.append("<tr><td class=lbl>%s</td><td class=nd colspan=4>collecting</td></tr>" % esc(w))
            continue
        lrows.append("<tr><td class=lbl>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (esc(w), bar(a[0] * 100.0 / ncore), f2(a[0]), f2(a[1]), f2(a[2]),
                        f2(a[0] / ncore)))
    out.append(section(
        "Load average", "Load is runnable plus uninterruptible tasks, so on %d cores a figure of "
        "%d means fully busy. The kernel keeps only 1, 5 and 15 minute averages, so the 30 minute "
        "row is a mean over our own samples." % (ncore, ncore),
        ["Window", "Average", "Minimum", "Peak", "Per core"], lrows))

    # Memory.
    out.append(section(
        "Memory", "Used is total minus MemAvailable, so cache and reclaimable buffers do not count "
        "as used. Swap is shown separately because this box already runs with swap in use.",
        ["Window", "Used", "Used bytes", "Peak used", "Swap used"],
        win_rows(W4, [("mem_pct", lambda m, w: cell(m, w, pct)),
                      ("mem_used", lambda m, w: cell(m, w, hb)),
                      ("mem_pct", lambda m, w: peak_cell(m, w, pct)),
                      ("swap_used", lambda m, w: cell(m, w, hb))],
                 barmetric="mem_pct")))

    # CPU, aggregate plus one column per core, plus steal.
    cores = cpu_cores()
    cols = [("cpu", lambda m, w: cell(m, w, pct))]
    cols += [(c, lambda m, w: cell(m, w, pct)) for c in cores]
    cols += [("steal", lambda m, w: cell(m, w, pct)),
             ("iowait", lambda m, w: cell(m, w, pct))]
    head = ["Window", "All cores"] + [c.replace("cpu", "Core ") for c in cores] + ["Steal", "IO wait"]
    out.append(section(
        "CPU usage", "Steal is time the hypervisor gave to another tenant, IO wait is time blocked "
        "on the disk. Both are counted out of the all cores figure, not into it.",
        head, win_rows(W8, cols, barmetric="cpu")))

    # Disk IO.
    out.append(section(
        "Disk IO", "The one disk on this box, %s. Utilisation is the share of wall clock time the "
        "disk had at least one request in flight." % esc(os.environ.get("HAILS_PERF_DISK", "vda")),
        ["Window", "Read", "Write", "IOPS", "Utilisation", "Peak read", "Peak write"],
        win_rows(W7, [("dsk_read", lambda m, w: cell(m, w, rate)),
                      ("dsk_write", lambda m, w: cell(m, w, rate)),
                      ("dsk_iops", lambda m, w: cell(m, w, lambda v: "%.1f" % v)),
                      ("dsk_util", lambda m, w: cell(m, w, pct)),
                      ("dsk_read", lambda m, w: peak_cell(m, w, rate)),
                      ("dsk_write", lambda m, w: peak_cell(m, w, rate))],
                 barmetric="dsk_util")))

    # Network IO.
    out.append(section(
        "Network IO", "The WAN interface %s only, excluding docker0, the bridges and the veth pairs. "
        "This is the real wire total in both directions, so it reads higher than the Bandwidth page, "
        "which counts response bodies only." % esc(os.environ.get("HAILS_PERF_NIC", "enp1s0")),
        ["Window", "Inbound", "Outbound", "Peak in", "Peak out"],
        win_rows(W7, [("net_rx", lambda m, w: cell(m, w, rate)),
                      ("net_tx", lambda m, w: cell(m, w, rate)),
                      ("net_rx", lambda m, w: peak_cell(m, w, rate)),
                      ("net_tx", lambda m, w: peak_cell(m, w, rate))],
                 barmetric="net_tx")))

    # Storage.
    grows = []
    for w in W4L:
        a = agg("fs_used", w)
        p = agg("fs_pct", w)
        if a is None or p is None:
            grows.append("<tr><td class=lbl>%s</td><td class=nd colspan=4>collecting</td></tr>" % esc(w))
            continue
        # Signed delta, first reading against last, so reclaimed space reads as negative.
        g = growth("fs_used", w)
        gtxt = "collecting" if g is None else ("+" if g >= 0 else "") + hb(abs(g))
        grows.append("<tr><td class=lbl>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (esc(w), bar(p[0]), pct(p[0]), hb(a[0]), hb(a[2]), gtxt))
    out.append(section(
        "Storage", "Growth is the change across the window, the first reading against the last, so a "
        "negative figure means space was reclaimed. Peak is the highest usage seen at any point.",
        ["Window", "Average used", "Average bytes", "Peak bytes", "Growth"], grows))

    out.append(services())
    return "".join(out)


def bucket_time(key, fmt):
    """Parse a bucket key back to epoch seconds. The collector keys in UTC, so timegm not mktime."""
    try:
        return calendar.timegm(time.strptime(key, fmt))
    except Exception:
        return None


def walk(tier, name, fmt, seconds):
    """Yield the buckets for one service that overlap the span, dropping those that end before it."""
    start = NOW - seconds
    width = 3600 if tier == "hourly" else 86400
    for k, b in ((SVC.get(tier) or {}).get(name) or {}).items():
        t = bucket_time(k, fmt)
        if t is None or t + width < start:
            continue
        if isinstance(b, list) and len(b) >= 4:
            yield b


def avail(name, seconds):
    """Return availability percent over a span, or None when the span is not covered yet.

    Hourly buckets inside 48 hours, daily beyond. The cold start guard stops a young collector
    reporting a flat 100 percent over 30 days."""
    if not SVC_SINCE or SVC_SINCE > NOW - seconds + 1:
        return None
    if seconds <= 172800:
        tier, fmt = "hourly", "%Y-%m-%dT%H"
    else:
        tier, fmt = "daily", "%Y-%m-%d"
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
                "yet. The collector fills this in on its first pass.</p></div>")
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
        # Latency only applies to http rows: local probes have no meaningful measurement.
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


# Assemble and write the page.
BODY = build()

foot = ("Sampled every %s seconds straight from /proc by hails-perf-collect.py. Averages are true "
        "means over the window, not point readings, and Peak columns are the highest single sample "
        "seen in that window."
        % esc(int(MET.get("interval") or 10)))
if SINCE:
    foot += ("<br>Collection began %s. Windows longer than that read collecting until enough history "
             "exists, and fill in on their own." % esc(stamp(SINCE)))
else:
    foot += ("<br>No samples on record yet. Check that hails-perf.service is running: "
             "<code>systemctl status hails-perf</code>.")
if UPDATED:
    foot += "<br>Collector last wrote %s." % esc(stamp(UPDATED))
    if NOW - UPDATED > 300:
        foot += (" That is more than 5 minutes ago, so the collector may have stopped.")

FAVICON = ('<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,AAABAAEAEBAQAAEABAAo'
           'AQAAFgAAACgAAAAQAAAAIAAAAAEABAAAAAAAgAAAAAAAAAAAAAAAEAAAAAAAAADGxsYAWFhYABwcHABfAP8A/9df'
           'AADXrwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIiIiIiIiIiIjMlUkQgAiIiIiIiIi'
           'IiIiIzJVJEIAAAIiIiIiIiIiIiMyVSRCAAIiIiIiIiIiIiIRERERERERERERERERERERIiIiIiIiIiIgACVVUiIiIi'
           'IiIiIiIiIiIAAlVVIiIiIiIiIiIiIiIhEREREREREREREREREREREAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
           'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">')

STYLE = """<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700;900&display=swap');
*{box-sizing:border-box}
body{font-family:'Roboto',system-ui,sans-serif;min-height:100vh;margin:0;padding:4.4rem 1.2rem 3rem;color:#e8e8f0;background:radial-gradient(1100px 600px at 50% -10%,rgba(111,76,255,.16),transparent 60%),radial-gradient(900px 500px at 88% 6%,rgba(1,110,218,.12),transparent 55%),radial-gradient(800px 480px at 10% 16%,rgba(217,0,192,.08),transparent 55%),#04050c}
.wrap{max-width:1120px;margin:0 auto}
h1{font-weight:900;font-size:1.5rem;margin:0 0 1rem}
h2{font-weight:700;font-size:1.05rem;margin:1.4rem 0 .4rem;color:#cfe0ff}
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
