#!/usr/bin/env python3
# Single pass analytics for one stats scope, written as a set of HTML pages into OUTDIR.
#
#   python3 hails-panels.py "<TITLE>" "<OUTDIR>" < preprocessed.json
#   python3 hails-panels.py --from-db --scope <hostname|all> "<TITLE>" "<OUTDIR>"
#
# Sessions, visits, entries and exits are inferred from client IP plus timestamp gaps, so they are
# approximate, not beacon accurate.
import sys, json, html, os, time, math, random, re, heapq, importlib.util
from urllib.parse import urlsplit, quote

HERE = os.path.dirname(os.path.abspath(__file__))


def load_pre():
    """hails-stats-pre.py is hyphenated, so it cannot be imported by name."""
    for cand in (os.path.join(HERE, "hails-stats-pre.py"), "/usr/local/bin/hails-stats-pre.py"):
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location("hails_stats_pre", cand)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    raise SystemExit("hails-stats-pre.py not found next to this script or in /usr/local/bin")


PRE = load_pre()

FONT_BASE = os.environ.get("HAILS_FONT_BASE", "/fonts").rstrip("/")
FONT_CSS = (
    "@font-face{font-family:'Roboto';font-style:normal;font-weight:100 900;font-display:swap;src:url(%s/roboto-latin-ext.woff2) format('woff2');unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}"
    "@font-face{font-family:'Roboto';font-style:normal;font-weight:100 900;font-display:swap;src:url(%s/roboto-latin.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}"
) % (FONT_BASE, FONT_BASE)

_args = sys.argv[1:]
FROM_DB = False
DB_SCOPE = None
_pos = []
_i = 0
while _i < len(_args):
    _a = _args[_i]
    if _a == "--from-db":
        FROM_DB = True
        _i += 1
    elif _a == "--scope":
        DB_SCOPE = _args[_i + 1] if _i + 1 < len(_args) else ""
        if not DB_SCOPE.strip():
            sys.stderr.write("hails-panels: --scope needs a value (a hostname, or \"all\")\n")
            sys.exit(2)
        _i += 2
    else:
        _pos.append(_a)
        _i += 1

TITLE = _pos[0] if _pos else "All domains (aggregate)"
OUTDIR = _pos[1] if len(_pos) > 1 else "."

_seed = os.environ.get("HAILS_SEED")
if _seed:
    try:
        random.seed(int(_seed))
    except ValueError:
        random.seed(_seed)
try:
    NOW = int(os.environ.get("HAILS_NOW") or time.time())
except ValueError:
    NOW = int(time.time())
DAY_S, WEEK_S, MON_S = 86400, 604800, 2592000
WINS = ("daily", "weekly", "monthly")
SPAN = {"daily": DAY_S, "weekly": WEEK_S, "monthly": MON_S}
CUT = {w: NOW - SPAN[w] for w in WINS}
PCUT = {w: NOW - 2 * SPAN[w] for w in WINS}
WLABEL = {"daily": "last 24 hours", "weekly": "last 7 days", "monthly": "last 30 days"}

OLDEST = 0   # oldest ts in the source, set during the parse loop below

# How stale the warehouse may be before --from-db refuses to render and lets the log path take over.
DB_STALE_S = int(os.environ.get("HAILS_DB_STALE_S") or 1800)

# Domains that count as our own. Unset means nothing is internal and every referrer is external.
INTERNAL_DOMAINS = tuple(d.strip().lower().lstrip(".")
                         for d in os.environ.get("HAILS_INTERNAL_DOMAINS", "").split(",") if d.strip())


APP_REF = {"fbapp": "Facebook app"}


def norm_netloc(netloc):
    """One canonical form for a referrer host, used both to group it and to classify it. urlsplit
    keeps the port and the FQDN trailing dot, either of which splits one domain across rows."""
    n = (netloc or "").lower().strip()
    n = n.rsplit("@", 1)[-1]
    i = n.rfind(":")
    if i > 0 and n[i + 1:].isdigit():
        n = n[:i]
    return n.rstrip(".")


def is_internal(netloc):
    """True if this referrer host is one of ours. Matches the registrable domain and any subdomain."""
    if not netloc or not INTERNAL_DOMAINS:
        return False
    n = norm_netloc(netloc)
    return any(n == d or n.endswith("." + d) for d in INTERNAL_DOMAINS)


def span_txt(secs):
    if secs >= 172800:
        return "%.1f days" % (secs / 86400.0)
    if secs >= 7200:
        return "%.1f hours" % (secs / 3600.0)
    return "%d minutes" % max(1, int(secs // 60))


def covered():
    return (NOW - OLDEST) if OLDEST else 0


def cur_short(w):
    return bool(OLDEST) and OLDEST > CUT[w] + 60


def prev_short(w):
    return bool(OLDEST) and OLDEST > PCUT[w] + 60


def detail_wlabel():
    if cur_short("weekly"):
        return "the %s collected so far" % span_txt(covered())
    return "the last 7 days"


def wlabel(w, base):
    if cur_short(w):
        return "%s, only %s collected" % (base, span_txt(covered()))
    return base


def coverage_note(w):
    if not OLDEST:
        return ""
    full = WLABEL[w].replace("last ", "")
    bits = []
    if cur_short(w):
        bits.append("This window covers %s so far, not the full %s, because the log only reaches back "
                    "to %s. The numbers below are real, the span is just shorter than the label."
                    % (span_txt(covered()), full, time.strftime("%Y-%m-%d %H:%M",
                                                                time.localtime(OLDEST))))
    if prev_short(w):
        bits.append("Comparisons against the preceding %s are hidden, since there is not a full prior "
                    "window to measure against." % full)
    return ("<div class=note>" + " ".join(bits) + "</div>") if bits else ""

FLATCAP = 500     # rows shown on a flat panel
XPAR = 200        # parents shown on a cross tab panel
XCHILD = 40       # children shown per cross tab parent
DETAIL_MIN_HITS = 2   # hits a uri needs in the window before it gets a detail record
DETAIL_MAX = 5000     # safety ceiling on detail records, busiest kept first
DETAIL_ROWS = 25      # rows per list on a detail page
PVCAP = 200000    # per window pageview buffer ceiling (session approximation)
SAMPCAP = 400     # per uri duration samples for percentiles
DSAMPCAP = 3000   # per window duration samples for the p95 KPI tile
GAP = 1800        # session gap in seconds (30 minutes)
ACC_CAP = 60000   # max distinct keys kept per accumulation dict
XCHILD_CAP = 2000 # max distinct children kept per cross tab parent

STATIC = (".css", ".js", ".mjs", ".map", ".jpg", ".jpeg", ".png", ".gif", ".ico", ".svg", ".webp",
          ".bmp", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".webm", ".ogg", ".mp3", ".wav",
          ".pdf", ".zip", ".gz", ".txt", ".xml", ".wasm", ".avif")
REASON = {200: "OK", 201: "Created", 202: "Accepted", 204: "No Content", 206: "Partial Content",
          301: "Moved Permanently", 302: "Found", 303: "See Other", 304: "Not Modified",
          307: "Temporary Redirect", 308: "Permanent Redirect", 400: "Bad Request", 401: "Unauthorized",
          403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 406: "Not Acceptable",
          408: "Request Timeout", 409: "Conflict", 410: "Gone", 413: "Payload Too Large",
          414: "URI Too Long", 416: "Range Not Satisfiable", 418: "I am a teapot", 421: "Misdirected",
          422: "Unprocessable", 425: "Too Early", 429: "Too Many Requests", 431: "Headers Too Large",
          451: "Unavailable For Legal Reasons", 500: "Internal Server Error", 501: "Not Implemented",
          502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout",
          505: "HTTP Version Not Supported"}

# Needs python3-maxminddb and python3-user-agents. If either is missing those columns render empty.
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
geo_cache, asn_cache, ua_cache = {}, {}, {}


def country_of(ip):
    if not GEO or not ip:
        return ""
    v = geo_cache.get(ip)
    if v is not None:
        return v
    try:
        r = GEO.get(ip) or {}
    except Exception:
        r = {}
    c = r.get("country") or {}
    name = (c.get("names") or {}).get("en") or c.get("iso_code") or "Unknown"
    geo_cache[ip] = name
    return name


def asn_of(ip):
    if not ASN or not ip:
        return ""
    v = asn_cache.get(ip)
    if v is not None:
        return v
    try:
        r = ASN.get(ip) or {}
    except Exception:
        r = {}
    num = r.get("autonomous_system_number")
    org = r.get("autonomous_system_organization") or ""
    s = (("AS%s " % num if num else "") + org).strip() or "Unknown"
    asn_cache[ip] = s
    return s


def os_browser(ua_str):
    if not ua_parse or not ua_str:
        return ("", "")
    v = ua_cache.get(ua_str)
    if v is not None:
        return v
    try:
        u = ua_parse(ua_str)
        os_s = (u.os.family + " " + (u.os.version_string or "")).strip() or "Other"
        bv = str(u.browser.version[0]) if u.browser.version else ""
        br_s = (u.browser.family + " " + bv).strip() or "Other"
    except Exception:
        os_s, br_s = "Other", "Other"
    v = ua_cache[ua_str] = (os_s, br_s)
    return v


def is_static(uri):
    return uri.split("?", 1)[0].lower().endswith(STATIC)


_tcache = {}


def tparts(ts):
    """(struct_time, "YYYY-MM-DD", "GGGG-Www") for a timestamp, memoised on the MINUTE rather than
    the hour, because 30 and 45 minute UTC offsets exist."""
    m = ts // 60
    v = _tcache.get(m)
    if v is None:
        lt = time.localtime(ts)
        v = _tcache[m] = (lt, time.strftime("%Y-%m-%d", lt), time.strftime("%G-W%V", lt))
    return v


_sstr = {}


def sstr(st):
    s = _sstr.get(st)
    if s is None:
        s = _sstr[st] = str(st)
    return s


# The three windows are NESTED (CUT monthly < weekly < daily), so there are four possible answers.
_CW_D = ("daily", "weekly", "monthly")
_CW_W = ("weekly", "monthly")
_CW_M = ("monthly",)


def windows_for(ts):
    # The ORDER matters: accumulator insertion order decides how tied rows sort at render.
    if ts >= CUT["daily"]:
        return _CW_D
    if ts >= CUT["weekly"]:
        return _CW_W
    if ts >= CUT["monthly"]:
        return _CW_M
    return ()


_PW = {w: (w,) for w in WINS}


def prev_windows_for(ts):
    for w in WINS:
        if PCUT[w] <= ts < CUT[w]:
            return _PW[w]
    return ()


# ENDSWITH, NOT EQUALITY: the aggregate scope prefixes the host, so the sentinel arrives as
# <host>/(probe). Not PRE.is_probe either, which is a different test taking a whole log object.
def is_probe(uri):
    return isinstance(uri, str) and uri.endswith(PRE.PROBE_SENTINEL)


# ---- accumulators -------------------------------------------------------------------------------
def nb():
    return {"hits": 0, "valid": 0, "failed": 0, "bytes": 0, "dur": 0, "durn": 0, "vis": set()}


def _bumpb(b, ok, ip, by, du):
    b["hits"] += 1
    if ok:
        b["valid"] += 1
    else:
        b["failed"] += 1
    b["bytes"] += by
    if du > 0:
        b["dur"] += du
        b["durn"] += 1
    if ip:
        b["vis"].add(ip)


def bump(d, k, ok, ip, by, du):
    if k is None:
        return
    b = d.get(k)
    if b is None:
        if len(d) >= ACC_CAP:
            return
        b = d[k] = nb()
    _bumpb(b, ok, ip, by, du)


def xbump(xt, pk, ck, ok, ip, by, du):
    if pk is None:
        return
    e = xt.get(pk)
    if e is None:
        if len(xt) >= ACC_CAP:
            return
        e = xt[pk] = {"b": nb(), "ch": {}}
    _bumpb(e["b"], ok, ip, by, du)
    if ck is not None:
        cb = e["ch"].get(ck)
        if cb is None:
            if len(e["ch"]) >= XCHILD_CAP:
                return
            cb = e["ch"][ck] = nb()
        _bumpb(cb, ok, ip, by, du)


_NKEY = {"samp": "samp_n", "dsamp": "dsamp_n"}


def ring(store, key, cap, val):
    # Algorithm R reservoir sampling: a uniform sample of all values seen, not just the most recent.
    nk = _NKEY.get(key) or (key + "_n")
    lst = store[key]
    n = store.get(nk, 0)
    if len(lst) < cap:
        lst.append(val)
    else:
        j = random.randint(0, n)
        if j < cap:
            lst[j] = val
    store[nk] = n + 1


FLATKEYS = ["requests", "static", "not_found", "hosts", "mime", "tls", "vhosts", "geo", "asn",
            "os", "browsers", "days"]


def new_state():
    return {"flat": {k: {} for k in FLATKEYS}, "xref": {}, "xsite": {},
            "xref_int": {}, "xsite_int": {}, "xstatus": {}, "xmethod": {},
            "err": {}, "tnow": {}, "slow": {}, "pv": [], "pv_over": False, "udet": {}, "blink": {},
            "vis": set(), "total": 0, "err_n": 0, "bytes": 0, "page": 0,
            "durs": 0, "durn": 0, "dsamp": [], "dsamp_n": 0, "last": {}, "visits": 0}


def new_prev():
    return {"vis": set(), "total": 0, "err_n": 0, "bytes": 0, "page": 0, "durs": 0, "durn": 0,
            "tprev": {}, "last": {}, "visits": 0}


CUR = {w: new_state() for w in WINS}
PRV = {w: new_prev() for w in WINS}

# ---- time breakdown state -----------------------------------------------------------------------
DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def newb():
    return {"hits": 0, "valid": 0, "failed": 0, "bytes": 0, "vis": set()}


t_hours = {h: newb() for h in range(24)}   # last 24 hours, by hour of day
t_days = {d: newb() for d in range(7)}     # last 7 days, by day of week
t_weeks = {}                               # last 30 days, by ISO week
t_heat = [[0] * 24 for _ in range(7)]      # last 30 days, hits by (day of week, hour)


# ---- record sources -----------------------------------------------------------------------------
# Two producers, one consumer: the warehouse producer rebuilds the same object shape the
# preprocessor writes, so the parse loop below never learns about database rows.

def stdin_records():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def db_records():
    """Yield preprocessor shaped objects straight out of events.db.

    Country, ASN, OS and browser are already resolved in the warehouse, so they are filled into the
    lookup caches as rows stream. A NULL stays uncached deliberately, so the live lookup still runs.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hails_query as hq

    if DB_SCOPE is None:
        sys.stderr.write("hails-panels: --from-db requires --scope (a hostname, or \"all\")\n")
        sys.exit(2)

    con = hq.connect()

    # Global rather than per scope: a quiet host can legitimately see nothing for hours.
    newest = con.execute("SELECT MAX(ts) FROM event").fetchone()[0]
    if newest is None or (NOW - newest) > DB_STALE_S:
        con.close()
        sys.stderr.write(
            "hails-panels: warehouse is stale, newest event is %s (%s), refusing to render\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest)) if newest else "none",
               ("%.1f hours old" % ((NOW - newest) / 3600.0)) if newest else "table empty"))
        sys.exit(4)

    scope = DB_SCOPE
    ids = hq.scope_ids(con, scope)

    if not ids:
        con.close()
        sys.stderr.write("hails-panels: scope %r matches no host in the warehouse "
                         "(unknown name, or dropped by HAILS_DROP_HOSTS)\n" % scope)
        sys.exit(3)
    # The warehouse stores the bare uri, so the aggregate scope's host prefix is reapplied here.
    prefix = (scope == "all")
    lo = PCUT["monthly"]
    idlist = "(" + ",".join(str(int(i)) for i in ids) + ")"

    # Per scope, and from its own query rather than from the row stream, which is floored at the
    # window bound below and would pin OLDEST to 60 days forever.
    mn = con.execute("SELECT MIN(ts) FROM event WHERE host_id IN %s" % idlist).fetchone()[0]
    if mn:
        global OLDEST
        OLDEST = mn if not OLDEST else min(OLDEST, mn)

    q = (
        "SELECT e.ts, e.status, e.size, e.duration, h.host, u.uri, ip.ip, ua.ua, r.ref, "
        "       m.val, ct.val, tl.val, lo.val, cs.val, ns.val, osv.val, brv.val "
        "FROM event e "
        "JOIN dim_host h ON h.id=e.host_id "
        "JOIN dim_uri  u ON u.id=e.uri_id "
        "JOIN dim_ip  ip ON ip.id=e.ip_id "
        "LEFT JOIN dim_ua  ua ON ua.id=e.ua_id "
        "LEFT JOIN dim_ref r  ON r.id=e.ref_id "
        "LEFT JOIN dim_str m  ON m.id=e.method_id "
        "LEFT JOIN dim_str ct ON ct.id=e.ctype_id "
        "LEFT JOIN dim_str tl ON tl.id=e.tls_id "
        "LEFT JOIN dim_str lo ON lo.id=e.loc_id "
        "LEFT JOIN dim_str cs ON cs.id=ip.country_id "
        "LEFT JOIN dim_str ns ON ns.id=ip.asn_id "
        "LEFT JOIN dim_str osv ON osv.id=ua.os_id "
        "LEFT JOIN dim_str brv ON brv.id=ua.browser_id "
        # (src_id, src_off) is the byte position, which IS log order. Ordering by ts is not the
        # same thing: the line is written when the response completes, ts is when it started.
        "WHERE e.ts>=? AND e.host_id IN %s ORDER BY e.src_id, e.src_off" % idlist)

    try:
        for (ts, st, size, dur, host, uri, ip, ua, ref, meth, ctype, tls, loc,
             cty, asn, os_v, br_v) in con.execute(q, (lo,)):
            if ip:
                if cty:
                    geo_cache[ip] = cty
                if asn:
                    asn_cache[ip] = asn
            if ua and os_v and br_v:
                ua_cache[ua] = (os_v, br_v)
            yield {
                "ts": ts,
                "status": st,
                "size": size,
                "duration": dur,
                "request": {
                    "client_ip": ip or "",
                    "host": host or "",
                    # isinstance, not truthiness: an empty uri must still yield the bare host, as it
                    # does in hails-stats-pre.py's aggregate writer.
                    "uri": ((host + uri) if (prefix and isinstance(uri, str)) else (uri or "")),
                    "method": meth or "",
                    "headers": {"User-Agent": [ua or ""], "Referer": [ref or ""]},
                    "tls": {"version": tls or ""},
                },
                "resp_headers": {"Content-Type": [ctype or ""], "Location": [loc or ""]},
            }
    finally:
        con.close()


# ---- parse --------------------------------------------------------------------------------------
for o in (db_records() if FROM_DB else stdin_records()):
    try:
        ts = int(o.get("ts"))
    except Exception:
        continue
    if ts > 0 and (not OLDEST or ts < OLDEST):
        OLDEST = ts
    cw = windows_for(ts)
    pw = prev_windows_for(ts)
    if not cw and not pw:
        continue
    req = o.get("request") or {}
    ip = req.get("client_ip") or ""
    try:
        st = int(o.get("status", 0))
    except Exception:
        st = 0
    ok = st < 400
    uri = req.get("uri") or ""
    static = is_static(uri) if uri else False
    method = req.get("method") or ""
    host = req.get("host") or ""
    hdrs = req.get("headers") or {}
    ref = hdrs.get("Referer")
    ref = (ref[0] if isinstance(ref, list) and ref else ref) if not isinstance(ref, str) else ref
    if ref in ("-", None):
        ref = ""
    rh = o.get("resp_headers") or {}
    ct = rh.get("Content-Type")
    ct = (ct[0] if isinstance(ct, list) and ct else ct) if not isinstance(ct, str) else ct
    ct = ct.split(";", 1)[0].strip() if ct else ""
    loc = rh.get("Location")
    loc = (loc[0] if isinstance(loc, list) and loc else loc) if not isinstance(loc, str) else loc
    loc = loc or ""
    tls = (req.get("tls") or {}).get("version") or ""
    ua = hdrs.get("User-Agent")
    ua = (ua[0] if isinstance(ua, list) and ua else ua) if not isinstance(ua, str) else ua
    ua = ua or ""
    try:
        by = int(o.get("size") or 0)
    except Exception:
        by = 0
    try:
        du = int(o.get("duration") or 0)   # microseconds (preprocessor)
    except Exception:
        du = 0
    netloc = ""
    if ref:
        try:
            netloc = urlsplit(ref).netloc
        except Exception:
            netloc = ""
        if not netloc:
            netloc = ref.split("/", 1)[0]
        netloc = norm_netloc(netloc)
        netloc = APP_REF.get(ref.split("://", 1)[0].lower(), netloc) if "://" in ref else netloc
    internal = bool(netloc) and is_internal(netloc)
    extref = netloc if (netloc and not internal and netloc != host) else ""
    country = country_of(ip)
    network = asn_of(ip)
    os_s, br_s = os_browser(ua)
    lt, day, wk = tparts(ts)

    if ts >= CUT["monthly"]:
        wb = t_weeks.get(wk)
        if wb is None:
            wb = t_weeks[wk] = newb()
        targets = [wb]
        if ts >= CUT["daily"]:
            targets.append(t_hours[lt.tm_hour])
        if ts >= CUT["weekly"]:
            targets.append(t_days[lt.tm_wday])
        for b in targets:
            b["hits"] += 1
            b["valid"] += 1 if ok else 0
            b["failed"] += 0 if ok else 1
            b["bytes"] += by
            if ip:
                b["vis"].add(ip)
        t_heat[lt.tm_wday][lt.tm_hour] += 1

    pageview = (not static) and ok and (ct.startswith("text/html") or ct == "")
    navable = (not static) and ok

    # A catch all redirect: a 3xx whose Location is a bare origin, sent for a path that is not the
    # root. That is a site bouncing an unknown path to its homepage, and it is not navigation.
    catchall = False
    if 300 <= st < 400 and loc:
        lp = urlsplit(loc)
        # An aggregate uri is host prefixed and so does not start with a slash, which is the
        # discriminator between the two row shapes.
        rp = uri.split("?", 1)[0]
        if not rp.startswith("/"):
            i = rp.find("/")
            rp = rp[i:] if i >= 0 else "/"
        catchall = lp.path in ("", "/") and not lp.query and rp not in ("", "/")

    for w in cw:
        S = CUR[w]
        f = S["flat"]
        if uri:
            bump(f["static"] if static else f["requests"], uri, ok, ip, by, du)
            if st == 404:
                bump(f["not_found"], uri, ok, ip, by, du)
        bump(f["hosts"], ip, ok, ip, by, du)
        bump(f["mime"], ct, ok, ip, by, du)
        bump(f["tls"], tls, ok, ip, by, du)
        bump(f["vhosts"], host, ok, ip, by, du)
        bump(f["geo"], country, ok, ip, by, du)
        bump(f["asn"], network, ok, ip, by, du)
        bump(f["os"], os_s, ok, ip, by, du)
        bump(f["browsers"], br_s, ok, ip, by, du)
        bump(f["days"], day, ok, ip, by, du)
        xbump(S["xstatus"], sstr(st), uri or None, ok, ip, by, du)
        if method:
            xbump(S["xmethod"], method, uri or None, ok, ip, by, du)
        if ref:
            if internal:
                xbump(S["xref_int"], ref, uri or None, ok, ip, by, du)
                xbump(S["xsite_int"], netloc, uri or None, ok, ip, by, du)
            else:
                xbump(S["xref"], ref, uri or None, ok, ip, by, du)
                xbump(S["xsite"], netloc, uri or None, ok, ip, by, du)
        if not ok and uri:
            e = S["err"].get(uri)
            if e is None and len(S["err"]) < ACC_CAP:
                e = S["err"][uri] = {"c4": 0, "c5": 0, "total": 0, "st": {}, "ref": {}, "nfref": {},
                                     "first": ts, "last": ts, "vis": set()}
            if e is not None:
                e["total"] += 1
                if st >= 500:
                    e["c5"] += 1
                elif st >= 400:
                    e["c4"] += 1
                e["st"][st] = e["st"].get(st, 0) + 1
                if extref:
                    e["ref"][extref] = e["ref"].get(extref, 0) + 1
                if st == 404 and ref and len(e["nfref"]) < XCHILD_CAP:
                    e["nfref"][ref] = e["nfref"].get(ref, 0) + 1
                e["first"] = min(e["first"], ts)
                e["last"] = max(e["last"], ts)
                if ip:
                    e["vis"].add(ip)
        # Probes are excluded on both sides of the comparison, here and in the previous window.
        if uri and not static and ok and method == "GET" and not is_probe(uri):
            t = S["tnow"].get(uri)
            if t is None and len(S["tnow"]) < ACC_CAP:
                t = S["tnow"][uri] = {"h": 0, "vis": set()}
            if t is not None:
                t["h"] += 1
                if ip:
                    t["vis"].add(ip)
        if uri:
            ud = S["udet"].get(uri)
            if ud is None and len(S["udet"]) < ACC_CAP:
                ud = S["udet"][uri] = {"hits": 0, "vis": set(), "st": {}, "meth": {}, "ctry": {},
                                       "asn": {}, "refs": {}, "days": {}, "durs": 0, "durn": 0}
            if ud is not None:
                ud["hits"] += 1
                if ip:
                    ud["vis"].add(ip)
                ud["st"][st] = ud["st"].get(st, 0) + 1
                if method:
                    ud["meth"][method] = ud["meth"].get(method, 0) + 1
                if country:
                    ud["ctry"][country] = ud["ctry"].get(country, 0) + 1
                if network:
                    ud["asn"][network] = ud["asn"].get(network, 0) + 1
                if ref and len(ud["refs"]) < XCHILD_CAP:
                    ud["refs"][ref] = ud["refs"].get(ref, 0) + 1
                ud["days"][day] = ud["days"].get(day, 0) + 1
                if du > 0:
                    ud["durs"] += du
                    ud["durn"] += 1

        # Broken links, keyed by the page the link sits ON rather than the missing target. The key
        # is shaped like a detail key: host prefixed in the aggregate scope, path only per domain.
        if uri and st == 404 and ref and netloc and not is_probe(uri):
            path = urlsplit(ref).path or "/"
            src = path if uri.startswith("/") else (netloc + path)
            bl = S["blink"].get(src)
            if bl is None and len(S["blink"]) < ACC_CAP:
                bl = S["blink"][src] = {}
            if bl is not None and len(bl) < XCHILD_CAP:
                bl[uri] = bl.get(uri, 0) + 1

        if uri and du > 0:
            sl = S["slow"].get(uri)
            if sl is None and len(S["slow"]) < ACC_CAP:
                sl = S["slow"][uri] = {"reqs": 0, "err": 0, "samp": [], "samp_n": 0}
            if sl is not None:
                sl["reqs"] += 1
                if not ok:
                    sl["err"] += 1
                ring(sl, "samp", SAMPCAP, du)
        if ip:
            S["vis"].add(ip)
            # ORDER SENSITIVE, and the log is only nearly chronological, so this undercounts by a
            # small bounded amount.
            lw = S["last"].get(ip)
            if lw is None or ts - lw > GAP:
                S["visits"] += 1
            S["last"][ip] = ts
        S["total"] += 1
        if not ok:
            S["err_n"] += 1
        S["bytes"] += by
        if du > 0:
            S["durs"] += du
            S["durn"] += 1
            ring(S, "dsamp", DSAMPCAP, du)
        if pageview:
            S["page"] += 1
        if navable:
            if not S["pv_over"]:
                if len(S["pv"]) >= PVCAP:
                    S["pv_over"] = True
                else:
                    S["pv"].append((ip, ts, uri, extref, country, st, method, du,
                                    1 if pageview else 0, 1 if catchall else 0))

    for w in pw:
        P = PRV[w]
        if ip:
            P["vis"].add(ip)
            lw = P["last"].get(ip)
            if lw is None or ts - lw > GAP:
                P["visits"] += 1
            P["last"][ip] = ts
        P["total"] += 1
        if not ok:
            P["err_n"] += 1
        P["bytes"] += by
        if du > 0:
            P["durs"] += du
            P["durn"] += 1
        # Same gate as the current window above. See the note there.
        if uri and not static and ok and method == "GET" and not is_probe(uri):
            if uri in P["tprev"] or len(P["tprev"]) < ACC_CAP:
                P["tprev"][uri] = P["tprev"].get(uri, 0) + 1
        if pageview:
            P["page"] += 1


# ---- session walk -------------------------------------------------------------------------------
def walk_sessions(events, want_detail):
    events.sort(key=lambda e: (e[0], e[1]))
    entry, exit_, pvc = {}, {}, {}
    detail = {} if want_detail else None
    visits = 0
    cur = []
    prev_ip, prev_ts = None, None

    def rec_uri_agg(uri, e):
        if detail is None:
            return
        d = detail.get(uri)
        if d is None:
            d = detail[uri] = {"prev": {}, "next": {}, "ext": {}, "exits": 0, "entries": 0}
        if e[3]:
            d["ext"][e[3]] = d["ext"].get(e[3], 0) + 1

    def flush(sess):
        nonlocal visits
        if not sess:
            return
        pages = [e for e in sess if e[8]]
        if pages:
            visits += 1
            eu, xu = pages[0][2], pages[-1][2]
            src = pages[0][3] or "(direct)"
            en = entry.get(eu)
            if en is None:
                en = entry[eu] = {"n": 0, "vis": set(), "src": {}}
            en["n"] += 1
            if pages[0][0]:
                en["vis"].add(pages[0][0])
            en["src"][src] = en["src"].get(src, 0) + 1
            ex = exit_.get(xu)
            if ex is None:
                ex = exit_[xu] = {"n": 0, "vis": set()}
            ex["n"] += 1
            if pages[-1][0]:
                ex["vis"].add(pages[-1][0])
        if detail is not None:
            # Probe edges are dropped here rather than by leaving probes out of the session buffer,
            # so session segmentation is unaffected.
            for i, e in enumerate(sess):
                u = e[2]
                rec_uri_agg(u, e)
                if i > 0:
                    pe = sess[i - 1]
                    pu = pe[2]
                    if e[9] or pe[9] or is_probe(u) or is_probe(pu):
                        continue
                    detail[u]["prev"][pu] = detail[u]["prev"].get(pu, 0) + 1
                    detail[pu]["next"][u] = detail[pu]["next"].get(u, 0) + 1
            if pages:
                detail[pages[0][2]]["entries"] += 1
                detail[pages[-1][2]]["exits"] += 1

    for e in events:
        ip, ts = e[0], e[1]
        if e[8]:
            pvc[e[2]] = pvc.get(e[2], 0) + 1
        if ip != prev_ip or (prev_ts is not None and ts - prev_ts > GAP):
            flush(cur)
            cur = []
        cur.append(e)
        prev_ip, prev_ts = ip, ts
    flush(cur)
    return entry, exit_, pvc, visits, detail


SESS = {}
DETAIL = None
for w in WINS:
    # The Visits KPI uses the streaming all activity count, not the pageview only count walk_sessions
    # returns.
    entry, exit_, pvc, _pv_visits, detail = walk_sessions(CUR[w]["pv"], want_detail=(w == "weekly"))
    SESS[w] = {"entry": entry, "exit": exit_, "pvc": pvc, "visits": CUR[w]["visits"]}
    if w == "weekly":
        DETAIL = detail


# ---- format helpers -----------------------------------------------------------------------------
def esc(s):
    return html.escape(str(s)) if s not in ("", None) else "(none)"


def hb(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (int(n), unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "0 B"


# hb renders "1.2 MB", which sorts above "9.0 KB" as text, so a byte cell carries the raw count for
# table.js to sort on.
def bcell(n, cls=""):
    return '<td%s data-s="%d">%s</td>' % ((" class=" + cls) if cls else "", int(n), hb(n))


def bspan(n):
    return '<span class=n data-s="%d">%s</span>' % (int(n), hb(n))


def avgms(b):
    return int(round(b["dur"] / b["durn"] / 1000.0)) if b.get("durn") else 0


def ms(us):
    return int(round(us / 1000.0))


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def reason(code):
    try:
        c = int(code)
    except Exception:
        return str(code)
    return REASON.get(c, "")


def dlink(uri):
    return "<a class=v href='detail.html#%s'>%s</a>" % (quote(str(uri), safe=""), esc(uri))


def bar(pct):
    return "<span class=bar style='width:%d%%'></span>" % pct


def fmt_delta(cur, prev, invert=False, w=None):
    if w is not None and prev_short(w):
        return ""
    if prev == 0:
        return "<span class=dn0>new</span>" if cur else ""
    p = (cur - prev) / float(prev) * 100.0
    up = p >= 0
    good = (not up) if invert else up
    cls = "up" if good else "down"
    arrow = "▲" if up else "▼"
    return "<span class=%s>%s %+.0f%%</span>" % (cls, arrow, p)


def when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def top(counter, n=1):
    if not counter:
        return ""
    return sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:n]


# ---- page shell ---------------------------------------------------------------------------------
FAVICON = ('<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,AAABAAEAEBAQAAEABAAo'
           'AQAAFgAAACgAAAAQAAAAIAAAAAEABAAAAAAAgAAAAAAAAAAAAAAAEAAAAAAAAADGxsYAWFhYABwcHABfAP8A/9df'
           'AADXrwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIiIiIiIiIiIjMlUkQgAiIiIiIiIi'
           'IiIiIzJVJEIAAAIiIiIiIiIiIiMyVSRCAAIiIiIiIiIiIiIRERERERERERERERERERERIiIiIiIiIiIgACVVUiIiIi'
           'IiIiIiIiIiIAAlVVIiIiIiIiIiIiIiIhEREREREREREREREREREREAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
           'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">')

STYLE = """<style>
""" + FONT_CSS + """
*{box-sizing:border-box}
body{font-family:'Roboto',system-ui,sans-serif;min-height:100vh;margin:0;padding:4.4rem 1.2rem 3rem;color:#e8e8f0;background:radial-gradient(1100px 600px at 50% -10%,rgba(111,76,255,.16),transparent 60%),radial-gradient(900px 500px at 88% 6%,rgba(1,110,218,.12),transparent 55%),radial-gradient(800px 480px at 10% 16%,rgba(217,0,192,.08),transparent 55%),#04050c}
.wrap{max-width:1120px;margin:0 auto}
h1{font-weight:900;font-size:1.5rem;margin:0 0 1rem}
h2{font-weight:700;font-size:1.05rem;margin:1.4rem 0 .6rem;color:#cfe0ff}
a{color:#7aa2f7}
.seg{display:inline-flex;gap:4px;background:rgba(13,14,33,.6);border:1px solid rgba(255,255,255,.1);border-radius:999px;padding:4px;margin:0 0 1.2rem}
.segbtn{background:none;border:none;color:rgba(255,255,255,.7);font:600 13px Roboto,system-ui,sans-serif;padding:7px 20px;border-radius:999px;cursor:pointer;transition:.2s}
.segbtn.active{background:linear-gradient(90deg,#016eda,#d900c0);color:#fff}
.segbtn:hover:not(.active){color:#fff}
.card{background:rgba(13,14,33,.85);border:1px solid rgba(255,255,255,.09);border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.45);padding:1rem 1.2rem;margin:0 0 1rem}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th{text-align:right;color:rgba(255,255,255,.5);font-weight:500;padding:.5rem .6rem;border-bottom:1px solid rgba(255,255,255,.12);white-space:nowrap}
th:first-child{text-align:left}
td{text-align:right;padding:.4rem .6rem;border-bottom:1px solid rgba(255,255,255,.06);white-space:nowrap}
td.lbl{text-align:left;position:relative;max-width:520px;overflow:hidden;text-overflow:ellipsis;color:#fff}
td.ok{color:#8fffca}
td.bad{color:#ff9db2}
.bar{position:absolute;left:0;bottom:1px;height:3px;border-radius:2px;background:linear-gradient(90deg,#016eda,#d900c0);opacity:.85}
.v{display:inline-block;max-width:500px;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom;color:#fff;text-decoration:none}
a.v:hover{text-decoration:underline}
tr.tot td{font-weight:700;color:#cfe0ff;border-top:1px solid rgba(255,255,255,.14)}
tr:hover td{background:rgba(111,76,255,.06)}
.empty{color:rgba(255,255,255,.4);padding:.6rem}
.note{background:rgba(255,196,84,.08);border:1px solid rgba(255,196,84,.32);border-left-width:3px;border-radius:8px;color:rgba(255,214,140,.92);padding:.7rem .9rem;margin:0 0 1rem;font-size:.85rem;line-height:1.55}
.foot{color:rgba(255,255,255,.42);font-size:.78rem;margin-top:1.2rem;line-height:1.6}
.up{color:#8fffca}.down{color:#ff9db2}.dn0{color:rgba(255,255,255,.45)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem;margin:0 0 1.2rem}
.tile{background:rgba(13,14,33,.85);border:1px solid rgba(255,255,255,.09);border-radius:14px;padding:.85rem 1rem}
.tile .k{font:600 11px Roboto,system-ui,sans-serif;color:rgba(255,255,255,.55);text-transform:uppercase;letter-spacing:.06em}
.tile .val{font:900 1.5rem/1.2 Roboto,system-ui,sans-serif;color:#fff;margin:.15rem 0}
.tile .d{font-size:.8rem}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem}
/* Cards in a grid are narrow and the labels are urls, so the table has to take its width from the
   card and not from its content, or a long url sets the minimum width and pushes the count column
   outside the card. table-layout:fixed does that, and it is also what lets the ellipsis already on
   td.lbl apply, since max-width on a cell is ignored under the default auto layout.
   Widths are proportional rather than pinned so headings like Entrances never clip, and .v only has
   its cap relaxed, keeping the inline-block and baseline the other cards are aligned to. */
.grid2 table{table-layout:fixed}
.grid2 th:first-child,.grid2 td.lbl{width:72%}
.grid2 td.lbl{max-width:none}
.grid2 .v{max-width:100%}
details.x{border-bottom:1px solid rgba(255,255,255,.06)}
details.x>summary{cursor:pointer;list-style:none;display:grid;grid-template-columns:1fr 66px 70px 62px 62px 92px 64px;gap:.4rem;padding:.45rem .3rem;align-items:center}
details.x>summary::-webkit-details-marker{display:none}
details.x>summary:hover{background:rgba(111,76,255,.06)}
summary .lbl{position:relative;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#fff}
summary .lbl::before{content:'\\25b8';color:#7aa2f7;margin-right:.35rem;font-size:.8em}
details.x[open]>summary .lbl::before{content:'\\25be'}
summary .n{text-align:right;color:rgba(255,255,255,.85)}
.xhead{display:grid;grid-template-columns:1fr 66px 70px 62px 62px 92px 64px;gap:.4rem;padding:.35rem .3rem;color:rgba(255,255,255,.5);font-size:.78rem;border-bottom:1px solid rgba(255,255,255,.12)}
.xhead span:not(:first-child){text-align:right}
.child{padding:.2rem .3rem .7rem 1.2rem}
.child table{font-size:.84rem}
</style>"""

SWITCHER_JS = """<script>
(function(){var btns=document.querySelectorAll('.segbtn');var V=['daily','weekly','monthly'];
function show(v){V.forEach(function(x){var el=document.getElementById('view-'+x);if(el)el.hidden=(x!==v);});
btns.forEach(function(b){b.classList.toggle('active',b.getAttribute('data-view')===v);});
try{localStorage.setItem('hailsTimeView',v);}catch(e){}}
btns.forEach(function(b){b.onclick=function(){show(b.getAttribute('data-view'));};});
var saved='daily';try{saved=localStorage.getItem('hailsTimeView')||'daily';}catch(e){}
if(V.indexOf(saved)<0)saved='daily';show(saved);})();
</script>
<script src="/nav.js"></script>
<script src="/table.js"></script>"""


def page(sub, views, foot):
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content=\"width=device-width,"
            "initial-scale=1\">\n<title>%s</title>\n%s\n%s\n<div class=wrap>\n<h1>%s</h1>\n"
            "<div class=seg><button class=\"segbtn active\" data-view=daily>Daily</button>"
            "<button class=segbtn data-view=weekly>Weekly</button>"
            "<button class=segbtn data-view=monthly>Monthly</button></div>\n"
            "<div class=view id=view-daily>%s</div>\n"
            "<div class=view id=view-weekly hidden>%s</div>\n"
            "<div class=view id=view-monthly hidden>%s</div>\n"
            "<p class=foot>%s</p>\n</div>\n%s\n") % (
        esc(sub), FAVICON, STYLE, esc(sub),
        coverage_note("daily") + views["daily"],
        coverage_note("weekly") + views["weekly"],
        coverage_note("monthly") + views["monthly"],
        foot, SWITCHER_JS)


FOOT_BASE = ("Daily is the last 24 hours, Weekly the last 7 days, Monthly the last 30 days, or as much "
             "of each as the log holds: a window shorter than its label says so at the top. All traffic "
             "including bots, so totals run higher than the humans only chart dashboard. Valid Requests "
             "are status under 400, Failed Requests are status 400 and above. Click a column heading "
             "to sort by it, click again to reverse, and shift click a second and third heading to "
             "sort by those as tiebreaks.")

# Licence condition of the DB-IP Lite databases: this must stay visible on any page showing country
# or network data.
DBIP_CREDIT = 'IP Geolocation by <a href="https://db-ip.com">DB-IP</a>.'


# ---- renderers ----------------------------------------------------------------------------------
def flat_table(d, by_key=False, link=False):
    if by_key:
        items = sorted(d.items(), key=lambda kv: kv[0])
    else:
        # len(d) below is the real total, since items is already truncated to FLATCAP.
        items = heapq.nlargest(FLATCAP, d.items(), key=lambda kv: kv[1]["hits"])
    if not items:
        return "<div class=card><div class=empty>No requests in this window.</div></div>"
    shown = items[:FLATCAP]
    mx = shown[0][1]["hits"] or 1
    th = tv2 = tf = tb = 0
    tv = set()
    rows = ""
    for k, b in shown:
        th += b["hits"]
        tv2 += b["valid"]
        tf += b["failed"]
        tb += b["bytes"]
        tv |= b["vis"]
        val = dlink(k) if link else ("<span class=v>%s</span>" % esc(k))
        rows += ("<tr><td class=lbl>%s%s</td><td>%s</td><td>%s</td><td class=ok>%s</td>"
                 "<td class=bad>%s</td>%s<td>%s</td></tr>") % (
            val, bar(int(round(100.0 * b["hits"] / mx))), b["hits"], len(b["vis"]),
            b["valid"], b["failed"], bcell(b["bytes"]), avgms(b))
    rows += ("<tr class=tot><td class=lbl>Total (shown)</td><td>%s</td><td>%s</td><td class=ok>%s</td>"
             "<td class=bad>%s</td>%s<td></td></tr>") % (th, len(tv), tv2, tf, bcell(tb))
    note = ""
    if len(d) > FLATCAP:
        note = "<p class=foot>Showing the top %d of %d distinct values by hits.</p>" % (FLATCAP, len(d))
    return ("<div class=\"card sortable\"><div class=tw><table><thead><tr><th>Value</th><th>Hits</th>"
            "<th>Unique Visitors</th><th>Valid</th><th>Failed</th><th>Bandwidth</th><th>Avg ms</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table></div></div>" + note)


def xtab(xt, child_cols, child_row, parent_link=False, parent_reason=False):
    items = heapq.nlargest(XPAR, xt.items(), key=lambda kv: kv[1]["b"]["hits"])
    if not items:
        return "<div class=card><div class=empty>No requests in this window.</div></div>"
    head = ("<div class=xhead><span>Value</span><span>Hits</span><span>Unique</span><span>Valid</span>"
            "<span>Failed</span><span>Bandwidth</span><span>Avg ms</span></div>")
    blocks = ""
    for pk, e in items[:XPAR]:
        b = e["b"]
        label = esc(pk)
        if parent_reason:
            r = reason(pk)
            label = esc(pk) + (" " + esc(r) if r else "")
        ch = sorted(e["ch"].items(), key=lambda kv: kv[1]["hits"], reverse=True)
        crows = ""
        for ck, cb in ch[:XCHILD]:
            crows += child_row(ck, cb)
        cmore = ("<p class=foot>Showing the top %d of %d.</p>" % (XCHILD, len(ch))) if len(ch) > XCHILD else ""
        childtbl = ("<div class=child><div class=tw><table><thead><tr>%s</tr></thead><tbody>%s</tbody>"
                    "</table></div>%s</div>") % (child_cols, crows or
                    "<tr><td class=empty colspan=7>No target url recorded.</td></tr>", cmore)
        blocks += ("<details class=x><summary><span class=lbl>%s</span><span class=n>%s</span>"
                   "<span class=n>%s</span><span class=n>%s</span><span class=n>%s</span>"
                   "%s<span class=n>%s</span></summary>%s</details>") % (
            label, b["hits"], len(b["vis"]), b["valid"], b["failed"], bspan(b["bytes"]), avgms(b),
            childtbl)
    note = ("<p class=foot>Showing the top %d of %d.</p>" % (XPAR, len(xt))) if len(xt) > XPAR else ""
    return "<div class=\"card sortable\">" + head + blocks + "</div>" + note


def child_url_row(uri, cb):
    return ("<tr><td class=lbl>%s</td><td>%s</td><td>%s</td><td class=ok>%s</td><td class=bad>%s</td>"
            "<td>%s</td><td>%s</td></tr>") % (dlink(uri), cb["hits"], len(cb["vis"]), cb["valid"],
                                              cb["failed"], hb(cb["bytes"]), avgms(cb))


CHILD_URL_COLS = ("<th>Target URL</th><th>Hits</th><th>Unique</th><th>Valid</th><th>Failed</th>"
                  "<th>Bandwidth</th><th>Avg ms</th>")


def error_table(err):
    items = heapq.nlargest(FLATCAP, err.items(), key=lambda kv: kv[1]["total"])
    if not items:
        return "<div class=card><div class=empty>No failed requests in this window.</div></div>"
    shown = items[:FLATCAP]
    mx = shown[0][1]["total"] or 1
    rows = ""
    for uri, e in shown:
        tstat = top(e["st"], 1)
        ts_lbl = ""
        if tstat:
            c = tstat[0][0]
            ts_lbl = "%s %s" % (c, reason(c))
        tref = top(e["ref"], 1)
        ref_lbl = esc(tref[0][0]) if tref else "(direct)"
        rows += ("<tr><td class=lbl><span class=v>%s</span>%s</td><td class=bad>%s</td><td>%s</td>"
                 "<td>%s</td><td>%s</td><td>%s</td><td class=lbl>%s</td><td>%s</td></tr>") % (
            dlink(uri), bar(int(round(100.0 * e["total"] / mx))), e["total"], e["c4"], e["c5"],
            esc(ts_lbl), len(e["vis"]), ref_lbl, when(e["last"]))
    return ("<div class=\"card sortable\"><div class=tw><table><thead><tr><th>URL</th><th>Total Errors</th>"
            "<th>4xx</th><th>5xx</th><th>Top Status</th><th>Unique</th><th>Top Referrer</th>"
            "<th>Last Seen</th></tr></thead><tbody>" + rows + "</tbody></table></div></div>")


def trending_block(tnow, tprev, mode):
    uris = set(tnow) | set(tprev)
    rowdata = []
    for u in uris:
        now = tnow[u]["h"] if u in tnow else 0
        prev = tprev.get(u, 0)
        vis = len(tnow[u]["vis"]) if u in tnow else 0
        rowdata.append((u, now, prev, now - prev, vis))
    if mode == "rising":
        rowdata = [r for r in rowdata if r[1] >= 10]
        rowdata.sort(key=lambda r: r[3], reverse=True)
    elif mode == "falling":
        rowdata = [r for r in rowdata if r[2] >= 10]
        rowdata.sort(key=lambda r: r[3])
    else:  # growth
        rowdata = [r for r in rowdata if r[1] >= 10 and r[2] >= 3]
        rowdata.sort(key=lambda r: (r[1] - r[2]) / float(r[2]), reverse=True)
    rowdata = rowdata[:100]
    if not rowdata:
        return "<div class=card><div class=empty>Not enough data for this view yet.</div></div>"
    rows = ""
    for u, now, prev, delta, vis in rowdata:
        if prev == 0:
            pc = "<span class=dn0>new</span>"
            # No prior hits is undefined growth, not zero, so it sorts as a huge percent.
            pcs = 1e9
        else:
            pcs = delta / float(prev) * 100.0
            pc = "<span class=%s>%+.0f%%</span>" % ("up" if delta >= 0 else "down", pcs)
        dcls = "up" if delta >= 0 else "down"
        rows += ("<tr><td class=lbl>%s</td><td>%s</td><td>%s</td><td class=%s>%+d</td>"
                 "<td data-s=\"%.4f\">%s</td><td>%s</td></tr>") % (
            dlink(u), now, prev, dcls, delta, pcs, pc, vis)
    return ("<div class=\"card sortable\"><div class=tw><table><thead><tr><th>URL</th><th>Hits now</th>"
            "<th>Hits prev</th><th>Change</th><th>Percent</th><th>Unique</th></tr></thead><tbody>"
            + rows + "</tbody></table></div></div>")


def trending_view(S, P, w):
    if prev_short(w):
        return ("<div class=card><div class=empty>Trending needs a full preceding %s to compare "
                "against, and the log does not reach back that far yet.</div></div>"
                % WLABEL[w].replace("last ", ""))
    return ("<h2>Rising</h2>" + trending_block(S["tnow"], P["tprev"], "rising") +
            "<h2>Fastest growing</h2>" + trending_block(S["tnow"], P["tprev"], "growth") +
            "<h2>Falling</h2>" + trending_block(S["tnow"], P["tprev"], "falling"))


def content_table(mime):
    items = sorted(mime.items(), key=lambda kv: kv[1]["hits"], reverse=True)
    if not items:
        return "<div class=card><div class=empty>No requests in this window.</div></div>"
    total_b = sum(b["bytes"] for _, b in items) or 1
    shown = items[:FLATCAP]
    mx = shown[0][1]["hits"] or 1
    rows = ""
    for k, b in shown:
        avgb = int(b["bytes"] / b["hits"]) if b["hits"] else 0
        rows += ("<tr><td class=lbl><span class=v>%s</span>%s</td><td>%s</td><td>%s</td>%s"
                 "%s<td>%.1f%%</td></tr>") % (
            esc(k), bar(int(round(100.0 * b["hits"] / mx))), b["hits"], len(b["vis"]),
            bcell(b["bytes"]), bcell(avgb), 100.0 * b["bytes"] / total_b)
    return ("<div class=\"card sortable\"><div class=tw><table><thead><tr><th>Content Type</th><th>Hits</th>"
            "<th>Unique</th><th>Bandwidth</th><th>Avg Bytes</th><th>% of Bandwidth</th></tr></thead>"
            "<tbody>" + rows + "</tbody></table></div></div>")


def hosts_view(hosts):
    items = sorted(hosts.items(), key=lambda kv: kv[1]["hits"], reverse=True)
    if not items:
        return "<div class=card><div class=empty>No requests in this window.</div></div>"

    def tbl(rows_items, title):
        if not rows_items:
            return ""
        mx = rows_items[0][1]["hits"] or 1
        rows = ""
        for ip, b in rows_items:
            rows += ("<tr><td class=lbl><span class=v>%s</span>%s</td><td class=lbl>%s</td>"
                     "<td class=lbl>%s</td><td>%s</td><td class=ok>%s</td><td class=bad>%s</td>"
                     "%s</tr>") % (
                esc(ip), bar(int(round(100.0 * b["hits"] / mx))), esc(country_of(ip)),
                esc(asn_of(ip)), b["hits"], b["valid"], b["failed"], bcell(b["bytes"]))
        return ("<h2>%s</h2><div class=\"card sortable\"><div class=tw><table><thead><tr><th>IP</th><th>Country</th>"
                "<th>Network</th><th>Hits</th><th>Valid</th><th>Failed</th><th>Bandwidth</th></tr>"
                "</thead><tbody>%s</tbody></table></div></div>") % (title, rows)

    by_hits = items[:FLATCAP]
    offenders = sorted([kv for kv in items if kv[1]["failed"] > 0],
                       key=lambda kv: kv[1]["failed"], reverse=True)[:50]
    out = tbl(by_hits, "Top visitors by hits")
    if offenders:
        out += tbl(offenders, "Top offenders by failed requests")
    return out


def entry_view(sess):
    entry = sess["entry"]
    items = sorted(entry.items(), key=lambda kv: kv[1]["n"], reverse=True)
    if not items:
        return "<div class=card><div class=empty>No sessions in this window.</div></div>"
    total = sum(e["n"] for _, e in items) or 1
    mx = items[0][1]["n"] or 1
    rows = ""
    for u, e in items[:FLATCAP]:
        src = top(e["src"], 1)
        src_lbl = esc(src[0][0]) if src else "(direct)"
        rows += ("<tr><td class=lbl>%s%s</td><td>%s</td><td>%s</td><td>%.1f%%</td><td class=lbl>%s</td>"
                 "</tr>") % (dlink(u), bar(int(round(100.0 * e["n"] / mx))), e["n"], len(e["vis"]),
                            100.0 * e["n"] / total, src_lbl)
    return ("<div class=\"card sortable\"><div class=tw><table><thead><tr><th>Entry Page</th><th>Entrances</th>"
            "<th>Unique</th><th>% of Entrances</th><th>Top Source</th></tr></thead><tbody>" + rows +
            "</tbody></table></div></div>")


def exit_view(sess):
    exit_ = sess["exit"]
    pvc = sess["pvc"]
    items = sorted(exit_.items(), key=lambda kv: kv[1]["n"], reverse=True)
    if not items:
        return "<div class=card><div class=empty>No sessions in this window.</div></div>"
    mx = items[0][1]["n"] or 1
    rows = ""
    for u, e in items[:FLATCAP]:
        pv = pvc.get(u, e["n"])
        rate = 100.0 * e["n"] / pv if pv else 0.0
        rows += ("<tr><td class=lbl>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%.1f%%</td></tr>") % (
            dlink(u), bar(int(round(100.0 * e["n"] / mx))), e["n"], len(e["vis"]), pv, rate)
    return ("<div class=\"card sortable\"><div class=tw><table><thead><tr><th>Exit Page</th><th>Exits</th>"
            "<th>Unique</th><th>Pageviews</th><th>Exit Rate</th></tr></thead><tbody>" + rows +
            "</tbody></table></div></div>")


def slow_view(slow):
    rows_data = []
    for u, sl in slow.items():
        if sl["reqs"] < 5:
            continue
        s = sorted(sl["samp"])
        rows_data.append((u, sl["reqs"], sl["err"], s))
    rows_data.sort(key=lambda r: percentile(r[3], 0.95), reverse=True)
    if not rows_data:
        return "<div class=card><div class=empty>Not enough timing data in this window.</div></div>"
    rows = ""
    for u, reqs, err, s in rows_data[:FLATCAP]:
        avg = int(sum(s) / len(s)) if s else 0
        rows += ("<tr><td class=lbl><span class=v>%s</span></td><td>%s</td><td>%s</td><td>%s</td>"
                 "<td>%s</td><td>%s</td><td>%.1f%%</td></tr>") % (
            dlink(u), reqs, ms(avg), ms(percentile(s, 0.5)), ms(percentile(s, 0.95)),
            ms(max(s)), 100.0 * err / reqs if reqs else 0.0)
    return ("<div class=\"card sortable\"><div class=tw><table><thead><tr><th>URL</th><th>Requests</th>"
            "<th>Avg ms</th><th>p50 ms</th><th>p95 ms</th><th>Max ms</th><th>Error %</th></tr></thead>"
            "<tbody>" + rows + "</tbody></table></div></div>")


# ---- overview ----------------------------------------------------------------------------------
def tile(k, val, delta=""):
    return "<div class=tile><div class=k>%s</div><div class=val>%s</div><div class=d>%s</div></div>" % (
        k, val, delta)


def mini(title, target, header, rows):
    return ("<div class=card><h2 style='margin-top:0'><a href='%s'>%s</a></h2><div class=tw><table>"
            "<thead><tr>%s</tr></thead><tbody>%s</tbody></table></div></div>") % (
        target, title, header, rows or "<tr><td class=empty>None yet.</td></tr>")


def overview_view(w):
    S, P = CUR[w], PRV[w]
    uv = len(S["vis"])
    vis = SESS[w]["visits"]
    pv = S["page"]
    tot = S["total"]
    errrate = 100.0 * S["err_n"] / tot if tot else 0.0
    perrrate = 100.0 * P["err_n"] / P["total"] if P["total"] else 0.0
    avg = int(S["durs"] / S["durn"] / 1000.0) if S["durn"] else 0
    ds = sorted(S["dsamp"])
    p95 = ms(percentile(ds, 0.95)) if ds else 0
    tiles = "".join([
        tile("Unique Visitors", "{:,}".format(uv), fmt_delta(uv, len(P["vis"]), w=w)),
        tile("Visits", "{:,}".format(vis), fmt_delta(vis, P.get("visits", 0), w=w)),
        tile("Pageviews", "{:,}".format(pv), fmt_delta(pv, P["page"], w=w)),
        tile("Total Requests", "{:,}".format(tot), fmt_delta(tot, P["total"], w=w)),
        tile("Error Rate", "%.1f%%" % errrate, fmt_delta(errrate, perrrate, invert=True, w=w)),
        tile("Avg Response", "%d ms" % avg, ""),
        tile("p95 Response", "%d ms" % p95, ""),
        tile("Bandwidth", hb(S["bytes"]), fmt_delta(S["bytes"], P["bytes"], w=w)),
    ])
    tiles = "<div class=tiles>%s</div>" % tiles

    def top5(d, keyf=lambda kv: kv[1]["hits"], link=False):
        it = heapq.nlargest(5, d.items(), key=keyf)
        r = ""
        for k, b in it:
            val = dlink(k) if link else "<span class=v>%s</span>" % esc(k)
            h = b["hits"] if isinstance(b, dict) and "hits" in b else b
            r += "<tr><td class=lbl title=\"%s\">%s</td><td>%s</td></tr>" % (esc(k), val, h)
        return r
    pages = top5(S["flat"]["requests"], link=True)
    refs = ""
    for k, e in heapq.nlargest(5, S["xref"].items(), key=lambda kv: kv[1]["b"]["hits"]):
        refs += "<tr><td class=lbl title=\"%s\"><span class=v>%s</span></td><td>%s</td></tr>" % (
            esc(k), esc(k), e["b"]["hits"])
    errs = ""
    for u, e in heapq.nlargest(5, S["err"].items(), key=lambda kv: kv[1]["total"]):
        errs += "<tr><td class=lbl title=\"%s\">%s</td><td class=bad>%s</td></tr>" % (
            esc(u), dlink(u), e["total"])
    ctry = top5(S["flat"]["geo"])
    entries = ""
    for u, e in heapq.nlargest(5, SESS[w]["entry"].items(), key=lambda kv: kv[1]["n"]):
        entries += "<tr><td class=lbl title=\"%s\">%s</td><td>%s</td></tr>" % (
            esc(u), dlink(u), e["n"])
    lists = ("<div class=grid2>"
             + mini("Top Pages", "requests.html", "<th>URL</th><th>Hits</th>", pages)
             + mini("Top Referrers", "referrers.html", "<th>Referrer</th><th>Hits</th>", refs)
             + mini("Error Pages", "error_pages.html", "<th>URL</th><th>Errors</th>", errs)
             + mini("Countries", "geo.html", "<th>Country</th><th>Hits</th>", ctry)
             + mini("Entry Pages", "entry.html", "<th>Entry</th><th>Entrances</th>", entries)
             + "</div>")
    return tiles + lists


# ---- detail.json + detail.html -----------------------------------------------------------------
def build_detail_json():
    out = {}
    udet = CUR["weekly"]["udet"]
    if not udet:
        return out
    errs = CUR["weekly"]["err"]
    blinks = CUR["weekly"]["blink"]
    ranked = sorted(udet.items(), key=lambda kv: kv[1]["hits"], reverse=True)
    for uri, u in ranked[:DETAIL_MAX]:
        if u["hits"] < DETAIL_MIN_HITS:
            break                       # sorted by hits, so everything after this is below it too
        d = DETAIL.get(uri) if DETAIL else None
        avg = int(u["durs"] / u["durn"] / 1000.0) if u["durn"] else 0
        rec = {
            "pv": u["hits"], "uv": len(u["vis"]),
            "entries": d["entries"] if d else 0,
            "exits": d["exits"] if d else 0,
            "avgms": avg,
            "prev": [[str(k), v] for k, v in top(d["prev"], DETAIL_ROWS)] if d else [],
            "next": [[str(k), v] for k, v in top(d["next"], DETAIL_ROWS)] if d else [],
            "ext": [[str(k), v] for k, v in top(d["ext"], DETAIL_ROWS)] if d else [],
            "st": [["%s %s" % (k, reason(k)), v] for k, v in top(u["st"], DETAIL_ROWS)],
            "meth": [[str(k), v] for k, v in top(u["meth"], DETAIL_ROWS)],
            "ctry": [[str(k), v] for k, v in top(u["ctry"], DETAIL_ROWS)],
        }
        rec["nav"] = 1 if (rec["prev"] or rec["next"] or rec["ext"]) else 0

        # Omitted when empty rather than written as an empty list, which keeps this file small.
        if u["asn"]:
            rec["asn"] = [[str(k), v] for k, v in top(u["asn"], DETAIL_ROWS)]
        if u["refs"]:
            rec["refs"] = [[str(k), v] for k, v in top(u["refs"], DETAIL_ROWS)]
        if u["days"]:
            rec["days"] = sorted([[k, v] for k, v in u["days"].items()])

        e = errs.get(uri)
        if e and e["total"]:
            rec["err"] = {
                "total": e["total"], "c4": e["c4"], "c5": e["c5"], "uv": len(e["vis"]),
                "st": [["%s %s" % (k, reason(k)), v] for k, v in top(e["st"], DETAIL_ROWS)],
                "ref": [[str(k), v] for k, v in top(e["ref"], DETAIL_ROWS)],
                "first": when(e["first"]), "last": when(e["last"]),
            }
            n404 = e["st"].get(404, 0)
            if n404:
                rec["nf"] = {"hits": n404,
                             "refs": [[str(k), v] for k, v in top(e["nfref"], DETAIL_ROWS)]}

        bl = blinks.get(uri)
        if bl:
            rec["blink"] = [[str(k), v] for k, v in top(bl, DETAIL_ROWS)]

        out[uri] = rec
    return out


DETAIL_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
__FAVICON__
__STYLE__
<div class=wrap>
<h1>Page detail</h1>
<p class=foot id=foot style="display:none">Before and after navigation is approximated from client IP and time, over __WLABEL__.</p>
<div id=body><div class=card><div class=empty>Loading.</div></div></div>
</div>
<script>
function esc(s){return String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
// Every list carries a line saying what it is counting. A bare heading leaves you guessing whether
// a number is hits, visitors or sessions.
function list(title,arr,note){var r='<div class=card><h2 style="margin-top:0">'+title+'</h2>';
if(note){r+='<p class=foot style="margin:-6px 0 10px">'+note+'</p>';}
if(!arr||!arr.length){return r+'<div class=empty>None.</div></div>';}
var tot=0;arr.forEach(function(x){tot+=x[1];});
var mx=arr[0][1]||1;
r+='<div class=tw><table><tbody>';
arr.forEach(function(x){
  var pct=Math.round(1000*x[1]/tot)/10;
  r+='<tr><td class=lbl><span class=v>'+esc(x[0])+'</span>'
    +'<span class=bar style="width:'+Math.round(100*x[1]/mx)+'%"></span></td>'
    +'<td>'+x[1]+'</td><td class=foot>'+pct+'%</td></tr>';});
return r+'</tbody></table></div></div>';}
// hidden entirely when there is nothing to show, rather than rendering an empty card
function opt(title,arr,note){return (arr&&arr.length)?list(title,arr,note):'';}
var PAGES=null;
function render(){
var el=document.getElementById('body');
var ft=document.getElementById('foot');
var uri=decodeURIComponent(location.hash.slice(1));
if(!uri){document.querySelector('h1').textContent='Page detail';el.innerHTML='<div class=card><div class=empty>Open this page by clicking a URL on any panel.</div></div>';return;}
document.querySelector('h1').textContent=uri;
var d=PAGES?PAGES[uri]:null;
if(!d){ft.style.display='none';el.innerHTML='<div class=card><div class=empty>No detail recorded for this URL. A URL needs at least two hits in __WLABEL__ to get a record.</div></div>';return;}
ft.style.display=d.nav?'':'none';
var e=d.err;
var tiles='<div class=tiles>'
+'<div class=tile><div class=k>Hits</div><div class=val>'+d.pv+'</div></div>'
+'<div class=tile><div class=k>Unique Visitors</div><div class=val>'+d.uv+'</div></div>';
if(d.nav){tiles+='<div class=tile><div class=k>Entrances</div><div class=val>'+d.entries+'</div></div>'
+'<div class=tile><div class=k>Exits</div><div class=val>'+d.exits+'</div></div>';}
tiles+='<div class=tile><div class=k>Avg Response</div><div class=val>'+d.avgms+' ms</div></div>';
if(e){tiles+='<div class=tile><div class=k>Failed</div><div class=val class=bad>'+e.total+'</div></div>';}
tiles+='</div>';

var out=tiles;

// Activity first: it frames everything below it by showing when the traffic actually happened.
if(d.days&&d.days.length){
  var dm=0;d.days.forEach(function(x){if(x[1]>dm)dm=x[1];});
  var rows='';
  d.days.forEach(function(x){rows+='<tr><td class=lbl><span class=v>'+esc(x[0])+'</span>'
    +'<span class=bar style="width:'+Math.round(100*x[1]/(dm||1))+'%"></span></td><td>'+x[1]+'</td></tr>';});
  out+='<div class=card><h2 style="margin-top:0">Requests over time</h2>'
    +'<p class=foot style="margin:-6px 0 10px">Requests per day across the window, all traffic.</p>'
    +'<div class=tw><table><tbody>'+rows+'</tbody></table></div></div>';
}

// Failures, then the two 404 cards, each hidden when it has nothing to say.
if(e){
  out+='<div class=card><h2 style="margin-top:0">Errors</h2>'
    +'<p class=foot style="margin:-6px 0 10px">Every failed request for this URL, status 400 and above. '
    +e.total+' failed, '+e.c4+' client (4xx) and '+e.c5+' server (5xx), affecting '+e.uv+' unique visitors. '
    +'First seen '+esc(e.first)+', last seen '+esc(e.last)+'.</p></div>';
  out+='<div class=grid2>'
    +opt('Failure statuses',e.st,'Which failures, and how often each.')
    +opt('Referrers that hit errors',e.ref,'External sites whose visitors landed on a failure here.')
    +'</div>';
}
if(d.nf){
  out+='<div class=card><h2 style="margin-top:0">404 Not Found</h2>'
    +'<p class=foot style="margin:-6px 0 10px">This URL was requested and did not exist, '+d.nf.hits+' times. '
    +'The list below is who is still linking to it.</p></div>';
  out+='<div class=grid2>'+opt('Linked from',d.nf.refs,'Full referring URL, so you can go and fix the link.')+'</div>';
}
out+='<div class=grid2>'
  +opt('Broken links on this page',d.blink,'404s that visitors reached directly from this page, so the bad link is here.')
  +'</div>';

var grid='<div class=grid2>';
if(d.nav){grid+=list('Came from (internal)',d.prev,'The page viewed immediately before this one, in the same session.')
+list('Went to (internal)',d.next,'The page viewed immediately after this one, in the same session.')
+list('External referrers',d.ext,'Outside sites that sent traffic here, by domain.');}
grid+=opt('Referring URLs',d.refs,'The exact page that linked here, full URL including internal links.')
+list('Status mix',d.st,'Every response status returned for this URL.')
+list('Methods',d.meth,'HTTP methods used against this URL.')
+list('Countries',d.ctry,'Where the requests came from, by country of the client IP.')
+opt('Networks',d.asn,'The network or provider each request came from.')
+'</div>';
el.innerHTML=out+grid;}
fetch('detail.json').then(function(r){return r.json();}).then(function(res){PAGES=res.pages||{};render();})
.catch(function(e){document.getElementById('body').innerHTML='<div class=card><div class=empty>Could not load detail data.</div></div>';});
window.addEventListener('hashchange',render);
</script>
<script src="/nav.js"></script>
"""


# ---- emit ---------------------------------------------------------------------------------------
def write(name, content):
    tmp = os.path.join(OUTDIR, "." + name)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, os.path.join(OUTDIR, name))


def views_from(fn):
    return {w: fn(w) for w in WINS}


write("index.html", page(TITLE + " : Overview", views_from(overview_view),
      "Numbers compare the current window to the one before it. Visits are sessions approximated from "
      "client IP and timestamp gaps of 30 minutes, over all activity including bots. Entry and Exit "
      "pages are based on page views only. " + FOOT_BASE))

FLAT_PAGES = [
    ("requests", "Requested Files", False, True),
    ("static", "Static Files", False, True),
    ("not_found", "404 Not Found", False, True),
    ("mime", "Content Types", False, False),   # skipped below, rendered by content_table instead
    ("tls", "TLS Versions", False, False),
    ("vhosts", "Virtual Hosts", False, False),
    ("geo", "Geo Location", False, False),
    ("asn", "Networks (ASN)", False, False),
    ("os", "Operating Systems", False, False),
    ("browsers", "Browsers", False, False),
    ("days", "Visitors by Day", True, False),
]
for key, label, by_key, link in FLAT_PAGES:
    if key == "mime":
        continue
    foot = (FOOT_BASE + " " + DBIP_CREDIT) if key in ("geo", "asn") else FOOT_BASE
    write(key + ".html", page(TITLE + " : " + label,
          {w: flat_table(CUR[w]["flat"][key], by_key, link) for w in WINS}, foot))

write("hosts.html", page(TITLE + " : Visitors (IPs)",
      {w: hosts_view(CUR[w]["flat"]["hosts"]) for w in WINS},
      "The offenders table ranks IPs by failed requests, which surfaces scanners and abuse. "
      + FOOT_BASE + " " + DBIP_CREDIT))

write("content_types.html", page(TITLE + " : Content Types",
      {w: content_table(CUR[w]["flat"]["mime"]) for w in WINS},
      "Bandwidth is the sum of response sizes. " + FOOT_BASE))

write("referrers.html", page(TITLE + " : Referrers",
      {w: xtab(CUR[w]["xref"], CHILD_URL_COLS, child_url_row) for w in WINS},
      "Outside sites only: our own domains are on the Internal Referrers page. Expand a referrer to "
      "see which pages on your site it drove traffic to. " + FOOT_BASE))
write("ref_sites.html", page(TITLE + " : Referring Sites",
      {w: xtab(CUR[w]["xsite"], CHILD_URL_COLS, child_url_row) for w in WINS},
      "The domain rollup of Referrers, outside sites only. Expand a site to see the landing pages it "
      "sent. " + FOOT_BASE))
write("referrers_internal.html", page(TITLE + " : Internal Referrers",
      {w: xtab(CUR[w]["xref_int"], CHILD_URL_COLS, child_url_row) for w in WINS},
      "Links from our own domains (" + (", ".join(INTERNAL_DOMAINS) or "none configured") + ") and "
      "their subdomains, so one page linking to another shows up here rather than as outside traffic. "
      + FOOT_BASE))
write("ref_sites_internal.html", page(TITLE + " : Internal Sites",
      {w: xtab(CUR[w]["xsite_int"], CHILD_URL_COLS, child_url_row) for w in WINS},
      "The domain rollup of Internal Referrers. Expand a site to see the landing pages it sent. "
      + FOOT_BASE))
write("status.html", page(TITLE + " : Status Codes",
      {w: xtab(CUR[w]["xstatus"], CHILD_URL_COLS, child_url_row, parent_reason=True) for w in WINS},
      "Expand a status code to see exactly which URLs produced it, ranked. " + FOOT_BASE))
write("methods.html", page(TITLE + " : HTTP Methods",
      {w: xtab(CUR[w]["xmethod"], CHILD_URL_COLS, child_url_row) for w in WINS},
      "Expand a method to see its top endpoints. Write methods (POST, PUT, PATCH, DELETE) reveal "
      "form and API traffic and scan patterns. " + FOOT_BASE))

write("error_pages.html", page(TITLE + " : Error Pages",
      {w: error_table(CUR[w]["err"]) for w in WINS},
      "URLs that returned status 400 and above, ranked by total errors. 5xx are server side, 4xx are "
      "client or missing. Top Referrer separates a broken inbound link from your own bug. " + FOOT_BASE))

write("trending.html", page(TITLE + " : Trending",
      {w: trending_view(CUR[w], PRV[w], w) for w in WINS},
      "Non static successful GET requests, this window versus the one before it. Rising is by absolute "
      "change, Fastest growing by percent, Falling flags pages that dropped off. " + FOOT_BASE))

write("entry.html", page(TITLE + " : Entry Pages",
      {w: entry_view(SESS[w]) for w in WINS},
      "The first page of each visit. Sessions are approximated from client IP and 30 minute gaps. " + FOOT_BASE))
write("exit.html", page(TITLE + " : Exit Pages",
      {w: exit_view(SESS[w]) for w in WINS},
      "The last page of each visit, with exit rate as exits over pageviews. Approximated from client "
      "IP and 30 minute gaps. " + FOOT_BASE))

write("slow.html", page(TITLE + " : Slowest Endpoints",
      {w: slow_view(CUR[w]["slow"]) for w in WINS},
      "Server response time in milliseconds, not visitor dwell time. p95 means 95 percent of requests "
      "were faster than this. Only URLs with at least 5 requests are shown. " + FOOT_BASE))

detail_json = build_detail_json()
write("detail.json", json.dumps({"pages": detail_json}))
DETAIL_PAGE = (DETAIL_HTML.replace("__TITLE__", esc(TITLE + " : Page detail"))
               .replace("__WLABEL__", detail_wlabel())
               .replace("__FAVICON__", FAVICON).replace("__STYLE__", STYLE))
write("detail.html", DETAIL_PAGE)


# ---- time breakdown (time.html) -------------------------------------------------------------------
def t_week_label(wk):
    try:
        mon = time.strftime("%Y-%m-%d", time.strptime(wk + "-1", "%G-W%V-%u"))
        return "Week of " + mon + " (" + wk + ")"
    except Exception:
        return wk


def t_table(buckets, order, labelfn):
    mx = max((buckets[k]["hits"] for k in order), default=0) or 1
    th = tva = tf = tb = 0
    tv = set()
    body = ""
    for k in order:
        b = buckets[k]
        th += b["hits"]
        tv |= b["vis"]
        tva += b["valid"]
        tf += b["failed"]
        tb += b["bytes"]
        pct = int(round(100.0 * b["hits"] / mx))
        body += (
            "<tr><td class=lbl>{lab}<span class=bar style='width:{pct}%'></span></td>"
            "<td>{hits}</td><td>{uniq}</td><td class=ok>{ok}</td>"
            "<td class=bad>{bad}</td><td>{bw}</td></tr>"
        ).format(lab=html.escape(labelfn(k)), pct=pct, hits=b["hits"],
                 uniq=len(b["vis"]), ok=b["valid"], bad=b["failed"], bw=hb(b["bytes"]))
    foot = (
        "<tr class=tot><td class=lbl>Total</td><td>{h}</td><td>{u}</td>"
        "<td class=ok>{v}</td><td class=bad>{f}</td><td>{bw}</td></tr>"
    ).format(h=th, u=len(tv), v=tva, f=tf, bw=hb(tb))
    return (
        "<div class=tw><table><thead><tr><th>Bucket</th><th>Hits</th><th>Unique Visitors</th>"
        "<th>Valid Requests</th><th>Failed Requests</th><th>Bandwidth</th></tr></thead>"
        "<tbody>" + body + foot + "</tbody></table></div>"
    )


def t_heatmap():
    mx = max((t_heat[d][h] for d in range(7) for h in range(24)), default=0) or 1
    cols = "".join("<th>%02d</th>" % h for h in range(24))
    rows = ""
    for d in range(7):
        cells = ""
        for h in range(24):
            v = t_heat[d][h]
            inten = v / mx
            if v:
                bg = "background:rgba(111,76,255,%.3f)" % (0.08 + inten * 0.82)
            else:
                bg = "background:rgba(255,255,255,.02)"
            label = str(v) if inten >= 0.15 else ""
            cells += "<td class=hc style='%s' title='%s %02d:00, %d hits'>%s</td>" % (
                bg, DOW[d], h, v, label)
        rows += "<tr><th class=hd>%s</th>%s</tr>" % (DOW[d][:3], cells)
    return ("<div class=tw><table class=heat><thead><tr><th></th>" + cols +
            "</tr></thead><tbody>" + rows + "</tbody></table></div>")


TIME_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>time breakdown</title>
<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,AAABAAEAEBAQAAEABAAoAQAAFgAAACgAAAAQAAAAIAAAAAEABAAAAAAAgAAAAAAAAAAAAAAAEAAAAAAAAADGxsYAWFhYABwcHABfAP8A/9dfAADXrwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIiIiIiIiIiIjMlUkQgAiIiIiIiIiIiIiIzJVJEIAAAIiIiIiIiIiIiMyVSRCAAIiIiIiIiIiIiIRERERERERERERERERERERIiIiIiIiIiIgACVVUiIiIiIiIiIiIiIiIAAlVVIiIiIiIiIiIiIiIhEREREREREREREREREREREAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">
<style>
""" + FONT_CSS + """
*{box-sizing:border-box}
body{font-family:'Roboto',system-ui,sans-serif;min-height:100vh;margin:0;padding:4.4rem 1.2rem 3rem;color:#e8e8f0;background:radial-gradient(1100px 600px at 50% -10%,rgba(111,76,255,.16),transparent 60%),radial-gradient(900px 500px at 88% 6%,rgba(1,110,218,.12),transparent 55%),radial-gradient(800px 480px at 10% 16%,rgba(217,0,192,.08),transparent 55%),#04050c}
.wrap{max-width:1000px;margin:0 auto}
h1{font-weight:900;font-size:1.7rem;margin:0 0 .2rem}
.sub{color:rgba(255,255,255,.55);margin:0 0 1.4rem;font-size:.9rem}
.seg{display:inline-flex;gap:4px;background:rgba(13,14,33,.6);border:1px solid rgba(255,255,255,.1);border-radius:999px;padding:4px;margin:0 0 1.4rem}
.segbtn{background:none;border:none;color:rgba(255,255,255,.7);font:600 13px Roboto,system-ui,sans-serif;padding:7px 20px;border-radius:999px;cursor:pointer;transition:.2s}
.segbtn.active{background:linear-gradient(90deg,#016eda,#d900c0);color:#fff}
.segbtn:hover:not(.active){color:#fff}
.card{background:rgba(13,14,33,.85);border:1px solid rgba(255,255,255,.09);border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.45);padding:1.2rem 1.3rem;margin:0 0 1rem}
.card h2{font-weight:700;font-size:.82rem;letter-spacing:.09em;text-transform:uppercase;color:rgba(255,255,255,.55);margin:0 0 1rem}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th{text-align:right;color:rgba(255,255,255,.5);font-weight:500;padding:.5rem .6rem;border-bottom:1px solid rgba(255,255,255,.12)}
th:first-child{text-align:left}
td{text-align:right;padding:.45rem .6rem;border-bottom:1px solid rgba(255,255,255,.06)}
td.lbl{text-align:left;position:relative;white-space:nowrap;color:#fff}
td.ok{color:#8fffca}
td.bad{color:#ff9db2}
.bar{position:absolute;left:0;bottom:2px;height:3px;border-radius:2px;background:linear-gradient(90deg,#016eda,#d900c0);opacity:.85}
tr.tot td{font-weight:700;color:#cfe0ff;border-top:1px solid rgba(255,255,255,.14)}
tr:hover td{background:rgba(111,76,255,.08)}
table.heat{font-size:.7rem;border-collapse:separate;border-spacing:2px}
table.heat th{padding:.2rem .25rem;color:rgba(255,255,255,.4);font-weight:500;border:none}
table.heat th.hd{text-align:right;color:rgba(255,255,255,.65)}
table.heat td.hc{min-width:26px;text-align:center;color:#fff;padding:.35rem .2rem;border:none;border-radius:4px}
table.heat tr:hover td{background:inherit}
.foot{color:rgba(255,255,255,.4);font-size:.78rem;margin-top:1.4rem;line-height:1.6}
</style>
<div class=wrap>
<h1>Time breakdown</h1>
<p class=sub>__SUB__</p>
<div class=seg>
<button class="segbtn active" data-view=daily>Daily</button>
<button class="segbtn" data-view=weekly>Weekly</button>
<button class="segbtn" data-view=monthly>Monthly</button>
</div>
<div class=view id=view-daily><div class=card><h2>Daily (__LDAY__): visits by hour of day</h2>__HOUR__</div></div>
<div class=view id=view-weekly hidden><div class=card><h2>Weekly (__LWEEK__): visits by day of week</h2>__DAY__</div></div>
<div class=view id=view-monthly hidden><div class=card><h2>Monthly (__LMON__): visits by week</h2>__WEEK__</div></div>
<div class=card><h2>Heatmap (__LMON__): day of week by hour, colored by hits</h2>__HEAT__</div>
<p class=foot>Daily is the last 24 hours by hour, Weekly the last 7 days by day of week, Monthly the last 30 days by week, or as much of each as the log holds: a heading that says only N collected is covering less than its full window, and a dark cell there means no data rather than a quiet hour. The heatmap sums hits by weekday and hour over the last 30 days, so brighter cells are busier times. All traffic including bots, so totals run higher than the humans only chart dashboard. Times in server local time. Valid Requests are responses with status under 400, Failed Requests are status 400 and above.</p>
</div>
<script>
(function(){
var btns=document.querySelectorAll('.segbtn');var V=['daily','weekly','monthly'];
function show(v){V.forEach(function(x){document.getElementById('view-'+x).hidden=(x!==v);});
btns.forEach(function(b){b.classList.toggle('active',b.getAttribute('data-view')===v);});
try{localStorage.setItem('hailsTimeView',v);}catch(e){}}
btns.forEach(function(b){b.onclick=function(){show(b.getAttribute('data-view'));};});
var saved='daily';try{saved=localStorage.getItem('hailsTimeView')||'daily';}catch(e){}
if(V.indexOf(saved)<0)saved='daily';show(saved);
})();
</script>
<script src="/nav.js"></script>
"""

write("time.html", TIME_PAGE.replace("__SUB__", html.escape(TITLE))
      .replace("__LDAY__", wlabel("daily", "last 24 hours"))
      .replace("__LWEEK__", wlabel("weekly", "last 7 days"))
      .replace("__LMON__", wlabel("monthly", "last 30 days"))
      .replace("__HOUR__", t_table(t_hours, list(range(24)), lambda h: "%02d:00" % h))
      .replace("__DAY__", t_table(t_days, list(range(7)), lambda d: DOW[d]))
      .replace("__WEEK__", t_table(t_weeks, sorted(t_weeks.keys()), t_week_label))
      .replace("__HEAT__", t_heatmap()))
