# Changelog

Notable changes to EZ-KEA. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While EZ-KEA is on `0.x`, environment variable names, settings keys, and the
on-disk layout under `data/` may still change between releases. Your Kea
configuration is never one of those things — EZ-KEA edits the file Kea already
reads and always backs it up first.

## [Unreleased]

### Fixed

- **Pulling a remote endpoint no longer discards edits that were made in
  EZ-KEA but not applied yet.** A daemon restart throws away its in-memory
  configuration, so the next `config-get` reports the older file; replacing the
  mirror with that would silently drop the operator's pending change. Pull is
  now the same three-way merge Apply uses: the daemon's current document comes
  in as the remote side, the edits made here survive, and the freshly pulled
  document becomes the baseline Apply replays them onto. A setting changed on
  both sides is reported by path and the pull is declined with the local copy
  untouched; a "discard my edits" checkbox on the Endpoints page takes the
  daemon's copy on request (the replaced copy is backed up first).

- **Applying to a remote endpoint no longer overwrites everything else that
  endpoint is running.** `config-set` replaces the daemon's entire running
  configuration, while the mirror Apply pushed was only as fresh as the last
  pull — so editing a subnet in EZ-KEA silently reverted anything that had
  changed on the daemon since. The reported case: `libdhcp_lease_cmds.so` was
  added to `hooks-libraries` by hand and the daemon reloaded; the next subnet
  edit pushed a mirror taken before that change, unloading the hook and making
  the Leases page fail with `'lease4-get-all' command not supported`. Apply now
  reads the daemon's current configuration with `config-get`, replays only
  `diff(pulled, mirror)` onto it, and pushes that — a setting changed on both
  sides is reported by path and stops the apply with nothing pushed and the
  local edit kept. A mirror that was never successfully pulled (still the
  seeded skeleton) or that belongs to a different endpoint URL is refused
  instead of being pushed, and a successful apply now also runs `config-write`
  so the daemon's own file keeps the change across a restart, then re-reads the
  daemon's configuration into the mirror and its merge baseline.

## [0.10.2] — 2026-10-20

### Added

- **Multiple Kea endpoints, selected from the navbar.** A new Endpoints page
  registers Kea instances and the selector at the top left of every page
  switches the whole UI to the chosen one. Two kinds: *local* (this host's
  daemon, its own config files — the pre-existing behaviour, now one entry
  among many) and *HTTP* (a daemon's own HTTP/HTTPS control socket). Adding an
  HTTP endpoint pulls the running configuration with `config-get` into a local
  mirror under `INSTANCE_DIR`, every page reads that mirror, Apply pushes it
  back with `config-test` → `config-set`, and **Pull config** re-syncs on
  demand. The selection lives in the session and is materialised per request
  (`InstanceScopedConfig` over a `flask.g` override), so no route had to learn
  what an endpoint is and two concurrent operators cannot see each other's
  daemon. Deleting an endpoint keeps its mirror directory: it is a copy of
  somebody's Kea configuration, not a cache to throw away silently.

  The UI follows what an endpoint can actually do, rather than assuming a local
  daemon: "This installation" is listed only when startup discovery found one
  (a standalone EZ-KEA managing remote endpoints selects its first endpoint
  instead), the IPv6 menu and dashboard card disappear without a v6 control
  channel, Log Search disappears for a remote endpoint (its logs stay on its own
  host), and the lease table is pulled rather than hidden: `lease4-get-all` /
  `lease6-get-all` land in a per-endpoint memfile mirror, so the Leases page,
  its search index (now also per endpoint) and the dashboard's active-lease
  count describe the selected daemon. The local `kea-dhcpN -t` pre-check is
  skipped for a remote endpoint — its mirror references hook libraries and auth
  files that only exist on the daemon's host, and the daemon's own
  `config-test` is the check that matters — and an endpoint's "Global Settings" page shows
  its URL/credentials instead of this installation's config-file and binary
  paths. The same checks run as request guards, so a bookmark to a hidden page
  is refused with an explanation rather than rendered empty.

- **Applying configuration through Kea's own HTTP API.** A fourth reload
  strategy (`KEA_RELOAD_STRATEGY=http`, "Kea HTTP API" in Application Settings)
  pushes the document EZ-KEA just wrote to the running daemon over its
  HTTP/HTTPS control socket — `config-test` first, `config-set` only if Kea
  accepted the test. A configuration the daemon refuses never reaches it, no
  reload/signal/socket file is involved, and because `config-set` keeps the new
  configuration in memory only, the file on disk stays the durable copy that a
  restart and Backups/Restore both act on. The endpoint is read from the
  daemon's `control-sockets` entry (defaults `127.0.0.1:8000`, and
  `[::1]:8000` for DHCPv6) unless the per-version `KEA_HTTP_URL`/`KEA_HTTP_URL6`
  setting (or the matching field on that version's Application Settings page)
  names one — per version because the two daemons cannot share a port. Status badges and the HA heartbeat now use whichever channel the daemon
  exposes, so an HTTP-only daemon reads as reachable instead of unconfigured.
  Kea 2.7.2 and later. Kea 3.x refuses to start with an unauthenticated HTTP
  control channel (and with a clear-text password of its own), so HTTP Basic
  credentials are read from `KEA_HTTP_USER`/`KEA_HTTP_PASSWORD` — environment
  only, never a file EZ-KEA writes — and a 401 is reported with that hint.
  HTTPS works for the transport against the system trust store; client
  certificates are not supported, so an HTTPS listener needs
  `cert-required: false`. Verified end to end against Kea 3.2.1: an accepted
  change applies live through `config-test`→`config-set`, a rejected one stops
  at `config-test` with the daemon still serving its previous configuration,
  and `config-set` leaves the file on disk untouched.

- **A Docker deployment, `deploy/`, with its own walk-through.** Two containers
  — EZ-KEA and the daemon — sharing one config directory mounted at the same
  absolute path in both, plus a `kea.env.example`, an initial config and
  `docs/deployment.md`. The document records the parts that fail silently if
  they are wrong, all of them measured against a real `kea-dhcp4` 2.2.0: the
  control socket is only reachable by the account that owns it (so the socket's
  directory is handed over and the daemon stops running as root), the
  pre-save `-t` check needs to be able to read `/etc/kea`, and a DHCP daemon
  inside Docker's default bridge cannot serve the hosts it is supposed to serve.

- **Client-class editor.** Kea's `client-classes` are now editable at both
  DHCP versions: name, the `test` expression, the DHCPv4 fixed-header fields
  (`next-server` / `boot-file-name`), and per-class `option-data`.
  Classification is a standard Kea configuration surface, like shared networks
  or prefix delegation, so the page is written in those terms and says nothing
  about what any class is for. Expressions are stored verbatim and judged by
  Kea's own parser at Test Config / Apply Changes; EZ-KEA validates what it can
  (a name collision, a non-IPv4 `next-server`, a missing expression) and
  otherwise stays out of the way. Reachable from the IPv4 and IPv6 menus.
- **Per-host DHCP options.** A reservation can now carry its own
  `option-data` (Kea's *Including Specific DHCPv4 Options in Reservations*),
  edited as name/data rows like the global and class editors. This is the
  right scope for "this one machine needs a different root-path / DNS /
  vendor option" without carving out a subnet or defining a class for it.
  Editing a reservation merges by name, so options the form does not own
  survive.
- **Per-host column on the reservations list.** Rows now show what a host
  overrides beyond its address: the boot fields it sets, any client classes
  it is reserved into, how many options are configured on it, and whether it
  boots via iPXE. The index schema grew the columns needed for this
  (`options_count`, `boot_config`, `classes`, `ipxe_script`), added through
  the existing migration path; the CSV export carries the same facts.
- **Per-host iPXE boot scripts, configured from the reservation.** A DHCPv4
  reservation's Boot Parameters section gains a **Boot with iPXE** checkbox
  and a script address. Saving it writes a Kea `client-class` for that host
  whose expression requires both the host's MAC and iPXE's option 77 user
  class, so the NIC firmware still receives the normal boot file and only the
  loaded iPXE is handed the script. The class is named deterministically from
  the reservation (`ez-kea-ipxe-<mac>-<subnet>`), kept first in
  `client-classes` so it outranks a general "send every iPXE client this
  script" class (Kea uses the first matching class's boot fields), and is bound
  to the reservation: the Client Classes page shows it as *Managed* with a link
  back instead of edit/delete, and it follows the reservation through edits,
  MAC changes, deletes, and subnet/shared-network removal.
- **Kea's fixed BOOTP-header parameters are now editable, at every scope that
  supports them.** `next-server` (`siaddr`) and `boot-file-name` (`file`) are
  ordinary scope keys in Kea, not options, so they were previously unreachable
  from EZ-KEA's option forms — and no option can stand in for `next-server`,
  which has no option code at all. Both are now fields on the global settings
  page, on subnet create/edit, and on reservation create/edit, so a single host
  can override the boot source without its own subnet. Blank means "remove the
  key"; `next-server` must be an IPv4 address. `server-hostname` (`sname`) is
  read by neither PXE nor iPXE firmware, so it is not offered as a field; a
  value already in the file is preserved through a save and stays editable on
  the Advanced Configuration page.
- **Global DHCP options are no longer limited to three presets.** The global
  settings page gains a free-form name/data table for any other option Kea
  accepts there, and renders every non-preset option already configured so a
  save round-trips it unchanged.
- **Advanced Configuration page** (`/advanced-config`, linked from both DHCP
  menus) — a raw editor for the whole Kea configuration document, reachable
  when a setting has no form: `client-classes`, `option-def`/`option-space`,
  hooks, vendor-specific scope parameters. It accepts Kea's `//`, `#` and
  `/* */` comments, refuses a document that isn't JSON or lacks the
  `Dhcp4`/`Dhcp6` root key, backs up before writing, and then hands the
  result to Kea's own syntax checker and reports what it said.
- **Saves are checked by Kea itself before they are kept.** A candidate config
  is written, handed to the daemon's own `-t` syntax check, and rolled back
  from the pre-write backup when Kea refuses it — so a config the daemon cannot
  load never lands on disk. This began as a guard on the client-class editor
  and the raw Advanced Configuration editor, the two places where arbitrary Kea
  syntax (class expressions above all) is typed, and now backs every write (see
  Fixed). When the check cannot be run at all (no Kea binary on this host, a
  timeout) the save still goes through and the operator is warned, because an
  environment problem must not make EZ-KEA unable to save. The Test Config
  button and the save path share one implementation
  (core/config_validate.py), so they cannot disagree about what Kea accepts.

### Fixed

- **Every Kea config write is now gated on Kea accepting it, not just the
  client-class and raw editors.** Those two called `save_validated_kea_config`;
  the subnet, reservation, option and global-settings forms called
  `save_kea_config`, which backs up and writes but never asked the daemon. A
  reservation with a fixed address outside its subnet was written happily and
  the next Kea reload died on it (`specified reservation '10.99.0.5' is not
  within the IPv4 subnet '10.77.0.0/24'`). The check now lives in
  `save_kea_config` itself, so no route can skip it, and a refusal raises
  `InvalidConfigError` with Kea's own words after restoring the previous file.
- **A DHCP option could not be removed from a client class, a reservation or
  the global settings page.** The only way to express "remove this" was to
  clear the row's name, and the parser read a blank row as "nothing said about
  this option" — so the option stayed put and deleting it looked broken. Each
  option row now has a red ✕: for a row that exists in the config it strikes
  the row through and records the removal (submitted as `…-deleted-<i>`), which
  the merge honours on save — including when the last option on a scope is
  removed, where the key goes away rather than becoming an empty list. For a
  row added in the same edit there is nothing on disk to remove, so the ✕ just
  drops the row. Clicking the same ✕ again puts a removed row back — the
  button is a toggle (its icon and label change), so a mis-click is undoable
  right up until the save.
- **Options keep their order when a row is removed and put back.** The merge
  rebuilt the list from a name-keyed submission, so an option that had already
  been removed was no longer in the config to be found and came back at the
  end. Each submitted row now carries the position it was rendered at, and the
  list is written back in that order.
- **A blank option table gave you nothing to type into.** The three option
  tables now seed a couple of blank rows, and "Add Option" appends more, so
  any number of options can be entered in one save instead of re-opening the
  form for each one.
- **The option tables' remove button could render collapsed to a sliver.** Its
  icon was a `<i data-lucide="x">` left for lucide to swap for an `<svg>` when
  the page loaded — but the row it lived in is `display:none` and gets *cloned*
  later by "Add Option", so a clone could carry a bare `<i>`, which has no box
  of its own: the button was then only as tall as its padding. Icons in that row
  are now emitted server-side as real SVG (templates/_icons.html), so nothing
  depends on when lucide runs, and `.btn svg` states the icon's size once
  instead of inheriting it from an attribute that may not be there.
- **The client-class editor's example expressions were wrong for real clients.**
  The hint suggested `substring(option[60].hex, 0, 4) == 'iPXE'` and
  `substring(option[77].hex, 2, 4) == 'iPXE'`; tested against an actual iPXE
  (1.0.0+git-20190125, QEMU, real DHCP), neither matches. A loaded iPXE sends
  the same `PXEClient:Arch:...:UNDI:...` vendor class as the firmware that
  loaded it, so option 60 cannot separate the two stages at all, and option 77
  arrives as `4d 04 69 50 58 45` — code, length, then the payload `iPXE` — so
  `option[77].hex` is `69505845` and the offset is 0. The hint now shows
  `substring(option[77].hex, 0, 4) == 'iPXE'`, which does match, and no longer
  offers a `option[61]` MAC-prefix example: Kea 2.2.0 did not expose the
  client-id that way in any of five forms tried.
- **A config path you configured explicitly now wins over auto-discovery.**
  With `DHCP_CONFIG_FILE` set (say `/kea/kea-dhcp4.conf`) but not yet on disk,
  `discover_environment()` fell through to the packaged
  `/etc/kea/kea-dhcp4.conf` and managed that instead: EZ-KEA edited, backed up
  and rendered a file the operator had not pointed it at, while the configured
  path was never even bootstrapped. `DHCP_CONFIG_FILE_EXPLICIT` (true when the
  environment variable is present) now makes the configured path authoritative,
  so the skeleton is created there and every later edit lands in the same file.
  Same rule for `DHCP6_CONFIG_FILE`.
- **A discovered config file no longer resets the configured daemon command.**
  `discover_environment()` returned the hardcoded `"kea-dhcp4"` (and
  `"keadctrl"` for the control binary) on the "config file found" paths, so a
  deployment that sets `KEA_DHCP4_CMD=/shared/kea-dhcp4` had it replaced at
  startup. The pre-save check then could not find the binary and silently
  degraded to "saved, but the check could not be run" on every write — the
  check appeared to be working while never running.
- **Editing a subnet's gateway or a reservation no longer deleted that
  scope's other options.** `edit_subnet()` rebuilt `option-data` from the one
  field its form owns, so any option it didn't know about — a hand-written
  `root-path`, a vendor suboption — vanished the first time someone changed
  the router. Options are now merged by name, preserving everything the form
  does not own.
- **The generated Kea config carried the wrong logger key, which Kea refuses
  to load.** Every skeleton and the app-settings log-path save emitted
  `output-options` (hyphenated); Kea's parser rejects that with *"got
  unexpected keyword"* and refuses to load the whole file — reproduced
  against a real `kea-dhcp4` 2.0.2, whose `-t` accepts only the underscore
  spelling `output_options` (the spelling this project's own Kea 3.2 testbed
  config uses). A fresh install therefore bootstrapped a config the daemon it
  was written for could not start with, and changing the log path broke a
  working one. `extract_log_file_from_config()` now reads either spelling so
  existing configs keep resolving, and the log-path save normalises a
  hyphenated key in place rather than writing a second one alongside it.
- **The Docker deployment's image did not build, and its healthcheck could
  never pass.** The fetch stage copied `git` out of an Alpine image: even
  when that path exists, the binary is linked against musl and cannot run on
  this glibc base, and it checked out a bare `main` after fetching into
  `refs/remotes/origin/*`, which git treats as a path. The checkout now
  happens in a Debian fetch stage, with the requested ref fetched by name, so
  a branch, tag and commit SHA all resolve; only the application tree is
  copied into the runtime image. The
  healthcheck already pointed at `/healthz`, which did not exist: there is now
  an unauthenticated probe that returns 200 only when the database and the
  managed Kea config are usable, 503 otherwise.
- **The compose file mounted the Kea config directory read-only into the
  one container that writes it.** EZ-KEA rewrites `/etc/kea/kea-dhcp4.conf` on
  every save, so a read-only bind mount made that fail with EROFS and left the
  deployment unable to change anything. The `ezkea` service now mounts
  `${KEA_ROOT}/conf` read-write; the daemon's own mounts are unchanged.

## [0.9.2] — 2026-08-02

### Fixed

Found by a second from-scratch install, this time onto a fresh Ubuntu 26 VM
running ISC's own Kea 3.2 packages untouched — same premise as the 0.9.1 pass
below, different (and worse) bugs. All four are regression-tested; see
AUDIT_FINDINGS.md for the full write-up of each.

- **A Kea config full of ISC's own `//`/`#`/`/* */` comments — i.e. every
  packaged install's default config, unmodified — silently parsed as empty**
  instead of failing loudly, and saving anything on top of that emptied
  skeleton overwrote the real subnets/reservations on disk with no warning
  anywhere. `load_json()` now strips Kea's comment syntax before parsing, and
  a file that still fails to parse after that raises instead of silently
  returning a blank skeleton.
- **Saving Global Settings could still duplicate the logger's output-path key**
  and get the config rejected by Kea. 0.9.1 fixed the two places that *write*
  this key but missed that the skeleton default `load_json()` falls back to
  still hardcoded the old, wrong spelling — so the fallback path alone was
  enough to reproduce the exact bug 0.9.1 believed it had closed.
- **A config-parse fallback could leak one protocol's data into the other
  protocol's file.** The fallback skeleton was shared, mutable, module-level
  state, and one DHCPv6 route fetched the DHCPv4 skeleton by mistake when its
  own config didn't parse — together, creating an IPv6 subnet could write a
  stray `Dhcp4` block into `kea-dhcp6.conf`, which Kea then refused to load.
  The fallback is now always an independent deep copy, and every DHCPv6 call
  site was audited to fetch the right skeleton.
- **Every DHCPv6 Prefix Delegation pool created through the UI was rejected by
  Kea.** The only way to create one stored the whole `prefix/length` string as
  Kea's `prefix` field and never set the separate `prefix-len` field Kea's
  schema requires, so `kea-dhcp6 -t` failed with "missing parameter
  'prefix-len'" on every single one. Fixed to submit `prefix` and `prefix-len`
  as the separate fields Kea expects.

## [0.9.1] — 2026-07-31

### Fixed

Found by deploying from scratch onto a clean Ubuntu 24.04 box running ISC's own
Kea 3.2 packages — every one of these was reproduced on that machine, and all
are regression-tested in `tests/test_greenfield_regressions.py`.

- **Kea configs are now genuinely backed up before every write.** Automatic
  backup did not exist: `copy_file()` was reachable only from the explicit
  Backup and Restore buttons, while all 23 config-write paths (subnets, pools,
  reservations, options, HA, global settings) called `save_json()` directly.
  Writes now go through `save_kea_config()`, which backs up first, prunes to the
  most recent 100 per config, and **refuses the write** if the backup fails.
- **Restore no longer fails on a normal packaged Kea install.** It used
  `shutil.copy2()`, which chmods the destination — and chmod needs *ownership*,
  which EZ-KEA does not have over a `_kea`-owned `/etc/kea/kea-dhcp4.conf`. It
  raised `EPERM` *after* writing the contents, so the restore silently succeeded
  while reporting HTTP 500. Now uses `copyfile()`, leaving ownership and mode to
  whoever set up Kea.
- **Saving Global Settings no longer writes a config Kea refuses to load.** It
  emitted `output_options` (underscore) alongside the correct `output-options`,
  which Kea rejects as a duplicate, and injected an empty
  `host-reservation-identifiers: []`, which Kea rejects outright. One Save click
  left the live config unloadable.
- **Saving Global Settings no longer silently demotes the other protocol to the
  sandbox.** On the first save the settings file was seeded from the `./data`
  defaults, so saving the DHCPv4 form pinned DHCPv6 to a sandbox path — and
  writing the file makes discovery stop looking for the running daemon. Every
  later DHCPv6 edit then went to a file `kea-dhcp6` never reads, with no warning.
  The version not being saved is now seeded from what discovery actually
  resolved.
- **The control-socket reload strategy works against a stock ISC config.** Those
  configs use a bare relative `socket-name` (`kea4-ctrl-socket`), which Kea
  resolves against `/var/run/kea`; EZ-KEA passed it to `connect()` verbatim, so
  it looked in its own working directory. This mattered doubly because Kea 3.2
  ships no `keactrl`, leaving the control socket as the only reload path.
- **The DHCPv6 Pools page shows standalone subnets.** Kea writes a plain
  single-subnet v6 server as a top-level `Dhcp6.subnet6[]`, and the page read
  only `shared-networks` — so such a server displayed as "No IPv6 Shared
  Networks Configured", hiding live subnets and inviting the operator to
  recreate config that already existed. Standalone v6 subnets can now be
  listed, created, deleted, and given options, matching DHCPv4.
- **An unreadable Kea config now explains itself instead of returning a 500.**
  `load_json()` handled missing and corrupt files but not `PermissionError`,
  which is the *normal* first-run state against packaged Kea (`/etc/kea` is 0750
  `_kea:_kea`). It now raises `ConfigAccessError`, rendered as a 503 naming the
  file and the group fix, and the log indexer no longer logs a traceback every
  60 seconds. It deliberately does **not** fall back to the empty skeleton —
  that would show a real server as having no subnets, and the next save would
  overwrite the operator's config with it.

### Removed

- `jsonify==0.5` from `requirements.txt` — an abandoned no-op package unrelated
  to Flask's `jsonify`, imported nowhere, with no wheel on PyPI, so it was built
  from source during the first command every install runs.

### Changed

- **Commercial licensing now states a price.** $500 a year for a single
  deployment — one production install, in one environment, run by one
  organization — with volume licensing quoted for anyone running more than
  one. The README, the in-app Licensing page, and the unlicensed footer note
  and banner all said "commercial use requires a license" without naming a
  number, so the only way to learn the price was to send an email. No licensing
  behaviour changed: nothing is gated, blocked, or expired, and the free
  noncommercial terms are exactly as they were.

## [0.9.0] — 2026-07-31

First tagged release. EZ-KEA has been public since 2026-07-29; this puts a
version on it so an install can be identified.

### Added

- **Kea 3.x support.** Kea 3.0 renamed the singular `control-socket` object to
  a `control-sockets` list. EZ-KEA now reads either spelling, and probes
  `kea-dhcp4 -v` to decide which one to write, so a generated config is
  accepted by the version actually installed. Configs written for Kea 2.x keep
  working untouched.
- **Control-socket reload strategy.** Sends `config-reload` down the daemon's
  own command socket and reports whether the reload succeeded. This is ISC's
  recommended path now that the Control Agent is gone, and it is the only one
  that works on Kea 3.2 installed from ISC's packages, which ship no `keactrl`
  at all. Selectable in Global Settings alongside `keactrl` and Docker SIGHUP.
- **Full-history log search** backed by a SQLite index, rather than tailing.
- **A Kea 3.2.0 testbed target** (`testbed/Dockerfile.kea-3.2`, ISC's own
  packages from Cloudsmith) next to the existing Ubuntu 2.0.2 one. Both build
  the same testbed, so `.env.testbed` and `test_suite.sh` work against either
  unchanged.

### Fixed

- HA peer status no longer breaks on Kea 3.x. It looked up the control socket
  by the pre-3.0 key name only, so on a 3.x config it found nothing and
  reported the peer unreachable.
- The testbed Kea container no longer crash-loops on a stale PID file. As PID 1
  it wrote `1` to its own PID file, saw that PID alive on the next start, and
  aborted with `DHCP4_ALREADY_RUNNING` — which under `restart: unless-stopped`
  only `--force-recreate` cleared.
- `test_suite.sh` runs against either Kea target: it resolves the Kea log path
  at runtime and reads the config on the host, rather than through a
  `docker exec python3` that ISC's 3.x images do not have. This also uncovered
  a test that passed by matching a label in its own capture file rather than a
  delivered DHCP option.

### Changed

- Relicensed to **PolyForm Noncommercial 1.0.0** (source-available; free and
  unlimited for noncommercial use, commercial use needs a paid license). All
  runtime enforcement was removed with it — the previous 100-lease free tier,
  grace period, and write lock are gone. Unlicensed installs show a footer
  note and nothing more.
- Branding standardized on EZ-KEA.

[Unreleased]: https://github.com/fenleytech/ez-kea/compare/v0.10.2...HEAD
[0.10.2]: https://github.com/fenleytech/ez-kea/compare/v0.9.2...v0.10.2
[0.9.2]: https://github.com/fenleytech/ez-kea/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/fenleytech/ez-kea/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/fenleytech/ez-kea/releases/tag/v0.9.0