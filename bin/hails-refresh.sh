#!/bin/bash
STATS=/srv/stats
ROLLUP=/usr/local/bin/hails-rollup.py
SERVEDGEN=/usr/local/bin/hails-served.py
PERFGEN=/usr/local/bin/hails-perf.py

rc=0

cfg(){ sed -n "s/^[[:space:]]*$1=//p" /etc/hails-stats/config.env 2>/dev/null | tail -1; }

python3 "$ROLLUP" --from-db || { echo "hails-refresh: rollup --from-db failed" >&2; rc=1; }

: "${HAILS_SERVED_ROOT:=$(cfg HAILS_SERVED_ROOT)}"
export HAILS_SERVED_ROOT
python3 "$SERVEDGEN" >/dev/null || { echo "hails-refresh: served.js generator failed" >&2; rc=1; }

# Same lock as the swap in hails-stats.sh.
(
  flock -w 60 9 || { echo "hails-refresh: could not take the swap lock" >&2; exit 1; }
  [ -d "$STATS/all" ] || exit 0
  python3 "$PERFGEN" "All domains (aggregate)" "$STATS/all" || exit 1
  chmod 644 "$STATS/all/perf.html" 2>/dev/null
) 9>/var/lib/hails-stats/swap.lock || { echo "hails-refresh: perf page render failed" >&2; rc=1; }

exit $rc
