# Self hosted analytics for Caddy

This is the analytics setup I run for my own sites. It reads Caddy's access log, builds a full
dashboard plus a dedicated page per panel per domain, and watches the server itself. No JavaScript
on your visitors, no third party, no cookies, nothing leaves your box. If you already run Caddy,
you can have this up in about ten minutes.

[GoAccess](https://goaccess.io/) does the log parsing and the chart dashboard. Everything around it
is Python and shell.

## What you get

A landing page listing every domain Caddy serves, and for each one:

* **Overview** with visitors, requests, bandwidth and the day by day trend
* **Requested Files**, **Static Files**, **404 Not Found**, **Entry** and **Exit Pages**
* **Trending** and **Slowest Endpoints**
* **Status Codes**, **Error Pages**, **HTTP Methods**, **Content Types**, **TLS Versions**
* **Visitors** by IP, **Geo Location**, **Networks** by ASN
* **Operating Systems** and **Browsers** parsed from the User-Agent
* **Bandwidth** with daily, weekly, monthly, yearly and all time views
* **Time Distribution** by hour of day, day of week and week of month
* **Performance**, an htop style view of the server: load, memory, storage, disk IO, network IO,
  per core CPU, uptime, and per service uptime
* **Network map**, a diagram of your hosts and what each one proxies to

Every page has a Domain and a Panel dropdown at the top, so you can jump straight from
"404s on one site" to "404s on another".

Two things worth knowing up front. The GoAccess dashboard counts humans only, since it drops
crawlers and unknown agents, while the custom pages count all traffic. So the numbers differ on
purpose. And the Bandwidth page counts response bodies only, so it always reads lower than what your
host bills you for.

## Before you start

You need:

* A Linux server you can SSH into as root. Debian or Ubuntu is the easy path.
* **Caddy** already serving your sites.
* **GoAccess** installed: `apt install goaccess`
* **Python 3.9 or newer**, already there on any current Debian or Ubuntu.
* A hostname for the dashboard, with DNS pointing at the server.

The deploy script installs `python3-maxminddb` and `python3-user-agents` for you, and downloads the
GeoIP databases.

## Setting it up

### 1. Get Caddy logging as JSON

The pipeline reads one access log covering every site. Add this snippet near the top of your
Caddyfile:

```
(access_log) {
	log {
		output file /var/log/caddy/access.log {
			roll_size 100MiB
			roll_keep 10
		}
		format json
	}
}
```

Then put `import access_log` inside each site block you want counted. The full fragment is in
`etc/caddy/access_log.snippet`.

If a CDN sits in front of you, also add the `trusted_proxies` global from
`etc/caddy/trusted_proxies.global`, otherwise every visitor logs as your CDN and the Visitors panel
is useless.

### 2. Add the dashboard site block

Copy `etc/caddy/stats.example.com.block` into your Caddyfile and change the hostname. It serves
`/srv/stats` behind basic auth and proxies `/admin` to the local admin backend.

### 3. Set a password

```bash
PW=$(openssl rand -hex 9)
caddy hash-password --plaintext "$PW" > /root/.stats_bcrypt
echo "your password is: $PW"
```

Write that password down. The deploy script reads the hash from `/root/.stats_bcrypt` and seeds the
login file from it.

### 4. Deploy

On your own machine:

```bash
git clone https://github.com/Hailey-Ross/statsDotHailsDotCC-public.git
cd statsDotHailsDotCC-public
cp .env.example .env
```

Edit `.env` with your server address, SSH key, dashboard hostname and the login name you want. Then:

```bash
bash deploy.sh
```

It copies everything over, installs the units, fetches the GeoIP databases, checks your Caddyfile
for the blocks it needs, and builds the dashboard. It never edits your Caddyfile, it only tells you
what is missing.

### 5. Tell it about your server

Edit `/etc/hails-stats/config.env` on the server. That is where you set which disk and interface to
chart, which services to watch, and which sites to probe for uptime. Every setting is commented in
`etc/hails-stats/config.example.env`. Then:

```bash
systemctl restart hails-perf
systemctl start hails-stats.service
```

Open your dashboard and you should have reports.

## How it fits together

```
/var/log/caddy/access.log
        |
        |  hails-stats-pre.py     normalizes the JSON for GoAccess
        v
   goaccess + the Python generators
        |
        v
   /srv/stats/{index.html, all/, d/<domain>/}      served by Caddy behind basic auth
        ^
        |  rebuilt every 5 minutes by hails-stats.timer
```

Separately, `hails-perf-collect.py` runs all the time, sampling `/proc` every 10 seconds so the
Performance page has real history. It costs about 13 MB of memory and a hundredth of one core.

Almost everything is rebuilt from the raw log every 5 minutes, which means `/srv/stats` is disposable.
Two things are not, and they live in `/var/lib/hails-stats`:

* `bandwidth.json`, the long term bandwidth history, because your logs roll away in about two months
  and yearly totals cannot be recovered afterwards
* `perf_*.bin`, the performance rings, for the same reason

Do not delete that directory unless you mean to lose the history.

## Configuration

Everything machine specific is in `/etc/hails-stats/config.env`. The ones you will actually care
about:

| Setting | What it does |
|---|---|
| `HAILS_PERF_DISK` | Disk for the Disk IO panel. Detected if unset, run `lsblk` to check |
| `HAILS_PERF_NIC` | Interface for Network IO. Detected if unset, must be your real NIC and not `docker0` |
| `HAILS_PERF_UNITS` | systemd units to show on the Performance page, comma separated |
| `HAILS_PERF_TARGETS` | Sites to probe for uptime, as `name\|url\|healthy codes` separated by semicolons |
| `HAILS_MAP_ORG` | Name shown on the network map |
| `HAILS_MAP_PUBLIC_HOST` | Set this to also publish a sanitized public copy of the map. Leave it unset and only the private one is built |

The uptime targets need one bit of care. Set the status codes each site really returns, not just
200. Plenty of healthy sites answer a redirect or an auth challenge on their root, and if you only
accept 200 they will show as down forever. Check first:

```bash
curl -o /dev/null -w '%{http_code}\n' https://your.site/
```

## Adding your own panel

Panels are registered in one place, the `DROP` array near the top of `bin/hails-stats.sh`. Each
entry is `"Label|key"` and the key is the filename without `.html`. Add an entry, write something
that produces `<scope dir>/<key>.html`, and it appears in the Panel dropdown everywhere.

Copy `bin/hails-bandwidth.py` if you want a starting point. Each generator is deliberately self
contained, carrying its own copy of the style block and helpers rather than importing them, so you
can change one page without touching the others.

## When it goes wrong

**Reports are empty.** Check the log is actually JSON and that the site block has `import access_log`.
Then run `systemctl start hails-stats.service` and read `journalctl -u hails-stats.service`.

**Caddy will not reload, permission denied on the access log.** This one bites everybody.
`caddy validate` runs as root and creates `/var/log/caddy/access.log` owned by root, and then Caddy,
running as the caddy user, cannot write to it. Fix it every time after validating:

```bash
chown -R caddy:caddy /var/log/caddy
systemctl reload caddy
```

**Geo and Networks panels are empty.** The GeoIP databases did not download. Run
`systemctl start hails-geoip.service` and check `/var/lib/GeoIP`.

**Visitors all show as one IP.** A CDN is in front and you have not set `trusted_proxies`.

**A container shows as unhealthy but the site is fine.** The container's own healthcheck is probably
wrong rather than the service being down. LinkStack is a known case, see
`etc/linkstack/docker-compose.yml` for the explanation and the fix.

**Service state looks stale.** systemd and docker are only polled every 5 minutes, so a container you
just restarted takes a moment to catch up. The `/proc` metrics are 10 seconds fresh.

## A note on fonts

Pages load Roboto from Google Fonts, which means each viewer makes one request to Google. That is
only you and whoever you give logins to, but if you would rather it did not happen, delete the
`@import` line at the top of `etc/goaccess/custom.css` and the matching one in each generator in
`bin/`. The font stack falls back to your system font and everything still looks fine.

## Credits

Built on [GoAccess](https://goaccess.io/) and [Caddy](https://caddyserver.com/), with GeoIP data
from [DB-IP](https://db-ip.com/). Full list in [CREDITS.md](CREDITS.md).

If you show the Geo or Networks panels publicly, keep the DB-IP credit visible. It is a condition of
their licence and it is already in the page footer.

## Licence

MIT, see [LICENSE](LICENSE). Do what you like with it. If it saves you an afternoon, that was the
point.
