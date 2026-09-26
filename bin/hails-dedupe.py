#!/usr/bin/env python3
"""Remove events that a second cursor over the same log file inserted twice.

The row that read further is kept, and the others go only once it holds every offset they hold.
Safe to run when there is nothing to repair.

    hails-dedupe.py              what it would do, changing nothing
    hails-dedupe.py --apply      do it
"""
import os
import sys
import time
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hails_db as db  # noqa: E402

BATCH = 20000


def log(msg):
    sys.stderr.write("hails-dedupe: %s\n" % msg)


def load_prune():
    for cand in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "hails-prune.py"),
                 "/usr/local/bin/hails-prune.py"):
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location("hails_prune", cand)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise SystemExit("hails-prune.py not found next to this script or in /usr/local/bin")


def groups(con):
    """Fingerprints held by more than one source row, keeper first."""
    out = []
    for (fp,) in con.execute("SELECT fp FROM source WHERE fp IS NOT NULL "
                             "GROUP BY fp HAVING COUNT(*) > 1 ORDER BY MIN(id)").fetchall():
        rows = con.execute(
            "SELECT s.id, s.path, s.offset, s.lines, "
            "       (SELECT COUNT(*) FROM event e WHERE e.src_id = s.id) "
            "FROM source s WHERE s.fp = ? ORDER BY s.offset DESC, s.id DESC", (fp,)).fetchall()
        out.append((fp, rows[0], rows[1:]))
    return out


def unsafe(con, keeper_id, victim_id):
    return con.execute(
        "SELECT COUNT(*) FROM event v WHERE v.src_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM event k WHERE k.src_id = ? AND k.src_off = v.src_off)",
        (victim_id, keeper_id)).fetchone()[0]


def delete_events(con, src_id):
    total = 0
    while True:
        con.execute("BEGIN")
        cur = con.execute("DELETE FROM event WHERE id IN "
                          "(SELECT id FROM event WHERE src_id = ? LIMIT ?)", (src_id, BATCH))
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        con.execute("COMMIT")
        total += n
        if n < BATCH:
            return total


def main():
    import fcntl

    apply = "--apply" in sys.argv

    lock_path = db.DB_PATH + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        log("another ingest, prune or dedupe holds the lock, skipping this run")
        return 0

    if not os.path.exists(db.DB_PATH):
        log("no warehouse at %s" % db.DB_PATH)
        return 1

    con = db.connect()
    db.check_tz(con)
    prune = load_prune()     # before anything is deleted, so a missing prune cannot strand a repair

    before = con.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    gs = groups(con)
    if not gs:
        log("%d event(s), no fingerprint is held by more than one source row, nothing to do" % before)
        con.close()
        return 0

    victims, doomed, refuse = [], 0, 0
    for fp, keeper, rest in gs:
        for v in rest:
            lost = unsafe(con, keeper[0], v[0])
            if lost:
                log("REFUSING src %d (%s): %d of its %d event(s) are at offsets src %d never read"
                    % (v[0], os.path.basename(v[1] or "?"), lost, v[4], keeper[0]))
                refuse += 1
                continue
            victims.append((v[0], v[4]))
            doomed += v[4]

    log("%d event(s), %d fingerprint(s) held twice, %d duplicate source row(s), %d event(s) to delete"
        % (before, len(gs), len(victims), doomed))
    for fp, keeper, rest in gs[:3]:
        log("  keep src %-3d %-46s offset %d" % (keeper[0], os.path.basename(keeper[1] or "?"),
                                                 keeper[2]))
        for v in rest:
            log("  drop src %-3d %-46s offset %d, %d event(s)"
                % (v[0], os.path.basename(v[1] or "?"), v[2], v[4]))
    if len(gs) > 3:
        log("  ... and %d more group(s)" % (len(gs) - 3))

    if refuse:
        log("%d source row(s) refused: they hold events the keeper does not. Nothing was deleted."
            % refuse)
        con.close()
        return 1

    if not apply:
        log("dry run, nothing changed. Re run with --apply.")
        con.close()
        return 0

    days = set(d for (d,) in con.execute(
        "SELECT DISTINCT day FROM event WHERE src_id IN (%s)"
        % ",".join("?" * len(victims)), [v[0] for v in victims]).fetchall())

    removed = 0
    for sid, _n in victims:
        removed += delete_events(con, sid)
    log("deleted %d event(s) across %d day(s)" % (removed, len(days)))

    for day in sorted(days):
        con.execute("BEGIN")
        db.refresh_day(con, day)
        con.execute("COMMIT")
    log("recomputed roll_day, roll_hour and roll_day_dim for %d day(s)" % len(days))

    # Only once their events are gone: a leftover row sharing the keeper's fingerprint would still be
    # adopted by the ingest.
    con.execute("BEGIN")
    for sid, _n in victims:
        left = con.execute("SELECT COUNT(*) FROM event WHERE src_id=?", (sid,)).fetchone()[0]
        if left == 0:
            con.execute("DELETE FROM source WHERE id=?", (sid,))
    con.execute("COMMIT")

    dims = prune.prune_dims(con)
    con.execute("PRAGMA incremental_vacuum")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    after = con.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    log("%d event(s) remain; reclaimed %s"
        % (after, ", ".join("%s %d" % (t, dims.get(t, 0)) for t, _, _ in prune.DIM_TABLES)))
    db.set_meta(con, "last_dedupe", str(int(time.time())))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
