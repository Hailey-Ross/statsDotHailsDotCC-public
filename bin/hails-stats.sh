#!/bin/bash
# Rebuilds the whole stats site under /srv/stats: a dashboard, one page per panel and a top bar for
# each scope (the aggregate plus each domain), plus a landing picker at /.
# Run by hails-stats.timer.
STATS=/srv/stats; CONF=/etc/goaccess/goaccess.conf; NAVFILE=/srv/stats/nav.js
PRE=/usr/local/bin/hails-stats-pre.py
PANELGEN=/usr/local/bin/hails-panels.py
ROLLUP=/usr/local/bin/hails-rollup.py
SERVEDGEN=/usr/local/bin/hails-served.py
BWGEN=/usr/local/bin/hails-bandwidth.py
PERFGEN=/usr/local/bin/hails-perf.py
RUN_T0=$(date +%s.%N)
HORIZON_DAYS=61
LOGS=$(find /var/log/caddy -maxdepth 1 -type f \( -name 'access.log' -o -name 'access-*.log*' \) \
  -mtime -"$HORIZON_DAYS" -printf '%T@\t%p\n' 2>/dev/null | sort -n | cut -f2-)
[ -z "$LOGS" ] && exit 0

TIMING="${HAILS_TIMING:-/var/lib/hails-stats/regen-timing.tsv}"
TIMING_KEEP=20000
RUN_TS=$(date +%s)
install -d "$(dirname "$TIMING")" 2>/dev/null
: >> "$TIMING" 2>/dev/null || TIMING=/dev/null
_T0=""
tstart(){ _T0=$(date +%s.%N); }
# tend PHASE [SCOPE] [SIZE]   SIZE is bytes or rows, whatever that phase is measured in.
tend(){
  [ -n "$_T0" ] || return 0
  local now w
  now=$(date +%s.%N)
  w=$(awk -v a="$_T0" -v b="$now" 'BEGIN{printf "%.3f", b - a}' 2>/dev/null) || w=""
  printf '%s\t%s\t%s\t%s\t%s\n' "$RUN_TS" "$1" "${2:-}" "$w" "${3:-}" >> "$TIMING" 2>/dev/null || true
  _T0=""
}
# Decompressed bytes of the log set. For a .gz that is the ISIZE footer, the last four bytes of the
# member, little endian.
CORPUS=$(while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
      *.gz) od -An -tu4 -j "$(( $(stat -c %s -- "$f" 2>/dev/null || echo 4) - 4 ))" -N4 -- "$f" 2>/dev/null | tr -d ' ' ;;
      *)    stat -c %s -- "$f" 2>/dev/null ;;
    esac
  done <<< "$LOGS" | awk '{s+=$1} END{print s+0}')

# Single keys read out of config.env rather than sourced: the values are unquoted and must never be
# evaluated by a shell.
cfg(){ sed -n "s/^[[:space:]]*$1=//p" /etc/hails-stats/config.env 2>/dev/null | tail -1; }
: "${HAILS_AGG_EXCLUDE:=$(cfg HAILS_AGG_EXCLUDE)}"
: "${HAILS_DROP_HOSTS:=$(cfg HAILS_DROP_HOSTS)}"
: "${HAILS_INTERNAL_DOMAINS:=$(cfg HAILS_INTERNAL_DOMAINS)}"
export HAILS_INTERNAL_DOMAINS

# Fonts are served same origin by default. Point this at another host and that host must send
# Access-Control-Allow-Origin, or the browser blocks the font.
: "${HAILS_FONT_BASE:=$(cfg HAILS_FONT_BASE)}"
: "${HAILS_FONT_BASE:=/fonts}"
HAILS_FONT_BASE="${HAILS_FONT_BASE%/}"
export HAILS_FONT_BASE
FONT_CSS="@font-face{font-family:'Roboto';font-style:normal;font-weight:100 900;font-display:swap;src:url(${HAILS_FONT_BASE}/roboto-latin-ext.woff2) format('woff2');unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}@font-face{font-family:'Roboto';font-style:normal;font-weight:100 900;font-display:swap;src:url(${HAILS_FONT_BASE}/roboto-latin.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}"

# One pass over the raw logs writes both the host prefixed aggregate ($AGGLOG) and one clean log per
# host under $SPLITDIR. HAILS_AGG_EXCLUDE hides a host from the aggregate only; HAILS_DROP_HOSTS
# discards it entirely, so it gets no split log and therefore no scope, no dropdown entry and no
# pages.
AGGLOG=$(mktemp); SPLITDIR=$(mktemp -d)
readlogs(){ local f; while IFS= read -r f; do [ -n "$f" ] || continue
    case "$f" in *.gz) zcat -- "$f";; *) cat -- "$f";; esac
  done <<< "$LOGS"; }
tstart
readlogs | python3 "$PRE" --split "$SPLITDIR" --agg "$AGGLOG" --agg-exclude "$HAILS_AGG_EXCLUDE" \
                         --drop-host "$HAILS_DROP_HOSTS" --tally "$SPLITDIR/tally.json"
tend preprocess "" "$CORPUS"

# hosts.tsv is the preprocessor's proof of work: written last, after every split log is flushed, and
# a non empty one is what gates the swap into place below. Empty means the pass produced nothing,
# which is a failure rather than an empty site.
if [ ! -s "$SPLITDIR/hosts.tsv" ]; then
  rm -rf "$AGGLOG" "$SPLITDIR"
  if [ -d "$STATS/all" ]; then
    echo "hails-stats: preprocessor produced no hosts, keeping the previous dashboard" >&2
    exit 1
  fi
  echo "hails-stats: no hosts in the log yet, nothing to build" >&2
  exit 0
fi

# Panel dropdown order.
DROP=( "Overview|index" "Requested Files|requests" "Trending|trending" "Referrers|referrers" \
"Referring Sites|ref_sites" "Internal Referrers|referrers_internal" "Internal Sites|ref_sites_internal" \
"Entry Pages|entry" "Exit Pages|exit" "Status Codes|status" \
"Error Pages|error_pages" "404 Not Found|not_found" "HTTP Methods|methods" "Content Types|content_types" \
"Bandwidth|bandwidth" \
"Static Files|static" "Slowest Endpoints|slow" "Visitors (IPs)|hosts" "Geo Location|geo" \
"Networks (ASN)|asn" "Operating Systems|os" "Browsers|browsers" "TLS Versions|tls" \
"Virtual Hosts|vhosts" "Visitors by Day|days" "Time Distribution|time" "Charts (dashboard)|dashboard" \
"Performance (VPS)|perf" )
# Machine wide panels: rendered into /all only, and the Panel dropdown routes there from any scope.
GLOBALKEYS="perf"

gen_scope(){ local dir="$1" title="$2" lf="$3" scope="$4"; mkdir -p "$dir"
  local lfsz; lfsz=$(stat -c %s "$lf" 2>/dev/null)
  tstart
  goaccess "$lf" --config-file="$CONF" --html-custom-js=/nav.js --html-report-title="$title" -o "$dir/.dashboard.html" 2>/dev/null && mv "$dir/.dashboard.html" "$dir/dashboard.html"
  tend goaccess "$scope" "$lfsz"
  # The warehouse is the source and the scope log is the fallback. hosts.tsv comes from the log, so a
  # domain that has just started serving is listed before the ingest has ever seen it; --from-db then
  # exits nonzero by design rather than writing an empty dashboard, and the log render fills the gap.
  tstart
  local perr; perr=$(mktemp)
  local psize="db"
  python3 "$PANELGEN" "$title" "$dir" --from-db --scope "$scope" 2>"$perr" \
    || { echo "hails-stats: warehouse render failed for scope '$scope' ($(tr '\n' ' ' < "$perr" | tail -c 300)), falling back to the log" >&2
         psize="$lfsz"
         python3 "$PANELGEN" "$title" "$dir" < "$lf"; }
  rm -f "$perr"
  tend panels "$scope" "$psize"
  tstart
  python3 "$BWGEN" "$title" "$dir" "$scope"
  tend bandwidth "$scope"
  if [ "$scope" = "all" ]; then tstart; python3 "$PERFGEN" "$title" "$dir"; tend perf "$scope"; fi; }

# Everything is built into a staging tree and swapped in at the end. $STAGE sits inside $STATS so
# the swap is a same filesystem rename.
STAGE="$STATS/.stage"
rm -rf "$STAGE" "$STATS"/all.html "$STATS"/*.html.bak
mkdir -p "$STAGE"
declare -A safeof; hosts=""
while IFS=$'\t' read -r h safe; do
  [ -z "$h" ] && continue
  hosts="$hosts $h"; safeof["$h"]="$safe"
done < "$SPLITDIR/hosts.tsv"

domainitems='{label:"All domains (aggregate)",base:"/all"}'; links=""
for h in $hosts; do safe=${safeof["$h"]}
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
# Web root assets, outside the all/ and d/ trees that get swapped.
sed "s|__FONT_BASE__|$HAILS_FONT_BASE|g" /etc/goaccess/custom.css > "$STATS/custom.css" 2>/dev/null && chmod 644 "$STATS/custom.css"
sed "s|__FONT_BASE__|$HAILS_FONT_BASE|g" /etc/goaccess/settings.html > "$STATS/settings.html" 2>/dev/null && chmod 644 "$STATS/settings.html"
cp /etc/goaccess/table.js "$STATS/table.js" 2>/dev/null && chmod 644 "$STATS/table.js"

install -d /var/lib/hails-stats

# Merged once per regen, before any scope work: the scope generators would race on the file. This is
# the only state in the pipeline that cannot be rebuilt from the logs, so the status is checked.
tstart
python3 "$ROLLUP" --tally "$SPLITDIR/tally.json" \
  || echo "hails-stats: rollup failed, bandwidth history NOT merged this run" >&2
tend rollup

# Public requests served counter. Does nothing unless HAILS_SERVED_ROOT is set.
: "${HAILS_SERVED_ROOT:=$(cfg HAILS_SERVED_ROOT)}"
export HAILS_SERVED_ROOT
tstart
python3 "$SERVEDGEN" >/dev/null 2>&1 || true
tend served

gen_scope "$STAGE/all" "All domains (aggregate)" "$AGGLOG" "all"
for h in $hosts; do safe=${safeof["$h"]}; gen_scope "$STAGE/d/$safe" "$h" "$SPLITDIR/$safe.log" "$h"; done

staged_ok=1
# Asserted separately: an empty host list would make the per host loop below vacuously true.
[ -n "$hosts" ] || staged_ok=0
[ -s "$STAGE/all/index.html" ] || staged_ok=0
for h in $hosts; do [ -s "$STAGE/d/${safeof[$h]}/index.html" ] || staged_ok=0; done
if [ "$staged_ok" != 1 ]; then
  echo "hails-stats: staged tree incomplete, keeping the previous dashboard" >&2
  rm -rf "$STAGE" "$AGGLOG" "$SPLITDIR"; exit 1
fi
tstart
find "$STAGE" -name '*.html' -exec chmod 644 {} +
for t in all d; do
  [ -d "$STAGE/$t" ] || continue
  rm -rf "$STATS/.$t.old"
  [ -d "$STATS/$t" ] && mv "$STATS/$t" "$STATS/.$t.old"
  mv "$STAGE/$t" "$STATS/$t"
  rm -rf "$STATS/.$t.old"
done
rm -rf "$STAGE"
tend swap

cat > "$STATS/.index.html" <<HTML
<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>hails analytics</title>
<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,AAABAAEAEBAQAAEABAAoAQAAFgAAACgAAAAQAAAAIAAAAAEABAAAAAAAgAAAAAAAAAAAAAAAEAAAAAAAAADGxsYAWFhYABwcHABfAP8A/9dfAADXrwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIiIiIiIiIiIjMlUkQgAiIiIiIiIiIiIiIzJVJEIAAAIiIiIiIiIiIiMyVSRCAAIiIiIiIiIiIiIRERERERERERERERERERERIiIiIiIiIiIgACVVUiIiIiIiIiIiIiIiIAAlVVIiIiIiIiIiIiIiIhEREREREREREREREREREREAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">
<style>
$FONT_CSS
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
# Top level only: the all/ and d/ trees were chmod'd in the staging area before the swap.
chmod 644 "$STATS"/*.html 2>/dev/null; chmod 644 "$NAVFILE"
rm -rf "$AGGLOG" "$SPLITDIR"

_T0="$RUN_T0"; tend total "" "$CORPUS"
if [ -f "$TIMING" ]; then
  lines=$(awk 'END{print NR}' "$TIMING" 2>/dev/null || echo 0)
  if [ "${lines:-0}" -gt "$TIMING_KEEP" ] 2>/dev/null; then
    ttmp=$(mktemp "$TIMING.XXXXXX" 2>/dev/null) \
      && tail -n "$TIMING_KEEP" "$TIMING" > "$ttmp" 2>/dev/null \
      && mv "$ttmp" "$TIMING" 2>/dev/null \
      || rm -f "$ttmp" 2>/dev/null
  fi
fi
# The regen's status must not be decided by its instrumentation.
exit 0
