# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import os

# Load environment variables (from OS environment, no dotenv needed)


class Config:
    DHCP_CONFIG_FILE = os.getenv("DHCP_CONFIG_FILE", "./data/kea-dhcp4.conf")
    DHCP_LEASES_FILE = os.getenv("DHCP_LEASES_FILE", "./data/kea-leases4.csv")
    DHCP_LOG_FILE    = os.getenv("DHCP_LOG_FILE",    "./data/kea-dhcp4.log")

    # Whether the path above was actually chosen by the operator (as opposed to
    # being this file's default). Auto-discovery uses this: a path you set
    # explicitly is where EZ-KEA must work, even when the Kea package happens to
    # have installed its own /etc/kea/kea-dhcp4.conf — silently managing that
    # file instead would mean every edit lands somewhere you are not looking.
    DHCP_CONFIG_FILE_EXPLICIT = os.getenv("DHCP_CONFIG_FILE") is not None

    # DHCPv6 equivalents — mirror the DHCPv4 settings above exactly.
    DHCP6_CONFIG_FILE = os.getenv("DHCP6_CONFIG_FILE", "./data/kea-dhcp6.conf")
    DHCP6_CONFIG_FILE_EXPLICIT = os.getenv("DHCP6_CONFIG_FILE") is not None
    DHCP6_LEASES_FILE = os.getenv("DHCP6_LEASES_FILE", "./data/kea-leases6.csv")
    DHCP6_LOG_FILE    = os.getenv("DHCP6_LOG_FILE",    "./data/kea-dhcp6.log")

    # --- Public demo --------------------------------------------------------
    # Off unless explicitly switched on, and the only thing that switches it on
    # is demo/systemd/ez-kea-demo.service. When set, the login page arrives with
    # the published demo credentials already in the fields: a visitor still sees
    # the real login screen (which is part of what the demo is showing off) but
    # doesn't have to go hunting for a password to get past it. Never enable
    # this on a deployment that manages a real network.
    PUBLIC_DEMO          = os.getenv("PUBLIC_DEMO", "").strip().lower() in ("1", "true", "yes", "on")
    PUBLIC_DEMO_USERNAME = os.getenv("PUBLIC_DEMO_USERNAME", "demo")
    PUBLIC_DEMO_PASSWORD = os.getenv("PUBLIC_DEMO_PASSWORD", "demo")

    # --- Log search index ---------------------------------------------------
    # Backing store for the searchable log history (see core/log_index.py).
    # Deliberately a separate file from ez-kea.db: the index is disposable and
    # can grow large, and neither of those should ever be true of the database
    # holding user accounts. Deleting it is safe — it rebuilds from the logs.
    LOG_INDEX_DB      = os.getenv("LOG_INDEX_DB", "./data/ez-kea-logindex.db")
    LOG_INDEX_ENABLED = os.getenv("LOG_INDEX_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
    # Seconds between background ingest passes. Ingest is incremental, so this
    # is the worst-case staleness of the Logs page, not a batch window.
    LOG_INDEX_INTERVAL = int(os.getenv("LOG_INDEX_INTERVAL", "60"))
    # How far back searchable history is kept. Generous by default because the
    # point of the index is answering questions about months ago; set to 0 to
    # keep everything and manage disk yourself.
    LOG_INDEX_RETENTION_DAYS = int(os.getenv("LOG_INDEX_RETENTION_DAYS", "365"))

    # --- Lease/reservation search index -------------------------------------
    # Backing store for fast search/filter/sort/export on the Leases and
    # Reservations pages (see core/state_index.py). Separate file from
    # ez-kea.db and from LOG_INDEX_DB for the same reason both of those are
    # separate: disposable, safe to delete, rebuilds itself.
    STATE_INDEX_DB      = os.getenv("STATE_INDEX_DB", "./data/ez-kea-stateindex.db")
    STATE_INDEX_ENABLED = os.getenv("STATE_INDEX_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
    # Seconds between background ingest passes. Lease/reservation state is
    # checked more interactively than logs ("did this device just get an
    # IP?"), so this defaults well below LOG_INDEX_INTERVAL.
    STATE_INDEX_INTERVAL = int(os.getenv("STATE_INDEX_INTERVAL", "20"))

    # Where an "http" Kea instance's pulled configuration is mirrored. One
    # directory per instance; safe to delete (it is a cache of what the daemon
    # reported, refreshed by "Pull" on the Endpoints page).
    INSTANCE_DIR     = os.getenv("INSTANCE_DIR",     "./data/instances/")
    BACKUP_DIR       = os.getenv("BACKUP_DIR",       "./data/backups/")
    SETTINGS_FILE    = os.getenv("SETTINGS_FILE",    "./data/ez-kea-settings.json")
    SECRET_KEY       = os.getenv("SECRET_KEY",       "dev")

    # SQLite DB for users, auth state, and license key.
    # Kea daemon settings continue to live in ez-kea-settings.json.
    SQLALCHEMY_DATABASE_URI        = os.getenv("DATABASE_URL", "sqlite:///" + os.path.abspath("./data/ez-kea.db"))
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Kea commands — overridable at runtime via UI (see settings_manager)
    KEA_DHCP4_CMD = os.getenv("KEA_DHCP4_CMD", "kea-dhcp4")
    KEA_DHCP6_CMD = os.getenv("KEA_DHCP6_CMD", "kea-dhcp6")
    KEA_CTRL_CMD  = os.getenv("KEA_CTRL_CMD",  "keactrl")

    # --- Docker-deployment settings -----------------------------------------
    # When KEA_DHCP4_CMD/KEA_CTRL_CMD exec into a container (e.g.
    # "docker exec <container> kea-dhcp4"), DHCP_CONFIG_FILE/DHCP_LOG_FILE
    # above are HOST paths that EZ-KEA itself reads/writes — they are not
    # necessarily valid inside the container's own filesystem namespace (the
    # volume mount may expose the same file at a different in-container
    # path). These two settings let a Docker deployment tell EZ-KEA what path
    # to pass to `-t` / write into Kea's own logger config instead. Left
    # blank (the default), both fall back to the host path above, which is
    # exactly correct for bare-metal/non-Docker deployments — so existing
    # non-Docker setups are unaffected.
    DHCP_CONFIG_FILE_IN_CONTAINER = os.getenv("DHCP_CONFIG_FILE_IN_CONTAINER", "")
    DHCP_LOG_FILE_IN_CONTAINER    = os.getenv("DHCP_LOG_FILE_IN_CONTAINER", "")
    DHCP6_CONFIG_FILE_IN_CONTAINER = os.getenv("DHCP6_CONFIG_FILE_IN_CONTAINER", "")
    DHCP6_LOG_FILE_IN_CONTAINER    = os.getenv("DHCP6_LOG_FILE_IN_CONTAINER", "")

    # Reload strategy, one of:
    #   "keactrl"        (default) runs `KEA_CTRL_CMD reload`
    #   "control-socket" sends `config-reload` on the daemon's UNIX control
    #                    socket -- no external binary, and the only strategy
    #                    that reports whether the reload actually succeeded.
    #                    Required for Kea 3.x from ISC's own packages, which
    #                    ship no keactrl at all.
    #   "sighup"         runs `docker kill -s HUP <KEA_DOCKER_CONTAINER>`, for
    #                    minimal Kea Docker images that lack the
    #                    /etc/kea/keactrl.conf keactrl needs to run at all.
    # This is an explicit, documented choice, never inferred: guessing wrong
    # here is worse than requiring one extra setting.
    KEA_RELOAD_STRATEGY = os.getenv("KEA_RELOAD_STRATEGY", "keactrl")
    KEA_DOCKER_CONTAINER = os.getenv("KEA_DOCKER_CONTAINER", "")

    # Endpoint of the daemon's own HTTP/HTTPS control channel, for the "http"
    # reload strategy (config-test, then config-set -- see core/kea_ctrl.py).
    # Blank means "read it out of the Kea config": the http/https entry in the
    # daemon's control-sockets list, whose socket-address/socket-port default to
    # 127.0.0.1:8000 (::1:8000 for DHCPv6). Set it explicitly when the listener
    # is not described there -- a daemon in another container with a published
    # port, another host, or an HTTPS endpoint behind a proxy.
    # Per version: the two daemons cannot share an address/port pair, so one
    # shared override could only ever point at one of them.
    KEA_HTTP_URL  = os.getenv("KEA_HTTP_URL", "")
    KEA_HTTP_URL6 = os.getenv("KEA_HTTP_URL6", "")

    # HTTP Basic credentials for that control channel. Environment-only on
    # purpose: secrets do not belong in ez-kea-settings.json, and Kea 3.x will
    # not start with an unauthenticated HTTP control channel anyway. Pair these
    # with the daemon's "authentication": {"type": "basic", ...} block, which
    # must use user-file/password-file -- Kea refuses a clear-text password in
    # its own config with "use of clear text 'password' is NOT SECURE".
    KEA_HTTP_USER     = os.getenv("KEA_HTTP_USER", "")
    KEA_HTTP_PASSWORD = os.getenv("KEA_HTTP_PASSWORD", "")
