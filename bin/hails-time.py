#!/usr/bin/env python3
# Time breakdown page for one stats scope, written as time.html.
# Reads the scope's preprocessed JSON log on stdin, prints a full HTML page on stdout.
# Daily / Weekly / Monthly tables plus a day of week by hour heatmap. Counts all traffic
# including bots, in server local time.
import sys, json, time, html

TITLE = sys.argv[1] if len(sys.argv) > 1 else "All domains (aggregate)"
DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def newb():
    return {"hits": 0, "valid": 0, "failed": 0, "bytes": 0, "vis": set()}


NOW = int(time.time())
C24, C7, C30 = NOW - 86400, NOW - 604800, NOW - 2592000
hours = {h: newb() for h in range(24)}    # Daily: last 24 hours, by hour of day
days = {d: newb() for d in range(7)}      # Weekly: last 7 days, by day of week
weeks = {}                                # Monthly: last 30 days, by week
heat = [[0] * 24 for _ in range(7)]       # last 30 days: hits by (day of week, hour)


def hb(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (int(n), unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "0 B"


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        o = json.loads(line)
    except Exception:
        continue
    try:
        ts = int(o.get("ts"))
    except Exception:
        continue
    if ts < C30:
        continue
    lt = time.localtime(ts)
    req = o.get("request") or {}
    ip = req.get("client_ip") or ""
    try:
        st = int(o.get("status", 0))
    except Exception:
        st = 0
    ok = st < 400
    try:
        by = int(o.get("size") or 0)
    except Exception:
        by = 0
    targets = []
    if ts >= C24:
        targets.append(hours[lt.tm_hour])
    if ts >= C7:
        targets.append(days[lt.tm_wday])
    wk = time.strftime("%G-W%V", lt)
    if wk not in weeks:
        weeks[wk] = newb()
    targets.append(weeks[wk])
    for b in targets:
        b["hits"] += 1
        b["valid"] += 1 if ok else 0
        b["failed"] += 0 if ok else 1
        b["bytes"] += by
        if ip:
            b["vis"].add(ip)
    heat[lt.tm_wday][lt.tm_hour] += 1


def week_label(wk):
    try:
        mon = time.strftime("%Y-%m-%d", time.strptime(wk + "-1", "%G-W%V-%u"))
        return "Week of " + mon + " (" + wk + ")"
    except Exception:
        return wk


def table(buckets, order, labelfn):
    mx = max((buckets[k]["hits"] for k in order), default=0) or 1
    th = tva = tf = tb = 0
    tv = set()
    body = ""
    for k in order:
        b = buckets[k]
        th += b["hits"]
        tv |= b["vis"]
        tva += b["valid"]
        tf += b["failed"]
        tb += b["bytes"]
        pct = int(round(100.0 * b["hits"] / mx))
        body += (
            "<tr><td class=lbl>{lab}<span class=bar style='width:{pct}%'></span></td>"
            "<td>{hits}</td><td>{uniq}</td><td class=ok>{ok}</td>"
            "<td class=bad>{bad}</td><td>{bw}</td></tr>"
        ).format(lab=html.escape(labelfn(k)), pct=pct, hits=b["hits"],
                 uniq=len(b["vis"]), ok=b["valid"], bad=b["failed"], bw=hb(b["bytes"]))
    foot = (
        "<tr class=tot><td class=lbl>Total</td><td>{h}</td><td>{u}</td>"
        "<td class=ok>{v}</td><td class=bad>{f}</td><td>{bw}</td></tr>"
    ).format(h=th, u=len(tv), v=tva, f=tf, bw=hb(tb))
    return (
        "<div class=tw><table><thead><tr><th>Bucket</th><th>Hits</th><th>Unique Visitors</th>"
        "<th>Valid Requests</th><th>Failed Requests</th><th>Bandwidth</th></tr></thead>"
        "<tbody>" + body + foot + "</tbody></table></div>"
    )


def heatmap():
    mx = max((heat[d][h] for d in range(7) for h in range(24)), default=0) or 1
    cols = "".join("<th>%02d</th>" % h for h in range(24))
    rows = ""
    for d in range(7):
        cells = ""
        for h in range(24):
            v = heat[d][h]
            inten = v / mx
            if v:
                bg = "background:rgba(111,76,255,%.3f)" % (0.08 + inten * 0.82)
            else:
                bg = "background:rgba(255,255,255,.02)"
            label = str(v) if inten >= 0.15 else ""
            cells += "<td class=hc style='%s' title='%s %02d:00, %d hits'>%s</td>" % (
                bg, DOW[d], h, v, label)
        rows += "<tr><th class=hd>%s</th>%s</tr>" % (DOW[d][:3], cells)
    return ("<div class=tw><table class=heat><thead><tr><th></th>" + cols +
            "</tr></thead><tbody>" + rows + "</tbody></table></div>")


hour_tbl = table(hours, list(range(24)), lambda h: "%02d:00" % h)
day_tbl = table(days, list(range(7)), lambda d: DOW[d])
week_tbl = table(weeks, sorted(weeks.keys()), week_label)
heat_tbl = heatmap()

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>time breakdown</title>
<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,AAABAAEAEBAQAAEABAAoAQAAFgAAACgAAAAQAAAAIAAAAAEABAAAAAAAgAAAAAAAAAAAAAAAEAAAAAAAAADGxsYAWFhYABwcHABfAP8A/9dfAADXrwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIiIiIiIiIiIjMlUkQgAiIiIiIiIiIiIiIzJVJEIAAAIiIiIiIiIiIiMyVSRCAAIiIiIiIiIiIiIRERERERERERERERERERERIiIiIiIiIiIgACVVUiIiIiIiIiIiIiIiIAAlVVIiIiIiIiIiIiIiIhEREREREREREREREREREREAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">
<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700;900&display=swap');
*{box-sizing:border-box}
body{font-family:'Roboto',system-ui,sans-serif;min-height:100vh;margin:0;padding:4.4rem 1.2rem 3rem;color:#e8e8f0;background:radial-gradient(1100px 600px at 50% -10%,rgba(111,76,255,.16),transparent 60%),radial-gradient(900px 500px at 88% 6%,rgba(1,110,218,.12),transparent 55%),radial-gradient(800px 480px at 10% 16%,rgba(217,0,192,.08),transparent 55%),#04050c}
.wrap{max-width:1000px;margin:0 auto}
h1{font-weight:900;font-size:1.7rem;margin:0 0 .2rem}
.sub{color:rgba(255,255,255,.55);margin:0 0 1.4rem;font-size:.9rem}
.seg{display:inline-flex;gap:4px;background:rgba(13,14,33,.6);border:1px solid rgba(255,255,255,.1);border-radius:999px;padding:4px;margin:0 0 1.4rem}
.segbtn{background:none;border:none;color:rgba(255,255,255,.7);font:600 13px Roboto,system-ui,sans-serif;padding:7px 20px;border-radius:999px;cursor:pointer;transition:.2s}
.segbtn.active{background:linear-gradient(90deg,#016eda,#d900c0);color:#fff}
.segbtn:hover:not(.active){color:#fff}
.card{background:rgba(13,14,33,.85);border:1px solid rgba(255,255,255,.09);border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.45);padding:1.2rem 1.3rem;margin:0 0 1rem}
.card h2{font-weight:700;font-size:.82rem;letter-spacing:.09em;text-transform:uppercase;color:rgba(255,255,255,.55);margin:0 0 1rem}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th{text-align:right;color:rgba(255,255,255,.5);font-weight:500;padding:.5rem .6rem;border-bottom:1px solid rgba(255,255,255,.12)}
th:first-child{text-align:left}
td{text-align:right;padding:.45rem .6rem;border-bottom:1px solid rgba(255,255,255,.06)}
td.lbl{text-align:left;position:relative;white-space:nowrap;color:#fff}
td.ok{color:#8fffca}
td.bad{color:#ff9db2}
.bar{position:absolute;left:0;bottom:2px;height:3px;border-radius:2px;background:linear-gradient(90deg,#016eda,#d900c0);opacity:.85}
tr.tot td{font-weight:700;color:#cfe0ff;border-top:1px solid rgba(255,255,255,.14)}
tr:hover td{background:rgba(111,76,255,.08)}
table.heat{font-size:.7rem;border-collapse:separate;border-spacing:2px}
table.heat th{padding:.2rem .25rem;color:rgba(255,255,255,.4);font-weight:500;border:none}
table.heat th.hd{text-align:right;color:rgba(255,255,255,.65)}
table.heat td.hc{min-width:26px;text-align:center;color:#fff;padding:.35rem .2rem;border:none;border-radius:4px}
table.heat tr:hover td{background:inherit}
.foot{color:rgba(255,255,255,.4);font-size:.78rem;margin-top:1.4rem;line-height:1.6}
</style>
<div class=wrap>
<h1>Time breakdown</h1>
<p class=sub>__SUB__</p>
<div class=seg>
<button class="segbtn active" data-view=daily>Daily</button>
<button class="segbtn" data-view=weekly>Weekly</button>
<button class="segbtn" data-view=monthly>Monthly</button>
</div>
<div class=view id=view-daily><div class=card><h2>Daily (last 24 hours): visits by hour of day</h2>__HOUR__</div></div>
<div class=view id=view-weekly hidden><div class=card><h2>Weekly (last 7 days): visits by day of week</h2>__DAY__</div></div>
<div class=view id=view-monthly hidden><div class=card><h2>Monthly (last 30 days): visits by week</h2>__WEEK__</div></div>
<div class=card><h2>Heatmap (last 30 days): day of week by hour, colored by hits</h2>__HEAT__</div>
<p class=foot>Daily is the last 24 hours by hour, Weekly the last 7 days by day of week, Monthly the last 30 days by week. The heatmap sums hits by weekday and hour over the last 30 days, so brighter cells are busier times. All traffic including bots, so totals run higher than the humans only chart dashboard. Times in server local time. Valid Requests are responses with status under 400, Failed Requests are status 400 and above.</p>
</div>
<script>
(function(){
var btns=document.querySelectorAll('.segbtn');var V=['daily','weekly','monthly'];
function show(v){V.forEach(function(x){document.getElementById('view-'+x).hidden=(x!==v);});
btns.forEach(function(b){b.classList.toggle('active',b.getAttribute('data-view')===v);});
try{localStorage.setItem('hailsTimeView',v);}catch(e){}}
btns.forEach(function(b){b.onclick=function(){show(b.getAttribute('data-view'));};});
var saved='daily';try{saved=localStorage.getItem('hailsTimeView')||'daily';}catch(e){}
if(V.indexOf(saved)<0)saved='daily';show(saved);
})();
</script>
<script src="/nav.js"></script>
"""

sys.stdout.write(PAGE.replace("__SUB__", html.escape(TITLE))
                     .replace("__HOUR__", hour_tbl)
                     .replace("__DAY__", day_tbl)
                     .replace("__WEEK__", week_tbl)
                     .replace("__HEAT__", heat_tbl))
