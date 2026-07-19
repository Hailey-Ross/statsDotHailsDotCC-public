# Self hosted analytics for Caddy

Hi! This is the analytics setup I run for my own sites, cleaned up so you can run it for yours.

It reads Caddy's access log and builds a dashboard, a page for every panel on every domain, a
drilldown on every URL, and a live view of the server itself.

Nothing runs in your visitors' browsers. No tracking script, no cookies, no third party. It is all
built from the log you are already writing. If you run Caddy, this is about ten minutes of setup.

[GoAccess](https://goaccess.io/) does the log parsing and the chart dashboard. The rest is Python and
shell.

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
* **Performance**, an htop style view of the server: load, memory, storage, disk IO, network IO, per
  core CPU, uptime, and per service uptime
* **Network map**, a diagram of your hosts and what each one proxies to

Every page has a Domain and a Panel dropdown, so you can hop from "404s on this site" to "404s on that
one" directly. It is the thing I use most.

Most pages have a Daily, Weekly and Monthly switcher, meaning the last 24 hours, 7 days and 30 days,
and your choice follows you around. Bandwidth is the odd one out: its switcher changes the bucket size
rather than the window.

### Page detail, my favourite bit

Every URL on every panel is a link. Click one and you get a report on that resource:

* **Hits, unique visitors and average response time**, plus entrances and exits where it is a page
* **Requests over time**, per day
* **Errors**, with the client and server split, which statuses came back, which referrers dropped
  visitors onto a failure, and when it first and last happened
* **404 Not Found**, with the full URL of whoever is still linking to it
* **Broken links on this page**, the 404s visitors hit straight after it, so the bad link is here
* **Came from** and **Went to**, the page viewed immediately before and after
* **Referring URLs**, the exact page that linked in, not just its domain
* **Status mix**, **Methods**, **Countries** and **Networks**

Sections with nothing to say are hidden rather than drawn empty. Any URL seen at least twice in the
window gets a record: pages, API endpoints, static assets, even requests that only ever failed.
Navigation is the exception and comes from page views alone, otherwise every page's next hop would be
its own CSS.

The drilldown uses the weekly window, so its numbers cover 7 days even when the switcher says Daily.

### Scanners

People are already walking lists of paths at your server looking for `.env` and `wp-login.php`. That
is normal.

They stay in every count, but they are kept out of the navigation lists where they would drown out
real journeys. They are spotted structurally, by the redirect a site sends when it bounces an unknown
path to its homepage, with a short list of known probe paths as backup.

If a whole host is mostly noise, `HAILS_AGG_EXCLUDE` keeps it out of the All domains view while it
keeps its own per domain pages.

### Numbers that look wrong

**GoAccess counts humans only.** It drops crawlers and unknown agents, while the custom pages count
all traffic. The two will not agree, on purpose.

**Bandwidth counts response bodies only.** It will always read lower than what your host bills you.

**Time Served is your response time**, not how long anyone read the page. A log cannot tell you that.

## Before you start

* A Linux server you can SSH into as root. Debian or Ubuntu is the easy path.
* **Caddy** already serving your sites.
* **GoAccess**: `apt install goaccess`
* **Python 3.9 or newer**, already on any current Debian or Ubuntu.
* A hostname for the dashboard, with DNS pointing at your server.

The deploy script handles `python3-maxminddb`, `python3-user-agents` and the GeoIP databases.

## Setting it up

### 1. Get Caddy logging as JSON

Near the top of your Caddyfile:

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

Then add `import access_log` to each site block you want counted. Full fragment in
`etc/caddy/access_log.snippet`.

If a CDN sits in front of you, add `trusted_proxies` from `etc/caddy/trusted_proxies.global` too.
Skip it and every visitor shows up as your CDN.

### 2. Add the dashboard site block

Copy `etc/caddy/stats.example.com.block` into your Caddyfile and change the hostname. It serves
`/srv/stats` behind basic auth and proxies `/admin` to the admin backend.

### 3. Set a password

```bash
PW=$(openssl rand -hex 9)
caddy hash-password --plaintext "$PW" > /root/.stats_bcrypt
echo "your password is: $PW"
```

Write it down before you close the terminal.

### 4. Deploy

```bash
git clone https://github.com/Hailey-Ross/statsDotHailsDotCC-public.git
cd statsDotHailsDotCC-public
cp .env.example .env
```

Fill in `.env` with your server address, SSH key, dashboard hostname and login name, then:

```bash
bash deploy.sh
```

**It will never edit your Caddyfile**, only tell you what is missing. That file usually fronts every
other site on your box.

### 5. Tell it about your server

Edit `/etc/hails-stats/config.env`: which disk and interface to chart, which services to watch, which
sites to probe. Everything is commented in `etc/hails-stats/config.example.env`. Then:

```bash
systemctl restart hails-perf
systemctl start hails-stats.service
```

### 6. More logins, later

The Settings page, behind the gear in the top bar, has an Admin section for adding and removing
logins. It only touches an isolated auth file, validates, and rolls back on failure, so it cannot take
your other sites down.

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

`hails-perf-collect.py` runs continuously, sampling `/proc` every 10 seconds so the Performance page
has real history. Under 20 MB of memory and about a hundredth of a core.

`/srv/stats` is rebuilt from the log every 5 minutes and is completely disposable. Two things are not,
and they live in `/var/lib/hails-stats`:

* `bandwidth.json`, the long term bandwidth history
* `perf_*.bin`, the performance rings

Your logs roll away in about two months and neither can be reconstructed afterwards, so do not delete
that directory unless you mean to lose the history.

## Configuration

Everything machine specific is in `/etc/hails-stats/config.env`:

| Setting | What it does |
|---|---|
| `HAILS_PERF_DISK` | Comma separated disks to chart. Found for you, so this is only for pinning the list by hand |
| `HAILS_PERF_NIC` | Comma separated interfaces. Physical ones are found for you, docker bridges and veths skipped |
| `HAILS_PERF_UNITS` | systemd units to show on the Performance page |
| `HAILS_PERF_TARGETS` | Sites to probe for uptime, as `name\|url\|healthy codes` separated by semicolons |
| `HAILS_MAP_ORG` | Name shown on the network map |
| `HAILS_MAP_PUBLIC_HOST` | Also publish a sanitized public copy of the map. Unset means only the private one |
| `HAILS_SERVED_ROOT` | Where to write `served.js` for the requests served counter. Unset means nothing is written |
| `HAILS_AGG_EXCLUDE` | Hosts to keep out of the All domains view, prefix matched. Each keeps its own per domain pages |
| `HAILS_FONT_BASE` | Where the stylesheets look for the bundled Roboto. Unset means `/fonts`, same origin, which needs no CORS |

Set the uptime status codes each site actually returns, not just 200. Plenty of healthy sites answer a
redirect or an auth challenge on their root and will otherwise look dead forever:

```bash
curl -o /dev/null -w '%{http_code}\n' https://your.site/
```

### Showing a requests served counter on a public page

Point `HAILS_SERVED_ROOT` at a directory your web server already serves and every rebuild drops
`served.js` there. It holds the total across all your domains, floored to two significant figures:
110,800 becomes 110k. It never rounds up.

Give your page an empty element with the id `served` and load the script. It fills in the text and
adds the class `on`, which is yours to style.

```html
<div id="served"></div>
<script src="https://assets.example.com/served.js" defer></script>
```

```css
#served { opacity: 0; transition: opacity .8s ease }
#served.on { opacity: 1 }
```

Running several sites? Point them all at one shared asset host and they stay in step. The file only
rewrites itself when the rounded number changes, so caches stay valid.

If your page has no `<meta name="viewport">`, phones lay it out at around 980px and shrink it to fit,
so 12px text lands at about 5px. Add the tag, which also makes your `max-width` queries work, or size
the counter in `vw`.

## When you resize the server

New cores, disks, volumes and interfaces appear after `systemctl restart hails-perf`.

**Your history survives it.** Everything countable keeps its own ring files, so adding a core creates
one new set and leaves the rest alone. Only genuinely new hardware starts empty, and it says
`collecting` until it has its own data rather than borrowing someone else's.

Docker bridges and `veth` pairs are ignored, otherwise the store fills with dead files. If you edit
the metric list in the source the record width changes, and the old file is moved aside as `.bin.old`
rather than overwritten.

## Adding your own panel

Panels are registered in one place, the `DROP` array near the top of `bin/hails-stats.sh`. Each entry
is `"Label|key"`, where the key is the filename without `.html`. Add an entry, write something that
produces `<scope dir>/<key>.html`, and it appears in the Panel dropdown everywhere.

Copy `bin/hails-bandwidth.py` for a starting point. Each generator is self contained, carrying its own
style block and helpers, so you can rip one apart without breaking the others.

## When it goes wrong

**Reports are empty.** Check the log is really JSON and the site block has `import access_log`. Then
`systemctl start hails-stats.service` and read `journalctl -u hails-stats.service`.

**Caddy will not reload, permission denied on the access log.** This one gets everybody. `caddy
validate` runs as root and creates `/var/log/caddy/access.log` owned by root, so Caddy cannot write to
its own log. Every time after validating:

```bash
chown -R caddy:caddy /var/log/caddy
systemctl reload caddy
```

Use `reload`, not `restart`. A failed reload keeps the running config so your sites stay up.

**Geo and Networks are empty.** The GeoIP databases did not download. Run
`systemctl start hails-geoip.service` and look in `/var/lib/GeoIP`.

**Every visitor is the same IP.** A CDN is in front and `trusted_proxies` is not set.

**A URL says no detail was recorded.** It needs at least two hits in the last 7 days.

**A container looks unhealthy but the site is fine.** Usually its healthcheck is wrong. LinkStack is a
known one, see `etc/linkstack/docker-compose.yml`.

**Service state looks stale.** systemd and docker are polled every 5 minutes. The `/proc` metrics are
10 seconds fresh.

## What is public and what is not

The dashboard is behind basic auth, all of it. Two things can be published on purpose, neither on by
default:

* the **network map**, with `HAILS_MAP_PUBLIC_HOST`. The public copy drops ports, localhost upstreams,
  filesystem paths and anything behind auth
* the **requests served counter**, with `HAILS_SERVED_ROOT`. One rounded number and nothing else

Roboto is bundled in `assets/fonts/` and served from your own box, so no page calls Google either.
`deploy.sh` puts it in `/srv/stats/fonts` and the stylesheets point at `/fonts`, same origin, nothing
to configure.

If you would rather serve it from a shared asset host, set `FONT_DIR` in `.env` and
`HAILS_FONT_BASE` in `config.env` to match. That host then has to send
`Access-Control-Allow-Origin`, because a font on another origin is a cross origin request. Miss that
header and the browser blocks the font and silently falls back to your system font, which looks close
enough that it is easy not to notice.

## Credits

Built on [GoAccess](https://goaccess.io/) and [Caddy](https://caddyserver.com/), with GeoIP data from
[DB-IP](https://db-ip.com/). Full list in [CREDITS.md](CREDITS.md).

If you show the Geo or Networks panels publicly, please keep the DB-IP credit visible. It is a
condition of their licence, and it is already in the page footer.

## Licence

MIT, see [LICENSE](LICENSE). Do whatever you like with it. If it saves you an afternoon, that was the
point.
