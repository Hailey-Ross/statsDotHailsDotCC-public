#!/bin/bash
# Deploys the pipeline to your server. Run from your own machine:  bash deploy.sh
# Server address and SSH key come from a local uncommitted .env, or from the environment.
# It never rewrites /etc/caddy/Caddyfile, which usually fronts every other site on the box: it only
# checks that the required blocks are there and warns if they are not.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# Anything already set in the environment wins over .env, so per run overrides work.
VPS="${VPS:-}"; SSH_KEY="${SSH_KEY:-}"
if [ -f "$HERE/.env" ]; then
  ENV_VPS="$VPS"; ENV_KEY="$SSH_KEY"
  set -a; . "$HERE/.env"; set +a
  [ -n "$ENV_VPS" ] && VPS="$ENV_VPS"
  [ -n "$ENV_KEY" ] && SSH_KEY="$ENV_KEY"
fi
: "${VPS:?missing VPS, copy .env.example to .env and set it (for example VPS=root@1.2.3.4)}"
: "${SSH_KEY:?missing SSH_KEY, copy .env.example to .env and set it (for example SSH_KEY=\$HOME/.ssh/key)}"
STATS_HOST="${STATS_HOST:-stats.example.com}"
AUTH_USER="${AUTH_USER:-admin}"
FONT_DIR="${FONT_DIR:-/srv/stats/fonts}"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new $VPS"
say(){ printf '\n==> %s\n' "$1"; }

say "Deploying scripts to /usr/local/bin"
# These scripts are only correct as a set. scp copies sequentially, so a transfer that stops partway
# leaves a mixed set on the server, which is why this step aborts rather than carrying on.
# hails_db.py and hails_query.py are modules, imported by hails-ingest.py, so they get no chmod +x.
scp -i "$SSH_KEY" "$HERE/bin/hails-stats.sh" "$HERE/bin/hails-stats-pre.py" "$HERE/bin/hails-geoip-refresh.sh" "$HERE/bin/hails-admin.py" "$HERE/bin/hails-panels.py" "$HERE/bin/hails-map.py" "$HERE/bin/hails-rollup.py" "$HERE/bin/hails-bandwidth.py" "$HERE/bin/hails-perf-collect.py" "$HERE/bin/hails-perf.py" "$HERE/bin/hails-served.py" "$HERE/bin/hails_db.py" "$HERE/bin/hails_query.py" "$HERE/bin/hails-ingest.py" "$HERE/bin/hails-verify.py" "$HERE/bin/hails-timing.py" "$HERE/bin/hails-diffrender.py" "$HERE/bin/hails-prune.py" "$VPS:/usr/local/bin/" || {
  echo "FATAL: script transfer failed PARTWAY. scp copies sequentially, so an unknown number of the" >&2
  echo "       scripts above were already overwritten and the server now holds a MIXED set. Re run" >&2
  echo "       this deploy until it completes before letting hails-stats.timer fire again." >&2
  exit 1; }
# Checked against what actually landed, so a truncated file is caught too.
$SSH "grep -q -- '--tally' /usr/local/bin/hails-stats-pre.py && grep -q -- '--tally' /usr/local/bin/hails-rollup.py" || {
  echo "FATAL: the deployed hails-stats-pre.py and hails-rollup.py do not both understand --tally." >&2
  echo "       Refusing to continue: running hails-stats.sh against this set would freeze" >&2
  echo "       /var/lib/hails-stats/bandwidth.json at today's values with no error anywhere." >&2
  exit 1; }
$SSH "chmod +x /usr/local/bin/hails-stats.sh /usr/local/bin/hails-stats-pre.py /usr/local/bin/hails-geoip-refresh.sh /usr/local/bin/hails-admin.py /usr/local/bin/hails-panels.py /usr/local/bin/hails-map.py /usr/local/bin/hails-rollup.py /usr/local/bin/hails-bandwidth.py /usr/local/bin/hails-perf-collect.py /usr/local/bin/hails-perf.py /usr/local/bin/hails-served.py /usr/local/bin/hails-ingest.py /usr/local/bin/hails-verify.py /usr/local/bin/hails-timing.py /usr/local/bin/hails-diffrender.py /usr/local/bin/hails-prune.py; chmod 644 /usr/local/bin/hails_db.py /usr/local/bin/hails_query.py; rm -f /usr/local/bin/hails-time.py"

say "Ensuring /etc/hails-stats/config.env (seeded once from the example, never overwritten)"
$SSH "install -d -m 755 /etc/hails-stats"
scp -i "$SSH_KEY" "$HERE/etc/hails-stats/config.example.env" "$VPS:/etc/hails-stats/config.example.env"
$SSH 'C=/etc/hails-stats/config.env;
if [ ! -s "$C" ]; then cp /etc/hails-stats/config.example.env "$C"; chmod 644 "$C"; echo "  seeded $C, edit it and rerun to apply";
else echo "  $C already present, left as is"; fi'

say "Deploying GoAccess config + glass skin to /etc/goaccess"
$SSH "install -d /etc/goaccess"
scp -i "$SSH_KEY" "$HERE/etc/goaccess/goaccess.conf" "$HERE/etc/goaccess/custom.css" "$HERE/etc/goaccess/settings.html" "$HERE/etc/goaccess/table.js" "$VPS:/etc/goaccess/"

say "Deploying the bundled Roboto to $FONT_DIR"
# A FONT_DIR on a different host to the dashboard must send Access-Control-Allow-Origin.
$SSH "install -d -m 755 '$FONT_DIR'"
scp -i "$SSH_KEY" "$HERE/assets/fonts/"* "$VPS:$FONT_DIR/"
$SSH "chmod 644 '$FONT_DIR'/*; ls '$FONT_DIR' | tr '\n' ' '; echo"

say "Deploying systemd units + timers (stats + ingest + geoip + admin + map + perf + served + verify + prune)"
scp -i "$SSH_KEY" "$HERE/etc/systemd/hails-stats.service" "$HERE/etc/systemd/hails-stats.timer" "$HERE/etc/systemd/hails-ingest.service" "$HERE/etc/systemd/hails-ingest.timer" "$HERE/etc/systemd/hails-geoip.service" "$HERE/etc/systemd/hails-geoip.timer" "$HERE/etc/systemd/hails-admin.service" "$HERE/etc/systemd/hails-map.service" "$HERE/etc/systemd/hails-map.timer" "$HERE/etc/systemd/hails-perf.service" "$HERE/etc/systemd/hails-served.service" "$HERE/etc/systemd/hails-verify.service" "$HERE/etc/systemd/hails-verify.timer" "$HERE/etc/systemd/hails-prune.service" "$HERE/etc/systemd/hails-prune.timer" "$VPS:/etc/systemd/system/"
# hails-perf is resident rather than timer driven, so it is restarted to pick up a changed collector.
$SSH "systemctl daemon-reload && systemctl enable --now hails-stats.timer hails-ingest.timer hails-geoip.timer hails-map.timer hails-verify.timer hails-prune.timer >/dev/null 2>&1; systemctl enable --now hails-admin.service >/dev/null 2>&1; systemctl enable hails-perf.service >/dev/null 2>&1; systemctl restart hails-perf.service >/dev/null 2>&1; echo stats-timer: \$(systemctl is-active hails-stats.timer) ingest-timer: \$(systemctl is-active hails-ingest.timer) geoip-timer: \$(systemctl is-active hails-geoip.timer) map-timer: \$(systemctl is-active hails-map.timer) verify-timer: \$(systemctl is-active hails-verify.timer) admin: \$(systemctl is-active hails-admin.service) perf: \$(systemctl is-active hails-perf.service)"

say "Ensuring the isolated stats auth import + admins file (seeded once, never overwritten)"
# The login lives in its own file so the Settings page can edit users without touching the Caddyfile.
$SSH "U='$AUTH_USER'; "'A=/etc/caddy/stats_auth; ADM=/etc/caddy/stats_admins;
if [ ! -s "$A" ]; then
  H=$(cat /root/.stats_bcrypt 2>/dev/null);
  if [ -n "$H" ]; then printf "basic_auth {\n\t%s %s\n}\n" "$U" "$H" > "$A"; chmod 640 "$A"; chown root:caddy "$A"; echo "  seeded $A from /root/.stats_bcrypt for user $U";
  else echo "  WARN: /root/.stats_bcrypt is empty, cannot seed $A (create it first, see the README)"; fi
else echo "  $A already present, left as is"; fi
if [ ! -s "$ADM" ]; then echo "$U" > "$ADM"; chmod 640 "$ADM"; chown root:caddy "$ADM"; echo "  seeded $ADM with admin: $U"; else echo "  $ADM already present, left as is"; fi'

say "Ensuring runtime dirs (owned by caddy)"
# /var/lib/hails-stats holds the durable history, so it must stay outside /srv/stats, which is wiped
# on every rebuild.
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
