# Releasing EZ-KEA

The version lives in exactly one file, `ez_kea/__about__.py`. Everything else
either derives from it (the footer, the image's `/opt/ez-kea/ez_kea/__about__.py`)
or is a copy that has to be kept in step by hand. `scripts/version_bump.py`
does that second part, checks the result, and cuts the release.

## The anchors it owns

| Where | What | Why it matters |
|---|---|---|
| `ez_kea/__about__.py` | `__version__` | the source of truth |
| `deploy/docker-compose.yml` | `image: <repo>:<version>` | what `docker compose up -d` pulls |
| `deploy/ezkea/Dockerfile` | the header's build examples | what people copy when building |
| `README.md`, `docs/deployment.md` | build/upgrade examples | same |
| `CHANGELOG.md` | `## [<version>]` heading and the compare links | `tests/test_version.py` asserts the heading exists |
| git tag `v<version>` | what `EZK_REF` builds from | the image is built from the pushed tag, not the tree |

The UI is not on that list on purpose: `ez_kea/__init__.py` injects
`ez_kea_version` into every template from `__about__.py`, so `templates/base.html`
can only ever show the real one. The script still asserts both of those anchors
exist, because deleting one would silently freeze the footer.

## Cutting a release

```bash
# 1. write the notes under "## [Unreleased]" in CHANGELOG.md
# 2. bump, commit, tag, push
python3 scripts/version_bump.py 1.2.3

# 3. build, verify and publish the image (after the push, so the tag exists)
python3 scripts/version_bump.py --check
docker build --build-arg EZK_SRC=https://github.com/myxewd/ez-kea.git \
  --build-arg EZK_REF=v1.2.3 -t dreamlord2/ezkea:1.2.3 deploy/ezkea
docker push dreamlord2/ezkea:1.2.3
```

Or let the script do the image too, which adds three checks that a manual build
does not have: the image's `/opt/ez-kea/.build-git-sha` must equal the commit
the tag points at, the `__about__.py` inside the image must read the new
version, and the image must **actually run** -- it is started with the same
environment compose uses, polled until `/healthz` answers, then stopped, and
the stop has to finish in under a second with exit code 0. A build that
succeeds is not evidence that the container comes up; a broken image is never
pushed. `--no-smoke` skips that last part.

```bash
python3 scripts/version_bump.py 1.2.3 --docker --latest
```

## Before you run it

- `[Unreleased]` must have notes. An empty section is refused (override with
  `--allow-empty-changelog`) because the changelog test only checks that the
  heading exists -- it would happily ship a release with no notes.
- **Commit your work first.** The image is built from the tag, so anything you
  leave modified is simply not in it. The script refuses to tag while tracked
  files outside the release are still dirty, and lists them; `--allow-dirty`
  overrides that, and you should almost never need it.
- The files the script rewrites are staged and committed for you, and only
  those.
- If a check fails before the commit, the rewritten files are restored, so a
  failed run leaves no half-bumped tree. `--keep-files` keeps them for
  inspection instead.

## Useful flags

| Flag | Effect |
|---|---|
| `--dry-run` | print every edit and every command, write nothing |
| `--check` | verify the tree agrees with `__about__.py`; exit 1 if not (CI-friendly) |
| `--no-commit`, `--no-tag`, `--no-push` | stop short of each git step |
| `--docker`, `--latest` | build, verify and publish; `--latest` also publishes `:latest` |
| `--date YYYY-MM-DD` | date for the changelog heading (default: today) |
| `--allow-dirty` | tag even though other tracked files are still modified |
| `--no-smoke` | skip the start/health/stop test of the built image |
| `--yes` | no confirmation prompt, for non-interactive runs |

## When a step fails

Re-run the same command. It is the resume button, not a second bump:

```bash
python3 scripts/version_bump.py 1.2.3 --docker --latest --yes
```

- **The push failed** (`SSL_ERROR_SYSCALL`, a timeout, an expired token). The
  files, the commit and the tag are already in place locally; nothing is lost.
  The script retries three times, then stops and says so. Fix the connection and
  run the same command: it notices the tree is already at 1.2.3 and continues
  with the tag and the push instead of refusing.
- **You committed something after the tag was made.** The tag now points at an
  older commit. An unpublished tag is moved to HEAD on the next run (with a
  warning); a published one never is -- pick a new version instead.
- **You forgot to commit the fix.** The run stops before writing anything and
  names the files. Commit them, run again.

## Why a fourth component

`0.10.2.1` is a re-cut of `0.10.2`: same announced release, one fix. SemVer has
no room for it, so the fourth component is treated as a hotfix counter and
`tests/test_version.py` accepts both `X.Y.Z` and `X.Y.Z.N`. Prefer a normal
`X.Y.Z` bump when the release has not shipped yet.
