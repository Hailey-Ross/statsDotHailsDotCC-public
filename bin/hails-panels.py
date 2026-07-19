#!/usr/bin/env python3
# Single pass analytics for one stats scope.
#
#   python3 hails-panels.py "<TITLE>" "<OUTDIR>" < preprocessed.json
#
# Writes every custom page into OUTDIR: the Overview (index.html), the field panels, the cross tab
# panels, Error Pages, Trending, Content Types, Entry/Exit pages, Slowest Endpoints, and a
# detail.json plus detail.html per page drilldown. Every page has a Daily / Weekly / Monthly
# window switcher. Counts all traffic including bots.
# Sessions, visits, entries and exits are inferred from client IP plus timestamp gaps, so they
# are approximate, not beacon accurate.
import sys, json, html, os, time, math, random
from urllib.parse import urlsplit, quote

TITLE = sys.argv[1] if len(sys.argv) > 1 else "All domains (aggregate)"
OUTDIR = sys.argv[2] if len(sys.argv) > 2 else "."
NOW = int(time.time())
DAY_S, WEEK_S, MON_S = 86400, 604800, 2592000
WINS = ("daily", "weekly", "monthly")
SPAN = {"daily": DAY_S, "weekly": WEEK_S, "monthly": MON_S}
CUT = {w: NOW - SPAN[w] for w in WINS}            # current window lower bound
PCUT = {w: NOW - 2 * SPAN[w] for w in WINS}       # previous window lower bound
WLABEL = {"daily": "last 24 hours", "weekly": "last 7 days", "monthly": "last 30 days"}

FLATCAP = 500     # rows shown on a flat panel
XPAR = 200        # parents shown on a cross tab panel
XCHILD = 40       # children shown per cross tab parent
DETAIL_MIN_HITS = 2   # hits a uri needs in the window before it gets a detail record
DETAIL_MAX = 5000     # safety ceiling on detail records, busiest kept first
PVCAP = 200000    # per window pageview buffer ceiling (session approximation)
SAMPCAP = 400     # per uri duration samples for percentiles
DSAMPCAP = 3000   # per window duration samples for the p95 KPI tile
GAP = 1800        # session gap in seconds (30 minutes)
ACC_CAP = 60000   # max distinct keys kept per accumulation dict (memory guard vs high cardinality)
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

# Geo and ASN come from the DB-IP databases, OS and Browser from the User-Agent.
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
        if len(d) >= ACC_CAP:   # memory guard: stop minting new keys past the ceiling
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


def ring(store, key, cap, val):
    # Algorithm R reservoir sampling: a uniform sample of all values seen, not just the most recent.
    lst = store[key]
    n = store.get(key + "_n", 0)
    if len(lst) < cap:
        lst.append(val)
    else:
        j = random.randint(0, n)
        if j < cap:
            lst[j] = val
    store[key + "_n"] = n + 1


FLATKEYS = ["requests", "static", "not_found", "hosts", "mime", "tls", "vhosts", "geo", "asn",
            "os", "browsers", "days"]


def new_state():
    return {"flat": {k: {} for k in FLATKEYS}, "xref": {}, "xsite": {}, "xstatus": {}, "xmethod": {},
            "err": {}, "tnow": {}, "slow": {}, "pv": [], "pv_over": False, "udet": {},
            "vis": set(), "total": 0, "err_n": 0, "bytes": 0, "page": 0,
            "durs": 0, "durn": 0, "dsamp": [], "dsamp_n": 0, "last": {}, "visits": 0}


def new_prev():
    return {"vis": set(), "total": 0, "err_n": 0, "bytes": 0, "page": 0, "durs": 0, "durn": 0,
            "tprev": {}, "last": {}, "visits": 0}


CUR = {w: new_state() for w in WINS}
PRV = {w: new_prev() for w in WINS}


# ---- parse --------------------------------------------------------------------------------------
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
    cw = [w for w in WINS if ts >= CUT[w]]
    pw = [w for w in WINS if PCUT[w] <= ts < CUT[w]]
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
        netloc = netloc.lower().strip()
    extref = netloc if (netloc and netloc != host) else ""
    country = country_of(ip)
    network = asn_of(ip)
    os_s, br_s = os_browser(ua)
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    pageview = (not static) and ok and (ct.startswith("text/html") or ct == "")
    navable = (not static) and ok   # joins the session walk so APIs get navigation, pageview or not

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
        xbump(S["xstatus"], str(st), uri or None, ok, ip, by, du)
        if method:
            xbump(S["xmethod"], method, uri or None, ok, ip, by, du)
        if ref:
            xbump(S["xref"], ref, uri or None, ok, ip, by, du)
            xbump(S["xsite"], netloc, uri or None, ok, ip, by, du)
        if not ok and uri:
            e = S["err"].get(uri)
            if e is None and len(S["err"]) < ACC_CAP:
                e = S["err"][uri] = {"c4": 0, "c5": 0, "total": 0, "st": {}, "ref": {},
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
                e["first"] = min(e["first"], ts)
                e["last"] = max(e["last"], ts)
                if ip:
                    e["vis"].add(ip)
        if uri and not static and ok and method == "GET":
            t = S["tnow"].get(uri)
            if t is None and len(S["tnow"]) < ACC_CAP:
                t = S["tnow"][uri] = {"h": 0, "vis": set()}
            if t is not None:
                t["h"] += 1
                if ip:
                    t["vis"].add(ip)
        # Per uri statistics for the detail drilldown, over every request: any method, any status,
        # any content type, static included. Kept separate from the pageview buffer above, which
        # stays narrow because it also feeds Entry/Exit pages. Being a plain dict rather than a per
        # event buffer, it also needs no session ordering and cannot hit PVCAP.
        if uri:
            ud = S["udet"].get(uri)
            if ud is None and len(S["udet"]) < ACC_CAP:
                ud = S["udet"][uri] = {"hits": 0, "vis": set(), "st": {}, "meth": {}, "ctry": {},
                                       "durs": 0, "durn": 0}
            if ud is not None:
                ud["hits"] += 1
                if ip:
                    ud["vis"].add(ip)
                ud["st"][st] = ud["st"].get(st, 0) + 1
                if method:
                    ud["meth"][method] = ud["meth"].get(method, 0) + 1
                if country:
                    ud["ctry"][country] = ud["ctry"].get(country, 0) + 1
                if du > 0:
                    ud["durs"] += du
                    ud["durn"] += 1

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
            lw = S["last"].get(ip)          # streaming sessions over all activity: a new session on
            if lw is None or ts - lw > GAP:  # a first sighting or a gap over GAP seconds
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
        # The session buffer takes every successful non static request, not only pageviews, so an
        # API endpoint gets real came from and went to chains. The trailing flag marks which events
        # are pageviews, so Entry/Exit pages and their pageview column stay computed from those
        # alone and do not start listing API endpoints.
        if navable:
            if not S["pv_over"]:
                if len(S["pv"]) >= PVCAP:
                    S["pv_over"] = True
                else:
                    S["pv"].append((ip, ts, uri, extref, country, st, method, du,
                                    1 if pageview else 0))

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
        if uri and not static and ok and method == "GET":
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

    # Only navigation lives here. The per uri statistics come from the broad accumulator in the main
    # loop, so keeping duplicates here would cost a second set of visitor IPs per uri for nothing.
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
        # Entry and Exit pages, and the visit count, come from the pageview events alone. A session
        # made only of API calls produces no entry or exit page, and one that begins with an API
        # call still reports its first real page as the entrance.
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
            # Adjacency spans the whole session, so an API call records the page that preceded it.
            for i, e in enumerate(sess):
                u = e[2]
                rec_uri_agg(u, e)
                if i > 0:
                    pu = sess[i - 1][2]
                    detail[u]["prev"][pu] = detail[u]["prev"].get(pu, 0) + 1
                    detail[pu]["next"][u] = detail[pu]["next"].get(u, 0) + 1
            # Entrances and exits stay page based, so they agree with the Entry/Exit panel.
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
    # walk_sessions gives Entry/Exit pages, per uri pageview counts and the weekly detail adjacency.
    # The Visits KPI keeps the streaming all activity count, not the pageview only count returned here.
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
    # a uri value rendered as a link to the per page detail drilldown
    return "<a class=v href='detail.html#%s'>%s</a>" % (quote(str(uri), safe=""), esc(uri))


def bar(pct):
    return "<span class=bar style='width:%d%%'></span>" % pct


def fmt_delta(cur, prev, invert=False):
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
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700;900&display=swap');
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
.foot{color:rgba(255,255,255,.42);font-size:.78rem;margin-top:1.2rem;line-height:1.6}
.up{color:#8fffca}.down{color:#ff9db2}.dn0{color:rgba(255,255,255,.45)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem;margin:0 0 1.2rem}
.tile{background:rgba(13,14,33,.85);border:1px solid rgba(255,255,255,.09);border-radius:14px;padding:.85rem 1rem}
.tile .k{font:600 11px Roboto,system-ui,sans-serif;color:rgba(255,255,255,.55);text-transform:uppercase;letter-spacing:.06em}
.tile .val{font:900 1.5rem/1.2 Roboto,system-ui,sans-serif;color:#fff;margin:.15rem 0}
.tile .d{font-size:.8rem}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem}
/* Cards here are narrow and the labels are urls, so the table has to take its width from the card
   and not from its content, or a long url sets the minimum width and pushes the count column outside
   the card. table-layout:fixed does that, and it is also what lets the ellipsis already on td.lbl
   apply, since max-width on a cell is ignored under the default auto layout.
   Widths are proportional rather than pinned so long headings never clip, and .v only has its cap
   relaxed, keeping the inline-block and baseline the other cards are aligned to. */
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
<script src="/nav.js"></script>"""


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
        esc(sub), FAVICON, STYLE, esc(sub), views["daily"], views["weekly"], views["monthly"],
        foot, SWITCHER_JS)


FOOT_BASE = ("Daily is the last 24 hours, Weekly the last 7 days, Monthly the last 30 days. All traffic "
             "including bots, so totals run higher than the humans only chart dashboard. Valid Requests "
             "are status under 400, Failed Requests are status 400 and above.")

# Required attribution for the DB-IP Lite databases, which are CC BY 4.0. Keep it on any page that
# shows country or network data.
DBIP_CREDIT = 'IP Geolocation by <a href="https://db-ip.com">DB-IP</a>.'


# ---- renderers ----------------------------------------------------------------------------------
def flat_table(d, by_key=False, link=False):
    if by_key:
        items = sorted(d.items(), key=lambda kv: kv[0])
    else:
        items = sorted(d.items(), key=lambda kv: kv[1]["hits"], reverse=True)
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
                 "<td class=bad>%s</td><td>%s</td><td>%s</td></tr>") % (
            val, bar(int(round(100.0 * b["hits"] / mx))), b["hits"], len(b["vis"]),
            b["valid"], b["failed"], hb(b["bytes"]), avgms(b))
    rows += ("<tr class=tot><td class=lbl>Total (shown)</td><td>%s</td><td>%s</td><td class=ok>%s</td>"
             "<td class=bad>%s</td><td>%s</td><td></td></tr>") % (th, len(tv), tv2, tf, hb(tb))
    note = ""
    if len(items) > FLATCAP:
        note = "<p class=foot>Showing the top %d of %d distinct values by hits.</p>" % (FLATCAP, len(items))
    return ("<div class=card><div class=tw><table><thead><tr><th>Value</th><th>Hits</th>"
            "<th>Unique Visitors</th><th>Valid</th><th>Failed</th><th>Bandwidth</th><th>Avg ms</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table></div></div>" + note)


def xtab(xt, child_cols, child_row, parent_link=False, parent_reason=False):
    items = sorted(xt.items(), key=lambda kv: kv[1]["b"]["hits"], reverse=True)
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
                   "<span class=n>%s</span><span class=n>%s</span></summary>%s</details>") % (
            label, b["hits"], len(b["vis"]), b["valid"], b["failed"], hb(b["bytes"]), avgms(b), childtbl)
    note = ("<p class=foot>Showing the top %d of %d.</p>" % (XPAR, len(items))) if len(items) > XPAR else ""
    return "<div class=card>" + head + blocks + "</div>" + note


def child_url_row(uri, cb):
    return ("<tr><td class=lbl>%s</td><td>%s</td><td>%s</td><td class=ok>%s</td><td class=bad>%s</td>"
            "<td>%s</td><td>%s</td></tr>") % (dlink(uri), cb["hits"], len(cb["vis"]), cb["valid"],
                                              cb["failed"], hb(cb["bytes"]), avgms(cb))


CHILD_URL_COLS = ("<th>Target URL</th><th>Hits</th><th>Unique</th><th>Valid</th><th>Failed</th>"
                  "<th>Bandwidth</th><th>Avg ms</th>")


def error_table(err):
    items = sorted(err.items(), key=lambda kv: kv[1]["total"], reverse=True)
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
    return ("<div class=card><div class=tw><table><thead><tr><th>URL</th><th>Total Errors</th>"
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
        else:
            pc = "<span class=%s>%+.0f%%</span>" % ("up" if delta >= 0 else "down",
                                                    delta / float(prev) * 100.0)
        dcls = "up" if delta >= 0 else "down"
        rows += ("<tr><td class=lbl>%s</td><td>%s</td><td>%s</td><td class=%s>%+d</td><td>%s</td>"
                 "<td>%s</td></tr>") % (dlink(u), now, prev, dcls, delta, pc, vis)
    return ("<div class=card><div class=tw><table><thead><tr><th>URL</th><th>Hits now</th>"
            "<th>Hits prev</th><th>Change</th><th>Percent</th><th>Unique</th></tr></thead><tbody>"
            + rows + "</tbody></table></div></div>")


def trending_view(S, P):
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
        rows += ("<tr><td class=lbl><span class=v>%s</span>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                 "<td>%s</td><td>%.1f%%</td></tr>") % (
            esc(k), bar(int(round(100.0 * b["hits"] / mx))), b["hits"], len(b["vis"]),
            hb(b["bytes"]), hb(avgb), 100.0 * b["bytes"] / total_b)
    return ("<div class=card><div class=tw><table><thead><tr><th>Content Type</th><th>Hits</th>"
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
                     "<td>%s</td></tr>") % (
                esc(ip), bar(int(round(100.0 * b["hits"] / mx))), esc(country_of(ip)),
                esc(asn_of(ip)), b["hits"], b["valid"], b["failed"], hb(b["bytes"]))
        return ("<h2>%s</h2><div class=card><div class=tw><table><thead><tr><th>IP</th><th>Country</th>"
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
    return ("<div class=card><div class=tw><table><thead><tr><th>Entry Page</th><th>Entrances</th>"
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
    return ("<div class=card><div class=tw><table><thead><tr><th>Exit Page</th><th>Exits</th>"
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
    return ("<div class=card><div class=tw><table><thead><tr><th>URL</th><th>Requests</th>"
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
        tile("Unique Visitors", "{:,}".format(uv), fmt_delta(uv, len(P["vis"]))),
        tile("Visits", "{:,}".format(vis), fmt_delta(vis, P.get("visits", 0))),
        tile("Pageviews", "{:,}".format(pv), fmt_delta(pv, P["page"])),
        tile("Total Requests", "{:,}".format(tot), fmt_delta(tot, P["total"])),
        tile("Error Rate", "%.1f%%" % errrate, fmt_delta(errrate, perrrate, invert=True)),
        tile("Avg Response", "%d ms" % avg, ""),
        tile("p95 Response", "%d ms" % p95, ""),
        tile("Bandwidth", hb(S["bytes"]), fmt_delta(S["bytes"], P["bytes"])),
    ])
    tiles = "<div class=tiles>%s</div>" % tiles

    def top5(d, keyf=lambda kv: kv[1]["hits"], link=False):
        it = sorted(d.items(), key=keyf, reverse=True)[:5]
        r = ""
        for k, b in it:
            val = dlink(k) if link else "<span class=v>%s</span>" % esc(k)
            h = b["hits"] if isinstance(b, dict) and "hits" in b else b
            # title carries the untruncated value, since these cards ellipsis long urls to fit.
            r += "<tr><td class=lbl title=\"%s\">%s</td><td>%s</td></tr>" % (esc(k), val, h)
        return r
    pages = top5(S["flat"]["requests"], link=True)
    refs = ""
    for k, e in sorted(S["xref"].items(), key=lambda kv: kv[1]["b"]["hits"], reverse=True)[:5]:
        refs += "<tr><td class=lbl title=\"%s\"><span class=v>%s</span></td><td>%s</td></tr>" % (
            esc(k), esc(k), e["b"]["hits"])
    errs = ""
    for u, e in sorted(S["err"].items(), key=lambda kv: kv[1]["total"], reverse=True)[:5]:
        errs += "<tr><td class=lbl title=\"%s\">%s</td><td class=bad>%s</td></tr>" % (
            esc(u), dlink(u), e["total"])
    ctry = top5(S["flat"]["geo"])
    entries = ""
    for u, e in sorted(SESS[w]["entry"].items(), key=lambda kv: kv[1]["n"], reverse=True)[:5]:
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
    # Statistics come from the broad per uri accumulator, so every uri seen often enough gets a
    # record, assets and API endpoints included. Navigation comes from the pageview walk and is
    # merged in only where it exists: computing it over all requests would bury each page's real
    # next hop under its own CSS, JS and images.
    out = {}
    udet = CUR["weekly"]["udet"]
    if not udet:
        return out
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
            "prev": [[str(k), v] for k, v in top(d["prev"], 8)] if d else [],
            "next": [[str(k), v] for k, v in top(d["next"], 8)] if d else [],
            "ext": [[str(k), v] for k, v in top(d["ext"], 8)] if d else [],
            "st": [["%s %s" % (k, reason(k)), v] for k, v in top(u["st"], 8)],
            "meth": [[str(k), v] for k, v in top(u["meth"], 8)],
            "ctry": [[str(k), v] for k, v in top(u["ctry"], 8)],
        }
        # lets the page hide the navigation section rather than drawing empty cards
        rec["nav"] = 1 if (rec["prev"] or rec["next"] or rec["ext"]) else 0
        out[uri] = rec
    return out


DETAIL_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
__FAVICON__
__STYLE__
<div class=wrap>
<h1>Page detail</h1>
<p class=foot id=foot style="display:none">Before and after navigation is approximated from client IP and time, over the last 7 days.</p>
<div id=body><div class=card><div class=empty>Loading.</div></div></div>
</div>
<script>
function esc(s){return String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function list(title,arr){var r='<div class=card><h2 style="margin-top:0">'+title+'</h2>';
if(!arr||!arr.length){return r+'<div class=empty>None.</div></div>';}
r+='<div class=tw><table><tbody>';arr.forEach(function(x){r+='<tr><td class=lbl><span class=v>'+esc(x[0])+'</span></td><td>'+x[1]+'</td></tr>';});
return r+'</tbody></table></div></div>';}
var PAGES=null;
function render(){
var el=document.getElementById('body');
var ft=document.getElementById('foot');
var uri=decodeURIComponent(location.hash.slice(1));
if(!uri){document.querySelector('h1').textContent='Page detail';el.innerHTML='<div class=card><div class=empty>Open this page by clicking a URL on any panel.</div></div>';return;}
document.querySelector('h1').textContent=uri;
var d=PAGES?PAGES[uri]:null;
if(!d){ft.style.display='none';el.innerHTML='<div class=card><div class=empty>No detail recorded for this URL. A URL needs at least two hits in the last 7 days to get a record.</div></div>';return;}
ft.style.display=d.nav?'':'none';
var tiles='<div class=tiles>'
+'<div class=tile><div class=k>Hits</div><div class=val>'+d.pv+'</div></div>'
+'<div class=tile><div class=k>Unique</div><div class=val>'+d.uv+'</div></div>';
if(d.nav){tiles+='<div class=tile><div class=k>Entrances</div><div class=val>'+d.entries+'</div></div>'
+'<div class=tile><div class=k>Exits</div><div class=val>'+d.exits+'</div></div>';}
tiles+='<div class=tile><div class=k>Avg Response</div><div class=val>'+d.avgms+' ms</div></div></div>';
var grid='<div class=grid2>';
if(d.nav){grid+=list('Came from (internal)',d.prev)+list('Went to (internal)',d.next)+list('External referrers',d.ext);}
grid+=list('Status mix',d.st)+list('Methods',d.meth)+list('Countries',d.ctry)+'</div>';
el.innerHTML=tiles+grid;}
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


# Overview
write("index.html", page(TITLE + " : Overview", views_from(overview_view),
      "Numbers compare the current window to the one before it. Visits are sessions approximated from "
      "client IP and timestamp gaps of 30 minutes, over all activity including bots. Entry and Exit "
      "pages are based on page views only. " + FOOT_BASE))

# Flat field panels
FLAT_PAGES = [
    ("requests", "Requested Files", False, True),
    ("static", "Static Files", False, True),
    ("not_found", "404 Not Found", False, True),
    ("mime", "Content Types", False, False),   # replaced below by content_table, kept for safety
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
    # Geo and ASN carry the DB-IP credit. Their licence (CC BY 4.0) requires it to stay visible.
    foot = (FOOT_BASE + " " + DBIP_CREDIT) if key in ("geo", "asn") else FOOT_BASE
    write(key + ".html", page(TITLE + " : " + label,
          {w: flat_table(CUR[w]["flat"][key], by_key, link) for w in WINS}, foot))

# Visitors (IPs) with country/network + offenders
write("hosts.html", page(TITLE + " : Visitors (IPs)",
      {w: hosts_view(CUR[w]["flat"]["hosts"]) for w in WINS},
      "The offenders table ranks IPs by failed requests, which surfaces scanners and abuse. "
      + FOOT_BASE + " " + DBIP_CREDIT))

# Content Types with bandwidth
write("content_types.html", page(TITLE + " : Content Types",
      {w: content_table(CUR[w]["flat"]["mime"]) for w in WINS},
      "Bandwidth is the sum of response sizes. " + FOOT_BASE))

# Cross tabs
write("referrers.html", page(TITLE + " : Referrers",
      {w: xtab(CUR[w]["xref"], CHILD_URL_COLS, child_url_row) for w in WINS},
      "Expand a referrer to see which pages on your site it drove traffic to. " + FOOT_BASE))
write("ref_sites.html", page(TITLE + " : Referring Sites",
      {w: xtab(CUR[w]["xsite"], CHILD_URL_COLS, child_url_row) for w in WINS},
      "The domain rollup of Referrers. Expand a site to see the landing pages it sent. " + FOOT_BASE))
write("status.html", page(TITLE + " : Status Codes",
      {w: xtab(CUR[w]["xstatus"], CHILD_URL_COLS, child_url_row, parent_reason=True) for w in WINS},
      "Expand a status code to see exactly which URLs produced it, ranked. " + FOOT_BASE))
write("methods.html", page(TITLE + " : HTTP Methods",
      {w: xtab(CUR[w]["xmethod"], CHILD_URL_COLS, child_url_row) for w in WINS},
      "Expand a method to see its top endpoints. Write methods (POST, PUT, PATCH, DELETE) reveal "
      "form and API traffic and scan patterns. " + FOOT_BASE))

# Error pages
write("error_pages.html", page(TITLE + " : Error Pages",
      {w: error_table(CUR[w]["err"]) for w in WINS},
      "URLs that returned status 400 and above, ranked by total errors. 5xx are server side, 4xx are "
      "client or missing. Top Referrer separates a broken inbound link from your own bug. " + FOOT_BASE))

# Trending
write("trending.html", page(TITLE + " : Trending",
      {w: trending_view(CUR[w], PRV[w]) for w in WINS},
      "Non static successful GET requests, this window versus the one before it. Rising is by absolute "
      "change, Fastest growing by percent, Falling flags pages that dropped off. " + FOOT_BASE))

# Sessions
write("entry.html", page(TITLE + " : Entry Pages",
      {w: entry_view(SESS[w]) for w in WINS},
      "The first page of each visit. Sessions are approximated from client IP and 30 minute gaps. " + FOOT_BASE))
write("exit.html", page(TITLE + " : Exit Pages",
      {w: exit_view(SESS[w]) for w in WINS},
      "The last page of each visit, with exit rate as exits over pageviews. Approximated from client "
      "IP and 30 minute gaps. " + FOOT_BASE))

# Slow endpoints
write("slow.html", page(TITLE + " : Slowest Endpoints",
      {w: slow_view(CUR[w]["slow"]) for w in WINS},
      "Server response time in milliseconds, not visitor dwell time. p95 means 95 percent of requests "
      "were faster than this. Only URLs with at least 5 requests are shown. " + FOOT_BASE))

# Per page detail
detail_json = build_detail_json()
write("detail.json", json.dumps({"pages": detail_json}))
# detail.html reads detail.json['pages'] at runtime
DETAIL_PAGE = (DETAIL_HTML.replace("__TITLE__", esc(TITLE + " : Page detail"))
               .replace("__FAVICON__", FAVICON).replace("__STYLE__", STYLE))
write("detail.html", DETAIL_PAGE)
