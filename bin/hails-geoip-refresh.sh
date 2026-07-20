#!/bin/bash
# Refreshes the DB-IP free databases to the current month and repoints the stable symlinks that
# goaccess.conf reads. Run monthly by hails-geoip.timer, and safe to rerun by hand.
set -u
install -d /var/lib/GeoIP
M=$(date +%Y-%m)
for kind in country asn; do
  f="dbip-${kind}-lite-${M}.mmdb"
  if [ ! -s "/var/lib/GeoIP/$f" ]; then
    if curl -fsS --max-time 120 "https://download.db-ip.com/free/${f}.gz" -o "/tmp/${f}.gz"; then
      gunzip -f "/tmp/${f}.gz" && mv "/tmp/${f}" "/var/lib/GeoIP/$f"
    else
      echo "WARN: could not fetch $f, keeping the existing database" >&2
    fi
  fi
  [ -s "/var/lib/GeoIP/$f" ] && ln -sf "/var/lib/GeoIP/$f" "/var/lib/GeoIP/dbip-${kind}.mmdb"
  ls -1t /var/lib/GeoIP/dbip-${kind}-lite-*.mmdb 2>/dev/null | tail -n +3 | xargs -r rm -f
done
ls -l /var/lib/GeoIP/dbip-country.mmdb /var/lib/GeoIP/dbip-asn.mmdb 2>/dev/null
