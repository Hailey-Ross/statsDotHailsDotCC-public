#!/usr/bin/env python3
# Network map and per domain sitemaps, generated from the live Caddyfile.
# Reads the Caddyfile, classifies every site block as static, redirect or proxy, and writes:
#   1. a self contained topology map to /srv/stats/map.html, behind the stats basic auth,
#   2. a public sanitized map into the public host's root, and
#   3. a sitemap.xml into each static file_server root.
# Run as root by hails-map.timer.
#
# Branding and the public page read from the environment, normally /etc/hails-stats/config.env:
# HAILS_MAP_ORG, HAILS_MAP_CANON, HAILS_MAP_PUBLIC_HOST and HAILS_MAP_PORTSVC.
#
# Usage:
#   hails-map.py                 production: /srv/stats/map.html + sitemap.xml into each /srv root
#   hails-map.py --dry <outdir>  test: write map.html and <host>.sitemap.xml into <outdir>, touch nothing else
#   hails-map.py --caddyfile <p> override the Caddyfile path (default /etc/caddy/Caddyfile)
import sys, os, re, html, time, glob, json

# Roboto is bundled rather than fetched from Google, so no page makes a third party call.
# One variable file per subset covers every weight from 100 to 900.
FONT_BASE = os.environ.get("HAILS_FONT_BASE", "/fonts").rstrip("/")
FONT_CSS = (
    "@font-face{font-family:'Roboto';font-style:normal;font-weight:100 900;font-display:swap;src:url(%s/roboto-latin-ext.woff2) format('woff2');unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}"
    "@font-face{font-family:'Roboto';font-style:normal;font-weight:100 900;font-display:swap;src:url(%s/roboto-latin.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}"
) % (FONT_BASE, FONT_BASE)

CADDYFILE = "/etc/caddy/Caddyfile"
STATS_DIR = "/srv/stats"
DRY = False
DRYOUT = None
argv = sys.argv[1:]
i = 0
while i < len(argv):
    a = argv[i]
    if a == "--dry":
        DRY = True
        DRYOUT = argv[i + 1] if i + 1 < len(argv) else "."
        i += 2
    elif a == "--caddyfile":
        CADDYFILE = argv[i + 1]
        i += 2
    else:
        i += 1

# Friendly labels for proxied localhost ports, as comma separated port|label pairs:
#   HAILS_MAP_PORTSVC=3000|Link shortener,8002|Radio
# Unlisted ports are drawn with their port number.
PORTSVC = {}
for _pair in os.environ.get("HAILS_MAP_PORTSVC", "").split(","):
    _p = [x.strip() for x in _pair.split("|")]
    if len(_p) == 2 and _p[0] and _p[1]:
        PORTSVC[_p[0]] = _p[1]
PORTSVC.setdefault("8770", "stats admin")

# Branding for both maps. PUBLIC_HOST also decides whether a public page is written at all.
ORG = os.environ.get("HAILS_MAP_ORG", "example")
PUBLIC_HOST = os.environ.get("HAILS_MAP_PUBLIC_HOST", "")
CANON = os.environ.get("HAILS_MAP_CANON", "https://%s/network.html" % (PUBLIC_HOST or "example.com"))
ORGURL = "https://%s/" % (PUBLIC_HOST or "example.com")
ORGID = ORGURL + "#org"


def registrable(host):
    # strip any scheme and trailing port, then take the last two labels
    h = host.split("://", 1)[-1]
    h = h.split(":", 1)[0]
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def parse_blocks(text):
    blocks = []
    depth = 0
    header = None
    body = []
    for raw in text.split("\n"):
        s = raw.strip()
        if depth == 0:
            if not s or s.startswith("#"):
                continue
            if s.endswith("{"):
                header = s[:-1].strip()
                body = []
                depth = 1
            continue
        opens = raw.count("{")
        closes = raw.count("}")
        nd = depth + opens - closes
        if nd <= 0:
            blocks.append((header, body))
            depth = 0
            header = None
            body = []
        else:
            body.append(raw)
            depth = nd
    return blocks


def strip_nested(text):
    # drop the contents of nested brace groups, so top level directives can be told apart from
    # ones that only live inside a handle or handle_errors sub block
    out = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def classify(header, body):
    # returns a list of host dicts for one site block, or None for a global block or snippet
    if not header or header.startswith("("):
        return None
    scheme = ""
    hdr = header
    if "://" in hdr:
        scheme = hdr.split("://", 1)[0]
    hosts = [h.split("://", 1)[-1] for h in re.split(r"[,\s]+", hdr) if h]
    if not hosts:
        return None
    text = "\n".join(body)
    top = strip_nested(text)
    proxied = "/etc/caddy/origin/" in text
    auth = ("stats_auth" in text) or bool(re.search(r"\bbasic_auth", text))
    root = None
    m = re.search(r"\broot\s+\*\s+(\S+)", text)   # any nesting level, not just top level
    if m:
        root = m.group(1)
    redir = None
    m = re.search(r"\bredir\s+(\S+)(?:\s+(\S+))?", top)
    if m:
        redir = (m.group(1), m.group(2) or "302")
    top_proxy = re.search(r"\breverse_proxy\s+(\S+)", top)
    proxies = re.findall(r"\breverse_proxy\s+(\S+)", text)
    subroutes = []
    for hm in re.finditer(r"handle(_path)?\s+(\S+)\s*\{([^}]*)\}", text):
        pm = re.search(r"reverse_proxy\s+(\S+)", hm.group(3))
        if pm:
            subroutes.append((hm.group(2), pm.group(1)))
    has_fs = bool(re.search(r"\bfile_server", text))

    if redir:                       # a top level redirect wins
        role = "redirect"
    elif top_proxy:                 # a top level reverse_proxy beats a nested static root
        role = "proxy"
    elif root or has_fs:            # static file serving, including a handle wrapped root
        role = "static"
    elif proxies:
        role = "proxy"
    else:
        role = "other"

    # the primary upstream is the first reverse_proxy that is not a path scoped subroute target
    sub_ups = {u for _, u in subroutes}
    primary = next((u for u in proxies if u not in sub_ups), proxies[0] if proxies else None)

    out = []
    for host in hosts:
        out.append({"host": host, "scheme": scheme, "regdom": registrable(host), "role": role,
                    "root": root, "redir": redir, "upstream": primary if role == "proxy" else None,
                    "subroutes": subroutes, "proxied": proxied, "auth": auth,
                    "insecure": "tls_insecure_skip_verify" in text})
    return out


def svc_of(upstream):
    if not upstream:
        return ("", "")
    port = upstream.rsplit(":", 1)[-1]
    return (PORTSVC.get(port, "service"), port)


def load_hosts():
    with open(CADDYFILE, encoding="utf-8") as fh:
        text = fh.read()
    hosts = []
    for header, body in parse_blocks(text):
        c = classify(header, body)
        if c:
            hosts.extend(c)
    # stable order: by registrable domain, then host
    hosts.sort(key=lambda h: (h["regdom"], h["host"], h["scheme"]))
    for idx, h in enumerate(hosts):
        h["id"] = idx           # unique per block, so two listeners on the same host do not collide
    return hosts


# ---- SVG topology map ---------------------------------------------------------------------------
HOST_X, HOST_W = 300, 250
SVC_X, SVC_W = 720, 320
NH = 40
ROW = 54
GH = 30
GGAP = 22
CF_ORANGE = "#f6821f"
CF_GREY = "#8a8a9a"


def esc(s):
    return html.escape(str(s))


def node(x, y, w, title, sub, dot=None):
    s = ("<g><rect x='%d' y='%d' rx='11' ry='11' width='%d' height='%d' fill='rgba(13,14,33,.9)' "
         "stroke='rgba(255,255,255,.14)'/>" % (x, y, w, NH))
    s += "<text x='%d' y='%d' fill='#fff' font-weight='700' font-size='13'>%s</text>" % (
        x + 14, y + 17, esc(title))
    if sub:
        s += "<text x='%d' y='%d' fill='rgba(255,255,255,.5)' font-size='10.5'>%s</text>" % (
            x + 14, y + 31, esc(sub))
    if dot:
        s += "<circle cx='%d' cy='%d' r='5' fill='%s'/>" % (x + w - 14, y + 20, dot)
    s += "</g>"
    return s


def build_svg(hosts):
    # host y positions, grouped by registrable domain with a header row per group
    groups = {}
    for h in hosts:
        groups.setdefault(h["regdom"], []).append(h)
    y = 20
    host_y = {}
    headers = []
    for reg in sorted(groups):
        headers.append((reg, y))
        y += GH
        for h in groups[reg]:
            host_y[h["id"]] = y
            y += ROW
        y += GGAP
    hosts_bottom = y
    # a redirect target is a hostname; resolve it to the canonical (non http) node id for that name
    id_of_name = {}
    for h in hosts:
        if h["host"] not in id_of_name or h["scheme"] != "http":
            id_of_name[h["host"]] = h["id"]

    # service / destination nodes (right column): one per static root, one per proxy service
    svc_keys = []          # ordered unique keys
    svc_label = {}
    for h in hosts:
        if h["role"] == "static" and h["root"]:
            k = ("root", h["root"])
            svc_label[k] = (h["root"].split("/")[-1] or h["root"], h["root"])
        elif h["role"] == "proxy" and h["upstream"]:
            name, port = svc_of(h["upstream"])
            k = ("svc", name, port)
            svc_label[k] = (name, h["upstream"])
        else:
            continue
        if k not in svc_keys:
            svc_keys.append(k)
    sy = 20
    svc_y = {}
    for k in svc_keys:
        svc_y[k] = sy
        sy += ROW + 4
    height = max(hosts_bottom, sy) + 20

    parts = ["<svg viewBox='0 0 1080 %d' width='100%%' xmlns='http://www.w3.org/2000/svg' "
             "font-family='Roboto,system-ui,sans-serif'>" % height]
    parts.append("<defs><marker id='ar' markerWidth='9' markerHeight='9' refX='7' refY='3' orient='auto'>"
                 "<path d='M0,0 L7,3 L0,6 Z' fill='#7aa2f7'/></marker>"
                 "<marker id='arr' markerWidth='9' markerHeight='9' refX='7' refY='3' orient='auto'>"
                 "<path d='M0,0 L7,3 L0,6 Z' fill='#c79bff'/></marker>"
                 "<linearGradient id='eg' x1='0' y1='0' x2='1' y2='0'>"
                 "<stop offset='0' stop-color='#016eda'/><stop offset='1' stop-color='#d900c0'/>"
                 "</linearGradient></defs>")

    edges, dashed = [], []
    for h in hosts:
        hy = host_y[h["id"]] + NH / 2
        if h["role"] == "static" and h["root"]:
            k = ("root", h["root"])
        elif h["role"] == "proxy" and h["upstream"]:
            name, port = svc_of(h["upstream"])
            k = ("svc", name, port)
        else:
            k = None
        if k is not None:
            ty = svc_y[k] + NH / 2
            x1, x2 = HOST_X + HOST_W, SVC_X
            dx = (x2 - x1) / 2
            edges.append("<path d='M%d %.1f C%d %.1f, %d %.1f, %d %.1f' fill='none' stroke='url(#eg)' "
                         "stroke-width='1.6' opacity='.8' marker-end='url(#ar)'/>" % (
                             x1, hy, x1 + dx, hy, x2 - dx, ty, x2, ty))
        if h["role"] == "redirect" and h["redir"]:
            tgt = registrable_target(h["redir"][0])
            if tgt in id_of_name:
                ty = host_y[id_of_name[tgt]] + NH / 2
                x = HOST_X
                dashed.append("<path d='M%d %.1f C%d %.1f, %d %.1f, %d %.1f' fill='none' stroke='#c79bff' "
                              "stroke-width='1.4' stroke-dasharray='5 4' opacity='.85' "
                              "marker-end='url(#arr)'/>" % (x, hy, x - 80, hy, x - 80, ty, x, ty))
                dashed.append("<text x='%d' y='%.1f' fill='#c79bff' font-size='10'>%s</text>" % (
                    x - 44, (hy + ty) / 2 - 3, esc(redir_code(h))))
    parts.extend(edges)
    parts.extend(dashed)

    for reg, gy in headers:
        parts.append("<text x='20' y='%d' fill='#cfe0ff' font-weight='800' font-size='13'>%s</text>" % (
            gy + 18, esc(reg)))
    for h in hosts:
        y0 = host_y[h["id"]]
        if h["role"] == "redirect":
            sub = redir_code(h) + " to " + shorten(h["redir"][0])
        elif h["role"] == "proxy":
            name, port = svc_of(h["upstream"])
            sub = "proxy : " + port
        elif h["role"] == "static":
            sub = "static"
        else:
            sub = h["role"]
        dot = CF_ORANGE if h["proxied"] else CF_GREY
        title = ("http : " + h["host"]) if h["scheme"] == "http" else h["host"]
        parts.append(node(HOST_X, y0, HOST_W, title, sub, dot))
    for k in svc_keys:
        name, detail = svc_label[k]
        parts.append(node(SVC_X, svc_y[k], SVC_W, name, detail))
    parts.append("</svg>")
    return "".join(parts)


def registrable_target(target):
    m = re.search(r"://([^/{]+)", target)
    return m.group(1) if m else target


def shorten(target):
    m = re.search(r"://([^/{]+)", target)
    return m.group(1) if m else target


def redir_code(h):
    code = (h.get("redir") or ("", "302"))[1]
    return {"permanent": "301", "temporary": "302"}.get(code, code)


# ---- inventory table ----------------------------------------------------------------------------
def backend_cell(h):
    if h["role"] == "redirect":
        return redir_code(h) + " : " + esc(shorten(h["redir"][0]))
    if h["role"] == "proxy":
        name, port = svc_of(h["upstream"])
        return "proxy : %s (%s)" % (esc(name), esc(h["upstream"]))
    if h["role"] == "static":
        return "static : " + esc(h["root"] or "")
    return esc(h["role"])


def notes_cell(h):
    n = []
    if h["scheme"] == "http":
        n.append("plain HTTP listener")
    if h["insecure"]:
        n.append("internal HTTPS upstream")
    for path, up in h["subroutes"]:
        name, port = svc_of(up)
        n.append("%s : %s (%s)" % (esc(path), esc(name), esc(up)))
    return ", ".join(n)


def build_table(hosts):
    rows = ""
    last = None
    for h in hosts:
        if h["regdom"] != last:
            rows += "<tr class=grp><td colspan=6>%s</td></tr>" % esc(h["regdom"])
            last = h["regdom"]
        cf = "proxied (orange)" if h["proxied"] else "grey (inferred)"
        tls = "Origin CA" if h["proxied"] else "ACME"
        title = ("http : " + h["host"]) if h["scheme"] == "http" else h["host"]
        rows += ("<tr><td class=lbl>%s</td><td>%s</td><td class=lbl>%s</td><td>%s</td><td>%s</td>"
                 "<td class=lbl>%s</td></tr>") % (esc(title), esc(h["role"]), backend_cell(h), tls, cf,
                                                  notes_cell(h))
    return ("<div class=card><div class=tw><table><thead><tr><th>Host</th><th>Role</th>"
            "<th>Backend or Target</th><th>TLS</th><th>Cloudflare</th><th>Notes</th></tr></thead>"
            "<tbody>" + rows + "</tbody></table></div></div>")


# ---- page ---------------------------------------------------------------------------------------
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
h1{font-weight:900;font-size:1.5rem;margin:0 0 .3rem}
.sub{color:rgba(255,255,255,.55);margin:0 0 1.2rem;font-size:.9rem}
a{color:#7aa2f7;text-decoration:none}a:hover{text-decoration:underline}
.card{background:rgba(13,14,33,.85);border:1px solid rgba(255,255,255,.09);border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.45);padding:1rem 1.2rem;margin:0 0 1rem}
.legend{display:flex;gap:1.2rem;flex-wrap:wrap;color:rgba(255,255,255,.7);font-size:.82rem;margin:0 0 1rem}
.legend span{display:inline-flex;align-items:center;gap:.4rem}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th{text-align:left;color:rgba(255,255,255,.5);font-weight:500;padding:.5rem .6rem;border-bottom:1px solid rgba(255,255,255,.12);white-space:nowrap}
td{padding:.4rem .6rem;border-bottom:1px solid rgba(255,255,255,.06);white-space:nowrap}
td.lbl{color:#fff}
tr.grp td{font-weight:800;color:#cfe0ff;background:rgba(111,76,255,.08);border-top:1px solid rgba(255,255,255,.14)}
tr:hover td{background:rgba(111,76,255,.06)}
.foot{color:rgba(255,255,255,.42);font-size:.78rem;margin-top:1rem;line-height:1.6}
</style>"""

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
__FAVICON__
__STYLE__
<div class=wrap>
<h1>Network map</h1>
<p class=sub>__SUB__</p>
<div class=legend>
<span><span class=dot style="background:#f6821f"></span>Cloudflare proxied (orange)</span>
<span><span class=dot style="background:#8a8a9a"></span>grey, DNS only (inferred)</span>
<span><span class=dot style="background:#7aa2f7"></span>solid edge : reverse proxy or static root</span>
<span><span class=dot style="background:#c79bff"></span>dashed edge : 301 redirect</span>
</div>
<div class=card>__SVG__</div>
<h1 style="font-size:1.1rem;margin:1.4rem 0 .6rem">Inventory</h1>
__TABLE__
<p class=foot>Generated from the live Caddyfile on __STAMP__. Cloudflare proxied versus grey is inferred from whether a host uses a Cloudflare Origin CA certificate, not confirmed from DNS. All times server local.</p>
</div>
<script src="/nav.js"></script>
"""


def build_page(hosts):
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    regs = sorted({h["regdom"] for h in hosts})
    sub = "%d hosts across %d domains: %s. Links show how each hostname routes through Caddy." % (
        len(hosts), len(regs), ", ".join(regs))
    return (PAGE.replace("__TITLE__", esc(ORG) + " network map").replace("__FAVICON__", FAVICON)
                .replace("__STYLE__", STYLE).replace("__SUB__", esc(sub))
                .replace("__SVG__", build_svg(hosts)).replace("__TABLE__", build_table(hosts))
                .replace("__STAMP__", stamp))


# Public sanitized map, for search engines.


def public_hosts(hosts):
    # only hosts a visitor could reach: auth walled hosts are dropped and each hostname appears
    # once, preferring the HTTPS block over a plain HTTP listener
    seen = {}
    for h in hosts:
        if h["auth"]:
            continue
        if h["host"] not in seen or h["scheme"] != "http":
            seen[h["host"]] = h
    return sorted(seen.values(), key=lambda x: (x["regdom"], x["host"]))


def public_role(h):
    if h["role"] == "redirect":
        return ("redirect", "redirects to " + shorten(h["redir"][0]))
    if h["role"] == "static":
        return ("website", "website")
    if h["role"] == "proxy":
        # lowercase the HAILS_MAP_PORTSVC label so it reads as a role, unlabelled ports stay "service"
        name, _ = svc_of(h["upstream"])
        return ("service", name.lower() if name and not name[0].isdigit() else "service")
    return ("site", "site")


def public_link(h):
    if h["role"] == "redirect":
        return "https://" + shorten(h["redir"][0])
    return "https://" + h["host"] + "/"


def build_public_svg(hosts):
    groups = {}
    for h in hosts:
        groups.setdefault(h["regdom"], []).append(h)
    y = 20
    hy = {}
    headers = []
    for reg in sorted(groups):
        headers.append((reg, y))
        y += GH
        for h in groups[reg]:
            hy[h["id"]] = y
            y += ROW
        y += GGAP
    height = y + 10
    id_of_name = {h["host"]: h["id"] for h in hosts}
    NX = 300
    parts = ["<svg viewBox='0 0 760 %d' width='100%%' xmlns='http://www.w3.org/2000/svg' "
             "font-family='Roboto,system-ui,sans-serif'>" % height]
    parts.append("<defs><marker id='pa' markerWidth='9' markerHeight='9' refX='7' refY='3' orient='auto'>"
                 "<path d='M0,0 L7,3 L0,6 Z' fill='#c79bff'/></marker></defs>")
    for h in hosts:
        if h["role"] == "redirect" and h["redir"]:
            tgt = registrable_target(h["redir"][0])
            if tgt in id_of_name and id_of_name[tgt] in hy:
                y1 = hy[h["id"]] + NH / 2
                y2 = hy[id_of_name[tgt]] + NH / 2
                x = NX + HOST_W
                parts.append("<path d='M%d %.1f C%d %.1f, %d %.1f, %d %.1f' fill='none' stroke='#c79bff' "
                             "stroke-width='1.4' stroke-dasharray='5 4' opacity='.85' "
                             "marker-end='url(#pa)'/>" % (x, y1, x + 80, y1, x + 80, y2, x, y2))
                parts.append("<text x='%d' y='%.1f' fill='#c79bff' font-size='10'>%s</text>" % (
                    x + 30, (y1 + y2) / 2 - 3, esc(redir_code(h))))
    for reg, gy in headers:
        parts.append("<text x='20' y='%d' fill='#cfe0ff' font-weight='800' font-size='13'>%s</text>" % (
            gy + 18, esc(reg)))
    for h in hosts:
        role, sub = public_role(h)
        parts.append(node(NX, hy[h["id"]], HOST_W, h["host"], sub))
    parts.append("</svg>")
    return "".join(parts)


def build_public_table(hosts):
    rows = ""
    last = None
    for h in hosts:
        if h["regdom"] != last:
            rows += "<tr class=grp><td colspan=3>%s</td></tr>" % esc(h["regdom"])
            last = h["regdom"]
        role, desc = public_role(h)
        rows += ("<tr><td class=lbl><a href='%s' rel='me'>%s</a></td><td>%s</td><td class=lbl>%s</td></tr>") % (
            esc(public_link(h)), esc(h["host"]), esc(role), esc(desc))
    return ("<div class=card><div class=tw><table><thead><tr><th>Site</th><th>Type</th>"
            "<th>What it is</th></tr></thead><tbody>" + rows + "</tbody></table></div></div>")


def build_jsonld(hosts):
    regs = sorted({h["regdom"] for h in hosts})
    same = ["https://%s/" % r for r in regs]
    items = []
    for i, h in enumerate(hosts, 1):
        _, desc = public_role(h)
        items.append({"@type": "ListItem", "position": i,
                      "item": {"@type": "WebSite", "name": h["host"], "url": public_link(h),
                               "description": desc}})
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "@id": ORGID, "name": ORG,
         "url": ORGURL, "sameAs": same},
        {"@type": "CollectionPage", "@id": CANON, "name": ORG + " network", "url": CANON,
         "about": {"@id": ORGID},
         "description": "The " + ORG + " network of sites and how they connect.",
         "mainEntity": {"@type": "ItemList", "itemListElement": items}}]}
    return '<script type="application/ld+json">%s</script>' % json.dumps(graph)


PUBPAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>__ORG__ network : sites and how they connect</title>
<meta name=description content="__DESC__">
<link rel=canonical href="__CANON__">
<meta name=robots content="index,follow">
<meta property="og:type" content="website">
<meta property="og:title" content="__ORG__ network">
<meta property="og:description" content="__DESC__">
<meta property="og:url" content="__CANON__">
__FAVICON__
__JSONLD__
__STYLE__
<div class=wrap>
<h1>__ORG__ network</h1>
<p class=sub>__SUB__</p>
<div class=legend><span><span class=dot style="background:#c79bff"></span>dashed edge : redirect</span></div>
<div class=card>__SVG__</div>
<h1 style="font-size:1.1rem;margin:1.4rem 0 .6rem">Sites</h1>
__TABLE__
<p class=foot>The sites in the __ORG__ network and how they link together. Generated on __STAMP__.</p>
</div>
"""


def build_public_page(hosts):
    ph = public_hosts(hosts)
    stamp = time.strftime("%Y-%m-%d", time.localtime())
    regs = sorted({h["regdom"] for h in ph})
    desc = "The %s network: %s and how they connect." % (ORG, ", ".join(regs))
    sub = "%d sites across %d domains, and how they link together." % (len(ph), len(regs))
    return (PUBPAGE.replace("__ORG__", esc(ORG))
                   .replace("__DESC__", esc(desc)).replace("__CANON__", esc(CANON))
                   .replace("__FAVICON__", FAVICON).replace("__JSONLD__", build_jsonld(ph))
                   .replace("__STYLE__", STYLE).replace("__SUB__", esc(sub))
                   .replace("__SVG__", build_public_svg(ph)).replace("__TABLE__", build_public_table(ph))
                   .replace("__STAMP__", stamp))


def public_root(hosts):
    """Static root of HAILS_MAP_PUBLIC_HOST, or None if it is unset and no public page is wanted."""
    if not PUBLIC_HOST:
        return None
    for h in hosts:
        if h["host"] == PUBLIC_HOST and h["role"] == "static" and h["root"]:
            return h["root"]
    return None


# ---- sitemaps -----------------------------------------------------------------------------------
def build_sitemaps(hosts):
    # group static hosts by root dir. STATS_DIR is skipped so the private dashboard is never indexed.
    by_root = {}
    for h in hosts:
        if h["role"] != "static" or not h["root"]:
            continue
        if h["root"].rstrip("/") == STATS_DIR:
            continue
        by_root.setdefault(h["root"], []).append(h["host"])
    written = []
    for root, hs in by_root.items():
        canon = sorted(hs, key=lambda x: (len(x), x))[0]   # shortest hostname is canonical
        if not os.path.isdir(root):
            continue
        urls = []
        for path in sorted(glob.glob(os.path.join(root, "**", "*.html"), recursive=True)):
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if rel.endswith("index.html"):
                loc = rel[:-len("index.html")]
            else:
                loc = rel
            loc = "/" + loc.lstrip("/")
            try:
                lastmod = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))
            except OSError:
                lastmod = ""
            urls.append((("https://%s%s" % (canon, loc)), lastmod))
        if not urls:
            continue
        body = ['<?xml version="1.0" encoding="UTF-8"?>',
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for loc, lastmod in urls:
            u = "  <url><loc>%s</loc>" % html.escape(loc)
            if lastmod:
                u += "<lastmod>%s</lastmod>" % lastmod
            u += "</url>"
            body.append(u)
        body.append("</urlset>\n")
        xml = "\n".join(body)
        if DRY:
            dest = os.path.join(DRYOUT, canon + ".sitemap.xml")
        else:
            dest = os.path.join(root, "sitemap.xml")
        write_file(dest, xml)
        written.append((canon, dest, len(urls)))
    return written


def write_file(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


# ---- main ---------------------------------------------------------------------------------------
def main():
    hosts = load_hosts()
    if DRY and not os.path.isdir(DRYOUT):
        os.makedirs(DRYOUT)
    # private full map, behind the stats basic auth
    map_out = os.path.join(DRYOUT, "map.html") if DRY else os.path.join(STATS_DIR, "map.html")
    write_file(map_out, build_page(hosts))
    # public sanitized map, written into the public host's static root.
    # must run before build_sitemaps so the sitemap walk picks it up.
    proot = public_root(hosts)
    pub_out = None
    if proot:
        pub_out = os.path.join(DRYOUT, "network.html") if DRY else os.path.join(proot, "network.html")
        write_file(pub_out, build_public_page(hosts))
    sm = build_sitemaps(hosts)
    print("map: %s (%d hosts)" % (map_out, len(hosts)))
    if pub_out:
        print("public map: %s" % pub_out)
    for canon, dest, n in sm:
        print("sitemap: %s -> %s (%d urls)" % (canon, dest, n))


if __name__ == "__main__":
    main()
