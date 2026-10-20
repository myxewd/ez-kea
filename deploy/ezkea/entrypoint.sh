#!/bin/sh
# deploy/ezkea/entrypoint.sh
#
# Runs as root, hands over, and leaves. There is no long-lived root process in
# this container.
#
# Only ownership is fixed here, never the Kea config file itself: that file is
# bind-mounted from the host, and a chown of it would rewrite the operator's
# own ownership on every restart (it cannot change the owner of a host file to
# an id this container does not map to anyway).
set -e

# ez-kea.db, the settings file, the two search indexes and the backups. The
# build gives the account these ids, so this is a no-op except when the
# operator overrode EZKEA_UID / EZKEA_GID to match a host account.
chown -R "${EZKEA_UID:-10001}:${EZKEA_GID:-10001}" /data 2>/dev/null || true

# The Kea daemon runs as _kea (uid 101, gid 103 in the official Debian image)
# and creates its UNIX control socket inside its own run directory. A socket
# is only connectable by a process that shares *the owner's* credentials, so
# "reload over the control socket" works exactly when this directory is owned
# by the EZ-KEA account and is not world-writable. Getting this wrong is
# silent: the daemon starts fine and every reload fails with EPERM.
install -d -o "${EZKEA_UID:-10001}" -g "${EZKEA_GID:-10001}" -m 0750 /run/kea

# exec: the app becomes PID 1 so it receives SIGTERM directly.
exec "$@"
