#!/usr/bin/env python3
"""Age raw events out of the warehouse and reclaim unreferenced dimension rows. Run daily by
hails-prune.timer.

Events are deleted a whole local day at a time, never on a raw timestamp, because hails-verify.py
skips pruned days by day key and a half emptied day would sail through that guard and fail. Retention
has a hard floor at MIN_DAYS: the monthly panel reads a 30 day window plus the 30 day window before
it, so anything shorter silently empties a page.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hails_db as db  # noqa: E402

import importlib.util  # noqa: E402


def load_pre():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hails-stats-pre.py")
    spec = importlib.util.spec_from_file_location("hails_stats_pre", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PRE = load_pre()

MIN_DAYS = 62          # below this the monthly window and its comparison window start losing rows
BATCH = 20000
DEFAULT_DAYS = 90


def log(msg):
    sys.stderr.write("hails-prune: %s\n" % msg)


def retention_days():
    raw = PRE.cfg("HAILS_EVENT_DAYS", str(DEFAULT_DAYS))
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        log("HAILS_EVENT_DAYS=%r is not a number, using %d" % (raw, DEFAULT_DAYS))
        return DEFAULT_DAYS
    if n < MIN_DAYS:
        log("HAILS_EVENT_DAYS=%d is below the %d day floor the monthly window needs, using %d"
            % (n, MIN_DAYS, MIN_DAYS))
        return MIN_DAYS
    return n


def prune_events(con, cutoff):
    """Delete events from days entirely older than cutoff, in batches. Returns rows removed.

    Whole days only, on the `day` key, so a day is either wholly present and checkable or wholly
    gone and skipped by the verifier.
    """
    keep_from = db.day_of(cutoff)
    total = 0
    while True:
        con.execute("BEGIN")
        cur = con.execute(
            "DELETE FROM event WHERE id IN (SELECT id FROM event WHERE day < ? LIMIT ?)",
            (keep_from, BATCH))
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        con.execute("COMMIT")
        total += n
        if n < BATCH:
            return total


# (table, event column, roll_day_dim dimension that also references it or None)
# dim_str and dim_host are deliberately absent: both are low cardinality and kept.
DIM_TABLES = (
    ("dim_ua",  "ua_id",  None),
    ("dim_uri", "uri_id", db.D_URI),
    ("dim_ip",  "ip_id",  db.D_IP),
    ("dim_ref", "ref_id", db.D_REF),
)


def prune_dims(con):
    """Drop dimension rows referenced by neither event nor roll_day_dim. Returns {table: removed}.

    A value still cited by any retained roll_day_dim row must survive, or that panel renders a bare
    id. NOT EXISTS rather than NOT IN, because ua_id and ref_id are nullable.
    """
    out = {}
    for table, col, dim in DIM_TABLES:
        keep_roll = ""
        params = ()
        if dim is not None:
            keep_roll = (" AND NOT EXISTS (SELECT 1 FROM roll_day_dim r "
                         "WHERE r.dim=? AND r.val_id=d.id)")
            params = (dim,)
        sql = ("DELETE FROM %s WHERE id IN (SELECT d.id FROM %s d "
               "WHERE NOT EXISTS (SELECT 1 FROM event e WHERE e.%s = d.id)%s LIMIT ?)"
               % (table, table, col, keep_roll))
        total = 0
        while True:
            con.execute("BEGIN")
            cur = con.execute(sql, params + (BATCH,))
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            con.execute("COMMIT")
            total += n
            if n < BATCH:
                break
        out[table] = total
    return out


def main():
    # Imported here, not at module scope, so the prune logic above stays importable without it.
    import fcntl

    days = retention_days()
    cutoff = int(time.time()) - days * 86400
    dry = "--dry-run" in sys.argv

    lock_path = db.DB_PATH + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock = open(lock_path, "w")
    try:
        # The same lock hails-ingest.py takes, so a prune cannot delete dimension rows an ingest is
        # busy interning.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        log("another ingest or prune holds the lock, skipping this run")
        return 0

    con = db.connect()
    db.init_schema(con)
    db.check_tz(con)

    before = con.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    oldest = con.execute("SELECT MIN(ts) FROM event").fetchone()[0]
    counts = {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t, _, _ in DIM_TABLES}

    log("retention %d days, cutoff %s, %d event(s), oldest %s"
        % (days, time.strftime("%Y-%m-%d", time.localtime(cutoff)), before,
           time.strftime("%Y-%m-%d", time.localtime(oldest)) if oldest else "none"))

    if dry:
        keep_from = db.day_of(cutoff)
        n = con.execute("SELECT COUNT(*) FROM event WHERE day < ?", (keep_from,)).fetchone()[0]
        d = con.execute("SELECT COUNT(DISTINCT day) FROM event WHERE day < ?", (keep_from,)).fetchone()[0]
        log("dry run: %d event(s) across %d whole day(s) are older than %d" % (n, d, keep_from))
        con.close()
        return 0

    removed = prune_events(con, cutoff)
    if removed == 0:
        # Nothing aged out, so no dimension row can have become unreferenced either.
        log("nothing to prune")
        con.close()
        return 0

    dims = prune_dims(con)
    con.execute("PRAGMA incremental_vacuum")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    log("removed %d event(s); %s" % (removed, ", ".join(
        "%s %d of %d" % (t, dims.get(t, 0), counts.get(t, 0)) for t, _, _ in DIM_TABLES)))
    db.set_meta(con, "last_prune", str(int(time.time())))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
