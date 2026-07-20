#!/usr/bin/env python3
"""Verify the events warehouse against a fresh pass over the raw logs. Run daily; exit 1 means the
warehouse is behind the log or an internal invariant failed.

File discovery is deliberately not imported from the ingest, because file discovery is the thing
under test. Normalization is imported, because it is definitional here rather than a claim.
"""
import sys
import os
import re
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
        if not re.search(r"access[-.].*log|access\.log", os.path.basename(p)):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        found[(st.st_dev, st.st_ino)] = p
    return found


def check_files(con, found):
    known = {}
    # Ordered by gen so the newest generation of a truncated file wins.
    for dev, ino, path, off, size, done in con.execute(
            "SELECT dev, inode, path, offset, size, done FROM source ORDER BY gen"):
        known[(dev, ino)] = (path, off, size, done)
    missed = [p for k, p in found.items() if k not in known]
    if missed:
        fail("%d log file(s) on disk that the ingester never opened: %s"
             % (len(missed), ", ".join(os.path.basename(m) for m in sorted(missed))))
    else:
        ok("every access log on disk (%d) has a cursor" % len(found))
    gone = [v[0] for k, v in known.items() if k not in found]
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
    """Every line of every discovered log, oldest file first, normalized through the shared code.

    Bounded by the ingest's committed cursor for each file, since traffic keeps arriving while this
    runs and an unbounded read would report the difference as missing rows.
    """
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
                    # The baseline must apply the same drop and mask rules the ingest applied, or
                    # this check reports dropped and masked rows as data the warehouse lost. It is a
                    # recomputation of what should have been stored, not of the raw file.
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
    uri = collections.defaultdict(collections.Counter)           # host -> Counter(uri)
    for o, req, host, ts in read_logs(found, limits):
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
        uri[host][u[:512]] += 1
    return day, hour, uri


def loss_evidence(con, found):
    """Reasons the warehouse may legitimately hold more than the log. Absent any of them, holding
    more is double counting and must fail."""
    reasons = []
    live = {(d, i) for (d, i) in found}
    for dev, ino, path in con.execute("SELECT dev, inode, path FROM source"):
        if (dev, ino) not in live:
            reasons.append("ingested file no longer on disk: %s" % os.path.basename(path or "?"))
    if con.execute("SELECT COUNT(*) FROM source WHERE gen > 0").fetchone()[0]:
        reasons.append("a log was truncated in place (gen > 0)")
    return reasons


def compare_days(con, logday, explained):
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
    if ahead:
        report = warn if explained else fail
        report("%d day/host pair(s) where the warehouse has MORE than the log.%s"
               % (len(ahead),
                  (" Explained by: " + "; ".join(explained)) if explained
                  else " Nothing explains this. Suspect DOUBLE COUNTING."))
        for k, want, got in ahead[:10]:
            print("        %s %-22s log=%s db=%s" % (db.day_str(k[0]), k[1], want, got))
    if extra:
        warn("%d day/host pair(s) in the warehouse with nothing in the log at all: %s"
             % (len(extra), ", ".join("%s/%s" % (db.day_str(d), h) for d, h in extra[:6])))


def compare_hours(con, loghour, explained):
    dbhour = {}
    for hr, host, hits, byt in con.execute(
            "SELECT r.hour, h.host, r.hits, r.bytes FROM roll_hour r JOIN dim_host h ON h.id = r.host_id"):
        dbhour[(hr, host)] = (hits, byt)
    low = high = 0
    for k, v in loghour.items():
        got = dbhour.get(k)
        if got is None:
            continue          # covered by the day comparison
        if got[0] != v[0]:
            if got[0] < v[0]:
                low += 1
            else:
                high += 1
            if low + high <= 5:
                print("        hour %d %-20s log=%s db=%s" % (k[0], k[1], tuple(v), got))
    if low:
        fail("%d hour bucket(s) behind the log: SQL localtime and Python localtime disagree" % low)
    if high and not explained:
        fail("%d hour bucket(s) AHEAD of the log with nothing to explain it: suspect double counting"
             % high)
    elif high:
        warn("%d hour bucket(s) ahead of the log (%s)" % (high, "; ".join(explained)))
    if not low and not high:
        ok("hour bucketing agrees between SQL and Python (%d buckets)" % len(dbhour))


def compare_uris(con, loguri, explained):
    """Top N URIs per host. Catches interning mistakes that aggregate totals cannot see, in
    particular the host prefixed uri leaking into dim_uri and splitting one URL into two."""
    bad = over = 0
    for host, counter in loguri.items():
        # TOPN + 1, because the sentinel is skipped below and is top one on every host.
        for u, n in counter.most_common(TOPN + 1):
            if u.endswith(PRE.PROBE_SENTINEL):
                continue
            row = con.execute(
                "SELECT COALESCE(SUM(d.hits), 0) FROM roll_day_dim d JOIN dim_uri x ON x.id = d.val_id "
                "JOIN dim_host h ON h.id = d.host_id WHERE d.dim = ? AND h.host = ? AND x.uri = ?",
                (db.D_URI, host, u)).fetchone()
            g = row[0] if row else 0
            if g != n:
                bad += 1
                over = over + 1 if g > n else over
                if bad <= 5:
                    print("        %-20s %-45s log=%d db=%s" % (host, u[:45], n, g))
    if bad and (over == 0 or not explained):
        fail("%d top URI row(s) disagree with the log (%d of them ahead)" % (bad, over))
    elif bad:
        warn("%d top URI row(s) ahead of the log (%s)" % (bad, "; ".join(explained)))
    else:
        ok("top %d URIs per host agree for %d host(s)" % (TOPN, len(loguri)))


def check_internal(con):
    # Scoped to days that still have raw events and no legacy contribution, since rollups outlive the
    # events they were built from.
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

    # A 0 val_id may only appear in D_STATUS, where it means an aborted connection.
    stray = con.execute("SELECT COUNT(*) FROM roll_day_dim WHERE val_id=0 AND dim<>?",
                        (db.D_STATUS,)).fetchone()[0]
    if stray:
        fail("%d roll_day_dim row(s) use val_id 0 outside D_STATUS: stale VAL_UNKNOWN encoding" % stray)
    else:
        ok("val_id 0 appears only where it means HTTP status 0")

    # Scoped to days that still have events. This is sound only because the prune deletes whole days:
    # a day cut mid way would still appear in DISTINCT day and fail on a fragment.
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

    dup = con.execute("SELECT COUNT(*) FROM (SELECT src_id, src_off FROM event "
                      "GROUP BY src_id, src_off HAVING COUNT(*) > 1)").fetchone()[0]
    if dup:
        fail("%d duplicated (src_id, src_off): the identity index is not doing its job" % dup)
    else:
        ok("no duplicate source offsets")

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
    logday, loghour, loguri = scan(found, limits)

    explained = loss_evidence(con, found)
    compare_days(con, logday, explained)
    compare_hours(con, loghour, explained)
    compare_uris(con, loguri, explained)
    check_internal(con)

    print("\n%d ok, %d note(s), %d failure(s), %.1fs" % (len(OK), len(WARN), len(FAIL), time.time() - t0))
    if FAIL:
        print("VERDICT: the warehouse does NOT agree with the log. Do not point any page at it.")
        return 1
    print("VERDICT: agreement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
