# deploy/ezkea/Dockerfile — the EZ-KEA container.
#
# Build context is this directory, NOT the repository root, so the project is
# fetched from git at image-build time instead of copied from a working tree:
#
#   docker build -t ez-kea:0.10.1 \
#     --build-arg EZK_SRC=https://github.com/myxewd/ez-kea.git \
#     --build-arg EZK_REF=v0.10.1 deploy/ezkea
#
# For a fork, point the two arguments at the fork and the commit you want.
# The image does NOT contain the repository's tests, docs or screenshots --
# only what the application serves and imports.
FROM python:3.12-slim-bookworm

ARG EZK_SRC=https://github.com/myxewd/ez-kea.git
ARG EZK_REF=main

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8080

# git is what fetches the project. It comes from its own image so the final
# image never carries a VCS client (nor ca-certificates on its account).
COPY --from=alpine/git:latest /usr/bin/git /usr/local/bin/git

WORKDIR /opt/ez-kea

# Shallow fetch of exactly one ref. Mapping every branch to a remote-tracking
# ref is what lets a branch name, a tag and a raw commit SHA all resolve with
# the same command; a plain --branch fetch cannot take a SHA at all.
RUN set -eux; \
    git init -q . ; \
    git remote add origin "${EZK_SRC}" ; \
    git fetch -q --depth 1 origin \
        "+refs/heads/*:refs/remotes/origin/*" \
        "+refs/tags/*:refs/tags/*" ; \
    git checkout -q --detach ${EZK_REF} ; \
    test -f requirements.txt ; \
    git rev-parse HEAD > .build-git-sha

# Runtime dependencies in their own layer, so a source change re-runs only the
# fetch above. requirements.txt also lists tooling the server never imports
# (pytest, reportlab); those are removed again before the layer is committed.
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
RUN groupadd --system --gid "${EZKEA_GID}" ezkea \
 && useradd --system --uid "${EZKEA_UID}" --gid "${EZKEA_GID}" \
        --home-dir /data --no-create-home --shell /usr/sbin/nologin ezkea \
 && install -d -o ezkea -g ezkea /data \
 && install -d -o ezkea -g ezkea /var/run/ez-kea

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

WORKDIR /opt/ez-kea
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "app.py"]
