#!/usr/bin/env python3
"""Incremental ingest of the Caddy access log into the events warehouse, run on its own timer.

This is the single writer of the warehouse and takes an exclusive lock for the whole run. It keeps
its own byte cursor per log file, keyed on (dev, inode) because rotation renames the file.
"""
import sys
import os
import io
import json
import time
import gzip
import fcntl
import importlib.util
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hails_db as db  # noqa: E402

BATCH = 20000          # rows per transaction, cursor committed with them
CHUNK = 4 << 20        # bytes per read


def log(msg):
    sys.stderr.write("hails-ingest: %s\n" % msg)


def load_pre():
    for cand in (os.path.join(HERE, "hails-stats-pre.py"), "/usr/local/bin/hails-stats-pre.py"):
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location("hails_stats_pre", cand)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise SystemExit("hails-stats-pre.py not found next to this script or in /usr/local/bin")


PRE = load_pre()

# Below load_pre() on purpose: these read config.env through PRE.
LOG_DIR = PRE.cfg("HAILS_LOG_DIR", "/var/log/caddy")
DROP_HOSTS = PRE.drop_hosts()
PROBE_PATHS = PRE.probe_paths()
LIVE = os.path.join(LOG_DIR, "access.log")

STATIC = (".css", ".js", ".mjs", ".map", ".jpg", ".jpeg", ".png", ".gif", ".ico", ".svg", ".webp",
          ".bmp", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".webm", ".ogg", ".mp3", ".wav",
          ".pdf", ".zip", ".gz", ".txt", ".xml", ".wasm", ".avif")

try:
    import maxminddb
    GEO = maxminddb.open_database("/var/lib/GeoIP/dbip-country.mmdb")
except Exception:
    GEO = None
try:
    import maxminddb as _mm
    ASN = _mm.open_database("/var/lib/GeoIP/dbip-asn.mmdb")
except Exception:
    ASN = None
try:
    from user_agents import parse as ua_parse
except Exception:
    ua_parse = None


def geo_epoch():
    try:
        return int(os.path.getmtime("/var/lib/GeoIP/dbip-country.mmdb"))
    except Exception:
        return 0


def resolve_ip(ip):
    country = asn = None
    if GEO:
        try:
            c = (GEO.get(ip) or {}).get("country") or {}
            country = (c.get("names") or {}).get("en") or c.get("iso_code") or "Unknown"
        except Exception:
            country = None
    if ASN:
        try:
            r = ASN.get(ip) or {}
            num = r.get("autonomous_system_number")
            org = r.get("autonomous_system_organization") or ""
            asn = (("AS%s " % num if num else "") + org).strip() or "Unknown"
        except Exception:
            asn = None
    return country, asn


def parse_ua(ua):
    if not ua_parse:
        return None, None
    u = ua_parse(ua)
    os_s = (u.os.family + " " + (u.os.version_string or "")).strip() or "Other"
    bv = str(u.browser.version[0]) if u.browser.version else ""
    br_s = (u.browser.family + " " + bv).strip() or "Other"
    return os_s, br_s


def ref_site(ref):
    if not ref:
        return None
    return PRE.ref_host(ref) or None


def first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v if isinstance(v, str) else None


def candidates():
    out = []
    try:
        names = os.listdir(LOG_DIR)
    except Exception as e:
        log("cannot list %s: %s" % (LOG_DIR, e))
        return []
    for n in sorted(names):
        if not (n == "access.log" or (n.startswith("access-") and ".log" in n)):
            continue
        p = os.path.join(LOG_DIR, n)
        try:
            st = os.stat(p)
        except Exception:
            continue
        if not os.path.isfile(p):
            continue
        out.append({"path": p, "dev": st.st_dev, "inode": st.st_ino, "size": st.st_size,
                    "mtime": st.st_mtime, "live": (p == LIVE), "gz": n.endswith(".gz")})
    rotated = sorted([f for f in out if not f["live"]], key=lambda f: f["mtime"])
    live = [f for f in out if f["live"]]
    return rotated + live


def open_checked(f):
    """Open, then fstat, never the reverse: a roll between the two would seek the old file's offset
    into the new file's bytes.

    Returns (handle, dev, inode, size, fp). size is None for a gz, because fstat reports the
    compressed length while every offset here is into the decompressed stream.
    """
    if f["gz"]:
        fh = gzip.open(f["path"], "rb")
        fp = db.fingerprint(fh.read(db.FP_BYTES))
        fh.seek(0)
        return fh, f["dev"], f["inode"], None, fp
    fh = open(f["path"], "rb")
    st = os.fstat(fh.fileno())
    fp = db.fingerprint(fh.read(db.FP_BYTES))
    fh.seek(0)
    return fh, st.st_dev, st.st_ino, st.st_size, fp


def new_gen(con, dev, inode, gen, path, size, fp, why):
    log("%s: %s, starting generation %d" % (path, why, gen + 1))
    return con.execute("INSERT INTO source(dev,inode,gen,path,fp,offset,size,updated) "
                       "VALUES(?,?,?,?,?,0,?,?)",
                       (dev, inode, gen + 1, path, fp, size, int(time.time()))).lastrowid


def source_row(con, dev, inode, path, size, fp):
    """Find this file's cursor, or start one. Returns (id, offset, gen, done).

    Identity is (dev, inode) confirmed by the content fingerprint, because an inode can be reused and
    a compressed copy gets a new one.
    """
    r = con.execute("SELECT id, offset, gen, done, fp FROM source WHERE dev=? AND inode=? "
                    "ORDER BY gen DESC LIMIT 1", (dev, inode)).fetchone()
    if r is not None:
        sid, off, gen, done, oldfp = r
        if oldfp is None and fp is not None:
            con.execute("UPDATE source SET fp=? WHERE id=?", (fp, sid))
            oldfp = fp
        if fp is not None and oldfp is not None and oldfp != fp:
            return new_gen(con, dev, inode, gen, path, size, fp, "inode reused by different content"), \
                0, gen + 1, 0
        if size is not None and size < off:
            # Truncated in place. A new generation gives the replayed offsets a fresh namespace.
            return new_gen(con, dev, inode, gen, path, size, fp,
                           "truncated (size %d < offset %d)" % (size, off)), 0, gen + 1, 0
        return sid, off, gen, done

    # Unknown inode: this may still be the compressed copy of a file already read.
    if fp is not None:
        r2 = con.execute("SELECT id, offset, gen, done, path FROM source WHERE fp=? "
                         "ORDER BY gen DESC LIMIT 1", (fp,)).fetchone()
        if r2 is not None:
            sid, off, gen, done, oldpath = r2
            try:
                con.execute("UPDATE source SET dev=?, inode=?, path=? WHERE id=?",
                            (dev, inode, path, sid))
            except Exception:
                pass
            log("%s is %s under a new inode (compressed or relinked), resuming its cursor at %d"
                % (os.path.basename(path), os.path.basename(oldpath or "?"), off))
            return sid, off, gen, done

    sid = con.execute("INSERT INTO source(dev,inode,gen,path,fp,offset,size,updated) "
                      "VALUES(?,?,0,?,?,0,?,?)",
                      (dev, inode, path, fp, size, int(time.time()))).lastrowid
    return sid, 0, 0, 0


def lines_from(fh, offset):
    """Yield (byte_offset, line) for complete newline terminated lines only. A partial trailing line
    is left unconsumed so the next run sees it whole."""
    fh.seek(offset)
    pos = offset
    pending = b""
    while True:
        chunk = fh.read(CHUNK)
        if not chunk:
            break
        data = pending + chunk if pending else chunk
        parts = data.split(b"\n")
        pending = parts.pop()
        for p in parts:
            yield pos, p
            pos += len(p) + 1


class Ingest:
    def __init__(self, con):
        self.con = con
        self.dims = db.Dims(con)
        self.epoch = geo_epoch()
        self.days = set()
        self.rows = 0
        self.skipped = 0

    def row_for(self, off, o):
        if PRE.is_probe(o):
            return None
        req, host = PRE.normalize(o)
        if not isinstance(host, str) or not host:
            return None
        if PRE.is_dropped(host, req, DROP_HOSTS):
            return None
        # Collapses a scanner probe to one sentinel uri. The event is still stored. Must run before
        # the uri is read below.
        PRE.mask_probe(req, PROBE_PATHS)
        ts = o.get("ts")
        if not isinstance(ts, int) or ts <= 0:
            return None
        uri = req.get("uri") or ""
        if not isinstance(uri, str):
            uri = ""
        # The uri stored is always the bare uri, never the host prefixed form the aggregate log uses.
        # The aggregate view prefixes at query time instead.
        uri = uri[:PRE.URI_MAX]
        ip = req.get("client_ip") or ""
        if not isinstance(ip, str):
            ip = ""
        hdrs = req.get("headers") or {}
        ua = first(hdrs.get("User-Agent"))
        ref = first(hdrs.get("Referer"))
        if ref in ("-", ""):
            ref = None
        rh = o.get("resp_headers") or {}
        ctype = first(rh.get("Content-Type"))
        if ctype:
            ctype = ctype.split(";", 1)[0].strip()
        loc = first(rh.get("Location"))
        tls = (req.get("tls") or {}).get("version")
        try:
            status = int(o.get("status") or 0)
        except Exception:
            status = 0
        try:
            size = int(o.get("size") or 0)
        except Exception:
            size = 0
        dur = o.get("duration")
        dur = dur if isinstance(dur, int) and dur >= 0 else 0
        D = self.dims
        return (0, off, ts, db.day_of(ts),
                D.host(host, PRE.safe(host)),
                D.uri(uri, uri.split("?", 1)[0].lower().endswith(STATIC)),
                D.ip(ip, resolve_ip, self.epoch) if ip else D.ip("", None, 0),
                D.ua(ua, parse_ua) if ua else None,
                D.ref(ref, ref_site) if ref else None,
                status, size, dur,
                D.string(db.K_METHOD, req.get("method")),
                D.string(db.K_CTYPE, ctype),
                D.string(db.K_TLS, tls if isinstance(tls, str) else None),
                D.string(db.K_LOC, loc))

    def flush(self, sid, batch, offset, size, first_ts, last_ts, nlines, more):
        # The cursor moves in the same transaction as the rows it covers, so a crash rolls back both
        # and those lines are simply read again.
        if batch:
            self.con.executemany(
                "INSERT OR IGNORE INTO event(src_id,src_off,ts,day,host_id,uri_id,ip_id,ua_id,"
                "ref_id,status,size,duration,method_id,ctype_id,tls_id,loc_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(sid,) + r[1:] for r in batch])
        self.con.execute(
            "UPDATE source SET offset=?, size=?, lines=lines+?, last_ts=?, "
            "first_ts=COALESCE(first_ts,?), updated=? WHERE id=?",
            (offset, size, nlines, last_ts, first_ts, int(time.time()), sid))
        self.con.execute("COMMIT")
        if more:
            self.con.execute("BEGIN")

    def file(self, f):
        """Ingest one file. Returns the source row id it used, or None if it could not be opened.

        The caller needs that id to mark the source as still present, or reap_lost sets done=1 on a
        live cursor.
        """
        try:
            fh, dev, inode, size, fp = open_checked(f)
        except Exception as e:
            log("cannot open %s: %s" % (f["path"], e))
            # A file that could not be opened must still count as seen if a cursor already exists,
            # or reap_lost writes it off as rotated away over a transient error.
            r = self.con.execute("SELECT id FROM source WHERE dev=? AND inode=? "
                                 "ORDER BY gen DESC LIMIT 1", (f["dev"], f["inode"])).fetchone()
            return r[0] if r else None
        with fh:
            gz = f["gz"]
            sid, offset, _gen, done = source_row(self.con, dev, inode, f["path"], size, fp)
            if done and not f["live"]:
                return sid
            if not gz and offset >= size:
                if not f["live"]:
                    self.con.execute("UPDATE source SET done=1, path=? WHERE id=?", (f["path"], sid))
                return sid
            batch, nlines = [], 0
            first_ts = last_ts = None
            pos = offset
            self.con.execute("BEGIN")
            # A gz is decompressed from the start, but offsets into the decompressed stream are the
            # same offsets the original plain file had.
            for off, raw in lines_from(fh, offset):
                pos = off + len(raw) + 1
                nlines += 1
                if not raw.strip():
                    continue
                try:
                    o = json.loads(raw)
                except Exception:
                    self.skipped += 1
                    continue
                try:
                    row = self.row_for(off, o)
                except Exception:
                    self.skipped += 1
                    continue
                if row is None:
                    self.skipped += 1
                    continue
                batch.append(row)
                ts = row[2]
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)
                self.days.add(row[3])
                if len(batch) >= BATCH:
                    self.flush(sid, batch, pos, size, first_ts, last_ts, nlines, True)
                    self.rows += len(batch)
                    batch, nlines, first_ts, last_ts = [], 0, None, None
            self.flush(sid, batch, pos, pos if gz else size, first_ts, last_ts, nlines, False)
            self.rows += len(batch)
            if not f["live"]:
                self.con.execute("UPDATE source SET done=1, path=? WHERE id=?", (f["path"], sid))
            return sid


def reap_lost(con, seen):
    lost = 0
    for sid, path, off, size in con.execute(
            "SELECT id, path, offset, size FROM source WHERE done=0").fetchall():
        if sid in seen:
            continue
        con.execute("UPDATE source SET done=1 WHERE id=?", (sid,))
        if size is not None and off < size:
            lost += 1
            log("lost the tail of %s: %d of %d bytes never read (rotated away before ingest)"
                % (path, size - off, size))
    if lost:
        db.set_meta(con, "lost_tails", int(db.get_meta(con, "lost_tails", 0) or 0) + lost)


def backfill_refsite(con):
    """Rewrite dim_ref.site_id with the canonical host, then replay the days that depend on it.

    A one off repair, invoked with --backfill-refsite. Days whose raw events have been pruned cannot
    be repaired.
    """
    D = db.Dims(con)
    rows = con.execute("SELECT id, ref, site_id FROM dim_ref").fetchall()
    changed = 0
    con.execute("BEGIN")
    for rid, ref, old_sid in rows:
        want = PRE.ref_host(ref) or None
        new_sid = D.string(db.K_REFSITE, want) if want else None
        if new_sid != old_sid:
            con.execute("UPDATE dim_ref SET site_id=? WHERE id=?", (new_sid, rid))
            changed += 1
    con.execute("COMMIT")
    log("rewrote site_id on %d of %d dim_ref row(s)" % (changed, len(rows)))
    # Replay unconditionally, even when nothing changed: a previous run killed midway can have left
    # site_id correct and some days not yet recomputed.
    days = [d for (d,) in con.execute("SELECT DISTINCT day FROM event ORDER BY day")]
    for d in days:
        con.execute("BEGIN")
        db.refresh_day(con, d)
        con.execute("COMMIT")
    log("recomputed %d day(s) so roll_day_dim D_REFSITE matches" % len(days))

    stale = [d for (d,) in con.execute(
        "SELECT DISTINCT day FROM roll_day_dim WHERE dim=? AND day NOT IN "
        "(SELECT DISTINCT day FROM event) ORDER BY day", (db.D_REFSITE,))]
    if stale:
        log("%d day(s) have no raw events left and keep the old split site_id: %s"
            % (len(stale), ", ".join(str(d) for d in stale[:10]) + (" ..." if len(stale) > 10 else "")))
    return 0


def main():
    t0 = time.time()
    lock_path = db.DB_PATH + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        log("another ingest holds the lock, skipping this run")
        return 0

    con = db.connect()
    db.init_schema(con)
    db.check_tz(con)

    if "--backfill-refsite" in sys.argv:
        rc = backfill_refsite(con)
        con.close()
        return rc

    files = candidates()
    if not files:
        log("no access logs found in %s" % LOG_DIR)
        return 0

    ing = Ingest(con)
    seen = set()
    for f in files:
        try:
            before = ing.rows
            sid = ing.file(f)
            if sid is not None:
                seen.add(sid)
            if ing.rows > before:
                log("%s: +%d rows" % (os.path.basename(f["path"]), ing.rows - before))
        except Exception as e:
            log("error on %s: %s" % (f["path"], e))
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            # The rollback took the uncommitted dim rows with it, so the cache must be discarded.
            ing.dims = db.Dims(con)

    con.execute("BEGIN")
    reap_lost(con, seen)
    con.execute("COMMIT")

    for day in sorted(ing.days):
        con.execute("BEGIN")
        db.refresh_day(con, day)
        con.execute("COMMIT")

    db.set_meta(con, "last_ingest", int(time.time()))
    log("%d rows, %d skipped, %d day(s) rolled up, %.1fs"
        % (ing.rows, ing.skipped, len(ing.days), time.time() - t0))
    con.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("fatal: %s" % e)
        sys.exit(1)
