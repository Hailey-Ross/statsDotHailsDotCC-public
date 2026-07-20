#!/usr/bin/env python3
"""Schema, connection and rollup helpers for the hails events warehouse. Imported, never executed.

event holds raw per request rows and is pruned; roll_* holds the per day and per hour summaries and
is kept forever.
"""
import sqlite3
import os
import time
import zlib
import datetime

SCHEMA_VERSION = 3
DB_PATH = os.environ.get("HAILS_DB", "/var/lib/hails-stats/events.db")

# Content fingerprint of a log file's opening bytes, used to recognise the same logical file after
# its inode changes.
FP_BYTES = 4096
FP_MIN = 512


def fingerprint(head):
    import hashlib
    if head is None or len(head) < FP_MIN:
        return None
    return hashlib.blake2b(head[:FP_BYTES], digest_size=16).digest()

K_METHOD, K_CTYPE, K_TLS, K_LOC = 1, 2, 3, 4
K_COUNTRY, K_ASN, K_OS, K_BROWSER, K_REFSITE = 5, 6, 7, 8, 9

(D_URI, D_STATUS, D_METHOD, D_CTYPE, D_TLS, D_REF, D_IP,
 D_COUNTRY, D_ASN, D_OS, D_BROWSER, D_REFSITE) = range(1, 13)

# Reserved val_ids in roll_day_dim. Negative because D_STATUS stores the status code as val_id and
# status 0 is a real value.
VAL_UNKNOWN = -2     # the attribute was absent or unresolved on those rows
VAL_OTHER = -1       # the summed tail beyond DIM_CAP, so the rows still add up to roll_day
DIM_CAP = 2000       # distinct values kept per (day, host, dim) before the tail folds into VAL_OTHER

DDL = """
CREATE TABLE dim_host(id INTEGER PRIMARY KEY, host TEXT NOT NULL UNIQUE, safe TEXT NOT NULL);
CREATE TABLE dim_uri (id INTEGER PRIMARY KEY, uri TEXT NOT NULL UNIQUE, is_static INTEGER NOT NULL);
CREATE TABLE dim_ip  (id INTEGER PRIMARY KEY, ip  TEXT NOT NULL UNIQUE,
                      country_id INTEGER, asn_id INTEGER, geo_epoch INTEGER);
CREATE TABLE dim_ua  (id INTEGER PRIMARY KEY, ua  TEXT NOT NULL UNIQUE,
                      os_id INTEGER, browser_id INTEGER);
CREATE TABLE dim_ref (id INTEGER PRIMARY KEY, ref TEXT NOT NULL UNIQUE, site_id INTEGER);
CREATE TABLE dim_str (id INTEGER PRIMARY KEY, kind INTEGER NOT NULL, val TEXT NOT NULL,
                      UNIQUE(kind, val));

-- One row per log file, identified by (dev, inode) because the rotator renames the file and the
-- inode follows the data, so a path is not an identity. gen bumps if a file is truncated in place,
-- which gives the replayed byte offsets a fresh namespace instead of colliding with the old content.
CREATE TABLE source(
  id INTEGER PRIMARY KEY, dev INTEGER NOT NULL, inode INTEGER NOT NULL,
  gen INTEGER NOT NULL DEFAULT 0, path TEXT, fp BLOB,
  offset INTEGER NOT NULL DEFAULT 0, size INTEGER,
  first_ts INTEGER, last_ts INTEGER, lines INTEGER NOT NULL DEFAULT 0,
  updated INTEGER, done INTEGER NOT NULL DEFAULT 0,
  UNIQUE(dev, inode, gen));
CREATE INDEX idx_source_fp ON source(fp);

CREATE TABLE event(
  id INTEGER PRIMARY KEY,
  src_id INTEGER NOT NULL, src_off INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  day INTEGER NOT NULL,
  host_id INTEGER NOT NULL, uri_id INTEGER NOT NULL, ip_id INTEGER NOT NULL,
  ua_id INTEGER, ref_id INTEGER,
  status INTEGER NOT NULL, size INTEGER NOT NULL, duration INTEGER NOT NULL,
  method_id INTEGER, ctype_id INTEGER, tls_id INTEGER, loc_id INTEGER);

-- Every consumer is a window scan, never a point lookup: WHERE ts >= ? and
-- WHERE host_id = ? AND ts >= ?. Nothing filters on uri_id, status, ip_id or ua_id, so indexing
-- them would cost a b tree insert per row for an index the planner would never choose.
CREATE INDEX idx_event_ts      ON event(ts);
CREATE INDEX idx_event_host_ts ON event(host_id, ts);
-- Identity, and the reason re ingesting a region is a no op rather than a double count. A byte
-- offset is stable for the life of an append only file and unique by construction, so it is
-- idempotent from ANY resume point.
CREATE UNIQUE INDEX idx_event_ident ON event(src_id, src_off);

CREATE TABLE roll_day(
  day INTEGER NOT NULL, host_id INTEGER NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0, valid INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
  dur_sum INTEGER NOT NULL DEFAULT 0, dur_n INTEGER NOT NULL DEFAULT 0,
  uv_nonadditive INTEGER NOT NULL DEFAULT 0,
  visitors BLOB,
  source TEXT NOT NULL DEFAULT 'raw',
  PRIMARY KEY(day, host_id)) WITHOUT ROWID;

CREATE TABLE roll_hour(
  hour INTEGER NOT NULL, host_id INTEGER NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0, valid INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
  uv_nonadditive INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(hour, host_id)) WITHOUT ROWID;

CREATE TABLE roll_day_dim(
  day INTEGER NOT NULL, host_id INTEGER NOT NULL, dim INTEGER NOT NULL, val_id INTEGER NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0, valid INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
  uv_nonadditive INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, host_id, dim, val_id)) WITHOUT ROWID;

CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
"""

ROLL_DIMS = (
    (D_URI,     "e.uri_id",       ""),
    (D_STATUS,  "e.status",       ""),
    (D_METHOD,  "e.method_id",    ""),
    (D_CTYPE,   "e.ctype_id",     ""),
    (D_TLS,     "e.tls_id",       ""),
    (D_REF,     "e.ref_id",       ""),
    (D_IP,      "e.ip_id",        ""),
    (D_COUNTRY, "ip.country_id",  "JOIN dim_ip ip ON ip.id = e.ip_id"),
    (D_ASN,     "ip.asn_id",      "JOIN dim_ip ip ON ip.id = e.ip_id"),
    (D_OS,      "ua.os_id",       "LEFT JOIN dim_ua ua ON ua.id = e.ua_id"),
    (D_BROWSER, "ua.browser_id",  "LEFT JOIN dim_ua ua ON ua.id = e.ua_id"),
    (D_REFSITE, "rf.site_id",     "LEFT JOIN dim_ref rf ON rf.id = e.ref_id"),
)


# day is YYYYMMDD and hour is YYYYMMDDHH, both in server local time.

def day_of(ts):
    lt = time.localtime(ts)
    return lt.tm_year * 10000 + lt.tm_mon * 100 + lt.tm_mday


def hour_of(ts):
    return day_of(ts) * 100 + time.localtime(ts).tm_hour


def day_str(day):
    return "%04d-%02d-%02d" % (day // 10000, day // 100 % 100, day % 100)


def day_bounds(day):
    """[start, end) epoch seconds for a local calendar day, DST safe."""
    d0 = datetime.date(day // 10000, day // 100 % 100, day % 100)
    d1 = d0 + datetime.timedelta(days=1)
    start = int(time.mktime((d0.year, d0.month, d0.day, 0, 0, 0, 0, 0, -1)))
    end = int(time.mktime((d1.year, d1.month, d1.day, 0, 0, 0, 0, 0, -1)))
    return start, end


def connect(path=None, readonly=False):
    path = path or DB_PATH
    fresh = not os.path.exists(path)
    if not readonly:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    # isolation_level=None means BEGIN and COMMIT are ours to place, which the ingest needs so the
    # cursor commits with the rows it covers.
    con = sqlite3.connect(path, timeout=15.0, isolation_level=None)
    con.execute("PRAGMA busy_timeout=15000")
    if fresh and not readonly:
        con.execute("PRAGMA auto_vacuum=INCREMENTAL")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA cache_size=-32000")
    con.execute("PRAGMA mmap_size=268435456")
    con.execute("PRAGMA foreign_keys=OFF")
    if readonly:
        con.execute("PRAGMA query_only=1")
    else:
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA wal_autocheckpoint=2000")
        con.execute("PRAGMA temp_store=MEMORY")
    if not readonly:
        # Every visitor IP is in here, so the file takes the access log's 0600 posture. After the WAL
        # pragma, which is what creates the sidecars holding the same rows.
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(path + suffix, 0o600)
            except OSError:
                pass
    return con


def init_schema(con):
    v = con.execute("PRAGMA user_version").fetchone()[0]
    if v == SCHEMA_VERSION:
        return False
    if v == 0:
        con.executescript(DDL)
        con.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        set_meta(con, "created", str(int(time.time())))
        set_meta(con, "tz_name", tz_name())
        return True
    if v == 1:
        con.execute("ALTER TABLE source ADD COLUMN fp BLOB")
        con.execute("CREATE INDEX IF NOT EXISTS idx_source_fp ON source(fp)")
        con.execute("PRAGMA user_version=2")
        v = 2
    if v == 2:
        con.execute("PRAGMA user_version=3")
        for (d,) in con.execute("SELECT DISTINCT day FROM event ORDER BY day").fetchall():
            con.execute("BEGIN")
            refresh_day(con, d)
            con.execute("COMMIT")
        return True
    if v == SCHEMA_VERSION:
        return True
    raise RuntimeError("events.db is schema v%d, this code speaks v%d" % (v, SCHEMA_VERSION))


def tz_name():
    return "%s/%s" % (time.tzname[0], time.tzname[1]) if time.daylight else time.tzname[0]


def get_meta(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_meta(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, str(v)))


def check_tz(con):
    stored = get_meta(con, "tz_name")
    now = tz_name()
    if stored and stored != now:
        raise RuntimeError("timezone changed from %r to %r: day buckets would no longer line up "
                           "with the stored history. Resolve deliberately before ingesting." %
                           (stored, now))


class Dims:
    """In process memo over the dim tables, so a lookup costs one SELECT per new value, not per row."""

    def __init__(self, con):
        self.con = con
        self._c = {}

    def _get(self, table, col, val, extra=None):
        # table and col are module constants, never caller input, so formatting them in is safe.
        key = (table, val)
        i = self._c.get(key)
        if i is not None:
            return i
        row = self.con.execute("SELECT id FROM %s WHERE %s=?" % (table, col), (val,)).fetchone()
        if row:
            i = row[0]
        else:
            cols = [col] + list((extra or {}).keys())
            vals = [val] + list((extra or {}).values())
            i = self.con.execute(
                "INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(cols), ",".join("?" * len(cols))),
                vals).lastrowid
        self._c[key] = i
        return i

    def host(self, h, safe):
        return self._get("dim_host", "host", h, {"safe": safe})

    def uri(self, u, is_static):
        return self._get("dim_uri", "uri", u, {"is_static": 1 if is_static else 0})

    def string(self, kind, val):
        if val is None or val == "":
            return None
        key = ("dim_str", (kind, val))
        i = self._c.get(key)
        if i is not None:
            return i
        row = self.con.execute("SELECT id FROM dim_str WHERE kind=? AND val=?",
                               (kind, val)).fetchone()
        i = row[0] if row else self.con.execute(
            "INSERT INTO dim_str(kind,val) VALUES(?,?)", (kind, val)).lastrowid
        self._c[key] = i
        return i

    def ip(self, ip, resolve=None, epoch=0):
        """resolve(ip) -> (country, asn), called once per distinct IP ever. An unresolved IP keeps
        NULL and geo_epoch 0 so a later pass can fill it."""
        key = ("dim_ip", ip)
        i = self._c.get(key)
        if i is not None:
            return i
        row = self.con.execute("SELECT id FROM dim_ip WHERE ip=?", (ip,)).fetchone()
        if row:
            i = row[0]
        else:
            country = asn = None
            if resolve:
                try:
                    country, asn = resolve(ip)
                except Exception:
                    country = asn = None
            i = self.con.execute(
                "INSERT INTO dim_ip(ip,country_id,asn_id,geo_epoch) VALUES(?,?,?,?)",
                (ip, self.string(K_COUNTRY, country), self.string(K_ASN, asn),
                 epoch if (country or asn) else 0)).lastrowid
        self._c[key] = i
        return i

    def ua(self, ua, parse=None):
        key = ("dim_ua", ua)
        i = self._c.get(key)
        if i is not None:
            return i
        row = self.con.execute("SELECT id FROM dim_ua WHERE ua=?", (ua,)).fetchone()
        if row:
            i = row[0]
        else:
            osn = br = None
            if parse:
                try:
                    osn, br = parse(ua)
                except Exception:
                    osn = br = None
            i = self.con.execute("INSERT INTO dim_ua(ua,os_id,browser_id) VALUES(?,?,?)",
                                 (ua, self.string(K_OS, osn), self.string(K_BROWSER, br))).lastrowid
        self._c[key] = i
        return i

    def ref(self, ref, siteof=None):
        key = ("dim_ref", ref)
        i = self._c.get(key)
        if i is not None:
            return i
        row = self.con.execute("SELECT id FROM dim_ref WHERE ref=?", (ref,)).fetchone()
        if row:
            i = row[0]
        else:
            site = None
            if siteof:
                try:
                    site = siteof(ref)
                except Exception:
                    site = None
            i = self.con.execute("INSERT INTO dim_ref(ref,site_id) VALUES(?,?)",
                                 (ref, self.string(K_REFSITE, site))).lastrowid
        self._c[key] = i
        return i


# A day's distinct ip_id set: sorted ids, delta encoded, varint packed, zlib'd. Stored so multi day
# unique counts stay exact after the raw rows are pruned.

def pack_visitors(ids):
    out = bytearray()
    prev = 0
    for i in sorted(ids):
        d = i - prev
        prev = i
        while d >= 0x80:
            out.append((d & 0x7F) | 0x80)
            d >>= 7
        out.append(d)
    return zlib.compress(bytes(out), 6)


def unpack_visitors(blob):
    if not blob:
        return []
    raw = zlib.decompress(blob)
    ids, cur, shift, prev = [], 0, 0, 0
    for b in raw:
        cur |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
            continue
        prev += cur
        ids.append(prev)
        cur, shift = 0, 0
    return ids


def refresh_day(con, day):
    lo, hi = day_bounds(day)
    con.execute("DELETE FROM roll_day     WHERE day=?  AND source='raw'", (day,))
    con.execute("DELETE FROM roll_day_dim WHERE day=?", (day,))
    con.execute("DELETE FROM roll_hour    WHERE hour>=? AND hour<=?", (day * 100, day * 100 + 23))

    con.execute("""
        INSERT INTO roll_day(day,host_id,hits,valid,failed,bytes,dur_sum,dur_n,uv_nonadditive,source)
        SELECT day, host_id, COUNT(*), SUM(status<400), SUM(status>=400), SUM(size),
               SUM(duration), SUM(duration>0), COUNT(DISTINCT ip_id), 'raw'
        FROM event WHERE ts>=? AND ts<? GROUP BY day, host_id
        ON CONFLICT(day,host_id) DO UPDATE SET
          hits=MAX(excluded.hits, roll_day.hits), valid=MAX(excluded.valid, roll_day.valid),
          failed=MAX(excluded.failed, roll_day.failed), bytes=MAX(excluded.bytes, roll_day.bytes),
          dur_sum=MAX(excluded.dur_sum, roll_day.dur_sum),
          dur_n=MAX(excluded.dur_n, roll_day.dur_n),
          uv_nonadditive=MAX(excluded.uv_nonadditive, roll_day.uv_nonadditive),
          source='mixed'""", (lo, hi))
    # MAX rather than overwrite: the DELETE above is scoped to source='raw', so a conflict here can
    # only be an imported historical row, which a partial raw total must not replace.

    for (host_id,) in con.execute(
            "SELECT DISTINCT host_id FROM event WHERE ts>=? AND ts<?", (lo, hi)).fetchall():
        ids = [r[0] for r in con.execute(
            "SELECT DISTINCT ip_id FROM event WHERE ts>=? AND ts<? AND host_id=?",
            (lo, hi, host_id))]
        con.execute("UPDATE roll_day SET visitors=? WHERE day=? AND host_id=?",
                    (pack_visitors(ids), day, host_id))

    con.execute("""
        INSERT INTO roll_hour(hour,host_id,hits,valid,failed,bytes,uv_nonadditive)
        SELECT day*100 + CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER),
               host_id, COUNT(*), SUM(status<400), SUM(status>=400), SUM(size),
               COUNT(DISTINCT ip_id)
        FROM event WHERE ts>=? AND ts<? GROUP BY 1, host_id""", (lo, hi))

    for dim, expr, join in ROLL_DIMS:
        rows = con.execute("""
            SELECT e.host_id, COALESCE(%s, %d) AS v, COUNT(*), SUM(e.status<400),
                   SUM(e.status>=400), SUM(e.size), COUNT(DISTINCT e.ip_id)
            FROM event e %s
            WHERE e.ts>=? AND e.ts<? GROUP BY e.host_id, v""" % (expr, VAL_UNKNOWN, join),
            (lo, hi)).fetchall()
        byhost = {}
        for r in rows:
            byhost.setdefault(r[0], []).append(r[1:])
        for host_id, vals in byhost.items():
            vals.sort(key=lambda r: r[1], reverse=True)
            keep, tail = vals[:DIM_CAP], vals[DIM_CAP:]
            out = [(day, host_id, dim, v[0], v[1], v[2], v[3], v[4], v[5]) for v in keep]
            if tail:
                # Folded rather than dropped, so the dimension still sums to roll_day.
                out.append((day, host_id, dim, VAL_OTHER,
                            sum(v[1] for v in tail), sum(v[2] for v in tail),
                            sum(v[3] for v in tail), sum(v[4] for v in tail),
                            max(v[5] for v in tail)))
            con.executemany("INSERT INTO roll_day_dim(day,host_id,dim,val_id,hits,valid,failed,"
                            "bytes,uv_nonadditive) VALUES(?,?,?,?,?,?,?,?,?)", out)
