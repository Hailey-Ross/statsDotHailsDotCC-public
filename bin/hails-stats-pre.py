#!/usr/bin/env python3
# Normalize Caddy JSON access log lines for GoAccess, and split them into the derived logs the
# regen needs. Also imported as a module, so everything with a side effect lives inside main().
import sys, json, os, time, re
from urllib.parse import unquote

VER = {768: "SSL3.0", 769: "TLS1.0", 770: "TLS1.1", 771: "TLS1.2", 772: "TLS1.3"}

# Matched against the User-Agent FIELD, never as a substring of the whole line.
PROBE_UA = "hails-perf-probe"

URI_MAX = 512


def safe(h):
    return "".join(c if (c.isascii() and (c.isalnum() or c in ".-")) else "_" for c in h)


def is_probe(o):
    try:
        ua = (o.get("request") or {}).get("headers", {}).get("User-Agent") or []
        return bool(ua) and ua[0] == PROBE_UA
    except Exception:
        return False


CONFIG = os.environ.get("HAILS_CONFIG", "/etc/hails-stats/config.env")


def cfg(key, default=""):
    """One key out of config.env, with the environment winning if it is already set."""
    v = os.environ.get(key)
    if v is not None:
        return v
    try:
        with open(CONFIG) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, val = line.partition("=")
                if k.strip() == key:
                    default = val.strip()
    except OSError:
        pass
    return default


def drop_hosts():
    return [p.strip() for p in cfg("HAILS_DROP_HOSTS").split(",") if p.strip()]


def is_dropped(host, req, prefixes):
    """True if this line must not be counted anywhere. Also strips a dropped Referer."""
    if not prefixes:
        return False
    if isinstance(host, str) and any(host.startswith(p) for p in prefixes):
        return True
    drop_referer(req, prefixes)
    return False


def ref_host(v):
    """Hostname of a Referer value, normalized the same way request hosts are."""
    if not isinstance(v, str):
        return ""
    s = v.split("://", 1)[-1]
    s = s.split("/", 1)[0]
    s = s.rsplit("@", 1)[-1]
    i = s.rfind(":")
    if i > 0 and s[i + 1:].isdigit():
        s = s[:i]
    s = s.lower()
    return s[4:] if s.startswith("www.") else s


def drop_referer(req, prefixes):
    """Remove the Referer header when it points at a dropped host. Mutates req in place."""
    if not prefixes:
        return
    hdrs = req.get("headers")
    if not isinstance(hdrs, dict):
        return
    for key in ("Referer", "Referrer"):
        v = hdrs.get(key)
        first = v[0] if isinstance(v, list) and v else v
        if isinstance(first, str) and any(ref_host(first).startswith(p) for p in prefixes):
            del hdrs[key]


PROBE_TOKENS = (
    ".env", "*.env", ".DS_Store",
    ".git/", ".svn/", ".aws/", ".ssh/", "_profiler/",
    "phpinfo.php", "php-info.php", "phpversion.php", "php.php", "i.php", "info.php", "test.php",
    "shell.php", "eval-stdin.php", "adminer.php", "phpmyadmin",
    "xmlrpc.php", "wp-config.php", "wp-login.php", "wp-admin/", "wp-includes/",
)

PROBE_QUERY_KEYS = ("rest_route",)

PROBE_SENTINEL = "/(probe)"

_PROBE_RE_CACHE = {}


def probe_paths():
    """The HAILS_PROBE_PATHS token list, falling back to PROBE_TOKENS. A value of "none" turns
    masking off; empty means the default list, since that is also what an unset variable looks like."""
    raw = cfg("HAILS_PROBE_PATHS").strip()
    if raw.lower() == "none":
        return ()
    toks = tuple(p.strip() for p in raw.split(",") if p.strip())
    return toks or PROBE_TOKENS


def probe_re(tokens):
    """Compile a token list into the segment anchored matcher, keyed by shape: trailing slash is a
    directory, leading star a suffix, leading dot a dotfile family, anything else a word family.

    EVERY REPEAT HERE MUST STAY UNAMBIGUOUS, or an adversarial path backtracks exponentially.
    """
    hit = _PROBE_RE_CACHE.get(tokens)
    if hit is not None:
        return hit
    parts = []
    for t in tokens:
        if t.endswith("/"):
            parts.append(re.escape(t))
        elif t.startswith("*"):
            parts.append(r"[\w.~-]*" + re.escape(t[1:]) + r"(?=$|/)")
        elif t.startswith("."):
            parts.append(re.escape(t) + r"(?:(?:[._~-]|\d)[\w.~-]*)?(?=$|/)")
        else:
            parts.append(re.escape(t) + r"(?:\.[\w~-]+)*(?=$|/)")
    rx = re.compile(r"(?:^|/)(?:" + "|".join(parts) + r")", re.I) if parts else None
    _PROBE_RE_CACHE[tokens] = rx
    return rx


def probe_path(uri):
    """The comparable path of a uri: query removed, escapes resolved, evasion padding stripped.

    Normalises for MATCHING only. The stored uri is never mutated by this.
    """
    if not isinstance(uri, str):
        return ""
    p = unquote(unquote(uri.split("?", 1)[0])).split("?", 1)[0]
    segs = []
    for s in p.split("/"):
        head = s.split(";", 1)[0]
        s = head if head else s.lstrip(";")
        s = "".join(c for c in s if c >= " ")
        if "::" in s:
            s = s.split("::", 1)[0]
        s = s.strip(" \t").rstrip("~").rstrip(".")
        if s and s != "." and s != "..":
            segs.append(s)
    return "/" + "/".join(segs) + "/"


def probe_query_hit(uri, keys=PROBE_QUERY_KEYS):
    """True if the uri carries a query parameter whose KEY is one of keys. Matched on the key alone,
    never as text anywhere in the uri, so a path or a visitor's search text cannot trigger it."""
    if not isinstance(uri, str) or "?" not in uri or not keys:
        return False
    for part in uri.split("?", 1)[1].split("&"):
        if unquote(part.split("=", 1)[0]).strip().lower() in keys:
            return True
    return False


def mask_probe(req, tokens, sentinel=PROBE_SENTINEL):
    """Collapse a scanner probe to one sentinel uri and drop its Referer. Mutates req in place.

    A MASK, NOT A DROP: the request stays in every count, only its identity goes.

    The sentinel's LEADING SLASH is load bearing, because the aggregate writer prefixes the host and
    hails-panels.py tells the two row shapes apart with startswith("/"). Downstream tests for the
    sentinel must therefore use endswith, not equality.
    """
    if not tokens:
        return False
    u = req.get("uri")
    if not isinstance(u, str):
        return False
    rx = probe_re(tokens)
    if not ((rx and rx.search(probe_path(u))) or probe_query_hit(u)):
        return False
    req["uri"] = sentinel
    hdrs = req.get("headers")
    if isinstance(hdrs, dict):
        for key in ("Referer", "Referrer"):
            if key in hdrs:
                del hdrs[key]
    return True


def normalize(o):
    """Normalize one parsed log object IN PLACE. Returns (request dict, host or None)."""
    try:
        o["ts"] = int(float(o.get("ts", 0)))
    except Exception:
        pass
    try:
        o["duration"] = int(float(o.get("duration", 0)) * 1000000)
    except Exception:
        o["duration"] = 0
    req = o.get("request") or {}
    h = req.get("host")
    if isinstance(h, str):
        i = h.rfind(":")
        if i > 0 and h[i + 1:].isdigit():
            h = h[:i]
        if h.startswith("www."):
            h = h[4:]
        req["host"] = h
    u = req.get("uri")
    if isinstance(u, str) and len(u) > URI_MAX:
        req["uri"] = u[:URI_MAX]
    tls = req.get("tls") or {}
    v = tls.get("version")
    if v in VER:
        tls["version"] = VER[v]
    return req, h


def probe_report(tokens, stream=None, out=None):
    """Dry run: what WOULD mask_probe erase, grouped by token. Writes nothing anywhere.

    Read the status column, not the count: a token matching a 2xx is a false positive, and masking
    cannot be undone once it has run.
    """
    stream = stream if stream is not None else sys.stdin
    out = out if out is not None else sys.stdout
    single = [(t, probe_re((t,))) for t in tokens]
    counts = {}
    for line in stream:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        req = o.get("request") or {}
        u = req.get("uri")
        if not isinstance(u, str):
            continue
        p = probe_path(u)
        for t, rx in single:
            if rx and rx.search(p):
                rec = counts.setdefault(t, {"n": 0, "st": {}, "eg": []})
                break
        else:
            if not probe_query_hit(u):
                continue
            rec = counts.setdefault("(query) " + ",".join(PROBE_QUERY_KEYS),
                                    {"n": 0, "st": {}, "eg": []})
        rec["n"] += 1
        st = o.get("status")
        rec["st"][st] = rec["st"].get(st, 0) + 1
        if len(rec["eg"]) < 5:
            rec["eg"].append("%s%s" % (req.get("host") or "", u[:100]))
    if not counts:
        out.write("no probe matches in this input\n")
        return 0
    out.write("%-22s %8s  %s\n" % ("TOKEN", "HITS", "STATUSES"))

    def served(rec):
        return sum(n for s, n in rec["st"].items() if isinstance(s, int) and 200 <= s < 300)

    for t, rec in sorted(counts.items(), key=lambda kv: (-served(kv[1]), -kv[1]["n"])):
        sts = ", ".join("%s:%d" % (s, n) for s, n in sorted(rec["st"].items(), key=lambda x: -x[1]))
        flag = " <== SERVED 2xx, CHECK THIS" if served(rec) else ""
        out.write("%-22s %8d  %s%s\n" % (t, rec["n"], sts, flag))
        for e in rec["eg"]:
            out.write("%-22s %8s  %s\n" % ("", "", e))
    return 0


def main(argv):
    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default

    if "--probe-report" in argv:
        return probe_report(probe_paths())

    PREFIX = "--prefix-host" in argv
    SPLIT = opt("--split")
    AGGOUT = opt("--agg")
    AGGEX = [p.strip() for p in (opt("--agg-exclude") or "").split(",") if p.strip()]
    DROP = [p.strip() for p in (opt("--drop-host") or "").split(",") if p.strip()]
    PROBE = probe_paths()
    TALLY = opt("--tally")
    tally = {} if TALLY else None
    # Memoised on the MINUTE, not the hour: 30 and 45 minute UTC offsets exist.
    daycache = {}

    def daykey(ts):
        m = ts // 60
        d = daycache.get(m)
        if d is None:
            try:
                d = daycache[m] = time.strftime("%Y-%m-%d", time.localtime(ts))
            except Exception:
                daycache[m] = False
                return None
        return d or None

    handles = {}
    aggfh = None
    if SPLIT:
        os.makedirs(SPLIT, exist_ok=True)
        if AGGOUT:
            aggfh = open(AGGOUT, "w")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if is_probe(o):
            continue
        req, h = normalize(o)
        if is_dropped(h, req, DROP):
            continue
        mask_probe(req, PROBE)
        # Bucketed exactly as hails-rollup.py buckets: same ts guard, same local day key, same
        # defaults. Any drift moves a request between days and the merge keeps the higher value.
        if tally is not None and isinstance(h, str) and h:
            try:
                ts = int(o.get("ts"))
            except Exception:
                ts = 0
            if ts > 0:
                try:
                    by = int(o.get("size") or 0)
                except Exception:
                    by = 0
                day = daykey(ts)
                if day is not None:
                    key = (h, day)
                    rec = tally.get(key)
                    if rec is None:
                        tally[key] = [by, 1]
                    else:
                        rec[0] += by
                        rec[1] += 1
        if not SPLIT:
            if PREFIX:
                u = req.get("uri")
                if isinstance(h, str) and isinstance(u, str):
                    req["uri"] = h + u
            sys.stdout.write(json.dumps(o) + "\n")
            continue
        clean = json.dumps(o)
        if isinstance(h, str) and h:
            fh = handles.get(h)
            if fh is None:
                fh = handles[h] = open(os.path.join(SPLIT, safe(h) + ".log"), "w")
            fh.write(clean + "\n")
            if aggfh is not None and not any(h.startswith(p) for p in AGGEX):
                u = req.get("uri")
                if isinstance(u, str):
                    req["uri"] = h + u
                    aggfh.write(json.dumps(o) + "\n")
                    req["uri"] = u
                else:
                    aggfh.write(json.dumps(o) + "\n")
    if SPLIT:
        # hosts.tsv is the regen script's proof that the split logs are complete, so it is written
        # last, after every other output is closed.
        for fh in handles.values():
            fh.close()
        if aggfh is not None:
            aggfh.close()

    if tally is not None:
        nested = {}
        for (host, day), rec in tally.items():
            nested.setdefault(host, {})[day] = rec
        os.makedirs(os.path.dirname(TALLY) or ".", exist_ok=True)
        ttmp = TALLY + ".tmp"
        with open(ttmp, "w", encoding="utf-8") as tfh:
            json.dump(nested, tfh, separators=(",", ":"), sort_keys=True)
        os.replace(ttmp, TALLY)

    if SPLIT:
        with open(os.path.join(SPLIT, "hosts.tsv"), "w") as m:
            for h in sorted(handles):
                m.write(h + "\t" + safe(h) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
