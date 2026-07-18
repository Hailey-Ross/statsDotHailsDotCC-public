#!/usr/bin/env python3
# Normalize Caddy JSON access log lines on stdin for GoAccess, writing them to stdout:
#   ts to int epoch, duration to integer microseconds, TLS version number to name, host stripped of
#   a trailing :port and a leading www.
# With --prefix-host it also prepends the host to the request uri, so the aggregate URL panels show
#   which domain each row belongs to. Aggregate scope only, never the per domain scopes.
# Deployed to the server at /usr/local/bin/hails-stats-pre.py
import sys, json
PREFIX = "--prefix-host" in sys.argv[1:]
VER={768:"SSL3.0",769:"TLS1.0",770:"TLS1.1",771:"TLS1.2",772:"TLS1.3"}
# Drop the uptime probe's own requests so the monitor does not rank as a top visitor.
# Must match the User-Agent hails-perf-collect.py sends, and must match on the User-Agent FIELD, not
# as a substring of the whole line, or real visits whose url or referer contain the string vanish.
PROBE_UA = "hails-perf-probe"
def is_probe(o):
    try:
        ua = (o.get("request") or {}).get("headers", {}).get("User-Agent") or []
        return bool(ua) and ua[0] == PROBE_UA
    except Exception:
        return False
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    try: o=json.loads(line)
    except Exception: continue
    if is_probe(o): continue
    try: o["ts"]=int(float(o.get("ts",0)))
    except Exception: pass
    try: o["duration"]=int(float(o.get("duration",0))*1000000)
    except Exception: o["duration"]=0
    req=o.get("request") or {}
    # Collapse host variants into one domain: strip a trailing :port and a leading www.
    h=req.get("host")
    if isinstance(h,str):
        i=h.rfind(":")
        if i>0 and h[i+1:].isdigit(): h=h[:i]
        if h.startswith("www."): h=h[4:]
        req["host"]=h
    if PREFIX:
        u=req.get("uri")
        if isinstance(h,str) and isinstance(u,str):
            req["uri"]=h+u
    tls=req.get("tls") or {}
    v=tls.get("version")
    if v in VER: tls["version"]=VER[v]
    sys.stdout.write(json.dumps(o)+"\n")
