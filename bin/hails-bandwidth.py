#!/usr/bin/env python3
# Bandwidth page for one stats scope, the aggregate or a single domain.
#
#   python3 hails-bandwidth.py "<TITLE>" "<OUTDIR>" "<host|all>"
#
# Reads the durable rollup written by hails-rollup.py, not the raw log and nothing on stdin,
# and writes bandwidth.html into OUTDIR.
# The Daily / Weekly / Monthly / Yearly / Total buttons pick bucket granularity, not a lookback window.
import sys, json, html, os, time

# Roboto is bundled rather than fetched from Google, so no page makes a third party call.
# One variable file per subset covers every weight from 100 to 900.
FONT_BASE = os.environ.get("HAILS_FONT_BASE", "/fonts").rstrip("/")
FONT_CSS = (
    "@font-face{font-family:'Roboto';font-style:normal;font-weight:100 900;font-display:swap;src:url(%s/roboto-latin-ext.woff2) format('woff2');unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}"
    "@font-face{font-family:'Roboto';font-style:normal;font-weight:100 900;font-display:swap;src:url(%s/roboto-latin.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}"
) % (FONT_BASE, FONT_BASE)

TITLE = sys.argv[1] if len(sys.argv) > 1 else "All domains (aggregate)"
OUTDIR = sys.argv[2] if len(sys.argv) > 2 else "."
SCOPE = sys.argv[3] if len(sys.argv) > 3 else "all"
STORE = os.environ.get("HAILS_ROLLUP", "/var/lib/hails-stats/bandwidth.json")

WINS = ("daily", "weekly", "monthly", "yearly", "total")
WBTN = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly",
        "yearly": "Yearly", "total": "Total"}
CAP = {"daily": 30, "weekly": 26, "monthly": 24, "yearly": 0, "total": 0}   # 0 means no cap
WNOTE = {"daily": "one row per day, most recent 30",
         "weekly": "one row per week, most recent 26",
         "monthly": "one row per calendar month, most recent 24",
         "yearly": "one row per calendar year",
         "total": "every day on record, combined"}


# ---- format helpers -----------------------------------------------------------------------------
def esc(s):
    return html.escape(str(s)) if s not in ("", None) else "(none)"


def hb(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (int(n), unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "0 B"


def num(n):
    return "{:,}".format(int(n))


def bar(pct):
    return "<span class=bar style='width:%d%%'></span>" % pct


def week_label(wk):
    # ISO week key to a label carrying the Monday of that week.
    try:
        t = time.strptime(wk + "-1", "%G-W%V-%u")
        return "%s (%s)" % (wk, time.strftime("%b %d", t))
    except Exception:
        return wk


def month_label(m):
    try:
        return time.strftime("%B %Y", time.strptime(m + "-01", "%Y-%m-%d"))
    except Exception:
        return m


def day_label(d):
    try:
        return "%s (%s)" % (d, time.strftime("%a", time.strptime(d, "%Y-%m-%d")))
    except Exception:
        return d


# ---- load the rollup ----------------------------------------------------------------------------
DOC = {}
try:
    with open(STORE, "r", encoding="utf-8") as fh:
        loaded = json.load(fh)
    if isinstance(loaded, dict):
        DOC = loaded
except Exception:
    DOC = {}

HOSTS = DOC.get("hosts") if isinstance(DOC.get("hosts"), dict) else {}
SINCE = DOC.get("since") or ""
UPDATED = DOC.get("updated") or 0

# days[day] = [bytes, hits] for this scope
days = {}
if SCOPE == "all":
    for h in HOSTS.values():
        if not isinstance(h, dict):
            continue
        for d, rec in h.items():
            cur = days.setdefault(d, [0, 0])
            cur[0] += int(rec[0])
            cur[1] += int(rec[1])
else:
    for d, rec in (HOSTS.get(SCOPE) or {}).items():
        days[d] = [int(rec[0]), int(rec[1])]


def bucket_of(day, win):
    if win == "daily":
        return day
    if win == "monthly":
        return day[:7]
    if win == "yearly":
        return day[:4]
    if win == "weekly":
        try:
            return time.strftime("%G-W%V", time.strptime(day, "%Y-%m-%d"))
        except Exception:
            return day
    return "total"


def label_of(key, win):
    if win == "daily":
        return day_label(key)
    if win == "weekly":
        return week_label(key)
    if win == "monthly":
        return month_label(key)
    return key


def series(win):
    out = {}
    for d, rec in days.items():
        k = bucket_of(d, win)
        cur = out.setdefault(k, [0, 0, 0])
        cur[0] += rec[0]
        cur[1] += rec[1]
        cur[2] += 1                      # days contributing to this bucket
    rows = sorted(out.items(), key=lambda kv: kv[0], reverse=True)
    if CAP.get(win):
        rows = rows[:CAP[win]]
    return rows


# ---- rendering ----------------------------------------------------------------------------------
def tile(k, val, sub=""):
    return ("<div class=tile><div class=k>%s</div><div class=val>%s</div>"
            "<div class=d>%s</div></div>" % (esc(k), val, sub))


def table(rows, win):
    if not rows:
        return ("<div class=card><p class=empty>No bandwidth on record yet for this scope. "
                "The history file fills as the pipeline runs.</p></div>")
    top = max(r[1][0] for r in rows) or 1
    tot_by = sum(r[1][0] for r in rows)
    tot_hits = sum(r[1][1] for r in rows)
    h = ["<div class=card><div class=tw><table><tr><th>Period</th><th>Bytes served</th>"
         "<th>Requests</th><th>Avg per request</th><th>Share</th></tr>"]
    for k, (by, hits, nd) in rows:
        pct = int(round(by * 100.0 / top))
        share = (by * 100.0 / tot_by) if tot_by else 0
        avg = hb(by / hits) if hits else "0 B"
        h.append("<tr><td class=lbl>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%.1f%%</td></tr>"
                 % (esc(label_of(k, win)), bar(pct), hb(by), num(hits), avg, share))
    h.append("<tr class=tot><td class=lbl>Total</td><td>%s</td><td>%s</td><td>%s</td><td>100.0%%</td></tr>"
             % (hb(tot_by), num(tot_hits), hb(tot_by / tot_hits) if tot_hits else "0 B"))
    h.append("</table></div></div>")
    return "".join(h)


def by_domain_table():
    # Aggregate scope only: bandwidth per domain, all time.
    rows = []
    for host, hd in HOSTS.items():
        if not isinstance(hd, dict):
            continue
        by = sum(int(r[0]) for r in hd.values())
        hits = sum(int(r[1]) for r in hd.values())
        rows.append((host, by, hits))
    rows.sort(key=lambda r: r[1], reverse=True)
    if not rows:
        return ""
    top = rows[0][1] or 1
    tot = sum(r[1] for r in rows) or 1
    h = ["<h2>By domain, all time</h2><div class=card><div class=tw><table>"
         "<tr><th>Domain</th><th>Bytes served</th><th>Requests</th><th>Share</th></tr>"]
    for host, by, hits in rows:
        h.append("<tr><td class=lbl>%s%s</td><td>%s</td><td>%s</td><td>%.1f%%</td></tr>"
                 % (esc(host), bar(int(round(by * 100.0 / top))), hb(by), num(hits), by * 100.0 / tot))
    h.append("</table></div></div>")
    return "".join(h)


def view(win):
    rows = series(win)
    tot_by = sum(r[1][0] for r in rows)
    tot_hits = sum(r[1][1] for r in rows)
    ndays = sum(r[1][2] for r in rows)
    tiles = [tile("Bytes served", hb(tot_by), esc(WNOTE[win])),
             tile("Requests", num(tot_hits), "in view"),
             tile("Avg per request", hb(tot_by / tot_hits) if tot_hits else "0 B", "response bodies"),
             tile("Avg per day", hb(tot_by / ndays) if ndays else "0 B",
                  "%s days on record" % num(ndays))]
    if rows and win != "total":
        peak = max(rows, key=lambda r: r[1][0])
        tiles.append(tile("Busiest period", hb(peak[1][0]), esc(label_of(peak[0], win))))
    body = "<div class=tiles>%s</div>" % "".join(tiles)
    if win == "total":
        body += by_domain_table() if SCOPE == "all" else table(rows, win)
    else:
        body += table(rows, win)
    return body


VIEWS = {w: view(w) for w in WINS}

SEG = "".join("<button class=\"segbtn%s\" data-view=%s>%s</button>"
              % (" active" if w == "daily" else "", w, WBTN[w]) for w in WINS)
PANES = "".join("<div class=view id=view-%s%s>%s</div>\n"
                % (w, "" if w == "daily" else " hidden", VIEWS[w]) for w in WINS)

foot = ("Bytes served is Caddy's per response <code>size</code>, the response body only. It excludes "
        "response headers and every inbound request byte, so this figure reads lower than the transfer "
        "your host bills you for. Treat it as served content volume, not billed traffic."
        "<br>Counts all traffic including bots, so totals run higher than the humans only GoAccess "
        "panels.")
if SINCE:
    foot += ("<br>Records begin %s. Yearly and Total cover only the span since then, not the full "
             "life of the sites: the Caddy logs roll away long before a year, so this page keeps its "
             "own history and it grows from here." % esc(SINCE))
else:
    foot += "<br>No history recorded yet. This page fills in once the pipeline has run."
if UPDATED:
    foot += "<br>History last updated %s." % time.strftime("%Y-%m-%d %H:%M", time.localtime(UPDATED))

# Uses its own localStorage key: the shared hailsTimeView key only accepts daily, weekly and
# monthly, so writing yearly or total into it would reset the other pages to Daily.
SWITCH = """<script>
(function(){var btns=document.querySelectorAll('.segbtn');var V=['daily','weekly','monthly','yearly','total'];
function show(v){V.forEach(function(x){var el=document.getElementById('view-'+x);if(el)el.hidden=(x!==v);});
btns.forEach(function(b){b.classList.toggle('active',b.getAttribute('data-view')===v);});
try{localStorage.setItem('hailsBandwidthView',v);}catch(e){}}
btns.forEach(function(b){b.onclick=function(){show(b.getAttribute('data-view'));};});
var saved='daily';try{saved=localStorage.getItem('hailsBandwidthView')||'daily';}catch(e){}
if(V.indexOf(saved)<0)saved='daily';show(saved);})();
</script>
<script src="/nav.js"></script>"""

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
h2{font-weight:700;font-size:1.05rem;margin:1.4rem 0 .6rem;color:#cfe0ff}
a{color:#7aa2f7}
code{background:rgba(255,255,255,.08);border-radius:4px;padding:1px 5px;font-size:.92em}
.seg{display:inline-flex;gap:4px;background:rgba(13,14,33,.6);border:1px solid rgba(255,255,255,.1);border-radius:999px;padding:4px;margin:0 0 1.2rem;flex-wrap:wrap}
.segbtn{background:none;border:none;color:rgba(255,255,255,.7);font:600 13px Roboto,system-ui,sans-serif;padding:7px 20px;border-radius:999px;cursor:pointer;transition:.2s}
.segbtn.active{background:linear-gradient(90deg,#016eda,#d900c0);color:#fff}
.segbtn:hover:not(.active){color:#fff}
.card{background:rgba(13,14,33,.85);border:1px solid rgba(255,255,255,.09);border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.45);padding:1rem 1.2rem;margin:0 0 1rem}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th{text-align:right;color:rgba(255,255,255,.5);font-weight:500;padding:.5rem .6rem;border-bottom:1px solid rgba(255,255,255,.12);white-space:nowrap}
th:first-child{text-align:left}
td{text-align:right;padding:.4rem .6rem;border-bottom:1px solid rgba(255,255,255,.06);white-space:nowrap}
td.lbl{text-align:left;position:relative;max-width:520px;overflow:hidden;text-overflow:ellipsis;color:#fff}
.bar{position:absolute;left:0;bottom:1px;height:3px;border-radius:2px;background:linear-gradient(90deg,#016eda,#d900c0);opacity:.85}
tr.tot td{font-weight:700;color:#cfe0ff;border-top:1px solid rgba(255,255,255,.14)}
tr:hover td{background:rgba(111,76,255,.06)}
.empty{color:rgba(255,255,255,.4);padding:.6rem}
.foot{color:rgba(255,255,255,.42);font-size:.78rem;margin-top:1.2rem;line-height:1.6}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem;margin:0 0 1.2rem}
.tile{background:rgba(13,14,33,.85);border:1px solid rgba(255,255,255,.09);border-radius:14px;padding:.85rem 1rem}
.tile .k{font:600 11px Roboto,system-ui,sans-serif;color:rgba(255,255,255,.55);text-transform:uppercase;letter-spacing:.06em}
.tile .val{font:900 1.5rem/1.2 Roboto,system-ui,sans-serif;color:#fff;margin:.15rem 0}
.tile .d{font-size:.8rem;color:rgba(255,255,255,.45)}
</style>"""

SUB = TITLE + " : Bandwidth"
OUT = ("<!doctype html><meta charset=utf-8><meta name=viewport content=\"width=device-width,"
       "initial-scale=1\">\n<title>%s</title>\n%s\n%s\n<div class=wrap>\n<h1>%s</h1>\n"
       "<div class=seg>%s</div>\n%s<div class=foot>%s</div>\n</div>\n%s\n"
       % (esc(SUB), FAVICON, STYLE, esc(SUB), SEG, PANES, foot, SWITCH))

os.makedirs(OUTDIR, exist_ok=True)
tmp = os.path.join(OUTDIR, ".bandwidth.html")
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(OUT)
os.replace(tmp, os.path.join(OUTDIR, "bandwidth.html"))
