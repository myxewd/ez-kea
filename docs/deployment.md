# Deploying EZ-KEA in production (Docker)

    +-------------------- reverse proxy (TLS) --------------------+
    |  dhcp-admin.example.com, reachable only from mgmt      |
    +-----------------------------+------------------------------+
                                  | http://127.0.0.1:8080
    +-----------------------------v------------------------------+
    | container: ezkea   (uid 10001)                         |
    |   reads + writes  /etc/kea/kea-dhcp4.conf              |
    |   runs            kea-dhcp4 -t   (before every save)   |
    |   reloads via     /run/kea/kea-dhcp4-ctrl.sock         |
    +---------+---------------------------------------+--------+
              | same host directories,                |
              | mounted at the same absolute paths    |
    +---------v-----------------+        /opt/ez-kea/kea/{conf,lib,log,run}
    | container: kea  (uid _kea)|        /opt/ez-kea/data
    |   kea-dhcp4 on UDP 67     |
    +---------------------------+

The file `kea/conf/kea-dhcp4.conf` is the contract between the two containers.
Both mount the *same host directory* at the *same absolute path* (`/etc/kea`),
which is why nothing here needs path translation. EZ-KEA is the only thing that
writes it; the daemon only reads it.

Every rule in §4 was measured against a real `kea-dhcp4` 2.2.0 running
in the images this directory builds: which account may connect to a control
socket, what a stale PID file does, how `kea-dhcp4 -t` behaves as an
unprivileged user, and what each of those failures looks like from the UI's
side. Where something could only be checked in a plain directory rather than a
Windows bind mount, the text says so.

---

## 0. What you need

| | |
|---|---|
| Docker Engine | 24+ with the Compose v2 plugin |
| Host | any Linux distribution; ~250 MB of image plus your Kea data |
| Network | the DHCP daemon must see the clients' broadcast domain (§4.1) |
| Time | about 15 minutes |

There is **no external service**: no Postgres, no Redis, no broker. Both SQLite
files live under `/opt/ez-kea/data`.

---

## 1. Layout

    /opt/ez-kea/
    |- data/                      EZ-KEA's own state (uid 10001)
    |  |- ez-kea.db               accounts, sessions, licences
    |  |- ez-kea-settings.json    paths the UI saved
    |  |- ez-kea-logindex.db      disposable: keyed log history
    |  |- ez-kea-stateindex.db    disposable: leases/reservations index
    |  `- backups/                every pre-write copy of the Kea config
    `- kea/
       |- conf/                   mounted at /etc/kea     (the config file)
       |- lib/                    mounted at /var/lib/kea (leases)
       |- log/                    mounted at /var/log/kea (kea/kea-dhcp4.log)
       `- run/                    mounted at /run/kea     (control socket)

```bash
sudo mkdir -p /opt/ez-kea/{data,kea/conf,kea/lib,kea/log/kea,kea/run}
sudo chown -R 10001:10001 /opt/ez-kea/data
```

`10001` is the uid the image creates its `ezkea` account with. If a host
account should own this data instead, pass `EZKEA_UID`/`EZKEA_GID` to the
container rather than chowning. `kea/run` stays owned by `root` for now; §4.2
hands it over, and *why* that matters is the one thing in this document that is
easy to get wrong and hard to notice.

---

## 2. Build the EZ-KEA image

The image is built from the git ref you name, not from whatever is in a working
tree, so "which version is deployed" has exactly one answer.

```bash
git clone https://github.com/myxewd/ez-kea.git /srv/ez-kea-src
cd /srv/ez-kea-src/deploy

docker build -t ez-kea:0.10.2.1 \
  --build-arg EZK_REF=v0.10.2.1 \
  ./ezkea
```

| build arg | default | meaning |
|---|---|---|
| `EZK_SRC` | `https://github.com/myxewd/ez-kea.git` | where to fetch the project from |
| `EZK_REF` | `main` | branch, tag or commit, checked out detached |
| `EZKEA_UID` / `EZKEA_GID` | `10001` | uid/gid the container process runs as |

What was built:

```bash
docker run --rm --entrypoint cat ez-kea:0.10.2.1 /opt/ez-kea/.build-git-sha
```

For an air-gapped host, run `docker save ez-kea:0.10.2.1 | gzip > ez-kea-0.10.2.1.tgz`
where GitHub is reachable, then `docker load` there.

---

## 3. Build the Kea image

The Kea container is deliberately boring: the distribution's
`kea-dhcp4-server` plus an entrypoint that lets it survive restarts.

```bash
cd /srv/ez-kea-src/deploy/kea
docker build -t ez-kea-kea:2.2.0 .
```

Debian bookworm ships Kea **2.2.0**, which is also the version whose config
syntax EZ-KEA writes by default (`control-socket`, singular). For Kea 3.x you
need ISC's own packages, the `control-sockets` list spelling, and the four 3.x
behaviour differences listed in `testbed/README.md`; `testbed/Dockerfile.kea-3.2`
has the repository setup.

---

## 4. The three things that have to be right

Each item here fails *quietly*: the daemon starts, the UI saves, and something
later does not work. Read them once now; they are referenced again where each
one matters.

### 4.1 The daemon has to be on the clients' network

DHCP is UDP/67 broadcast and needs a relay for every subnet that is not local.
Docker's default bridge does not carry the host NIC's broadcast traffic, so a
container on it can only serve other containers.

Two layouts work:

**A. Run the daemon on the host (recommended).** Install
`kea-dhcp4-server` on the host and leave the `kea` service out of compose.
EZ-KEA still holds the file; §7.1 covers the command shim and the one thing it
cannot do for you.

**B. Give the container `network_mode: host`.** Then `kea-dhcp4` sees the host's
NICs by name, and `interfaces-config` must name the right one
(`ens192`, `eth1`, …). The `run/` mount stays shared with EZ-KEA, so reloads
work unchanged. Nothing is published with `network_mode: host`.

### 4.2 The control socket has to be reachable by uid 10001

    srwxr-xr-x 1 10001 10001 0 ... kea-dhcp4-ctrl.sock    <- reload works
    srwxr-xr-x 1 _kea  _kea  0 ... kea-dhcp4-ctrl.sock    <- reload gets EPERM
    srwxr-xr-x 1 root  root  0 ... kea-dhcp4-ctrl.sock    <- reload gets EPERM

Kea creates its UNIX control socket mode **0755**, owned by the user it runs
as. A UNIX socket is usable by another account only when the **owner**
credentials match (socket owner, or the owner of an ancestor directory) *and*
the mode grants it. A supplementary group does not help, and the group bits of a
root-owned socket are not consulted at all. So both of these have to hold:

1. **The daemon must not run as root.** The Kea image's entrypoint drops to
   `_kea` with `setpriv` for exactly this reason. Kea itself does not switch
   user; started as root it stays root and lays down a root-owned socket.
2. **The socket's directory must be owned by uid 10001.** The daemon creates
   the socket with the *directory's* owner, so a directory owned by 10001 is a
   handover point: what Kea creates is already owned by whoever has to connect.

One command on the host, before the first start:

```bash
sudo chown 10001:10001 /opt/ez-kea/kea/run && sudo chmod 0750 /opt/ez-kea/kea/run
```

(Run it against the host path. Ownership set from *inside* a container on a
Windows or macOS Docker Desktop bind mount does not stick, so on those hosts set
it in the Docker Desktop file-sharing settings or run the daemon on a Linux
host — which is where a DHCP server belongs anyway.)

Kea 3.x additionally refuses a control-socket directory more permissive than
0750, which the same command satisfies.

**[verify]** after the first start:

```bash
ls -l /opt/ez-kea/kea/run/kea-dhcp4-ctrl.sock
# expect  srwxr-xr-x ... 10001 10001     or     ... ezkea ezkea
```

If the owner is `_kea` or `root`, the handover did not happen: the directory
was not owned by 10001 when Kea created the socket. Fix the ownership and
restart the `kea` container (a running daemon keeps the socket it made). The Kea
entrypoint prints a warning naming this at startup rather than leaving you to
discover it from a UI error later.

Optionally, make it explicit in the config instead — Kea 2.2+ can set the
socket's owner and mode itself:

```json
"control-sockets": [
  { "socket-type": "unix",
    "socket-name": "/run/kea/kea-dhcp4-ctrl.sock",
    "user-context": { "user": "ezkea", "group": "ezkea", "mode": "0750" } }
]
```

### 4.3 The syntax check has to be able to read the config

Every save is handed to `kea-dhcp4 -t` before it is kept. That binary reads
`/etc/kea/kea-dhcp4.conf`, and ISC packages ship `/etc/kea` as `_kea:_kea
0750`, which uid 10001 cannot read. Pick one:

- `chmod 755 /etc/kea` on the host (its contents are not a secret; the
  backups hold the same data), **or**
- put the EZ-KEA account in the `_kea` group at build time. This image does not
  add a supplementary group, because §4.2 shows a group would not have made the
  control socket work anyway.

**[verify]** `docker exec ez-kea id` should print `uid=10001 gid=10001`.

When the check cannot run at all, the save still goes through — an environment
problem must not make the UI unusable — but the response says the config was
saved *without* being checked. That message is the signal this section was
skipped.

---

## 5. Configure and start

### 5.1 Environment

```bash
cd /srv/ez-kea-src/deploy
cp kea.env.example kea.env

SECRET=$(openssl rand -hex 32)
sed -i "s/^SECRET_KEY=dev/SECRET_KEY=$SECRET/" kea.env
chmod 600 kea.env
```

Adjust the paths in `kea.env` if they are not `/opt/ez-kea`.

### 5.2 The first config file

EZ-KEA bootstraps a skeleton if the file is missing, but that skeleton has no
interfaces and no subnets: the daemon would start and answer nothing. Create it
once:

```bash
sudo cp /srv/ez-kea-src/deploy/init/kea-dhcp4.conf /opt/ez-kea/kea/conf/kea-dhcp4.conf
sudo $EDITOR /opt/ez-kea/kea/conf/kea-dhcp4.conf    # set "interfaces": ["eth0"]
```

After this, **stop editing the file**: EZ-KEA owns it now, backs it up before
every write, and refuses to keep a change Kea rejects.

### 5.3 Start

```bash
cd /srv/ez-kea-src/deploy
docker compose --env-file kea.env up -d
docker compose ps        # both healthy
docker compose logs -f ezkea
```

EZ-KEA listens on `127.0.0.1:8080` and **refuses to start on a non-loopback
address while `SECRET_KEY` is still `dev`**. That refusal is the feature.

---

## 6. First login, and a real check

1. Reach the UI through the reverse proxy (§8), or
   `ssh -L 8080:127.0.0.1:8080 root@kea-host` while setting up.
2. The seeded account is `admin` / `changeme` and forces a username and password
   change at first login. Do that immediately.
3. **Global Settings → Save** once. This is the first time EZ-KEA runs
   `kea-dhcp4 -t` and the first time it reloads the daemon: if §4 is wrong,
   this is where you find out, with the reason in the message.
4. Add a subnet with a small pool and let a client take a lease.
   `docker compose exec kea kea-dhcp4 -t /etc/kea/kea-dhcp4.conf; echo $?`
   should print `0`, and `/opt/ez-kea/kea/lib/kea-leases4.csv` should grow.

A save that says *"Kea rejected this configuration"* saved nothing: the new file
was rolled back and the previous one restored. Kea's own words are in the
message.

---

## 7. Host-installed alternatives

### 7.1 The Kea command shim

EZ-KEA executes only `kea-dhcp4`, `kea-dhcp6`, `keactrl` and `docker`
(allowlisted by basename). If the daemon runs on the host while EZ-KEA runs in
a container, the container needs a way to run the host's binary; mount the
host's files and use a `kea-dhcp4` wrapper:

```yaml
# ezkea service, additions
    pid: "host"
    volumes:
      - /usr/sbin/kea-dhcp4:/usr/local/bin/kea-dhcp4:ro
      - /usr/lib/x86_64-linux-gnu/kea:/usr/lib/x86_64-linux-gnu/kea:ro
```

`kea-dhcp4 -t /etc/kea/kea-dhcp4.conf` then reads and validates the mounted
file directly. The reload, however, has to reach the host daemon's socket: with
`/run/kea` bind-mounted into the container and ownership set as in §4.2, the
`control-socket` strategy works unchanged, because the socket is the same
file in both namespaces. `sighup` is only for containerised daemons.

### 7.2 The host daemon must run as `_kea`

```bash
systemctl edit kea-dhcp4-server     # add:
#   [Service]
#   User=_kea
#   Group=_kea
sudo install -d -o 10001 -g 10001 -m 0750 /run/kea
```

This is the same rule as §4.2, applied on the host, and it is the documented
integration pattern in `testbed/README.md`.

### 7.3 systemd unit for the compose project

```ini
# /etc/systemd/system/ez-kea.service
[Unit]
Description=EZ-KEA web UI (Docker)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/srv/ez-kea-src/deploy
ExecStart=/usr/bin/docker compose --env-file kea.env up -d --remove-orphans
ExecStop=/usr/bin/docker compose --env-file kea.env down

[Install]
WantedBy=multi-user.target
```

### 7.4 Applying over the daemon's HTTP control socket

Kea 2.7.2 and later can expose the same JSON command channel over HTTP/HTTPS,
so EZ-KEA never has to touch the daemon's process at all. Add a listener to the
daemon's `control-sockets` list (the 3.0+ spelling; Kea 2.x had no HTTP listener
at all), then pick **Kea HTTP API** in Application Settings:

```json
"Dhcp4": {
    "control-sockets": [
        {"socket-type": "unix", "socket-name": "/run/kea/kea-dhcp4-ctrl.sock"},
        {"socket-type": "http", "socket-address": "127.0.0.1", "socket-port": 8000}
    ]
}
```

Applying then runs `config-test` on the document EZ-KEA just wrote and stops
there if Kea refuses it, so a configuration the daemon will not load never
reaches it; `config-set` follows only on success. The file remains the source of
truth (config-set changes memory only), which is what keeps Backups/Restore
meaningful.

#### Kea 3.x requires authentication on that channel

A stock Kea 3.x daemon that is given an HTTP control socket **refuses to start**:

    DHCP4_INIT_FAIL configuration error using file '...': Unsecured HTTP control channel

That is the runtime security policy (§*Runtime Security Policy Checking*) and it
is fatal unless the daemon is started with `-X`. The same policy also rejects a
clear-text password in Kea's own config (`use of clear text 'password' is NOT
SECURE`), so the compliant shape is basic auth backed by files:

```json
"control-sockets": [{
    "socket-type": "http",
    "socket-address": "127.0.0.1",
    "socket-port": 8000,
    "authentication": {
        "type": "basic",
        "realm": "kea-dhcp4",
        "clients": [{"user-file": "/etc/kea/users.txt",
                     "password-file": "/etc/kea/passwords.txt"}]
    }
}]
```

`users.txt` holds one user per line, `passwords.txt` the matching passwords, both
mode 0640 owned by the daemon's user. Give EZ-KEA the same credential in its own
environment, never in a file it writes:

    KEA_HTTP_USER=admin
    KEA_HTTP_PASSWORD=...        # keep kea.env at mode 0600

Without those, every apply fails with `HTTP 401: Unauthorized` and the message
says exactly that. Verified against Kea 3.2.1, including that `config-set`
leaves the file on disk untouched (it only changes the running configuration),
which is why an apply to a *remote* endpoint follows it with `config-write` —
see below.

#### Applying to a remote endpoint merges, it does not overwrite

A remote endpoint's configuration is somebody else's daemon, reached over a
channel that is not EZ-KEA's own file. `config-set` replaces that daemon's
*entire* running configuration, so pushing the local mirror as-is would undo
anything that changed on the daemon since the last pull. Apply therefore:

1. reads the daemon's configuration with `config-get`;
2. replays onto it only the changes EZ-KEA itself made since the last successful
   pull (`diff(pulled, mirror)`), leaving every other key — hooks, control
   sockets, lease database, subnets added elsewhere — exactly as the daemon
   reports it;
3. `config-test`, then `config-set`, on the merged document; and
4. `config-write`, so the daemon's own file keeps the change and a restart does
   not revert it.

If the daemon changed the same setting EZ-KEA changed, Apply refuses: nothing is
pushed, the daemon keeps running its previous configuration, and the unapplied
edit stays in the local copy until you reconcile the two. A local copy that was
never successfully pulled (or that belongs to a different endpoint URL) is
refused the same way instead of being pushed.

**Pull is a merge too.** A restart throws away a remote daemon's in-memory
configuration, so the next `config-get` reports the older file on disk. Rather
than replacing the mirror with it and silently dropping work that has not been
applied, **Pull config** merges the same way Apply does: what the daemon reports
comes in, edits made here survive, and the pulled document becomes the new
baseline. On a conflict the pull is declined and the local copy is left exactly
as it was. Tick **discard my edits** on the Endpoints page to take the daemon's
copy instead; the replaced copy is backed up first, so Backups/Restore can still
bring it back.

The remote endpoint's **Leases** page needs the daemon to answer `lease4-get-all`
(`lease6-get-all` for DHCPv6). Since Kea 3.0 those commands live in the lease
commands hook, which the daemon has to load explicitly:

```json
"hooks-libraries": [
    {"library": "/usr/lib/x86_64-linux-gnu/kea/hooks/libdhcp_lease_cmds.so"}
]
```

The library ships in the `isc-kea-hooks` package (Debian/Ubuntu). Without it,
the page explains that the command is unsupported instead of showing an empty
table. EZ-KEA also skips its local `kea-dhcpN -t` check for a remote endpoint:
the mirror references that daemon's hook libraries and auth files, which need
not exist on EZ-KEA's host, so the daemon's own `config-test` is the authority.

Three more things to know:

- **Every daemon needs its own address/port.** Kea refuses to share one between
  `kea-dhcp4` and `kea-dhcp6`; the documented defaults are
  `127.0.0.1:8000` for DHCPv4 and `[::1]:8000` for DHCPv6. The override is per
  version for the same reason: `KEA_HTTP_URL` (or the DHCPv4 page's field) and
  `KEA_HTTP_URL6` (DHCPv6 page).
- **A port published out of the Kea container is reachable from EZ-KEA's
  container** as long as you set **Kea HTTP Control URL** to the address EZ-KEA
  can see (e.g. `http://kea:8000/`), because the `control-sockets` entry
  describes the daemon's *own* namespace. Credentials then cross that network:
  keep it on a private bridge, or use `https`.
- **HTTPS is supported for the transport only.** Client certificates (which
  `cert-required` defaults to demanding) are not, so an HTTPS listener must set
  `cert-required` to `false`; the server certificate is verified against the
  system trust store.

---

## 8. Reverse proxy and TLS

EZ-KEA speaks plain HTTP on `127.0.0.1:8080` and does not terminate TLS. It
sets no `ProxyFix` middleware: do not add `X-Forwarded-Prefix`, and serve it at
the root of its own hostname.

```nginx
server {
    listen 443 ssl http2;
    server_name dhcp-admin.example.com;
    ssl_certificate     /etc/ssl/certs/dhcp-admin.pem;
    ssl_certificate_key /etc/ssl/private/dhcp-admin.key;

    # The UI is the way into your DHCP configuration. Keep it on the
    # management network, never on a network with DHCP clients.
    allow 10.0.0.0/8;
    deny  all;

    client_max_body_size 8m;     # the raw config editor posts the whole file

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s; # a save runs kea-dhcp4 -t
    }
}
```

---

## 9. Operations

| Task | How |
|---|---|
| Update | `docker build --build-arg EZK_REF=v0.10.2.1 -t ez-kea:0.10.2.1 ./ezkea`, change the tag in `docker-compose.yml`, `docker compose --env-file kea.env up -d`. `/data` is untouched. |
| Roll back a config | Global Settings → Backup/Restore, or copy the newest file out of `/opt/ez-kea/data/backups`. |
| Back up EZ-KEA | `tar czf ez-kea-$(date +%F).tgz /opt/ez-kea/data`. The two `*-index.db` files are disposable; `ez-kea.db` and `ez-kea-settings.json` are not. |
| Read the daemon log | `/opt/ez-kea/kea/log/kea/kea-dhcp4.log`, or Log Search in the UI. |
| Rotate the session key | Change `SECRET_KEY` and `docker compose up -d`; everyone is logged out. |
| Licence key | `EZ_KEA_LICENSE_KEY` in `kea.env`, or paste it in the UI. Verified offline. |

Health:

```bash
curl -fsS http://127.0.0.1:8080/healthz && echo ok    # 200 when the app is up
docker compose exec kea kea-dhcp4 -t /etc/kea/kea-dhcp4.conf; echo $?
```

Both run automatically: the first as EZ-KEA's healthcheck, the second as the
Kea container's.

---

## 10. Security notes

- **No read-only role.** Any authenticated account can create, change and delete
  DHCP configuration. One account per person; remove leavers.
- **The UI is privileged.** Treat `127.0.0.1:8080` like a root SSH key.
- **`SECRET_KEY` is the session key.** Rotate it if `kea.env` was ever exposed.
- **Backups contain whatever you typed**, including options naming internal
  hosts. They live under `/opt/ez-kea/data`.
- **No capabilities needed.** Do not run the UI container privileged: the
  privileged work (the syntax check, the reload) goes through the files and
  sockets in §4.

---

## 11. Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `Refusing to start: SECRET_KEY is still the insecure default` | `kea.env` not loaded, or `HOST` is non-loopback with `SECRET_KEY=dev` | `docker compose --env-file kea.env config` |
| Save says it was saved *without* being checked | the container cannot run `kea-dhcp4` or cannot read `/etc/kea` | §4.3 |
| Apply Changes: *"… control socket: … Permission denied"* | §4.2 — socket owner or mode | `ls -l /opt/ez-kea/kea/run` |
| Apply Changes: *"… No such file or directory"* | the daemon never started, or uses another socket name | `docker compose logs kea`; compare `socket-name` |
| Daemon crash-loops with `DHCP4_ALREADY_RUNNING` | stale PID file in a reused container filesystem | the entrypoint clears it; otherwise `docker compose up -d --force-recreate kea` |
| Clients get no address | wrong NIC in `interfaces-config`, or the daemon is not on the client network | §4.1; `docker compose logs kea` |
| Dashboard badge: "Unreachable" | the socket path in the config is not the one the daemon created | §4.2 |
| The UI edits a config you did not point it at | `DHCP_CONFIG_FILE` unset, so discovery found a packaged `/etc/kea` | `docker compose exec ezkea env` |

---

## 12. Not the same thing as `testbed/`

`testbed/` proves EZ-KEA against a real Kea with simulated VLANs and clients on
one host. It is a test rig, not a deployment template: paths relative to the
checkout, `docker exec` wrappers for the Kea commands, SIGHUP for reloads. This
document is the deployment path.