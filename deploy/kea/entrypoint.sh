#!/bin/sh
# deploy/kea/entrypoint.sh
#
# Started as root by the container runtime. Checks what has to be true for
# EZ-KEA to be able to talk to this daemon, fixes what it can, then drops to
# the daemon's own account. Everything below was worked out by testing it, not
# assumed: each check is a failure that otherwise shows up much later as an
# 'EPERM' in the UI.
set -e

KEA_USER="${KEA_USER:-_kea}"
KEA_GROUP="${KEA_GROUP:-_kea}"
RUN_DIR="/run/kea"

log() { echo "[kea] $*"; }

if [ "$(id -u)" = "0" ]; then
    # These are normally bind mounts of host directories, so they have to exist
    # and be writable by the account the daemon will run as.
    for dir in "$RUN_DIR" /var/lib/kea /var/log/kea /var/log/kea/kea; do
        [ -d "$dir" ] || mkdir -p "$dir"
    done
    for dir in /var/lib/kea /var/log/kea; do
        if [ "$(stat -c %u "$dir")" != "$(id -u "$KEA_USER")" ]; then
            log "hint: $dir is not owned by $KEA_USER; the daemon needs to write its leases/logs there"
        fi
    done

    # The control socket is created with THIS directory's owner, mode 0755.
    # EZ-KEA connects as uid 10001, and a UNIX socket is only usable by a
    # process matching the socket's owner (or the owner of an ancestor
    # directory); a supplementary group does not help, and a root-owned
    # socket's group bits are not consulted at all. So the directory has to
    # belong to 10001, and the daemon must not be root when it binds -- both
    # are needed, and both failures look identical in `ls`.
    if [ "$(stat -c %u "$RUN_DIR")" = "0" ]; then
        log "WARNING: $RUN_DIR is owned by root. The control socket laid down here will not"
        log "         be reachable by EZ-KEA (uid 10001). chown the corresponding host"
        log "         directory to 10001:10001. Kea is still starting."
    fi

    # A PID file naming a live process makes Kea refuse to start
    # (DHCP4_ALREADY_RUNNING). Inside a reused container filesystem the PID it
    # names is Kea's own PID 1, so this is a permanent crash loop with
    # `restart: unless-stopped`. A PID file present at container start is stale
    # by definition.
    rm -f "$RUN_DIR"/*.pid /var/run/kea/*.pid 2>/dev/null || true

    # Kea opens its log file itself and creates it 0640. If that file does not
    # exist yet and Kea is running as _kea, EZ-KEA (uid 10001) cannot read the
    # log through the shared directory. Pre-create the files readable so Kea
    # opens an existing one for append instead. Both /var/log/kea/x.log and
    # /var/log/kea/kea/x.log are covered: Kea 3.x insists on the deeper path.
    for f in /var/log/kea/kea-dhcp4.log /var/log/kea/kea-dhcp6.log \
             /var/log/kea/kea/kea-dhcp4.log /var/log/kea/kea/kea-dhcp6.log; do
        [ -e "$f" ] || : > "$f"
        chmod 0644 "$f" 2>/dev/null || true
    done

    log "dropping privileges to $KEA_USER:$KEA_GROUP"
    exec setpriv --reuid "$KEA_USER" --regid "$KEA_GROUP" --init-groups \
         --inh-caps=-all --ambient-caps=-all "$@"
fi

exec "$@"