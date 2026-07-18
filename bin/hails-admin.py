#!/usr/bin/env python3
# Stats admin backend: a JSON API for managing dashboard logins and running admin actions
# (rebuild now, refresh GeoIP), plus a read only status. Run as root by hails-admin.service,
# since it edits the Caddy auth import and reloads Caddy.
#
# It binds 127.0.0.1 only and trusts Caddy to enforce basic_auth and pass the name in X-Auth-User,
# so it must never be exposed directly. Mutating endpoints additionally require an admin actor,
# the X-Admin-CSRF header, and the actor's own password. Commands run as argument arrays, never a shell.

import json, os, re, subprocess, tempfile, time, threading, warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUTH_FILE = "/etc/caddy/stats_auth"        # imported inside the dashboard basic_auth block
ADMINS_FILE = "/etc/caddy/stats_admins"    # one admin username per line
CADDYFILE = "/etc/caddy/Caddyfile"
LOG_FILE = "/var/log/hails-admin.log"
PORT = 8770
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
HASH_RE = re.compile(r"^\s*(\S+)\s+(\$2[aby]\$\S+)\s*$")
_lock = threading.Lock()


def log(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def read_users():
    users = {}
    try:
        with open(AUTH_FILE) as f:
            for line in f:
                m = HASH_RE.match(line)
                if m:
                    users[m.group(1)] = m.group(2)
    except FileNotFoundError:
        pass
    return users


def write_users(users):
    # Regenerate the whole basic_auth block atomically so Caddy can import it verbatim.
    body = "basic_auth {\n"
    for name in sorted(users):
        body += "\t%s %s\n" % (name, users[name])
    body += "}\n"
    d = os.path.dirname(AUTH_FILE)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, AUTH_FILE)
        os.chmod(AUTH_FILE, 0o640)
        try:
            import shutil
            shutil.chown(AUTH_FILE, "root", "caddy")
        except Exception:
            pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def read_admins():
    s = set()
    try:
        with open(ADMINS_FILE) as f:
            for line in f:
                n = line.strip()
                if n and not n.startswith("#"):
                    s.add(n)
    except FileNotFoundError:
        pass
    return s


def write_admins(admins):
    d = os.path.dirname(ADMINS_FILE)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as f:
        for n in sorted(admins):
            f.write(n + "\n")
    os.replace(tmp, ADMINS_FILE)
    os.chmod(ADMINS_FILE, 0o640)


def verify(name, password):
    users = read_users()
    h = users.get(name)
    if not h or not isinstance(password, str) or not password:
        return False
    # Prefer the bcrypt module, fall back to crypt. crypt is removed in Python 3.13, so the
    # bcrypt module is the path that keeps working on newer Python.
    try:
        import bcrypt
        return bcrypt.checkpw(password.encode(), h.encode())
    except ImportError:
        pass
    except Exception:
        return False
    try:
        import crypt
        return crypt.crypt(password, h) == h
    except Exception:
        return False


def hash_pw(password):
    out = subprocess.run(["caddy", "hash-password", "--plaintext", password],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError("hash-password failed")
    return out.stdout.strip()


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def caddy_reload(prev_auth):
    # Validate the Caddyfile, fix log ownership, then reload. Rolls the auth file back if validation fails.
    v = run(["caddy", "validate", "--config", CADDYFILE, "--adapter", "caddyfile"])
    if v.returncode != 0:
        with open(AUTH_FILE, "w") as f:
            f.write(prev_auth)
        return False, "caddy validate failed, rolled back: " + (v.stderr or v.stdout)[-400:]
    run(["chown", "-R", "caddy:caddy", "/var/log/caddy"])
    r = run(["systemctl", "reload", "caddy"])
    if r.returncode != 0:
        return False, "caddy reload failed: " + (r.stderr or r.stdout)[-400:]
    return True, "ok"


def status():
    users = read_users()
    admins = read_admins()
    geoip = ""
    try:
        geoip = os.path.basename(os.readlink("/var/lib/GeoIP/dbip-country.mmdb"))
    except Exception:
        pass
    pages = 0
    for root, _dirs, files in os.walk("/srv/stats"):
        pages += sum(1 for x in files if x.endswith(".html"))
    last = ""
    try:
        last = time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(os.path.getmtime("/srv/stats/all/index.html")))
    except Exception:
        pass

    def active(unit):
        return run(["systemctl", "is-active", unit]).stdout.strip()

    return {
        "users": [{"name": n, "admin": n in admins} for n in sorted(users)],
        "pages": pages,
        "geoip": geoip,
        "lastRebuild": last,
        "timers": {"stats": active("hails-stats.timer"), "geoip": active("hails-geoip.timer")},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "hails-admin"

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _actor(self):
        return self.headers.get("X-Auth-User", "")

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # Gate a mutating request: admin actor, CSRF header, and a valid actor password.
    def _guard(self, data):
        actor = self._actor()
        if not actor or actor not in read_admins():
            self._send(403, {"error": "admin only"}); return None
        if self.headers.get("X-Admin-CSRF") != "1":
            self._send(403, {"error": "missing csrf header"}); return None
        if not verify(actor, data.get("authPassword", "")):
            self._send(403, {"error": "password check failed"}); return None
        return actor

    def do_GET(self):
        if self.path == "/admin/api/whoami":
            actor = self._actor()
            return self._send(200, {"user": actor, "isAdmin": actor in read_admins()})
        if self.path == "/admin/api/status":
            actor = self._actor()
            if actor not in read_admins():
                return self._send(403, {"error": "admin only"})
            return self._send(200, status())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        with _lock:
            self._post()

    def _post(self):
        data = self._body()
        p = self.path
        if p == "/admin/api/rebuild":
            if not self._guard(data): return
            run(["systemctl", "start", "hails-stats.service"], timeout=180)
            log("%s rebuild" % self._actor())
            return self._send(200, {"ok": True})
        if p == "/admin/api/geoip":
            if not self._guard(data): return
            run(["systemctl", "start", "hails-geoip.service"], timeout=180)
            log("%s geoip refresh" % self._actor())
            return self._send(200, {"ok": True})

        # User handling endpoints all edit the auth import then reload Caddy.
        if p in ("/admin/api/users/add", "/admin/api/users/remove",
                 "/admin/api/users/password", "/admin/api/users/role"):
            actor = self._guard(data)
            if not actor: return
            username = (data.get("username") or "").strip()
            if not USER_RE.match(username):
                return self._send(400, {"error": "invalid username"})
            users = read_users()
            admins = read_admins()
            prev = "basic_auth {\n" + "".join("\t%s %s\n" % (n, users[n]) for n in sorted(users)) + "}\n"

            if p == "/admin/api/users/add":
                npw = data.get("newPassword") or ""
                if not (8 <= len(npw) <= 128):
                    return self._send(400, {"error": "password must be 8 to 128 characters"})
                users[username] = hash_pw(npw)
                write_users(users)
                ok, msg = caddy_reload(prev)
                if not ok: return self._send(500, {"error": msg})
                log("%s add user %s" % (actor, username))
                return self._send(200, {"ok": True})

            if p == "/admin/api/users/remove":
                if username not in users:
                    return self._send(404, {"error": "no such user"})
                if len(users) <= 1:
                    return self._send(400, {"error": "cannot remove the last user"})
                if username in admins and len(admins) <= 1:
                    return self._send(400, {"error": "cannot remove the last admin"})
                del users[username]
                write_users(users)
                ok, msg = caddy_reload(prev)
                if not ok: return self._send(500, {"error": msg})
                if username in admins:
                    admins.discard(username); write_admins(admins)
                log("%s remove user %s" % (actor, username))
                return self._send(200, {"ok": True})

            if p == "/admin/api/users/password":
                if username not in users:
                    return self._send(404, {"error": "no such user"})
                npw = data.get("newPassword") or ""
                if not (8 <= len(npw) <= 128):
                    return self._send(400, {"error": "password must be 8 to 128 characters"})
                users[username] = hash_pw(npw)
                write_users(users)
                ok, msg = caddy_reload(prev)
                if not ok: return self._send(500, {"error": msg})
                log("%s reset password for %s" % (actor, username))
                return self._send(200, {"ok": True})

            if p == "/admin/api/users/role":
                if username not in users:
                    return self._send(404, {"error": "no such user"})
                make_admin = bool(data.get("admin"))
                if not make_admin and username in admins and len(admins) <= 1:
                    return self._send(400, {"error": "cannot demote the last admin"})
                if make_admin:
                    admins.add(username)
                else:
                    admins.discard(username)
                write_admins(admins)
                log("%s set admin=%s for %s" % (actor, make_admin, username))
                return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass  # stay quiet, actions are logged to LOG_FILE


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
