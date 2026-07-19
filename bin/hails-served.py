#!/usr/bin/env python3
# Public requests served counter for the stats pipeline.
# Totals the hits field across every host and every day in the bandwidth rollup and writes the
# sentence "Over N requests served" into served.js, which any public page can load to display it.
# Reads no stdin and never touches the raw access log, like hails-bandwidth.py.
# Driven by hails-served.timer every 6 hours from 00:00 UTC, not by hails-stats.sh: the figure is a
# headline, so it steps at a readable pace rather than every 5 minutes. The rollup is refreshed every
# 5 minutes regardless, so the number is current whenever this runs.
# Rounding floors to two significant figures, never to nearest: the sentence says "Over N", so it
# must not claim more traffic than actually happened. 110,800 renders as 110k.
# Writes only when the rendered text changes, so browser and proxy caches stay valid on the days the
# rounded figure does not move.
# Opt in: with HAILS_SERVED_ROOT unset, nothing is written at all.
import os, json, sys

ROOT = os.environ.get("HAILS_SERVED_ROOT", "")
if not ROOT:
    sys.exit(0)

STORE = os.environ.get("HAILS_ROLLUP", "/var/lib/hails-stats/bandwidth.json")
OUT = os.path.join(ROOT, "served.js")

# Optional head start added to the live count, for traffic served before the rollup existed.
try:
    BASELINE = int(os.environ.get("HAILS_SERVED_BASELINE", "0"))
except ValueError:
    BASELINE = 0


def clean(n):
    """Floor to two significant figures, then suffix. 37779 gives 37k, 4832 gives 4.8k."""
    if n < 1000:
        return str(n)
    step = 10 ** (len(str(n)) - 2)
    n = (n // step) * step
    for lim, suf in ((10 ** 9, "B"), (10 ** 6, "M"), (10 ** 3, "k")):
        if n >= lim:
            v = n / lim
            if v < 10:
                txt = "%.1f" % v
                if txt.endswith(".0"):
                    txt = txt[:-2]
            else:
                txt = str(int(v))
            return txt + suf
    return str(n)


# Total the rollup. Any failure exits quietly having written nothing, which leaves the last good
# line on the page rather than blanking it.
try:
    with open(STORE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(0)

total = BASELINE
try:
    for days in (data.get("hosts") or {}).values():
        for rec in days.values():
            total += int(rec[1])
except Exception:
    sys.exit(0)

if total <= 0:
    sys.exit(0)

# The page owns the styling: this only fills in the text and adds the class the page fades in with.
# It writes into #servedText when that exists and falls back to #served. json.dumps handles the
# quoting, so the sentence can never break out of the string literal.
text = "Over %s requests served" % clean(total)
body = ('(function(){var w=document.getElementById("served");if(!w)return;'
        'var e=document.getElementById("servedText")||w;'
        'e.textContent=%s;w.className="on";})();\n' % json.dumps(text))

try:
    with open(OUT, "r", encoding="utf-8") as fh:
        if fh.read() == body:
            sys.exit(0)          # unchanged, leave mtime alone
except Exception:
    pass                          # missing or unreadable, fall through and write it

# Write to a temp file in the same directory then rename, so a reader never sees a partial file.
try:
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)
except Exception:
    sys.exit(0)

print(text)
