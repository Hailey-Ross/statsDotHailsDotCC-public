#!/bin/bash
# Rebuilds the whole stats site under /srv/stats: for each scope (aggregate plus each domain) a
# dashboard, one page per panel, and a top bar with Domain and Panel switchers, plus a landing
# picker at /.
# Deployed to the VPS at /usr/local/bin/hails-stats.sh ; run every 5 min by hails-stats.timer.
STATS=/srv/stats; CONF=/etc/goaccess/goaccess.conf; NAVFILE=/srv/stats/nav.js
PRE=/usr/local/bin/hails-stats-pre.py
TIMEGEN=/usr/local/bin/hails-time.py
PANELGEN=/usr/local/bin/hails-panels.py
ROLLUP=/usr/local/bin/hails-rollup.py
SERVEDGEN=/usr/local/bin/hails-served.py
BWGEN=/usr/local/bin/hails-bandwidth.py
PERFGEN=/usr/local/bin/hails-perf.py
LOGS=$(ls -tr /var/log/caddy/access.log* 2>/dev/null); [ -z "$LOGS" ] && exit 0
TMP=$(mktemp); cat $LOGS | python3 "$PRE" > "$TMP"
# Aggregate variant: same data with the host prefixed onto each uri, so aggregate URL panels show
# which domain each row is for. Per domain scopes use the clean $TMP instead.
AGGLOG=$(mktemp); cat $LOGS | python3 "$PRE" --prefix-host > "$AGGLOG"
SCOPELOG=$(mktemp)

# GoAccess renders only the chart dashboard; every dedicated panel page comes from hails-panels.py,
# so GOAPANELS is intentionally empty.
GOAPANELS=( )
# DROP is the Panel dropdown order.
DROP=( "Overview|index" "Requested Files|requests" "Trending|trending" "Referrers|referrers" \
"Referring Sites|ref_sites" "Entry Pages|entry" "Exit Pages|exit" "Status Codes|status" \
"Error Pages|error_pages" "404 Not Found|not_found" "HTTP Methods|methods" "Content Types|content_types" \
"Bandwidth|bandwidth" \
"Static Files|static" "Slowest Endpoints|slow" "Visitors (IPs)|hosts" "Geo Location|geo" \
"Networks (ASN)|asn" "Operating Systems|os" "Browsers|browsers" "TLS Versions|tls" \
"Virtual Hosts|vhosts" "Visitors by Day|days" "Time Distribution|time" "Charts (dashboard)|dashboard" \
"Performance (VPS)|perf" )
# Panels that are machine wide rather than per domain: rendered into /all only.
GLOBALKEYS="perf"
ALLMOD="REQUESTS REQUESTS_STATIC NOT_FOUND REFERRERS REFERRING_SITES HOSTS GEO_LOCATION ASN OS BROWSERS VISITORS VISIT_TIMES STATUS_CODES MIME_TYPE TLS_TYPE VIRTUAL_HOSTS KEYPHRASES REMOTE_USER CACHE_STATUS"
ignore_except(){ local keep="$1" out=""; for m in $ALLMOD; do [ "$m" != "$keep" ] && out="$out --ignore-panel=$m"; done; echo "$out"; }

gen_scope(){ local dir="$1" title="$2" lf="$3" scope="$4"; mkdir -p "$dir"
  # GoAccess writes dashboard.html; hails-panels.py writes index.html, the scope's landing page.
  goaccess "$lf" --config-file="$CONF" --html-custom-js=/nav.js --html-report-title="$title" -o "$dir/.dashboard.html" 2>/dev/null && mv "$dir/.dashboard.html" "$dir/dashboard.html"
  # Single panel pages override the config to stay expanded with more rows; the dashboard above
  # keeps the collapsed config default.
  local pp='{"theme":"darkBlue","perPage":100,"layout":"horizontal","autoHideTables":false}'
  local e key label mod; for e in "${GOAPANELS[@]}"; do IFS='|' read -r key label mod <<< "$e"
    goaccess "$lf" --config-file="$CONF" --html-custom-js=/nav.js --html-prefs="$pp" $(ignore_except "$mod") --html-report-title="$title - $label" -o "$dir/.$key.html" 2>/dev/null && mv "$dir/.$key.html" "$dir/$key.html"
  done
  # hails-panels.py does one log pass and emits index.html plus every custom panel page.
  # hails-time.py emits the time breakdown plus heatmap.
  python3 "$PANELGEN" "$title" "$dir" < "$lf"
  python3 "$TIMEGEN" "$title" < "$lf" > "$dir/.time.html" && mv "$dir/.time.html" "$dir/time.html"
  # hails-bandwidth.py reads the durable rollup, not this scope log, so it takes the scope name
  # ("all" or the host) instead of stdin.
  python3 "$BWGEN" "$title" "$dir" "$scope"
  # hails-perf.py reads the rings from hails-perf-collect.py. Vitals are machine wide, so this
  # renders for the aggregate scope only.
  if [ "$scope" = "all" ]; then python3 "$PERFGEN" "$title" "$dir"; fi; }

rm -rf "$STATS/all" "$STATS/d" "$STATS"/all.html "$STATS"/*.html.bak
hosts=$(grep -oE "\"host\": ?\"[^\"]+\"" "$TMP" | sed -E "s/.*\"host\": ?\"([^\"]+)\".*/\1/" | sort -u)

# nav data
declare -A safeof; domainitems='{label:"All domains (aggregate)",base:"/all"}'; links=""
for h in $hosts; do safe=$(printf '%s' "$h" | tr -c 'A-Za-z0-9.-' '_'); safeof["$h"]="$safe"
  domainitems="$domainitems,{label:\"$h\",base:\"/d/$safe\"}"; links="$links<li><a href=\"/d/$safe/index.html\">$h</a></li>"; done
panelitems=""; for e in "${DROP[@]}"; do IFS='|' read -r label key <<< "$e"; panelitems="$panelitems{label:\"$label\",key:\"$key\"},"; done; panelitems=${panelitems%,}

globalitems=""; for k in $GLOBALKEYS; do globalitems="$globalitems\"$k\","; done; globalitems=${globalitems%,}

cat > "$NAVFILE" <<JS
(function(){var DOMAINS=[$domainitems];var PANELS=[$panelitems];var GLOBAL=[$globalitems];
if(!document.querySelector("link[rel~='icon']")){var fi=document.createElement('link');fi.rel='icon';fi.type='image/x-icon';fi.href='data:image/x-icon;base64,AAABAAEAEBAQAAEABAAoAQAAFgAAACgAAAAQAAAAIAAAAAEABAAAAAAAgAAAAAAAAAAAAAAAEAAAAAAAAADGxsYAWFhYABwcHABfAP8A/9dfAADXrwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIiIiIiIiIiIjMlUkQgAiIiIiIiIiIiIiIzJVJEIAAAIiIiIiIiIiIiMyVSRCAAIiIiIiIiIiIiIRERERERERERERERERERERIiIiIiIiIiIgACVVUiIiIiIiIiIiIiIiIAAlVVIiIiIiIiIiIiIiIhEREREREREREREREREREREAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';(document.head||document.documentElement).appendChild(fi);}
var p=location.pathname.split('/').filter(Boolean);var base='/all',file='index.html';
if(p[0]==='all'){base='/all';file=p[1]||'index.html';}else if(p[0]==='d'){base='/d/'+p[1];file=p[2]||'index.html';}
var curKey=file.replace('.html','');
var SEL='background:rgba(13,14,33,.72);color:#e8e8f0;border:1px solid rgba(255,255,255,.16);border-radius:999px;padding:6px 14px;font:500 13px Roboto,system-ui,sans-serif;max-width:46vw;cursor:pointer;outline:none';
function mk(){var s=document.createElement('select');s.style.cssText=SEL;return s;}
function lab(t){var s=document.createElement('span');s.textContent=t;s.style.cssText='color:rgba(255,255,255,.55);font:600 11px Roboto,system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase';return s;}
function mount(){if(document.getElementById('domain-switcher')||!document.body)return;
var bar=document.createElement('div');bar.id='domain-switcher';
bar.style.cssText='position:fixed;top:0;left:0;right:0;z-index:99999;background:rgba(8,9,20,.72);-webkit-backdrop-filter:blur(16px) saturate(140%);backdrop-filter:blur(16px) saturate(140%);color:#e8e8f0;padding:9px 16px;font:14px Roboto,system-ui,sans-serif;border-bottom:1px solid transparent;border-image:linear-gradient(90deg,#016eda,#d900c0) 1;display:flex;gap:10px;align-items:center;flex-wrap:wrap;box-shadow:0 6px 24px rgba(0,0,0,.5)';
var dot=document.createElement('span');dot.style.cssText='width:7px;height:7px;border-radius:50%;background:#6f4cff;box-shadow:0 0 10px #6f4cff;display:inline-block';
var brand=document.createElement('a');brand.href='/';brand.textContent='hails analytics';brand.style.cssText='font:900 15px Roboto,system-ui,sans-serif;color:#fff;text-decoration:none;letter-spacing:.02em;margin-right:6px';
function isGlobal(k){return GLOBAL.indexOf(k)>=0;}
var ds=mk();DOMAINS.forEach(function(d){var o=document.createElement('option');o.value=d.base;o.textContent=d.label;if(base===d.base)o.selected=true;ds.appendChild(o);});
// A global panel exists only under /all, so switching domain from one lands on that domain's overview.
ds.onchange=function(){location.href=ds.value+'/'+(isGlobal(curKey)?'index.html':file);};
var ps=mk();
PANELS.forEach(function(x){var o=document.createElement('option');o.value=x.key;o.textContent=x.label;if(curKey===x.key)o.selected=true;ps.appendChild(o);});
// Machine wide panels are rendered into /all only, so route there whatever scope we are in.
ps.onchange=function(){location.href=(isGlobal(ps.value)?'/all':base)+'/'+ps.value+'.html';};
var lnkcss='color:#cfe0ff;text-decoration:none;font:600 13px Roboto,system-ui,sans-serif;padding:6px 14px;border:1px solid rgba(255,255,255,.16);border-radius:999px;background:rgba(13,14,33,.72)';
var mapl=document.createElement('a');mapl.href='/map.html';mapl.textContent='Map';mapl.style.cssText='margin-left:auto;'+lnkcss;
var gear=document.createElement('a');gear.href='/settings.html';gear.textContent='⚙ Settings';gear.style.cssText=lnkcss;
bar.appendChild(dot);bar.appendChild(brand);bar.appendChild(lab('Domain'));bar.appendChild(ds);bar.appendChild(lab('Panel'));bar.appendChild(ps);bar.appendChild(mapl);bar.appendChild(gear);
document.body.insertBefore(bar,document.body.firstChild);document.body.style.paddingTop='56px';}
if(document.body)mount();document.addEventListener('DOMContentLoaded',mount);window.addEventListener('load',mount);setTimeout(mount,600);})();
JS
chmod 644 "$NAVFILE"
# Refresh the skin and settings page from the deployed sources so the web root serves current assets.
cp -f /etc/goaccess/custom.css "$STATS/custom.css" 2>/dev/null && chmod 644 "$STATS/custom.css"
cp -f /etc/goaccess/settings.html "$STATS/settings.html" 2>/dev/null && chmod 644 "$STATS/settings.html"

# Merge this pass into the durable bandwidth store, once per regen and before any scope work: the
# scope generators would race on the file. It is the only state surviving the rm -rf above.
install -d /var/lib/hails-stats
python3 "$ROLLUP" < "$TMP"

# Public requests served counter, read straight off the rollup just merged. Does nothing unless
# HAILS_SERVED_ROOT is set. It rewrites its file only when the rounded figure changes, so running it
# every regen costs a read and a compare on the runs where nothing moved. Fully guarded: the counter
# must never be able to fail the dashboard rebuild.
#
# systemd passes HAILS_SERVED_ROOT in from /etc/hails-stats/config.env via EnvironmentFile, but a
# manual run of this script gets no such thing. Pull just that one key out rather than sourcing the
# file: config.env holds unquoted values containing pipes and parentheses that a shell must never
# evaluate.
if [ -z "${HAILS_SERVED_ROOT:-}" ] && [ -r /etc/hails-stats/config.env ]; then
  HAILS_SERVED_ROOT=$(sed -n 's/^[[:space:]]*HAILS_SERVED_ROOT=//p' /etc/hails-stats/config.env | tail -1)
  export HAILS_SERVED_ROOT
fi
python3 "$SERVEDGEN" >/dev/null 2>&1 || true

gen_scope "$STATS/all" "All domains (aggregate)" "$AGGLOG" "all"
for h in $hosts; do safe=${safeof["$h"]}; grep -F "\"host\": \"$h\"" "$TMP" > "$SCOPELOG"; gen_scope "$STATS/d/$safe" "$h" "$SCOPELOG" "$h"; done

cat > "$STATS/.index.html" <<HTML
<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>hails analytics</title>
<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,AAABAAEAEBAQAAEABAAoAQAAFgAAACgAAAAQAAAAIAAAAAEABAAAAAAAgAAAAAAAAAAAAAAAEAAAAAAAAADGxsYAWFhYABwcHABfAP8A/9dfAADXrwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIiIiIiIiIiIjMlUkQgAiIiIiIiIiIiIiIzJVJEIAAAIiIiIiIiIiIiMyVSRCAAIiIiIiIiIiIiIRERERERERERERERERERERIiIiIiIiIiIgACVVUiIiIiIiIiIiIiIiIAAlVVIiIiIiIiIiIiIiIhEREREREREREREREREREREAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">
<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700;900&display=swap');
*{box-sizing:border-box}
body{font-family:'Roboto',system-ui,sans-serif;min-height:100vh;margin:0;padding:3.5rem 1.2rem;color:#e8e8f0;background:radial-gradient(1100px 600px at 50% -10%,rgba(111,76,255,.16),transparent 60%),radial-gradient(900px 500px at 88% 6%,rgba(1,110,218,.12),transparent 55%),radial-gradient(800px 480px at 10% 16%,rgba(217,0,192,.08),transparent 55%),#04050c}
.wrap{max-width:720px;margin:0 auto}
h1{font-weight:900;font-size:2rem;margin:0 0 .3rem;letter-spacing:.01em}
.sub{color:rgba(255,255,255,.55);margin:0 0 2rem;font-size:.95rem}
a{color:#0085ff;text-decoration:none}
.agg{display:flex;align-items:center;gap:.6rem;margin:0 0 2rem;padding:1rem 1.3rem;border-radius:16px;color:#fff;font-size:1.1rem;font-weight:500;background:linear-gradient(rgba(13,14,33,.72),rgba(13,14,33,.72)) padding-box,radial-gradient(circle at left top,#016eda,#d900c0) border-box;border:1.5px solid transparent;-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);box-shadow:0 8px 30px rgba(0,0,0,.45);transition:box-shadow .3s,transform .3s}
.agg:hover{box-shadow:0 8px 30px rgba(0,0,0,.5),0 0 22px rgba(111,76,255,.42);transform:translateY(-2px)}
h2{font-weight:700;font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;color:rgba(255,255,255,.5);margin:0 0 .8rem}
ul{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:.7rem}
li a{display:block;padding:.75rem 1rem;border-radius:12px;background:rgba(13,14,33,.55);border:1px solid rgba(255,255,255,.09);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);color:#e8e8f0;font-size:1rem;transition:border-color .25s,box-shadow .25s,transform .25s}
li a:hover{border-color:rgba(111,76,255,.5);box-shadow:0 0 20px rgba(111,76,255,.3);transform:translateY(-2px);color:#fff}
.tip{color:rgba(255,255,255,.4);font-size:.8rem;margin-top:2rem}
</style>
<div class=wrap>
<h1>hails analytics</h1><p class=sub>self hosted traffic across the whole hails network</p>
<a class=agg href="/all/index.html"><span style="width:9px;height:9px;border-radius:50%;background:#6f4cff;box-shadow:0 0 12px #6f4cff;display:inline-block"></span> All domains (aggregate)</a>
<h2>Individual domains</h2><ul>$links</ul>
<p class=tip>Tip: use the Domain and Panel dropdowns at the top of any report to jump around.</p>
</div>
HTML
mv "$STATS/.index.html" "$STATS/index.html"
find "$STATS" -name '*.html' -exec chmod 644 {} +; chmod 644 "$NAVFILE"
rm -f "$TMP" "$SCOPELOG" "$AGGLOG"
