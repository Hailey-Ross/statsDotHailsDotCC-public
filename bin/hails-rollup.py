#!/usr/bin/env python3
# Durable per day, per host bandwidth history for the stats pipeline.
# Reads the preprocessed JSON access log on stdin (output of hails-stats-pre.py) and merges it into
# /var/lib/hails-stats/bandwidth.json, the pipeline's only long lived state.
# Run once per regen over the full log, before any per scope work: the scope generators would race
# on this file.
# Merge rule is max(stored, fresh) per host per day, never sum: the whole log set is reparsed every
# run, so summing would multiply every historical day by the number of runs.
import sys, json, os, time

STORE = os.environ.get("HAILS_ROLLUP", "/var/lib/hails-stats/bandwidth.json")

# Read the fresh pass: fresh[host][day] = [bytes, hits]
fresh = {}
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
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    d = fresh.setdefault(host, {})
    rec = d.get(day)
    if rec is None:
        d[day] = [by, 1]
    else:
        rec[0] += by
        rec[1] += 1

# Load the existing store.
old = {}
try:
    with open(STORE, "r", encoding="utf-8") as fh:
        loaded = json.load(fh)
    if isinstance(loaded, dict) and isinstance(loaded.get("hosts"), dict):
        old = loaded["hosts"]
except Exception:
    old = {}   # missing or corrupt store: start over rather than lose this run

# Merge old and fresh, taking the larger value for each host and day.
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

# Write atomically via tmp plus os.replace so a reader never sees a half file.
os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
tmp = STORE + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
os.replace(tmp, STORE)
