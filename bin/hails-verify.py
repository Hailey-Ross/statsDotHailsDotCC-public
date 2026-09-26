#!/usr/bin/env python3
import sys
import os
import json
import gzip
import time
import glob
import importlib.util
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hails_db as db  # noqa: E402

LOG_DIR = os.environ.get("HAILS_LOG_DIR", "/var/log/caddy")
TOPN = 20

FAIL = []
WARN = []
OK = []


def fail(msg):
    FAIL.append(msg)
    print("FAIL  %s" % msg)


def warn(msg):
    WARN.append(msg)
    print("note  %s" % msg)


def ok(msg):
    OK.append(msg)
    print("ok    %s" % msg)


def load_pre():
    for cand in (os.path.join(HERE, "hails-stats-pre.py"), "/usr/local/bin/hails-stats-pre.py"):
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location("hails_stats_pre", cand)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise SystemExit("hails-stats-pre.py not found")


PRE = load_pre()
DROP_HOSTS = PRE.drop_hosts()
PROBE_PATHS = PRE.probe_paths()


def discover():
    found = {}
    for p in glob.glob(os.path.join(LOG_DIR, "access*")):
        if not os.path.isfile(p):
            continue
        n = os.path.basename(p)
        # Must match the ingest's rule in candidates().
        if not (n == "access.log"
                or (n.startswith("access-") and (n.endswith(".log") or n.endswith(".log.gz")))):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        found[(st.st_dev, st.st_ino)] = p
    return found


def file_fp(p):
    try:
        with (gzip.open if p.endswith(".gz") else open)(p, "rb") as fh:
            return db.fingerprint(fh.read(db.FP_BYTES))
    except Exception:
        return None


def check_files(con, found):
    """Inode numbers are reused, so only a row with a matching fingerprint answers for a file."""
    rows = collections.defaultdict(list)
    for dev, ino, gen, path, fp, off, size, done in con.execute(
            "SELECT dev, inode, gen, path, fp, offset, size, done FROM source ORDER BY gen"):
        rows[(dev, ino)].append((gen, path, fp, off, size, done))
    known, missed, wrong = {}, [], []
    for k, p in found.items():
        cands = rows.get(k)
        if not cands:
            missed.append(p)
            continue
        fp = file_fp(p)
        mine = [c for c in cands if fp is None or c[2] is None or c[2] == fp]
        if not mine:
            wrong.append(p)
            continue
        _gen, path, _fp, off, size, done = mine[-1]
        known[k] = (path, off, size, done)
    if missed:
        fail("%d log file(s) on disk that the ingester never opened: %s"
             % (len(missed), ", ".join(os.path.basename(m) for m in sorted(missed))))
    else:
        ok("every access log on disk (%d) has a cursor" % len(found))
    if wrong:
        fail("%d log file(s) whose inode is held only by cursors for OTHER content (a reused inode "
             "the ingest never adopted): %s"
             % (len(wrong), ", ".join(os.path.basename(m) for m in sorted(wrong))))
    here = set(found.values())
    gone = sorted({c[1] for k, cs in rows.items() if k not in found for c in cs
                   if c[1] and c[1] not in here})
    if gone:
        warn("%d ingested file(s) no longer on disk (rotated away). The warehouse now holds history "
             "the log cannot: %s" % (len(gone), ", ".join(os.path.basename(g) for g in sorted(gone))))
    for k, p in found.items():
        path, off, size, done = known.get(k, (None, None, None, None))
        if path is None:
            continue
        try:
            cur = os.path.getsize(p)
        except OSError:
            continue
        if p.endswith(".gz"):
            continue
        if off < cur:
            behind = cur - off
            if behind > 4 << 20:
                warn("cursor for %s is %d bytes behind end of file" % (os.path.basename(p), behind))
    return known


READ_CHUNK = 4 << 20


def lines_upto(fh, limit):
    left = limit
    pending = b""
    while left > 0:
        chunk = fh.read(min(READ_CHUNK, left))
        if not chunk:
            break
        left -= len(chunk)
        parts = (pending + chunk).split(b"\n")
        pending = parts.pop()
        for p in parts:
            yield p
    if pending:
        yield pending


def read_logs(found, limits):
    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return float("inf")

    paths = sorted(found.items(), key=lambda kv: (mtime(kv[1]), kv[1]))
    for key, p in paths:
        limit = limits.get(key)
        if limit is None:
            continue                      # no cursor: reported as a hard failure by check_files
        opener = gzip.open if p.endswith(".gz") else open
        try:
            with opener(p, "rb") as fh:
                for raw in lines_upto(fh, limit):
                    if not raw.strip():
                        continue
                    try:
                        o = json.loads(raw)
                    except Exception:
                        continue
                    if PRE.is_probe(o):
                        continue
                    req, host = PRE.normalize(o)
                    if not isinstance(host, str) or not host:
                        continue
                    if PRE.is_dropped(host, req, DROP_HOSTS):
                        continue
                    PRE.mask_probe(req, PROBE_PATHS)
                    ts = o.get("ts")
                    if not isinstance(ts, int) or ts <= 0:
                        continue
                    yield o, req, host, ts
        except Exception as e:
            fail("could not read %s: %s" % (p, e))


def scan(found, limits):
    day = collections.defaultdict(lambda: [0, 0, 0, 0, set()])   # (day,host) -> h,v,f,bytes,ips
    hour = collections.defaultdict(lambda: [0, 0])               # (hour,host) -> hits,bytes
    uri = collections.defaultdict(collections.Counter)           # (day,host) -> Counter(uri)
    span = [None, None]
    for o, req, host, ts in read_logs(found, limits):
        span[0] = ts if span[0] is None else min(span[0], ts)
        span[1] = ts if span[1] is None else max(span[1], ts)
        try:
            st = int(o.get("status") or 0)
        except Exception:
            st = 0
        try:
            sz = int(o.get("size") or 0)
        except Exception:
            sz = 0
        d = db.day_of(ts)
        e = day[(d, host)]
        e[0] += 1
        e[1] += 1 if st < 400 else 0
        e[2] += 1 if st >= 400 else 0
        e[3] += sz
        e[4].add(req.get("client_ip") or "")
        h = hour[(db.hour_of(ts), host)]
        h[0] += 1
        h[1] += sz
        u = req.get("uri") or ""
        uri[(d, host)][u[:512]] += 1
    return day, hour, uri, tuple(span)


def loss_evidence(con, found):
    reasons = []
    live = {(d, i) for (d, i) in found}
    for dev, ino, path in con.execute("SELECT dev, inode, path FROM source"):
        if (dev, ino) not in live:
            reasons.append("ingested file no longer on disk: %s" % os.path.basename(path or "?"))
    if con.execute("SELECT COUNT(*) FROM source WHERE gen > 0").fetchone()[0]:
        reasons.append("a log was truncated in place (gen > 0)")
    return reasons


def covered(span):
    """Only the log's first and last day may be short of the warehouse."""
    lo, hi = span
    if lo is None or hi is None:
        return set(), "nothing"
    lo_day, hi_day = db.day_of(lo), db.day_of(hi)
    days = set()
    d = lo_day
    while d < hi_day:
        d = db.day_of(db.day_bounds(d)[1])
        if d < hi_day:
            days.add(d)
    if not days:
        return set(), "no whole day (the log spans %s to %s)" % (db.day_str(lo_day),
                                                                 db.day_str(hi_day))
    return days, "%d whole day(s), %s to %s (%s and %s are partial at the edges)" % (
        len(days), db.day_str(min(days)), db.day_str(max(days)),
        db.day_str(lo_day), db.day_str(hi_day))


def split_excuse(pairs, full_days, dayof):
    fine, bad = [], []
    for item in pairs:
        (bad if dayof(item) in full_days else fine).append(item)
    return fine, bad


def compare_days(con, logday, explained, full_days):
    dbday = {}
    for d, host, hits, valid, failed, byt, uv in con.execute(
            "SELECT r.day, h.host, r.hits, r.valid, r.failed, r.bytes, r.uv_nonadditive "
            "FROM roll_day r JOIN dim_host h ON h.id = r.host_id WHERE r.source='raw'"):
        dbday[(d, host)] = (hits, valid, failed, byt, uv)

    behind, ahead, equal, extra = [], [], 0, []
    for k, e in sorted(logday.items()):
        want = (e[0], e[1], e[2], e[3], len(e[4]))
        got = dbday.get(k)
        if got is None:
            behind.append((k, want, None))
            continue
        if got == want:
            equal += 1
        elif all(g >= w for g, w in zip(got, want)):
            ahead.append((k, want, got))
        else:
            behind.append((k, want, got))
    for k in sorted(dbday):
        if k not in logday:
            extra.append(k)

    if behind:
        fail("%d day/host pair(s) where the warehouse has LESS than the log. Rows were missed:"
             % len(behind))
        for k, want, got in behind[:10]:
            print("        %s %-22s log=%s db=%s" % (db.day_str(k[0]), k[1], want, got))
    else:
        ok("no day/host pair is behind the log")
    if equal:
        ok("%d day/host pair(s) match exactly (hits, valid, failed, bytes, uniques)" % equal)
    edge, inside = split_excuse(ahead, full_days, lambda t: t[0][0])
    if inside:
        fail("%d day/host pair(s) where the warehouse has MORE than the log on a day the log holds "
             "IN FULL. Nothing explains this. Suspect DOUBLE COUNTING." % len(inside))
        for k, want, got in inside[:10]:
            print("        %s %-22s log=%s db=%s" % (db.day_str(k[0]), k[1], want, got))
    if edge:
        warn("%d day/host pair(s) ahead of the log only on its partial edge days, which is the "
             "warehouse being deeper than the log. Explained by: %s"
             % (len(edge), "; ".join(explained) if explained else "log retention"))
        for k, want, got in edge[:4]:
            print("        %s %-22s log=%s db=%s" % (db.day_str(k[0]), k[1], want, got))
    if not ahead:
        ok("no day/host pair is ahead of the log")

    edge_x, inside_x = split_excuse(extra, full_days, lambda k: k[0])
    if inside_x:
        fail("%d day/host pair(s) in the warehouse on a day the log holds IN FULL, with nothing in "
             "the log at all: %s" % (len(inside_x),
                                     ", ".join("%s/%s" % (db.day_str(d), h) for d, h in inside_x[:6])))
    if edge_x:
        warn("%d day/host pair(s) in the warehouse with nothing in the log at all, all outside the "
             "log's full coverage: %s"
             % (len(edge_x), ", ".join("%s/%s" % (db.day_str(d), h) for d, h in edge_x[:6])))


def compare_hours(con, loghour, explained, full_days):
    dbhour = {}
    for hr, host, hits, byt in con.execute(
            "SELECT r.hour, h.host, r.hits, r.bytes FROM roll_hour r JOIN dim_host h ON h.id = r.host_id"):
        dbhour[(hr, host)] = (hits, byt)
    low = high_edge = high_inside = 0
    shown = 0
    for k, v in loghour.items():
        got = dbhour.get(k)
        if got is None:
            continue
        if got[0] == v[0]:
            continue
        inside = (k[0] // 100) in full_days
        if got[0] < v[0]:
            low += 1
        elif inside:
            high_inside += 1
        else:
            high_edge += 1
        if shown < 5 and (low or high_inside):
            shown += 1
            print("        hour %d %-20s log=%s db=%s" % (k[0], k[1], tuple(v), got))
    if low:
        fail("%d hour bucket(s) behind the log: SQL localtime and Python localtime disagree" % low)
    if high_inside:
        fail("%d hour bucket(s) AHEAD of the log inside its full coverage: suspect double counting"
             % high_inside)
    if high_edge:
        warn("%d hour bucket(s) ahead of the log on its partial edge days (%s)"
             % (high_edge, "; ".join(explained) if explained else "log retention"))
    if not low and not high_inside and not high_edge:
        ok("hour bucketing agrees between SQL and Python (%d buckets)" % len(dbhour))


def compare_uris(con, loguri, full_days):
    # Only over the days the log holds in full, or a deeper warehouse always looks ahead.
    if not full_days:
        warn("the log holds no whole day, so there is nothing to check the top URIs against")
        return
    byhost = collections.defaultdict(collections.Counter)
    for (d, host), c in loguri.items():
        if d in full_days:
            byhost[host].update(c)
    if not byhost:
        warn("no log lines fall on a day the log holds in full")
        return

    days = sorted(full_days)
    q = ",".join("?" * len(days))
    bad = over = 0
    for host, counter in sorted(byhost.items()):
        for u, n in counter.most_common(TOPN):
            if u.endswith(PRE.PROBE_SENTINEL):
                continue
            row = con.execute(
                "SELECT COALESCE(SUM(d.hits), 0) FROM roll_day_dim d JOIN dim_uri x ON x.id = d.val_id "
                "JOIN dim_host h ON h.id = d.host_id WHERE d.dim = ? AND h.host = ? AND x.uri = ? "
                "AND d.day IN (%s)" % q, [db.D_URI, host, u] + days).fetchone()
            g = row[0] if row else 0
            if g != n:
                bad += 1
                over = over + 1 if g > n else over
                if bad <= 5:
                    print("        %-20s %-45s log=%d db=%s" % (host, u[:45], n, g))
    if bad:
        fail("%d top URI row(s) disagree with the log over the %d day(s) it holds in full "
             "(%d of them ahead)" % (bad, len(days), over))
    else:
        ok("top %d URIs per host agree over the log's %d full day(s), %d host(s)"
           % (TOPN, len(days), len(byhost)))


def check_internal(con):
    edays = [d for (d,) in con.execute("SELECT DISTINCT day FROM event")]
    impure = {d for (d,) in con.execute("SELECT DISTINCT day FROM roll_day WHERE source <> 'raw'")}
    days = [d for d in edays if d not in impure]
    skipped = len(edays) - len(days)
    if not days:
        warn("no pure raw day to reconcile")
    else:
        q = ",".join("?" * len(days))
        tot = con.execute("SELECT COUNT(*) FROM event WHERE day IN (%s)" % q, days).fetchone()[0]
        rd = con.execute("SELECT COALESCE(SUM(hits),0) FROM roll_day WHERE source='raw' "
                         "AND day IN (%s)" % q, days).fetchone()[0]
        rh = con.execute("SELECT COALESCE(SUM(hits),0) FROM roll_hour WHERE hour/100 IN (%s)" % q,
                         days).fetchone()[0]
        if tot == rd == rh:
            ok("event count reconciles with roll_day and roll_hour over %d raw day(s) (%d)%s"
               % (len(days), tot, ", %d day(s) skipped as pruned or legacy backed" % skipped
                  if skipped else ""))
        else:
            fail("reconciliation over %d raw day(s): events=%d roll_day=%d roll_hour=%d"
                 % (len(days), tot, rd, rh))
        rd = tot   # dimension totals below compare against the same scope

    ndim = bad_dim = 0
    if days:
        q = ",".join("?" * len(days))
        for (dim,) in con.execute("SELECT DISTINCT dim FROM roll_day_dim WHERE day IN (%s)" % q,
                                  days).fetchall():
            ndim += 1
            s = con.execute("SELECT COALESCE(SUM(hits),0) FROM roll_day_dim WHERE dim=? "
                            "AND day IN (%s)" % q, [dim] + days).fetchone()[0]
            if s != rd:
                bad_dim += 1
                fail("dim %d sums to %d over the raw days, roll_day sums to %d: the tail fold lost "
                     "rows" % (dim, s, rd))
        if not bad_dim:
            ok("all %d dimensions sum back to roll_day (the tail fold is not lossy)" % ndim)

    stray = con.execute("SELECT COUNT(*) FROM roll_day_dim WHERE val_id=0 AND dim<>?",
                        (db.D_STATUS,)).fetchone()[0]
    if stray:
        fail("%d roll_day_dim row(s) use val_id 0 outside D_STATUS: stale VAL_UNKNOWN encoding" % stray)
    else:
        ok("val_id 0 appears only where it means HTTP status 0")

    bad = 0
    for d, hid, blob, uv in con.execute(
            "SELECT day, host_id, visitors, uv_nonadditive FROM roll_day WHERE source='raw' "
            "AND day IN (SELECT DISTINCT day FROM event)"):
        lo, hi = db.day_bounds(d)
        exact = {r[0] for r in con.execute(
            "SELECT DISTINCT ip_id FROM event WHERE ts>=? AND ts<? AND host_id=?", (lo, hi, hid))}
        got = set(db.unpack_visitors(blob))
        if got != exact or len(exact) != uv:
            bad += 1
    if bad:
        fail("%d visitor blob(s) do not match the raw distinct IPs" % bad)
    else:
        ok("every visitor blob round trips to the exact distinct IP set")

    dupfp = con.execute(
        "SELECT COUNT(*) FROM (SELECT fp FROM source WHERE fp IS NOT NULL "
        "GROUP BY fp HAVING COUNT(*) > 1)").fetchone()[0]
    if dupfp:
        rows = con.execute(
            "SELECT s.id, s.path FROM source s WHERE s.fp IN (SELECT fp FROM source "
            "WHERE fp IS NOT NULL GROUP BY fp HAVING COUNT(*) > 1) ORDER BY s.fp, s.id "
            "LIMIT 6").fetchall()
        fail("%d fingerprint(s) held by more than one source row: the same log has been read under "
             "two cursors and its rows are in here twice. Run hails-dedupe.py." % dupfp)
        for sid, path in rows:
            print("        src %-4d %s" % (sid, os.path.basename(path or "?")))
    else:
        ok("every fingerprint is held by exactly one source row (no log read twice)")

    dup = con.execute("SELECT COUNT(*) FROM (SELECT src_id, src_off FROM event "
                      "GROUP BY src_id, src_off HAVING COUNT(*) > 1)").fetchone()[0]
    if dup:
        fail("%d duplicated (src_id, src_off): the identity index is not doing its job" % dup)
    else:
        ok("the (src_id, src_off) identity index holds")

    tz = db.get_meta(con, "tz_name")
    if tz != db.tz_name():
        fail("timezone changed since creation (%r -> %r): every stored day bucket is now suspect"
             % (tz, db.tz_name()))
    else:
        ok("timezone unchanged since the warehouse was created (%s)" % tz)

    odd = []
    for (d,) in con.execute("SELECT DISTINCT day FROM roll_day ORDER BY day"):
        lo, hi = db.day_bounds(d)
        if (hi - lo) not in (82800, 86400, 90000):
            odd.append((d, hi - lo))
        if db.day_of(lo) != d or db.day_of(hi - 1) != d:
            fail("day %d bounds do not map back to the same day" % d)
    if odd:
        warn("day(s) with a non 24 hour span (expected across a DST change): %s" % odd)
    ok("day boundaries map back to their own day")


def main():
    t0 = time.time()
    con = db.connect(readonly=True)
    v = con.execute("PRAGMA user_version").fetchone()[0]
    if v != db.SCHEMA_VERSION:
        print("FAIL  warehouse is schema v%d, this verifier speaks v%d" % (v, db.SCHEMA_VERSION))
        return 1
    print("hails-verify: %s, schema v%d" % (db.DB_PATH, v))

    found = discover()
    known = check_files(con, found)
    limits = {k: v[1] for k, v in known.items()}
    print("      scanning %d log file(s), bounded by the ingest cursor..." % len(found))
    logday, loghour, loguri, span = scan(found, limits)

    full_days, describe = covered(span)
    print("      the log holds %s: discrepancies there are failures, not notes" % describe)

    explained = loss_evidence(con, found)
    compare_days(con, logday, explained, full_days)
    compare_hours(con, loghour, explained, full_days)
    compare_uris(con, loguri, full_days)
    check_internal(con)

    print("\n%d ok, %d note(s), %d failure(s), %.1fs" % (len(OK), len(WARN), len(FAIL), time.time() - t0))
    if FAIL:
        print("VERDICT: the warehouse does NOT agree with the log. Do not point any page at it.")
        return 1
    print("VERDICT: agreement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
