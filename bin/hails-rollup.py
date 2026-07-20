#!/usr/bin/env python3
# Durable per day, per host bandwidth history, merged into /var/lib/hails-stats/bandwidth.json.
# Counts come from --tally FILE, or from the preprocessed JSON log on stdin when it is not given.
#
# Merge rule is max(stored, fresh) per host per day, never sum: the whole log set is reparsed every
# run, so summing would multiply every historical day by the number of runs.
import sys, json, os, time

STORE = os.environ.get("HAILS_ROLLUP", "/var/lib/hails-stats/bandwidth.json")

TALLY = None
if "--tally" in sys.argv:
    i = sys.argv.index("--tally")
    if i + 1 >= len(sys.argv) or sys.argv[i + 1].startswith("-"):
        sys.stderr.write("hails-rollup: --tally needs a file path\n")
        sys.exit(2)
    TALLY = sys.argv[i + 1]

# fresh[host][day] = [bytes, hits]
fresh = {}
if TALLY:
    try:
        with open(TALLY, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, dict):
            raise ValueError("tally is not an object")
    except Exception as e:
        sys.stderr.write("hails-rollup: unusable tally %s: %s\n" % (TALLY, e))
        sys.exit(1)
    for host, days in loaded.items():
        if not host or not isinstance(days, dict):
            continue
        out = {}
        for day, rec in days.items():
            try:
                out[day] = [int(rec[0]), int(rec[1])]
            except Exception:
                continue
        if out:
            fresh[host] = out
else:
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
        if ts <= 0:
            continue
        host = (o.get("request") or {}).get("host") or ""
        if not host:
            continue
        try:
            by = int(o.get("size") or 0)
        except Exception:
            by = 0
        # This bucketing must stay identical to the preprocessor's, or the two paths disagree about
        # which day a request belongs to and the merge freezes the boundary permanently.
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        d = fresh.setdefault(host, {})
        rec = d.get(day)
        if rec is None:
            d[day] = [by, 1]
        else:
            rec[0] += by
            rec[1] += 1

# Merging an empty pass would rewrite every historical day unchanged and exit 0, which is
# indistinguishable from a healthy run while today stops growing.
if not fresh:
    sys.stderr.write("hails-rollup: fresh pass counted nothing, refusing to merge and leaving %s "
                     "untouched (tally missing or stdin empty?)\n" % STORE)
    sys.exit(1)

old = {}
try:
    with open(STORE, "r", encoding="utf-8") as fh:
        loaded = json.load(fh)
    if isinstance(loaded, dict) and isinstance(loaded.get("hosts"), dict):
        old = loaded["hosts"]
except Exception:
    old = {}

merged = {}
for host in set(old) | set(fresh):
    o_days = old.get(host) or {}
    f_days = fresh.get(host) or {}
    out = {}
    for day in set(o_days) | set(f_days):
        ob = o_days.get(day) or [0, 0]
        fb = f_days.get(day) or [0, 0]
        try:
            ob = [int(ob[0]), int(ob[1])]
        except Exception:
            ob = [0, 0]
        out[day] = [max(ob[0], fb[0]), max(ob[1], fb[1])]
    if out:
        merged[host] = out

days_seen = [d for h in merged.values() for d in h]
doc = {
    "since": min(days_seen) if days_seen else "",
    "updated": int(time.time()),
    "hosts": merged,
}

os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
tmp = STORE + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
os.replace(tmp, STORE)
