#!/bin/bash
# Deploy the analytics pipeline to your server. Run it from your own machine:  bash deploy.sh
# (Git Bash on Windows, or any shell with ssh and scp on Linux or macOS.)
#
# It deploys the scripts, GoAccess config, systemd units, and GeoIP databases, then rebuilds the
# dashboard. It does NOT rewrite /etc/caddy/Caddyfile, since that file usually fronts every other
# site on the box; instead it CHECKS that the required blocks are present and warns if they are not.
#
# Config (server address + SSH key) is read from a local .env file that is NOT committed.
# First time:  cp .env.example .env  and fill in your values.
# You can also override per run from the environment:  VPS=root@1.2.3.4 SSH_KEY=~/.ssh/key bash deploy.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# Load .env, but let anything already set in the environment win (so per run overrides work).
VPS="${VPS:-}"; SSH_KEY="${SSH_KEY:-}"
if [ -f "$HERE/.env" ]; then
  ENV_VPS="$VPS"; ENV_KEY="$SSH_KEY"
  set -a; . "$HERE/.env"; set +a
  [ -n "$ENV_VPS" ] && VPS="$ENV_VPS"
  [ -n "$ENV_KEY" ] && SSH_KEY="$ENV_KEY"
fi
: "${VPS:?missing VPS, copy .env.example to .env and set it (for example VPS=root@1.2.3.4)}"
: "${SSH_KEY:?missing SSH_KEY, copy .env.example to .env and set it (for example SSH_KEY=\$HOME/.ssh/key)}"
# The dashboard hostname and the login name seeded on a fresh install. Both can live in .env.
STATS_HOST="${STATS_HOST:-stats.example.com}"
AUTH_USER="${AUTH_USER:-admin}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new $VPS"
say(){ printf '\n==> %s\n' "$1"; }

say "Deploying scripts to /usr/local/bin"
scp -i "$SSH_KEY" "$HERE/bin/hails-stats.sh" "$HERE/bin/hails-stats-pre.py" "$HERE/bin/hails-geoip-refresh.sh" "$HERE/bin/hails-admin.py" "$HERE/bin/hails-time.py" "$HERE/bin/hails-panels.py" "$HERE/bin/hails-map.py" "$HERE/bin/hails-rollup.py" "$HERE/bin/hails-bandwidth.py" "$HERE/bin/hails-perf-collect.py" "$HERE/bin/hails-perf.py" "$VPS:/usr/local/bin/"
$SSH "chmod +x /usr/local/bin/hails-stats.sh /usr/local/bin/hails-stats-pre.py /usr/local/bin/hails-geoip-refresh.sh /usr/local/bin/hails-admin.py /usr/local/bin/hails-time.py /usr/local/bin/hails-panels.py /usr/local/bin/hails-map.py /usr/local/bin/hails-rollup.py /usr/local/bin/hails-bandwidth.py /usr/local/bin/hails-perf-collect.py /usr/local/bin/hails-perf.py"

say "Ensuring /etc/hails-stats/config.env (seeded once from the example, never overwritten)"
# Everything machine specific lives here: which disk and NIC to chart, which units and sites to
# watch, and the map branding. Seeded once so a redeploy never clobbers your edits.
$SSH "install -d -m 755 /etc/hails-stats"
scp -i "$SSH_KEY" "$HERE/etc/hails-stats/config.example.env" "$VPS:/etc/hails-stats/config.example.env"
$SSH 'C=/etc/hails-stats/config.env;
if [ ! -s "$C" ]; then cp /etc/hails-stats/config.example.env "$C"; chmod 644 "$C"; echo "  seeded $C, edit it and rerun to apply";
else echo "  $C already present, left as is"; fi'

say "Deploying GoAccess config + glass skin to /etc/goaccess"
$SSH "install -d /etc/goaccess"
scp -i "$SSH_KEY" "$HERE/etc/goaccess/goaccess.conf" "$HERE/etc/goaccess/custom.css" "$HERE/etc/goaccess/settings.html" "$VPS:/etc/goaccess/"

say "Deploying systemd units + timers (stats + geoip + admin + map + perf)"
scp -i "$SSH_KEY" "$HERE/etc/systemd/hails-stats.service" "$HERE/etc/systemd/hails-stats.timer" "$HERE/etc/systemd/hails-geoip.service" "$HERE/etc/systemd/hails-geoip.timer" "$HERE/etc/systemd/hails-admin.service" "$HERE/etc/systemd/hails-map.service" "$HERE/etc/systemd/hails-map.timer" "$HERE/etc/systemd/hails-perf.service" "$VPS:/etc/systemd/system/"
# hails-perf is resident, not timer driven, and it is restarted rather than reloaded so a changed
# collector actually takes effect.
$SSH "systemctl daemon-reload && systemctl enable --now hails-stats.timer hails-geoip.timer hails-map.timer >/dev/null 2>&1; systemctl enable --now hails-admin.service >/dev/null 2>&1; systemctl enable hails-perf.service >/dev/null 2>&1; systemctl restart hails-perf.service >/dev/null 2>&1; echo stats-timer: \$(systemctl is-active hails-stats.timer) geoip-timer: \$(systemctl is-active hails-geoip.timer) map-timer: \$(systemctl is-active hails-map.timer) admin: \$(systemctl is-active hails-admin.service) perf: \$(systemctl is-active hails-perf.service)"

say "Ensuring the isolated stats auth import + admins file (seeded once, never overwritten)"
# The login lives in its own file rather than inline in the Caddyfile, so the Settings page can add
# and remove users without ever touching the config that fronts your other sites.
$SSH "U='$AUTH_USER'; "'A=/etc/caddy/stats_auth; ADM=/etc/caddy/stats_admins;
if [ ! -s "$A" ]; then
  H=$(cat /root/.stats_bcrypt 2>/dev/null);
  if [ -n "$H" ]; then printf "basic_auth {\n\t%s %s\n}\n" "$U" "$H" > "$A"; chmod 640 "$A"; chown root:caddy "$A"; echo "  seeded $A from /root/.stats_bcrypt for user $U";
  else echo "  WARN: /root/.stats_bcrypt is empty, cannot seed $A (create it first, see the README)"; fi
else echo "  $A already present, left as is"; fi
if [ ! -s "$ADM" ]; then echo "$U" > "$ADM"; chmod 640 "$ADM"; chown root:caddy "$ADM"; echo "  seeded $ADM with admin: $U"; else echo "  $ADM already present, left as is"; fi'

say "Ensuring runtime dirs (owned by caddy)"
# /var/lib/hails-stats holds the durable bandwidth history. Root owned: only the root run regen
# script writes it, and it must NOT sit under /srv/stats, which is wiped on every rebuild.
$SSH "install -d -o caddy -g caddy -m 755 /srv/stats /var/log/caddy; install -d -m 755 /var/lib/hails-stats; chown -R caddy:caddy /var/log/caddy"

say "Ensuring python3-maxminddb + python3-user-agents (Geo/ASN lookups and OS/Browser parsing)"
$SSH "python3 -c 'import maxminddb, user_agents' 2>/dev/null && echo '  already present' || { apt-get install -y python3-maxminddb python3-user-agents >/dev/null 2>&1 && echo '  installed' || echo '  WARN: install failed, the Geo/ASN/OS/Browser pages will be empty'; }"

say "Ensuring GeoIP databases (DB-IP free, country + asn)"
$SSH 'install -d /var/lib/GeoIP; M=$(date +%Y-%m); ok=1;
for kind in country asn; do
  f="dbip-${kind}-lite-${M}.mmdb";
  if [ ! -s "/var/lib/GeoIP/$f" ]; then
    if curl -fsS --max-time 60 "https://download.db-ip.com/free/${f}.gz" -o "/tmp/${f}.gz"; then
      gunzip -f "/tmp/${f}.gz" && mv "/tmp/${f}" "/var/lib/GeoIP/$f";
    else echo "  WARN: could not fetch $f (keeping any existing db)"; ok=0; fi
  fi
  [ -s "/var/lib/GeoIP/$f" ] && ln -sf "/var/lib/GeoIP/$f" "/var/lib/GeoIP/dbip-${kind}.mmdb";
done
ls -l /var/lib/GeoIP/dbip-country.mmdb /var/lib/GeoIP/dbip-asn.mmdb 2>/dev/null'

say "Checking the Caddyfile for the required blocks (NOT auto editing it)"
$SSH "H='$STATS_HOST'; "'C=/etc/caddy/Caddyfile;
grep -q "(access_log)" $C   && echo "  OK  (access_log) snippet present" || echo "  MISSING: (access_log) snippet + import access_log in each site block (see etc/caddy/access_log.snippet)";
grep -q "$H {" $C && echo "  OK  $H block present"  || echo "  MISSING: $H block (see etc/caddy/stats.example.com.block; needs a DNS record pointing at this server and a hash in /root/.stats_bcrypt)";
grep -q "trusted_proxies" $C && echo "  OK  trusted_proxies global present" || echo "  MISSING: trusted_proxies global block (see etc/caddy/trusted_proxies.global, only needed if a CDN sits in front)";
grep -q "stats_auth" $C && echo "  OK  stats_auth import present" || echo "  MISSING: in the $H block use '\''import /etc/caddy/stats_auth'\'' instead of an inline basic_auth (see etc/caddy/stats.example.com.block)";
grep -q "127.0.0.1:8770" $C && echo "  OK  /admin route present" || echo "  MISSING: /admin route to the admin backend (see etc/caddy/stats.example.com.block, needed for the Settings admin section)"'

say "Rebuilding the dashboard now"
$SSH "chown -R caddy:caddy /var/log/caddy; systemctl start hails-stats.service; echo pages: \$(find /srv/stats -name '*.html' | wc -l)"

say "Building the network map + sitemaps now"
$SSH "systemctl start hails-map.service; echo map: \$(test -f /srv/stats/map.html && echo written || echo MISSING); echo sitemaps: \$(ls /srv/*/sitemap.xml 2>/dev/null | wc -l)"

cat <<DONE

==> Done. Reminders:
  * If any Caddy block above was MISSING, edit /etc/caddy/Caddyfile by hand using the fragments in
    etc/caddy/, then:  caddy validate --config /etc/caddy/Caddyfile  &&  chown -R caddy:caddy /var/log/caddy  &&  systemctl reload caddy
  * Your dashboard is at https://$STATS_HOST (basic auth, user '$AUTH_USER').
  * After ANY 'caddy validate' run as root, chown /var/log/caddy back to caddy before reloading.
    Validate runs as root and pre creates the access log owned by root, and then Caddy itself
    cannot write it. This one costs people hours, so it is worth remembering.
DONE
