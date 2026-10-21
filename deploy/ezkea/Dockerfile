# deploy/ezkea/Dockerfile — the EZ-KEA container.
#
# Build context is this directory, NOT the repository root, so the project is
# fetched from git at image-build time instead of copied from a working tree:
#
#   docker build -t ez-kea:0.10.2.2 \
#     --build-arg EZK_SRC=https://github.com/myxewd/ez-kea.git \
#     --build-arg EZK_REF=v0.10.2.2 deploy/ezkea
#
# For a fork, point the two arguments at the fork and the ref you want. EZK_REF
# may be a branch, a tag or a commit SHA, but it has to exist on EZK_SRC -- push
# the commit/tag before building it. Only the application tree (app.py,
# ez_kea/, templates/, static/, LICENSE) reaches the runtime image; the
# repository's tests, docs, demo and testbed stay in the fetch stage.

# ── fetch ────────────────────────────────────────────────────────────────────
# Git and the checkout live in a throwaway stage. The runtime image therefore
# carries no VCS client, and does not depend on a binary copied out of another
# image: a binary from an Alpine image is linked against musl and cannot run on
# this glibc base at all.
FROM debian:bookworm-slim AS source

ARG EZK_SRC=https://github.com/myxewd/ez-kea.git
ARG EZK_REF=main

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# One shallow fetch of exactly the requested ref. Fetching the ref by name puts
# its commit in FETCH_HEAD, which is what makes a branch, a tag and a commit
# SHA resolve the same way. Checking out "main" directly would not: after this
# fetch the branch only exists as refs/remotes/origin/main, and git checkout
# treats the bare name as a path.
RUN set -eux; \
    git init -q .; \
    git remote add origin "${EZK_SRC}"; \
    git fetch -q --depth 1 origin "${EZK_REF}"; \
    git checkout -q --detach FETCH_HEAD; \
    test -f requirements.txt; \
    git rev-parse HEAD > .build-git-sha

# ── runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /opt/ez-kea

# Runtime dependencies in their own layer, so a source change re-runs only the
# fetch and the copies below. requirements.txt also lists tooling the server
# never imports (pytest, reportlab); those are removed before the layer is
# committed.
COPY --from=source /src/requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
 && python -m pip uninstall -y -q pytest reportlab \
 && python -m pip check \
 && python -m pip uninstall -y -q pip \
 && rm -rf /root/.cache

# The application account. 10001 is the default the entrypoint and the app's
# own settings file assume; pass EZKEA_UID / EZKEA_GID to the container to
# match a host account that will own /data, or override here with --build-arg.
ARG EZKEA_UID=10001
ARG EZKEA_GID=10001
# app.py creates ./instance and ./data relative to the checkout on startup.
# They are unused in this deployment (state is mounted at /data), but they have
# to exist and belong to the account the process runs as.
RUN groupadd --system --gid "${EZKEA_GID}" ezkea \
 && useradd --system --uid "${EZKEA_UID}" --gid "${EZKEA_GID}" \
        --home-dir /data --no-create-home --shell /usr/sbin/nologin ezkea \
 && install -d -o ezkea -g ezkea /data \
 && install -d -o ezkea -g ezkea /var/run/ez-kea \
 && install -d -o ezkea -g ezkea /opt/ez-kea/instance /opt/ez-kea/data

# Only what the application serves and imports; everything else stayed in the
# fetch stage. The licence text ships with the image alongside the app.
COPY --from=source /src/app.py ./
COPY --from=source /src/ez_kea ./ez_kea
COPY --from=source /src/templates ./templates
COPY --from=source /src/static ./static
COPY --from=source /src/LICENSE ./LICENSE
COPY --from=source /src/.build-git-sha ./

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# The one path EZ-KEA writes to that is not a bind mount of Kea's own files:
# database, settings, backups, and the two disposable search indexes.
VOLUME ["/data"]

EXPOSE 8080

# Answered by the app itself (database open, config file readable), not by the
# container runtime. Uses only the standard library: this image has no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "app.py"]
