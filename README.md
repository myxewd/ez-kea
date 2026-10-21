<p align="center">
  <img src="static/icons/icon-192.png" width="96" alt="EZ-KEA logo">
</p>

<h1 align="center">EZ-KEA</h1>

<p align="center">
  <b>Stop editing Kea JSON by hand.</b><br>
  A web UI for ISC Kea DHCP that manages the config your daemons already run.
</p>

<p align="center">
  <a href="https://github.com/fenleytech/ez-kea/releases"><img src="https://img.shields.io/github/v/release/fenleytech/ez-kea?label=version&color=blue" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue.svg" alt="PolyForm Noncommercial License"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/kea-2.x%20%7C%203.x-blue.svg" alt="Supports ISC Kea 2.x and 3.x">
  <a href="#development"><img src="https://img.shields.io/badge/tests-472-brightgreen.svg" alt="472 tests"></a>
  <a href="https://demo.ezkea.com"><img src="https://img.shields.io/badge/demo-live-brightgreen.svg" alt="Live demo"></a>
</p>

![EZ-KEA managing DHCPv4 shared networks and pools](docs/images/pools.png)

EZ-KEA is a self-hosted web interface for ISC Kea DHCP. It installs on the same host as your Kea daemons, works out which configuration file they are actually running, and lets you manage subnets, pools, reservations, prefix delegation, DHCP options and the HA hook through forms instead of nested JSON. It also indexes your Kea logs so you can answer "who had this address three weeks ago" without grepping gzipped archives.

It is not a DHCP server and does not replace one. Uninstall it and what's left behind is an ordinary Kea config file your daemons keep serving from.

## Why EZ-KEA?

Kea is a good DHCP server with a configuration format that is genuinely painful to edit by hand. A subnet lives several objects deep, a pool is a string inside an array inside that object, and a stray comma takes DHCP down for everyone. The usual answers are to write your own templating around it, or to adopt a fleet-management platform that wants to own your deployment.

EZ-KEA takes the narrow path instead:

- **It edits your real config, in place.** No parallel database, no import/export step, nothing to reconcile when someone edits the JSON by hand.
- **It finds that config itself.** No paths to enter before you can see anything, and no chance of pointing at a stale copy.
- **It backs up before every write** and refuses the write if the backup fails, then syntax-checks with Kea's own binary before applying.
- **It stays out of the way.** One Python process, no agents, no external database server, no hosted control plane, no outbound network calls.

## Who it's for

Network and infrastructure engineers, ISPs, MSPs, and anyone running Kea in production who wants a management interface without redesigning the deployment around it. Homelabs are welcome too — noncommercial use is free and unlimited.

## Live demo

**<https://demo.ezkea.com>** — the login page comes with the demo account already filled in (`demo` / `demo`), just hit Sign In.

It is a shared sandbox with fake data and it resets every 30 minutes, so break whatever you want.

More screenshots: [live dashboard](docs/images/dashboard.png) · [log search](docs/images/logsearch.png) · [adding a subnet](docs/images/new-subnet.png) · [editing a subnet](docs/images/edit-subnet.png) · [IPv6 pools and prefix delegation](docs/images/pools6.png) · [high availability](docs/images/ha.png) · [reservations](docs/images/reservations.png) · [leases](docs/images/leases.png) · [IPv6 leases](docs/images/leases6.png)

## Quick start

Requires **Python 3.10+**. Kea is optional — without it, EZ-KEA starts in demo mode. Kea **2.x and 3.x** are both supported, including 3.2, which dropped the Control Agent and ships no `keactrl`.

On Debian and Ubuntu, `python3-venv` is a separate package and is **not** installed with Python, so install it first or `python3 -m venv` fails:

```bash
sudo apt install python3-venv        # Debian/Ubuntu only
```

```bash
git clone https://github.com/fenleytech/ez-kea.git
cd ez-kea
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

Open <http://127.0.0.1:8080> and log in with `admin` / `changeme`. It will make you change both right away.

By default it only listens on loopback. To let other machines reach it, set a real secret and a bind address. It will not start on a non-loopback address if `SECRET_KEY` is still the default:

```bash
export SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
export HOST=0.0.0.0
python3 app.py
```

### Docker

`deploy/` builds an EZ-KEA image that fetches the project from git at build
time, and a two-service compose file that runs it next to a Kea container with a
shared config directory:

```bash
cd deploy
docker build -t ez-kea:0.10.2 --build-arg EZK_REF=v0.10.2 ./ezkea
docker build -t ez-kea-kea:2.2.0 ./kea
cp kea.env.example kea.env      # set SECRET_KEY
docker compose --env-file kea.env up -d
```

[**docs/deployment.md**](docs/deployment.md) is the walk-through: paths and
ownership, the three setups that silently break (where the daemon listens, who
owns the control socket, whether the pre-save `kea-dhcp4 -t` check can read the
config), reverse proxy, backup/restore, upgrades and troubleshooting.

## Pointing it at a real Kea

If you already run Kea, EZ-KEA finds it — skip to the permissions note below.

If you are starting from nothing, ISC publishes packages for current Kea; distro
repos generally do not (Ubuntu 24.04 still ships 2.x). For Kea 3.2 on Ubuntu
`noble`:

```bash
curl -1sLf https://dl.cloudsmith.io/public/isc/kea-3-2/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/isc-kea-3-2.gpg
echo "deb [signed-by=/usr/share/keyrings/isc-kea-3-2.gpg] \
https://dl.cloudsmith.io/public/isc/kea-3-2/deb/ubuntu noble main" \
  | sudo tee /etc/apt/sources.list.d/isc-kea-3-2.list
sudo apt update && sudo apt install isc-kea-dhcp4 isc-kea-dhcp6
```

Units are `isc-kea-dhcp4-server` / `isc-kea-dhcp6-server`, config lives in
`/etc/kea/`, and the daemons run as `_kea`. See ISC's
[installation docs](https://kea.readthedocs.io/) for other distributions and for
building from the tarball.

### Permissions EZ-KEA needs

**This is the step people get stuck on.** ISC's packages lock everything down to
`_kea:_kea` mode 0750/0640 — including the binaries — so an unprivileged EZ-KEA
cannot read the config, run the syntax check, read leases or logs, or reach the
control socket. Grant it access by putting EZ-KEA's account in Kea's group and
making the configs group-writable:

```bash
sudo usermod -aG _kea $USER          # or: -aG _kea ezkea, for a service account
sudo chmod g+w /etc/kea/kea-dhcp4.conf /etc/kea/kea-dhcp6.conf
```

Log back in (or restart the service) so the new group takes effect. On distros
that call the account `kea` rather than `_kea`, use that instead.

### Reloading Kea 3.x

Kea 3.2 ships **no `keactrl`**, so EZ-KEA's default reload strategy cannot work
there. In **Global Settings → Reload Strategy**, choose **Control socket
(config-reload)** (`KEA_RELOAD_STRATEGY=control-socket`), which talks to the
daemon's own socket and reports whether the reload actually succeeded. Make sure
the daemon has a `control-socket` (or 3.0+ `control-sockets`) block — ISC's
shipped config does.

Choose **Kea HTTP API** (`KEA_RELOAD_STRATEGY=http`) instead and EZ-KEA does not
reload anything: it hands the configuration to the running daemon over its
HTTP/HTTPS control channel, with **`config-test` first and `config-set` only if
Kea accepted the test**. Nothing is applied when the test is refused, and the
file on disk stays exactly what was pushed, so a restart lands on the same
configuration. That needs an `http` (or `https`) entry in the daemon's
`control-sockets` — Kea 2.7.2 and later — with a distinct address/port per
daemon. Leave **Kea HTTP Control URL** blank to have it read from that config, or
set it when the listener is not described there, e.g. a port published out of
another container (`KEA_HTTP_URL` for DHCPv4, `KEA_HTTP_URL6` for DHCPv6 — one
URL per daemon, since they cannot share a port).

**Kea 3.x will not start with an unauthenticated HTTP control channel** — it
exits with `Unsecured HTTP control channel` — and it refuses a clear-text
password in its own config just as firmly. Configure basic authentication with
`user-file`/`password-file` on the daemon and put the same credential in
EZ-KEA's environment as `KEA_HTTP_USER`/`KEA_HTTP_PASSWORD` (never in a file
EZ-KEA writes). Running Kea with `-X` also silences the policy check, at the
cost of the policy check.

## Architecture

One Python process (waitress serving a Flask app) on the same host as Kea.

- **Discovery.** EZ-KEA determines the config file the running Kea process is actually using, so you never tell it where your config lives and it can't drift onto a stale copy. Mechanically it reads `/proc` for `kea-dhcp4` and `kea-dhcp6` and takes the path each daemon was launched with, falling back to `/etc/kea/` and then `/usr/local/etc/kea/`. Every path is overridable in Global Settings. With no Kea present it sandboxes to `./data/` and runs in demo mode.
- **In-place editing.** Changes go into Kea's own JSON. Writes to a given config file are serialized with a lock.
- **Backup, validate, apply.** Every config write copies the existing file first and aborts if that copy fails, keeping the hundred most recent (namespaced by a hash of the config path, so a restore can't grab the wrong file's backup). Applying runs `kea-dhcpN -t` and stops on failure, then reloads via `keactrl`, the control socket's `config-reload`, a SIGHUP to a Docker container, or — without any reload at all — the daemon's HTTP API (`config-test` then `config-set`) — an explicit setting, never guessed.
- **Storage.** Two embedded SQLite files under `./data/`: one for local accounts and settings, one a disposable log index that rebuilds itself if you delete it. No Postgres, no MySQL, no broker, nothing else to run.
- **No egress.** No telemetry, no CDN, no update check, no license server. Fonts and icons are vendored; license keys verify offline against a public key compiled into the application. It runs on an isolated management VLAN with no internet access.

## What it does

- **Live homepage dashboard** — separate IPv4 and IPv6 cards, each with an HA status badge, active lease/reservation/subnet counts, an async daemon-health badge (Running, Not Configured, Unreachable, or Demo Mode), and pool-utilization bars grouped by shared network (a standalone subnet counts as its own group), expandable to per-subnet detail.
- **DHCPv4 and DHCPv6** — shared networks, standalone subnets, address pools, and per-subnet DHCP options for both families. Subnets and shared networks are created, edited in place, and deleted through the UI (one address pool each per subnet). A subnet's CIDR is locked once created, but its gateway, pool range, and static-only toggle are editable; a shared network's name can be renamed. DHCP options are editable in place, and editing one field never discards the options a form doesn't know about.
- **Client classes** — Kea's `client-classes` are editable at both DHCP versions: the name, the `test` expression, the DHCPv4 fixed-header fields (`next-server` / `boot-file-name`), and per-class `option-data`. Expressions are stored verbatim and validated by Kea's own parser, with EZ-KEA catching what it can (name collisions, a non-IPv4 `next-server`) before the write.
- **Per-host options** — a reservation can carry its own DHCP options by name, so one machine can get a different `root-path`, DNS server or vendor option without a subnet or a class of its own. The reservations list shows what each host overrides (boot fields, client classes, option count).
- **Per-host iPXE scripts** — tick **Boot with iPXE** on a reservation and enter the host's script address; EZ-KEA writes and maintains the Kea `client-class` that hands that host its script, only once iPXE is running (the firmware still gets the normal boot file), and ranks it ahead of a general iPXE class. The generated class is bound to the reservation: it is marked *Managed* and read-only on the Client Classes page, and is updated or removed with the reservation.
- **Boot parameters and arbitrary configuration** — Kea's fixed BOOTP-header parameters (`next-server`/`siaddr` and `boot-file-name`/`file`; `server-hostname`/`sname` is readable by no PXE client and left to the raw editor) are ordinary scope keys rather than options, so they are edited as fields at global, subnet and reservation scope; global options are free-form by name. Anything with no form at all — `client-classes`, `option-def`/`option-space`, hooks — is reachable through a raw configuration editor that backs up first and then validates with Kea's own parser.
- **Prefix delegation** — `pd-pools` with a delegated length, validated against the pool's own prefix, so downstream routers and CPE get a block rather than a single address.
- **Reservations** — MAC-based for DHCPv4; DUID-based for DHCPv6, covering fixed addresses, delegated prefixes, or both on one reservation. Editable in place (MAC/DUID, IP, and hostname; the subnet is locked). The list pages have free-text search, a subnet CIDR filter, sortable columns, CSV export, and pagination.
- **High availability** — writes and edits Kea's `libdhcp_ha.so` hook for hot-standby, load-balancing or passive-backup, with the peer list, roles and timers as fields and the peer set validated before saving. A heartbeat button reads current peer state back off the daemon's control socket. EZ-KEA configures HA and reads its status; it does not drive failover, and the Control Agent each peer talks to is out of scope.
- **Historical log search** — a background thread indexes your Kea logs into SQLite (FTS5 where available), including rotated and gzipped archives, and survives rotation by inode change, truncation or `copytruncate`. Search by MAC (any spelling, including inside a client-id or DUID), IPv4, IPv6, CIDR range, or free text, filtered by severity, address family and time range. Results export to CSV and every search is a linkable URL. Retention defaults to 365 days.
- **Leases** — active DHCPv4 and DHCPv6 lease tables read from Kea's memfile CSV, with free-text search (MAC/IP/hostname, or DUID/IPv6/hostname for v6), a subnet CIDR filter, a status filter, an expiration-timeframe filter (presets like "expiring in the next 24 hours" plus a custom date range), sortable columns, CSV export, and pagination.
- **Validation, backup and restore** — overlap and range checks in the forms, automatic pre-write backups, and restore from the UI. Every write — not just the Apply button — is handed to Kea's own `-t` syntax check and rolled back if the daemon refuses it, so a config Kea cannot load never stays on disk.
- **Kea 2.x and 3.x** — EZ-KEA detects the daemon's version and writes the control-socket syntax that version accepts (3.0 renamed `control-socket` to `control-sockets`), and reads either. Exercised against Kea 2.0.2 and 3.2.0 in the testbed.
- **Multiple Kea endpoints** — a selector at the top left of every page switches the whole UI to a registered Kea instance: a **local** one (this host's daemon, its own config files) or an **HTTP** one (any daemon exposing its own control socket). Adding an HTTP endpoint pulls the running configuration with `config-get` into a local mirror, every page reads that mirror, and **Pull config** re-syncs on demand — bringing in what the daemon reports while keeping edits that have not been applied yet (unless you tick "discard my edits"). Apply does not push that copy wholesale: it reads the daemon's current configuration, replays only the edits EZ-KEA itself made since the pull onto it (a setting both sides changed stops the apply with nothing pushed and the local edit kept), pushes the result with `config-test` → `config-set`, then persists it with `config-write` so a restart does not revert it. A mirror that was never successfully pulled can never be applied. Selection lives in the session and is applied per request, so two operators on two different Kea servers never see each other's data. "This installation" only appears when a local Kea was actually detected, and the UI adapts to what an endpoint can honestly offer: no IPv6 menu (or dashboard card) without a v6 control channel, no Log Search for a remote endpoint (its logs stay on its own host), and an endpoint's own settings page instead of this installation's file/binary paths. The lease table is not hidden: it is pulled with `lease4-get-all`/`lease6-get-all` into a per-endpoint memfile mirror, so the Leases page, its search index and the dashboard's active-lease count all describe the selected daemon (the daemon needs the `libdhcp_lease_cmds.so` hook, shipped in `isc-kea-hooks`).
- **Multi-user auth** — local accounts, optional TOTP two-factor with recovery codes, and SMTP-backed password reset. Accounts are administrator or standard; admins additionally manage users, licensing and email settings. There is no read-only role — any account can change DHCP configuration.

Full reference is in the [wiki](https://github.com/fenleytech/ez-kea/wiki).

## Security

This edits your production DHCP config, so a few things matter:

- Backs up your Kea config before **every** write, and refuses to write if that backup fails
- Checks syntax with Kea's own binary before applying, and aborts the apply on failure
- Runs as whatever user you start it as, no root needed if your file perms allow it
- Local accounts with optional TOTP two-factor, recovery codes, and SMTP password reset
- Binds to 127.0.0.1 by default and refuses to bind elsewhere until you set SECRET_KEY
- Makes no outbound network calls

If you are putting it behind a reverse proxy or running Kea in Docker, see the [Security hardening](https://github.com/fenleytech/ez-kea/wiki/Security-Hardening) and [Installation](https://github.com/fenleytech/ez-kea/wiki/Installation) guides in the wiki.

## EZ-KEA and Stork

ISC Stork is a good tool for managing a fleet of Kea and BIND servers — central monitoring, HA status, pool utilization, Postgres and Grafana integration. If that is your problem, use Stork.

EZ-KEA solves a different one. It is for the operator who already runs Kea, is happy with how it's deployed, and wants to stop editing its configuration by hand:

- No agents, no external database server, no extra services
- Finds your config itself, from the running processes
- Edits the file Kea is actually running, with automatic backups and a syntax check
- An interface you can hand to someone who would rather not live in a JSON file

Free and unlimited for noncommercial use. Commercial use is $500 a year per deployment, with a free 30-day eval to try it at work first.

## Running it for real

Systemd unit, reverse proxies, and non-standard Kea layouts are all in the [Installation guide](https://github.com/fenleytech/ez-kea/wiki/Installation).

## Licensing

EZ-KEA is source-available under [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0).

In short:

- **Free for noncommercial use.** Everything included, no limits. That covers homelabs, personal use, learning, charities, schools, and government use.
- **Commercial use needs a license.** If it manages DHCP for a business or you get paid to run it for someone else, that is **$500 a year for a single deployment** — one production install, in one environment, run by one organization. Email **<sales@ezkea.com>**.
- **More than one deployment?** ISPs, MSPs, enterprises, and multi-site operators get volume licensing, priced on how many you run rather than off a list price. Email **<sales@ezkea.com>** with the number and I will quote it.
- **Want to try it at work first?** Free 30 day commercial eval, no questions asked. Email **<sales@ezkea.com>** and I will send you a trial key.

Nothing is gated on the license: no feature flags, no lease ceiling, no expiry. An unlicensed install larger than a homelab shows a banner above the page suggesting you get one, and that is the whole of it. It will not lock you out of your own config, licensed or not.

The [LICENSE](LICENSE) file is what counts, this is just a summary. More detail in [Licensing](https://github.com/fenleytech/ez-kea/wiki/Licensing).

SPDX identifier: `PolyForm-Noncommercial-1.0.0`. Source files have `SPDX-License-Identifier` headers so scanners pick it up. GitHub shows "Other" in the sidebar because its detector only knows OSI and FSF licenses.

## Documentation

The [wiki](https://github.com/fenleytech/ez-kea/wiki) has the rest:

| | |
| --- | --- |
| [Installation](https://github.com/fenleytech/ez-kea/wiki/Installation) | systemd, reverse proxies, Docker-based Kea |
| [Deployment scenarios](https://github.com/fenleytech/ez-kea/wiki/Deployment-Scenarios) | How it picks LIVE vs DEMO mode |
| [Configuration reference](https://github.com/fenleytech/ez-kea/wiki/Configuration-Reference) | All environment variables and UI overrides |
| [Security hardening](https://github.com/fenleytech/ez-kea/wiki/Security-Hardening) | Secret keys, 2FA, file perms |
| [High availability](https://github.com/fenleytech/ez-kea/wiki/High-Availability) | Setting up the HA hook |
| [Testbed](https://github.com/fenleytech/ez-kea/wiki/Testbed) | Containerized Kea for end-to-end testing |
| [Licensing](https://github.com/fenleytech/ez-kea/wiki/Licensing) | What is free, when you need to pay |

## Releases

Tagged releases are on the [Releases page](https://github.com/fenleytech/ez-kea/releases), and what changed in each is in [CHANGELOG.md](CHANGELOG.md). The running version is shown in the app footer, so you can tell what an install is on without checking out the repo.

## Support

- **Bugs and feature requests** — [GitHub Issues](https://github.com/fenleytech/ez-kea/issues)
- **Commercial licensing** — <sales@ezkea.com>

## Development

**472 unit and route tests**, running with no Kea install and no network access:

```bash
pip install -r requirements.txt
python -m pytest
```

Beyond that there is a containerized testbed that boots a **real** Kea server on simulated VLAN interfaces and drives **actual** DHCP transactions through it, so leases and logs under test are real rather than fixtures. It runs against Kea 2.0.2 by default and Kea 3.2.0 with `KEA_DOCKERFILE=Dockerfile.kea-3.2` — which is how the 3.x packaging differences (no `keactrl`, the logger output path, the socket directory mode) were found rather than assumed. See [testbed/README.md](testbed/README.md):

```bash
cd testbed && bash test_suite.sh
```

Bug reports and feature requests are welcome in [Issues](https://github.com/fenleytech/ez-kea/issues). I am not taking pull requests right now. Because EZ-KEA is sold under a commercial license too, merging code needs a licensing agreement with each contributor and that overhead is not worth it for a project this size. A good bug report helps more.

## License

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0) — see [LICENSE](LICENSE). Free for noncommercial use, commercial use needs a license, see [Licensing](#licensing) above.

