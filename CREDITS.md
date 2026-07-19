# Credits

None of this would exist without the projects below. Most of the heavy lifting is theirs, this
repo is mostly glue and a skin.

## The core

**[GoAccess](https://goaccess.io/)** by Gerardo Orellana, MIT licensed. It does the actual log
parsing and renders the chart dashboard. The whole pipeline is built around it. If you find this
project useful, go star GoAccess first.

**[Caddy](https://caddyserver.com/)** by Matthew Holt and the Caddy community, Apache 2.0. It
serves the sites, writes the JSON access log everything here reads, handles TLS, and provides the
basic auth in front of the dashboard.

## Data

**[DB-IP](https://db-ip.com/)** IP to Country Lite and IP to ASN Lite databases, licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). These power the Geo Location and
Networks panels.

> IP Geolocation by DB-IP: https://db-ip.com

That attribution is a licence condition, not a courtesy. If you run this and show the Geo or ASN
panels publicly, keep the credit visible. It already appears in the footer of both pages.

## Python libraries

**[maxminddb](https://github.com/maxmind/MaxMind-DB-Reader-python)** by MaxMind, Apache 2.0, reads
the DB-IP database files. Debian ships it as `python3-maxminddb`.

**[python-user-agents](https://github.com/selwin/python-user-agents)** by Selwin Ong, MIT, parses
User-Agent strings into the Operating Systems and Browsers panels. Debian ships it as
`python3-user-agents`. It builds on
**[ua-parser](https://github.com/ua-parser/uap-python)** and the shared
[uap-core](https://github.com/ua-parser/uap-core) regex database.

## Fonts

**[Roboto](https://fonts.google.com/specimen/Roboto)** by Christian Robertson and the Roboto Project
Authors, licensed under the [SIL Open Font License 1.1](https://openfontlicense.org/). It is bundled
in `assets/fonts/` and served from your own box, so no page makes a request to Google.

The licence text ships beside it as `assets/fonts/OFL.txt` and must stay with the files if you
redistribute them. Two variable `woff2` files, the latin and latin-ext subsets, cover every weight
from 100 to 900.

## Mentioned elsewhere

`etc/linkstack/docker-compose.yml` carries a healthcheck fix for
**[LinkStack](https://linkstack.org/)** (AGPL 3.0). That file is a reference note only, nothing here
depends on LinkStack and no LinkStack code is included.

The network map reads Cloudflare's [published IP ranges](https://www.cloudflare.com/ips/) to work
out which of your hosts sit behind a proxy.

## Standards

Structured data on the public network page follows [schema.org](https://schema.org/), and the
generated sitemaps follow [sitemaps.org](https://www.sitemaps.org/).
