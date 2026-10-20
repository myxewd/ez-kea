# deploy/kea/Dockerfile - the Kea daemon container.
#
#   cd deploy/kea && docker build -t ez-kea-kea:2.2.0 .
#
# Debian bookworm ships Kea 2.2.0, which is also the version whose config
# syntax EZ-KEA writes by default (a singular "control-socket" object). For
# Kea 3.x see testbed/Dockerfile.kea-3.2: the packages come from ISC's own
# repository, the spelling becomes a "control-sockets" list, keactrl no longer
# exists, and the logger path must be under /var/log/kea.
FROM debian:bookworm-slim

ARG KEA_PACKAGE=kea-dhcp4-server

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        "${KEA_PACKAGE}" tzdata \
 && rm -rf /var/lib/apt/lists/*

# Every directory the daemon writes to, created here so the first start does
# not depend on a host directory happening to exist. Ownership is fixed again
# by the entrypoint, because a bind mount hides whatever was baked in.
RUN install -d -o _kea -g _kea -m 0750 /run/kea /var/lib/kea /var/log/kea

COPY entrypoint.sh /usr/local/bin/kea-entrypoint.sh
RUN chmod 0755 /usr/local/bin/kea-entrypoint.sh

# The daemon stays in the foreground as PID 1 so it receives signals directly.
ENTRYPOINT ["/usr/local/bin/kea-entrypoint.sh"]
CMD ["kea-dhcp4", "-c", "/etc/kea/kea-dhcp4.conf"]
