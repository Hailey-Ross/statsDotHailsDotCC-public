#!/usr/bin/env python3
# Merge rule is max(stored, fresh) per host per day, never sum: every run recounts the same days.
import sys, json, os, time, fcntl

STORE = os.environ.get("HAILS_ROLLUP", "/var/lib/hails-stats/bandwidth.json")

TALLY = None
if "--tally" in sys.argv:
    i = sys.argv.index("--tally")
    if i + 1 >= len(sys.argv) or sys.argv[i + 1].startswith("-"):
        sys.stderr.write("hails-rollup: --tally needs a file path\n")
        sys.exit(2)
    TALLY = sys.argv[i + 1]

FROM_DB = "--from-db" in sys.argv
if FROM_DB and TALLY:
    sys.stderr.write("hails-rollup: --from-db and --tally are alternatives, pass one\n")
    sys.exit(2)

DRY = "--dry-run" in sys.argv

# --rewrite-from replaces stored days instead of taking max(). For repairs, never on a schedule.
REWRITE_FROM = None
if "--rewrite-from" in sys.argv:
    i = sys.argv.index("--rewrite-from")
    if i + 1 >= len(sys.argv) or sys.argv[i + 1].startswith("-"):
        sys.stderr.write("hails-rollup: --rewrite-from needs a YYYY-MM-DD date\n")
        sys.exit(2)
    REWRITE_FROM = sys.argv[i + 1]
    try:
        time.strptime(REWRITE_FROM, "%Y-%m-%d")
    except ValueError:
        sys.stderr.write("hails-rollup: --rewrite-from %r is not a YYYY-MM-DD date\n" % REWRITE_FROM)
        sys.exit(2)
    if not FROM_DB:
        sys.stderr.write("hails-rollup: --rewrite-from needs --from-db. The tally and the stdin path "
                         "see only the retained logs, so anything older would be "
                         "rewritten to nothing\n")
        sys.exit(2)

FORGET = set()
for i, a in enumerate(sys.argv):
    if a == "--forget-host" and i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-"):
        FORGET.add(sys.argv[i + 1])
if FORGET and not REWRITE_FROM:
    sys.stderr.write("hails-rollup: --forget-host only runs alongside --rewrite-from, so a routine "
                     "merge can never drop a host\n")
    sys.exit(2)

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
elif FROM_DB:
    def seeded():
        try:
            with open(STORE, "r", encoding="utf-8") as fh:
                return bool((json.load(fh) or {}).get("hosts"))
        except Exception:
            return False

    def read_warehouse():
        import hails_query as hq   # noqa: E402
        import hails_db as hdb     # noqa: E402
        if not os.path.exists(hdb.DB_PATH):
            raise IOError("no warehouse at %s" % hdb.DB_PATH)
        con = hq.connect()
        dropped = hq.drop_hosts()
        out = {}
        for day, host, by, hits in con.execute(
                "SELECT r.day, h.host, r.bytes, r.hits "
                "FROM roll_day r JOIN dim_host h ON h.id = r.host_id"):
            if not host:
                continue
            if dropped and any(host.startswith(p) for p in dropped):
                continue
            try:
                by, hits = int(by or 0), int(hits or 0)
            except (TypeError, ValueError):
                continue
            if hits <= 0:
                continue
            out.setdefault(host, {})[hdb.day_str(day)] = [by, hits]
        return out

    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, HERE)
    try:
        fresh = read_warehouse()
    except Exception as e:
        if seeded():
            sys.stderr.write("hails-rollup: cannot read the warehouse: %s\n" % e)
            sys.exit(1)
        sys.stderr.write("hails-rollup: no readable warehouse yet (%s) and no stored history, "
                         "nothing to merge\n" % e)
        sys.exit(0)

    if not fresh and not seeded():
        sys.stderr.write("hails-rollup: warehouse holds no rollup rows yet and there is no stored "
                         "history, nothing to merge\n")
        sys.exit(0)
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
        # Must bucket exactly as the preprocessor does, or the merge freezes the day boundary.
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        d = fresh.setdefault(host, {})
        rec = d.get(day)
        if rec is None:
            d[day] = [by, 1]
        else:
            rec[0] += by
            rec[1] += 1

if not fresh:
    why = ("the warehouse returned no rollup rows" if FROM_DB else
           "tally missing or stdin empty?")
    sys.stderr.write("hails-rollup: fresh pass counted nothing, refusing to merge and leaving %s "
                     "untouched (%s)\n" % (STORE, why))
    sys.exit(1)

LOCK = STORE + ".lock"
LOCK_TIMEOUT_S = 10.0
os.makedirs(os.path.dirname(LOCK) or ".", exist_ok=True)
lockfh = open(LOCK, "w")
deadline = time.time() + LOCK_TIMEOUT_S
while True:
    try:
        fcntl.flock(lockfh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if time.time() >= deadline:
            sys.stderr.write("hails-rollup: could not take the write lock on %s within %.0fs, "
                             "leaving %s untouched\n" % (LOCK, LOCK_TIMEOUT_S, STORE))
            sys.exit(1)
        time.sleep(0.1)

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
    if host in FORGET:
        continue
    o_days = old.get(host) or {}
    f_days = fresh.get(host) or {}
    out = {}
    for day in set(o_days) | set(f_days):
        ob = o_days.get(day) or [0, 0]
        fb = f_days.get(day)
        try:
            ob = [int(ob[0]), int(ob[1])]
        except Exception:
            ob = [0, 0]
        if REWRITE_FROM is not None and day >= REWRITE_FROM:
            if fb is None:
                continue
            out[day] = [int(fb[0]), int(fb[1])]
        else:
            fb = fb or [0, 0]
            out[day] = [max(ob[0], fb[0]), max(ob[1], fb[1])]
    if out:
        merged[host] = out

if REWRITE_FROM is not None:
    def total(hosts):
        return (sum(v[0] for d in hosts.values() for v in d.values()),
                sum(v[1] for d in hosts.values() for v in d.values()))

    ob, oh = total(old)
    nb, nh = total(merged)
    sys.stderr.write("hails-rollup: rewriting from %s%s\n"
                     % (REWRITE_FROM, (", forgetting " + ", ".join(sorted(FORGET))) if FORGET else ""))
    for host in sorted(set(old) | set(merged)):
        a = old.get(host) or {}
        b = merged.get(host) or {}
        dh = sum(v[1] for v in b.values()) - sum(v[1] for v in a.values())
        if dh:
            sys.stderr.write("hails-rollup:   %-22s hits %+d\n" % (host, dh))
    sys.stderr.write("hails-rollup: total hits %d -> %d (%+d), bytes %.1f GB -> %.1f GB\n"
                     % (oh, nh, nh - oh, ob / 1e9, nb / 1e9))
    if DRY:
        sys.stderr.write("hails-rollup: dry run, %s left untouched\n" % STORE)
        fcntl.flock(lockfh, fcntl.LOCK_UN)
        lockfh.close()
        sys.exit(0)
    bak = "%s.pre-rewrite.%s" % (STORE, time.strftime("%Y%m%d-%H%M%S"))
    try:
        with open(STORE, "rb") as a, open(bak, "wb") as b:
            b.write(a.read())
        sys.stderr.write("hails-rollup: backed up to %s\n" % bak)
    except Exception as e:
        sys.stderr.write("hails-rollup: could not back up %s: %s, refusing to rewrite\n" % (STORE, e))
        fcntl.flock(lockfh, fcntl.LOCK_UN)
        lockfh.close()
        sys.exit(1)
elif DRY:
    sys.stderr.write("hails-rollup: --dry-run only applies to --rewrite-from\n")
    fcntl.flock(lockfh, fcntl.LOCK_UN)
    lockfh.close()
    sys.exit(2)

days_seen = [d for h in merged.values() for d in h]
doc = {
    "since": min(days_seen) if days_seen else "",
    "updated": int(time.time()),
    "hosts": merged,
}

os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
tmp = "%s.tmp.%d" % (STORE, os.getpid())
try:
    import glob
    for stale in glob.glob(STORE + ".tmp.*"):
        try:
            os.unlink(stale)
        except OSError:
            pass
except Exception:
    pass
try:
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, STORE)
finally:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    fcntl.flock(lockfh, fcntl.LOCK_UN)
    lockfh.close()
