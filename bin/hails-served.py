#!/usr/bin/env python3
# Totals the hits in the bandwidth rollup and writes "Over N requests served" into served.js for any
# public page to load. Run from hails-stats.sh once per regen, after the rollup has been merged.
# With HAILS_SERVED_ROOT unset it writes nothing at all.
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


# Floors rather than rounds to nearest: the sentence says "Over N" and must not overstate.
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

text = "Over %s requests served" % clean(total)
body = ('(function(){var w=document.getElementById("served");if(!w)return;'
        'var e=document.getElementById("servedText")||w;'
        'e.textContent=%s;w.className="on";})();\n' % json.dumps(text))

try:
    with open(OUT, "r", encoding="utf-8") as fh:
        if fh.read() == body:
            sys.exit(0)
except Exception:
    pass

try:
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)
except Exception:
    sys.exit(0)

print(text)
