#!/usr/bin/env python3
"""Read only query layer over the hails events warehouse. Imported, never executed.

This module never writes: it opens the database read only, and the ingest is the single writer.
Windows are snapped to whole periods, so daily is the last 24 whole hours and weekly and monthly the
last 7 and 30 whole days, ending at the last completed hour or day rather than at this instant.
"""
import os
import time
import datetime

import hails_db as db

# Which table a roll_day_dim val_id resolves against. D_STATUS is absent because that dimension
# stores the HTTP status directly as the val_id.
DIM_SOURCE = {
    db.D_URI:     ("dim_uri", "uri"),
    db.D_REF:     ("dim_ref", "ref"),
    db.D_IP:      ("dim_ip",  "ip"),
    db.D_METHOD:  ("dim_str", db.K_METHOD),
    db.D_CTYPE:   ("dim_str", db.K_CTYPE),
    db.D_TLS:     ("dim_str", db.K_TLS),
    db.D_COUNTRY: ("dim_str", db.K_COUNTRY),
    db.D_ASN:     ("dim_str", db.K_ASN),
    db.D_OS:      ("dim_str", db.K_OS),
    db.D_BROWSER: ("dim_str", db.K_BROWSER),
    db.D_REFSITE: ("dim_str", db.K_REFSITE),
}

LABEL_UNKNOWN = "(unknown)"
LABEL_OTHER = "(other)"


def connect(path=None):
    return db.connect(path, readonly=True)


def days_back(n, now=None):
    now = int(now if now is not None else time.time())
    today = datetime.date(*time.localtime(now)[:3])
    out = []
    for i in range(n, 0, -1):
        d = today - datetime.timedelta(days=i)
        out.append(d.year * 10000 + d.month * 100 + d.day)
    return out


def hours_back(n=24, now=None):
    now = int(now if now is not None else time.time())
    lt = time.localtime(now)
    top = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, -1)))
    out = []
    for i in range(n, 0, -1):
        t = top - i * 3600
        out.append(db.hour_of(t))
    return out


WINDOWS = {"daily": 1, "weekly": 7, "monthly": 30}


def window_days(view, now=None):
    if view not in WINDOWS:
        raise ValueError("unknown window %r" % (view,))
    return days_back(WINDOWS[view], now=now)


def agg_exclude():
    v = os.environ.get("HAILS_AGG_EXCLUDE")
    if v is None:
        v = ""
        try:
            with open(os.environ.get("HAILS_CONFIG", "/etc/hails-stats/config.env")) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, val = line.partition("=")
                    if k.strip() == "HAILS_AGG_EXCLUDE":
                        v = val.strip()
        except OSError:
            pass
    return [p.strip() for p in v.split(",") if p.strip()]


def hosts(con):
    return {h: (i, s) for i, h, s in con.execute("SELECT id, host, safe FROM dim_host")}


def drop_hosts():
    """HAILS_DROP_HOSTS prefixes, applied at read time as well as at write time, because adding a
    prefix later leaves every row already stored in place."""
    v = os.environ.get("HAILS_DROP_HOSTS")
    if v is None:
        v = ""
        try:
            with open(os.environ.get("HAILS_CONFIG", "/etc/hails-stats/config.env")) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, val = line.partition("=")
                    if k.strip() == "HAILS_DROP_HOSTS":
                        v = val.strip()
        except OSError:
            pass
    return [p.strip() for p in v.split(",") if p.strip()]


def scope_ids(con, scope):
    dropped = drop_hosts()
    if scope != "all":
        if dropped and any(scope.startswith(p) for p in dropped):
            return []
        r = con.execute("SELECT id FROM dim_host WHERE host=?", (scope,)).fetchone()
        return [r[0]] if r else []
    ex = agg_exclude()
    out = []
    for hid, host, _safe in con.execute("SELECT id, host, safe FROM dim_host"):
        if dropped and any(host.startswith(p) for p in dropped):
            continue
        if not any(host.startswith(p) for p in ex):
            out.append(hid)
    return out


def _in(ids):
    """Inline id list. Safe because every id comes from dim_host.id, never from user input."""
    return "(" + ",".join(str(int(i)) for i in ids) + ")"


# The warehouse can be younger than the window asked for. Rather than relabel, it refuses any window
# longer than the collected history and renders `collecting` instead.

COLLECTING = "collecting"


def coverage(con, ids, days):
    if not ids or not days:
        return 0, len(days or []), None
    lo, hi = min(days), max(days)
    rows = con.execute(
        "SELECT COUNT(DISTINCT day), MIN(day) FROM roll_day "
        "WHERE day>=? AND day<=? AND host_id IN %s" % _in(ids), (lo, hi)).fetchone()
    n, oldest = (rows or (0, None))
    return (n or 0), len(days), (db.day_str(oldest) if oldest else None)


def coverage_hours(con, ids, hours):
    if not ids or not hours:
        return 0, len(hours or []), None
    lo, hi = min(hours), max(hours)
    rows = con.execute(
        "SELECT COUNT(DISTINCT hour), MIN(hour) FROM roll_hour "
        "WHERE hour>=? AND hour<=? AND host_id IN %s" % _in(ids), (lo, hi)).fetchone()
    n, oldest = (rows or (0, None))
    return (n or 0), len(hours), (str(oldest) if oldest else None)


def collecting_since(con):
    v = db.get_meta(con, "created")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class WindowState(object):
    __slots__ = ("view", "ok", "covered", "asked", "oldest", "since", "label")

    def __init__(self, view, ok, covered, asked, oldest, since, label):
        self.view, self.ok = view, ok
        self.covered, self.asked, self.oldest = covered, asked, oldest
        self.since, self.label = since, label

    def __repr__(self):
        return "<WindowState %s ok=%s %d/%d>" % (self.view, self.ok, self.covered, self.asked)


def window_state(con, ids, view, now=None, allow_partial=False):
    since = collecting_since(con)
    if view == "daily":
        periods = hours_back(24, now=now)
        covered, asked, oldest = coverage_hours(con, ids, periods)
        unit = "hour"
    else:
        periods = window_days(view, now=now)
        covered, asked, oldest = coverage(con, ids, periods)
        unit = "day"

    ok = bool(ids) and asked > 0 and (covered >= asked if not allow_partial else covered > 0)
    if ok:
        return WindowState(view, True, covered, asked, oldest, since, None)

    if not ids:
        label = "%s, no data for this scope yet" % COLLECTING
    elif covered == 0:
        label = "%s, nothing recorded for this window yet" % COLLECTING
    else:
        label = "%s, %d of %d %ss so far" % (COLLECTING, covered, asked, unit)
    if since:
        label += " (collection began %s)" % time.strftime("%Y-%m-%d %H:%M", time.localtime(since))
    return WindowState(view, False, covered, asked, oldest, since, label)


def servable_views(con, ids, now=None):
    return {v: window_state(con, ids, v, now=now) for v in WINDOWS}


def totals(con, ids, days):
    """Summed roll_day counters for a scope over whole days.

    Uniques are not here on purpose: summing uv_nonadditive over a range inflates it by roughly the
    day count. Call uniques() instead, which unions the visitor blobs and is exact.
    """
    z = {"hits": 0, "valid": 0, "failed": 0, "bytes": 0, "dur_sum": 0, "dur_n": 0}
    if not ids or not days:
        return z
    r = con.execute(
        "SELECT SUM(hits), SUM(valid), SUM(failed), SUM(bytes), SUM(dur_sum), SUM(dur_n) "
        "FROM roll_day WHERE day>=? AND day<=? AND host_id IN %s" % _in(ids),
        (min(days), max(days))).fetchone()
    if not r or r[0] is None:
        return z
    for k, v in zip(("hits", "valid", "failed", "bytes", "dur_sum", "dur_n"), r):
        z[k] = int(v or 0)
    return z


def uniques(con, ids, days):
    """Exact distinct visitor count over a multi day window, by unioning the packed ip_id sets. This
    is the one metric a per day rollup cannot answer by addition."""
    if not ids or not days:
        return 0
    seen = set()
    for (blob,) in con.execute(
            "SELECT visitors FROM roll_day WHERE day>=? AND day<=? AND host_id IN %s" % _in(ids),
            (min(days), max(days))):
        if blob:
            seen.update(db.unpack_visitors(blob))
    return len(seen)


def hourly(con, ids, hours):
    out = {}
    if not ids or not hours:
        return out
    for hr, hits, valid, failed, byt in con.execute(
            "SELECT hour, SUM(hits), SUM(valid), SUM(failed), SUM(bytes) FROM roll_hour "
            "WHERE hour>=? AND hour<=? AND host_id IN %s GROUP BY hour" % _in(ids),
            (min(hours), max(hours))):
        out[hr] = {"hits": int(hits or 0), "valid": int(valid or 0),
                   "failed": int(failed or 0), "bytes": int(byt or 0)}
    return out


def dim_top(con, ids, dim, days, limit=500):
    """Top values of one dimension over a window, as [(val_id, {counters}), ...] by hits desc.

    VAL_OTHER survives into the result, or the rows would stop adding up to roll_day. uv_upper is a
    summed uv_nonadditive and so an upper bound per row, never an exact count.
    """
    if not ids or not days:
        return []
    rows = con.execute(
        "SELECT val_id, SUM(hits) h, SUM(valid), SUM(failed), SUM(bytes), SUM(uv_nonadditive) "
        "FROM roll_day_dim WHERE dim=? AND day>=? AND day<=? AND host_id IN %s "
        "GROUP BY val_id ORDER BY h DESC LIMIT ?" % _in(ids),
        (dim, min(days), max(days), limit)).fetchall()
    return [(int(v), {"hits": int(h or 0), "valid": int(va or 0), "failed": int(f or 0),
                      "bytes": int(b or 0), "uv_upper": int(u or 0)})
            for v, h, va, f, b, u in rows]


def resolve(con, dim, val_ids):
    out = {}
    ids = [int(v) for v in val_ids]
    for v in ids:
        if v == db.VAL_OTHER:
            out[v] = LABEL_OTHER
        elif v == db.VAL_UNKNOWN:
            out[v] = LABEL_UNKNOWN
    real = [v for v in ids if v >= 0]
    if not real:
        return out
    if dim == db.D_STATUS:
        # The val_id is the status code, with no table to look it up in.
        for v in real:
            out[v] = str(v)
        return out
    src = DIM_SOURCE.get(dim)
    if not src:
        for v in real:
            out[v] = str(v)
        return out
    table, col = src
    if table == "dim_str":
        for i, val in con.execute(
                "SELECT id, val FROM dim_str WHERE kind=? AND id IN %s" % _in(real), (col,)):
            out[int(i)] = val
    else:
        for i, val in con.execute(
                "SELECT id, %s FROM %s WHERE id IN %s" % (col, table, _in(real))):
            out[int(i)] = val
    # A dangling val_id renders as its id rather than being dropped, so the panel still sums.
    for v in real:
        out.setdefault(v, "id:%d" % v)
    return out


def dim_page(con, ids, dim, days, limit=500):
    rows = dim_top(con, ids, dim, days, limit=limit)
    labels = resolve(con, dim, [v for v, _ in rows])
    return [(labels.get(v, str(v)), c) for v, c in rows]
