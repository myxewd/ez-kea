#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Cut an EZ-KEA release: bump the version everywhere it is pinned.

The version is stored once and derived everywhere else. ez_kea/__about__.py is
the source of truth; the app reads it at import time and injects it into every
template as ez_kea_version, which templates/base.html renders in the footer for
signed-in users; the container image ships that same file, so

    docker run --rm --entrypoint cat <image> /opt/ez-kea/ez_kea/__about__.py

answers "what is deployed" for an install that has no git metadata at all.

That leaves a short list of genuinely duplicated copies, and every one of them
has caused, or will cause, a release that looks fine and is not:

  ez_kea/__about__.py            the source of truth itself
  deploy/docker-compose.yml      image: <repo>:<version>  -- what "up -d" pulls
  deploy/ezkea/Dockerfile        the documented build/ref examples
  README.md, docs/deployment.md  the commands operators copy and paste
  CHANGELOG.md                   the release heading and the compare links
  git tag v<version>             what the image is built from (EZK_REF)

Bumping by hand is how the tag goes out as v0.10.2.1 while compose still pulls
0.10.2, and the operator who re-runs "docker compose up -d" keeps the old build.
So this script does not guess: it locates each anchor, refuses to start when one
is missing or already moved, rewrites them in a single pass, checks the result
(including tests/test_version.py, and for an image build, the contents of the
image that was just built), and only then commits, tags, pushes and publishes.

Typical use:

    # 1. write the release notes under "## [Unreleased]" in CHANGELOG.md
    # 2. bump, commit, tag and push
    python3 scripts/version_bump.py 0.10.2.1

    # 3. build the image from the pushed tag, verify what landed in it, publish
    python3 scripts/version_bump.py 0.10.2.1 --docker --latest --yes

    # see every edit and every command without touching anything
    python3 scripts/version_bump.py 0.10.2.1 --dry-run

    # files only, no git and no docker
    python3 scripts/version_bump.py 0.10.2.1 --no-commit --no-docker

    # confirm a working tree is internally consistent (CI-friendly, exit 1)
    python3 scripts/version_bump.py --check

Add --yes for non-interactive runs. The version may be plain SemVer
(X.Y.Z[-pre]) or carry a fourth hotfix counter (X.Y.Z.N), which is what
tests/test_version.py accepts too.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# CHANGELOG headings use an em dash: "## [0.10.2] \u2014 2024-01-01".
DASH = "\u2014"

REPO_ROOT = Path(__file__).resolve().parent.parent

ABOUT = "ez_kea/__about__.py"
COMPOSE = "deploy/docker-compose.yml"
DOCKERFILE = "deploy/ezkea/Dockerfile"
CHANGELOG = "CHANGELOG.md"
DOC_FILES = ("README.md", "docs/deployment.md")
# Every file a release pins. Deliberately not every file in the tree: prose
# (this script's own docstring, docs/releasing.md) may mention a version
# without pinning it, and CHANGELOG.md is history by design.
ANCHOR_FILES = (ABOUT, COMPOSE, DOCKERFILE, *DOC_FILES)
# Everything a bump owns and commits. The changelog is not a version anchor (it
# is full of old versions on purpose) but it is part of the release, so the
# stray-change check and the resume path both work from this wider list.
RELEASE_FILES = (*ANCHOR_FILES, CHANGELOG)
APP_INIT = "ez_kea/__init__.py"
BASE_TEMPLATE = "templates/base.html"
VERSION_TEST = "tests/test_version.py"

# X.Y.Z or X.Y.Z.N, optionally with a pre-release suffix. Mirrors the regex in
# tests/test_version.py: the fourth component is a hotfix counter, not SemVer.
VERSION_RE = re.compile(r"^(?P<base>\d+(?:\.\d+){2,3})(?:-(?P<pre>[0-9A-Za-z.-]+))?$")

ABOUT_RE = re.compile(r'(?m)^(?P<head>__version__[ \t]*=[ \t]*")(?P<v>[^"]+)(?P<tail>")')
COMPOSE_RE = re.compile(
    r"(?m)^(?P<head>[ \t]*image:[ \t]*)(?P<image>[^:\s]+):(?P<tag>[^\s#]+)(?P<tail>[ \t]*)$"
)
DOCKER_TAG_RE = re.compile(r"-t ez-kea:(?P<tag>\d[\w.\-]*)")
EZK_REF_RE = re.compile(r"EZK_REF=(?P<ref>v?\d[\w.\-]*)")
# Matches "ez-kea:0.10.2" and "ez-kea-0.10.2.tgz" but stops at the version;
# the image name "ez-kea-kea:2.2.0" is deliberately not a match.
DOC_TAG_RE = re.compile(r"ez-kea[:\-](?P<tag>\d+(?:\.\d+){1,3}(?:-[0-9A-Za-z.]+)?)")


# ---------------------------------------------------------------- plumbing --


class Failure(Exception):
    """A condition that must stop the release before anything is written."""


def die(message: str):
    raise Failure(message)


@dataclass
class Edit:
    """One file, before and after, with its own line ending remembered.

    This checkout runs with core.autocrlf=true and a mix of LF and CRLF files
    (README.md is CRLF, CHANGELOG.md is LF). Reading with universal newlines and
    writing back with the platform default would rewrite every line of a file
    to change four characters, so the original ending is kept and restored.
    """

    rel: str
    before: str
    after: "str | None"
    newline: str
    raw_before: str

    @property
    def changed(self) -> bool:
        return self.after is not None and self.after != self.before

    def raw_after(self) -> str:
        return (self.after or self.before).replace("\n", self.newline)

    def write(self, path: Path) -> None:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(self.raw_after())

    def restore(self, path: Path) -> None:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(self.raw_before)


def read_text(path: Path) -> str:
    """File contents with CRLF folded to LF, for matching and diffing."""
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def read_edit(path: Path, rel: str) -> Edit:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        raw = handle.read()
    crlf = raw.count("\r\n")
    newline = "\r\n" if crlf * 2 >= raw.count("\n") else "\n"
    return Edit(rel=rel, before=raw.replace("\r\n", "\n"), after=None,
                newline=newline, raw_before=raw)


def run(argv, *, cwd=None, dry_run=False, capture=False, env=None,
        timeout=None):
    """Echo and run one command. Under --dry-run it is echoed and skipped."""
    print(f"  $ {' '.join(argv)}", flush=True)
    if dry_run:
        return subprocess.CompletedProcess(argv, 0, "", "")
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
        check=False,
    )


def git(root: Path, *args: str, dry_run: bool = False, timeout=None) -> str:
    proc = run(["git", "-C", str(root), *args], dry_run=dry_run, capture=True,
               timeout=timeout)
    if proc.returncode != 0:
        die(f"git {' '.join(args)} failed:\n{(proc.stderr or proc.stdout).strip()}")
    return proc.stdout or ""


# ------------------------------------------------------------ version math --


def parse_version(value: str) -> tuple:
    match = VERSION_RE.match(value)
    if not match:
        die(f"{value!r} is not a version (want X.Y.Z or X.Y.Z.N, optionally -pre)")
    return tuple(int(part) for part in match.group("base").split("."))


def validate_version(value: str) -> tuple:
    return parse_version(value.strip().lstrip("v"))


def read_version(root: Path) -> str:
    match = ABOUT_RE.search(read_text(root / ABOUT))
    if not match:
        die(f'{ABOUT}: no __version__ = "..." line found')
    return match.group("v")


def compose_image(text: str):
    matches = list(COMPOSE_RE.finditer(text))
    if len(matches) != 1:
        die(
            f"{COMPOSE}: expected exactly one uncommented 'image: <repo>:<tag>' "
            f"line, found {len(matches)}"
        )
    return matches[0].group("image"), matches[0].group("tag")


# ------------------------------------------------------------- the rewrite --


def stage(plan: dict, root: Path, rel: str, after: str, note: str) -> None:
    edit = read_edit(root / rel, rel)
    if after == edit.before:
        print(f"  = {rel}: already at the new version ({note})")
    else:
        edit.after = after
        print(f"  * {rel}: {note}")
    plan[rel] = edit


def bump_about(text: str, old: str, new: str) -> str:
    match = ABOUT_RE.search(text)
    if match.group("v") != old:
        die(f"{ABOUT} holds {match.group('v')!r}, expected {old!r}")
    return ABOUT_RE.sub(lambda m: m.group("head") + new + m.group("tail"), text, count=1)


def bump_compose(text: str, new: str) -> str:
    compose_image(text)  # validates that there is exactly one anchor
    return COMPOSE_RE.sub(
        lambda m: m.group("head") + m.group("image") + ":" + new + m.group("tail"),
        text,
        count=1,
    )


def bump_dockerfile(text: str, new: str) -> str:
    tags = [m.group("tag") for m in DOCKER_TAG_RE.finditer(text)]
    refs = [m.group("ref") for m in EZK_REF_RE.finditer(text)]
    if not tags or not refs:
        die(
            f"{DOCKERFILE}: expected a '-t ez-kea:<version>' example and an "
            f"'EZK_REF=<ref>' example in the header comment"
        )
    text = DOCKER_TAG_RE.sub(lambda m: "-t ez-kea:" + new, text)
    text = EZK_REF_RE.sub(lambda m: "EZK_REF=v" + new, text)
    return text


def standalone(old: str) -> "re.Pattern[str]":
    """A matcher for the old version as a whole version, not as a fragment.

    It has to hit "v0.10.2" and "ez-kea-0.10.2.tgz" while leaving "0.10.20"
    and the new "0.10.2.1" alone -- which is why a plain "not followed by a
    digit or a dot" is wrong: a file extension is a dot too.
    """
    return re.compile(r"(?<![\d.])" + re.escape(old) + r"(?!\d)(?!\.\d)")


def bump_docs(text: str, old: str, new: str):
    return standalone(old).subn(new, text)


def bump_changelog(text: str, old: str, new: str, date: str,
                   allow_empty: bool) -> str:
    heading = f"## [{new}]"
    if heading not in text:
        match = re.search(r"(?m)^##[ \t]+\[Unreleased\][^\n]*\n", text)
        if not match:
            die(f"{CHANGELOG}: no '## [Unreleased]' section to promote")
        body_start = match.end()
        following = re.search(r"(?m)^##[ \t]+\[", text[body_start:])
        if not following:
            die(f"{CHANGELOG}: no released section after [Unreleased]")
        body_end = body_start + following.start()
        body = text[body_start:body_end].strip("\n")
        if not body.strip():
            if not allow_empty:
                die(
                    f"{CHANGELOG}: [Unreleased] is empty, so v{new} would ship "
                    "without notes. Write them there first, or pass "
                    "--allow-empty-changelog."
                )
            body = "### Changed\n\n- _Release notes to be completed._"
        text = (
            text[:body_start]
            + "\n"
            + f"{heading} {DASH} {date}"
            + "\n\n"
            + body
            + "\n\n"
            + text[body_end:]
        )

    match = re.search(r"(?m)^\[Unreleased\]:[ \t]*(?P<url>\S+)[ \t]*$", text)
    if not match:
        die(f"{CHANGELOG}: no '[Unreleased]: <url>' link definition")
    url = match.group("url")
    suffix = f"/compare/v{old}...HEAD"
    if not url.endswith(suffix):
        if url.endswith(f"/compare/v{new}...HEAD"):
            return text  # an earlier run already rewrote the links
        die(
            f"{CHANGELOG}: the [Unreleased] compare link does not end in "
            f"{suffix!r}; refusing to guess. Fix the link, then re-run."
        )
    base = url[: -len(suffix)]  # ends with "/compare"
    if re.search(rf"(?m)^\[{re.escape(new)}\]:", text):
        return text
    replacement = (
        f"[Unreleased]: {base}/compare/v{new}...HEAD\n"
        f"[{new}]: {base}/compare/v{old}...v{new}"
    )
    return text[: match.start()] + replacement + text[match.end():]


# ------------------------------------------------------------ verification --


def check_anchors(root: Path, version: str) -> list:
    """Return every anchor that does not agree with version."""
    problems = []

    try:
        found = read_version(root)
        if found != version:
            problems.append(f"{ABOUT}: {found} != {version}")
    except (Failure, OSError) as exc:
        problems.append(str(exc))

    try:
        text = read_text(root / COMPOSE)
        image, tag = compose_image(text)
        if tag != version:
            problems.append(f"{COMPOSE}: {image}:{tag} != {image}:{version}")
    except (Failure, OSError) as exc:
        problems.append(str(exc))

    try:
        text = read_text(root / DOCKERFILE)
        for tag in sorted({m.group("tag") for m in DOCKER_TAG_RE.finditer(text)}):
            if tag != version:
                problems.append(f"{DOCKERFILE}: -t ez-kea:{tag}")
        for ref in sorted({m.group("ref") for m in EZK_REF_RE.finditer(text)}):
            if ref.lstrip("v") != version:
                problems.append(f"{DOCKERFILE}: EZK_REF={ref}")
    except OSError as exc:
        problems.append(str(exc))

    try:
        if f"## [{version}]" not in read_text(root / CHANGELOG):
            problems.append(f"{CHANGELOG}: no '## [{version}]' heading")
    except OSError as exc:
        problems.append(str(exc))

    for rel in DOC_FILES:
        try:
            text = read_text(root / rel)
        except OSError as exc:
            problems.append(str(exc))
            continue
        for tag in sorted({m.group("tag") for m in DOC_TAG_RE.finditer(text)}):
            if tag != version:
                problems.append(f"{rel}: ez-kea:{tag}")
        for ref in sorted({m.group("ref") for m in EZK_REF_RE.finditer(text)}):
            if ref.lstrip("v") != version:
                problems.append(f"{rel}: EZK_REF={ref}")

    # The UI does not store a copy: it renders whatever __about__.py says.
    # These two anchors are what make that true, so their absence is a bug.
    for rel, needle in ((APP_INIT, "ez_kea_version=__version__"),
                        (BASE_TEMPLATE, "ez_kea_version")):
        try:
            if needle not in read_text(root / rel):
                problems.append(f"{rel}: {needle!r} is gone; the UI would no longer show it")
        except OSError as exc:
            problems.append(str(exc))

    return problems


def leftovers(root: Path, old: str) -> list:
    """Lines in an anchor file that still pin the old version."""
    matcher = standalone(old)
    found = []
    for rel in ANCHOR_FILES:
        for number, line in enumerate(read_text(root / rel).splitlines(), 1):
            if matcher.search(line):
                found.append(f"{rel}:{number}: {line.strip()}")
    return found


def show_diff(rel: str, before: str, after: str, limit: int = 40) -> None:
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=1,
            lineterm="",
        )
    )
    for line in lines[:limit]:
        print("    " + line)
    if len(lines) > limit:
        print(f"    ... ({len(lines) - limit} more diff lines)")


def indent(lines, prefix: str) -> str:
    return "\n".join(prefix + line for line in lines)


def run_version_tests(root: Path) -> str:
    """Run the pytest guard. Returns a status string or dies."""
    probe = run([sys.executable, "-c", "import flask, pytest"], capture=True)
    if probe.returncode != 0:
        return "skipped (this python cannot import flask/pytest; run inside the venv)"

    argv = [sys.executable, "-m", "pytest", VERSION_TEST, "-q"]
    note = ""
    try:
        import fcntl  # noqa: F401
    except ImportError:
        # ez_kea imports fcntl the moment it starts, so on Windows the app
        # cannot boot and the page-render test can only error. The two
        # assertions that actually guard a bump need no app and still run.
        argv += ["-k", "not test_version_is_exposed_to_templates"]
        note = " (page-render test skipped: this host has no fcntl)"

    print("\nRunning the version guard (tests/test_version.py) ...")
    proc = run(argv, cwd=root, capture=True)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    print(indent(output.splitlines()[-8:], "    "))
    if proc.returncode != 0:
        die(f"{VERSION_TEST} failed; nothing was committed.")
    return "ok" + note


# --------------------------------------------------------------- the parts --


def git_remote_url(root: Path) -> str:
    url = git(root, "remote", "get-url", "origin").strip()
    if url.startswith("git@"):
        # git@github.com:owner/repo.git -> https://github.com/owner/repo.git
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    return url


def tracked_changes(root: Path) -> set:
    """Paths of tracked files with uncommitted modifications."""
    paths = set()
    for line in git(root, "status", "--porcelain").splitlines():
        if line.startswith("??"):
            continue
        path = line[3:].strip()
        if " -> " in path:  # a rename: the destination is what matters
            path = path.split(" -> ")[-1]
        paths.add(path.strip('"'))
    return paths


def tag_on_origin(root: Path, tagref: str):
    """True / False, or None when origin cannot be reached to ask."""
    try:
        proc = run(["git", "-C", str(root), "ls-remote", "--tags", "origin",
                    f"refs/tags/{tagref}"], capture=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return bool((proc.stdout or "").strip())


def push_ref(root: Path, refspec: str, label: str, attempts: int = 3) -> None:
    """git push, retried: the network is the one step that fails for reasons
    that have nothing to do with the release."""
    detail = ""
    for attempt in range(1, attempts + 1):
        proc = run(["git", "-C", str(root), "push", "origin", refspec], capture=True)
        if proc.returncode == 0:
            return
        detail = (proc.stderr or proc.stdout).strip()
        last = detail.splitlines()[-1] if detail else "no output"
        if attempt < attempts:
            wait = 2 * attempt
            print(f"  ! {label}: {last}")
            print(f"    retrying in {wait}s ({attempt}/{attempts}) ...")
            time.sleep(wait)
    die(
        f"pushing {label} failed after {attempts} attempts:\n{detail}\n\n"
        "Nothing is lost: the files, the commit and the tag are all in place "
        "locally. Fix the connection and run the same command again -- it "
        "resumes this release instead of starting over."
    )


def do_git(root: Path, new: str, files: list, *, commit: bool, tag: bool,
           push: bool) -> None:
    head = git(root, "rev-parse", "HEAD").strip()
    tagref = f"v{new}"

    if commit and not files:
        print("\nNothing to commit (no file changed).")
        commit = False

    if commit:
        if git(root, "status", "--porcelain", "--", *files).strip() == "":
            print("  = nothing to commit (files already at this version)")
        else:
            print("\nCommitting ...")
            git(root, "add", "--", *files)
            git(root, "commit", "-m", f"release: v{new}")
            head = git(root, "rev-parse", "HEAD").strip()
    else:
        print("\nSkipping the commit (--no-commit).")

    if tag:
        existing = git(root, "tag", "--list", tagref).strip()
        if existing:
            target = git(root, "rev-list", "-n", "1", tagref).strip()
            if target and target != head:
                published = tag_on_origin(root, tagref)
                if published:
                    die(
                        f"tag {tagref} is already published and points at "
                        f"{target[:12]}, not HEAD ({head[:12]}). A published tag "
                        "cannot be moved; pick a new version."
                    )
                if published is None:
                    die(
                        f"tag {tagref} points at {target[:12]}, not HEAD "
                        f"({head[:12]}), and origin could not be reached to check "
                        "whether it is published. Fix the connection and re-run: "
                        "an unpublished tag can be moved, a published one cannot."
                    )
                print(
                    f"  ! tag {tagref} pointed at {target[:12]}; moving it to HEAD "
                    f"({head[:12]}) -- it was never pushed"
                )
                git(root, "tag", "-f", "-a", tagref, "-m", f"EZ-KEA {tagref}")
            else:
                print(f"  = tag {tagref} already exists at HEAD")
        else:
            print("\nTagging ...")
            git(root, "tag", "-a", tagref, "-m", f"EZ-KEA {tagref}")
    else:
        print("Skipping the tag (--no-tag).")

    if push:
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch in ("", "HEAD"):
            die("HEAD is detached; check out a branch before pushing, or pass --no-push")
        print(f"\nPushing {branch} and {tagref} to origin ...")
        push_ref(root, f"HEAD:refs/heads/{branch}", f"{branch}")
        if tag:
            push_ref(root, f"refs/tags/{tagref}", f"tag {tagref}")
    else:
        print("Skipping the push (--no-push).")


def image_repo(root: Path, args) -> str:
    image, _ = compose_image(read_text(root / COMPOSE))
    return getattr(args, "image", None) or image


def do_docker(root: Path, args, new: str) -> None:
    image = image_repo(root, args)
    if not args.docker:
        print(
            "\nSkipping docker (no --docker). To publish the image after the push:\n"
            f"  python3 scripts/version_bump.py --check\n"
            f"  docker build --build-arg EZK_SRC=<repo> --build-arg EZK_REF=v{new} "
            f"-t {image}:{new} deploy/ezkea"
        )
        return

    # --docker was pre-flighted in main(), so this only runs when the tag is
    # pushed or --docker-ref points somewhere buildable.
    src = args.docker_src or git_remote_url(root)
    ref = args.docker_ref or f"v{new}"
    build_dir = root / "deploy" / "ezkea"

    print(f"\nBuilding {image}:{new} from {ref} ...")
    build = ["docker", "build",
             "--build-arg", f"EZK_SRC={src}",
             "--build-arg", f"EZK_REF={ref}"]
    if args.platform:
        build += ["--platform", args.platform]
    build += ["-t", f"{image}:{new}", str(build_dir)]
    run(build)

    # The image fetches a ref; prove the ref really produced this version
    # instead of trusting the build log.
    print("\nVerifying the image ...")
    expected = git(root, "rev-list", "-n", "1", ref).strip()
    got = run(["docker", "run", "--rm", "--entrypoint", "cat", f"{image}:{new}",
               "/opt/ez-kea/.build-git-sha"], capture=True)
    sha = (got.stdout or "").strip()
    if sha != expected:
        die(
            f"image {image}:{new} was built from {sha[:12] or '<nothing>'} but "
            f"{ref} is {expected[:12]}; refusing to publish a mismatched image "
            "(is the tag on the remote yet?)"
        )
    inside = run(["docker", "run", "--rm", "--entrypoint", "cat",
                  f"{image}:{new}", "/opt/ez-kea/ez_kea/__about__.py"],
                 capture=True)
    if f'"{new}"' not in (inside.stdout or ""):
        die(
            f"image {image}:{new} does not report version {new}; nothing was "
            "pushed."
        )
    print(f"  = .build-git-sha matches {ref} ({expected[:12]})")
    print(f"  = ez_kea/__about__.py inside the image says {new}")

    if not getattr(args, "no_smoke", False):
        smoke_test(image, new)

    if args.latest:
        print(f"\nTagging {image}:latest ...")
        run(["docker", "tag", f"{image}:{new}", f"{image}:latest"])

    print(f"\nPushing {image}:{new} ...")
    run(["docker", "push", f"{image}:{new}"])
    if args.latest:
        run(["docker", "push", f"{image}:latest"])


def smoke_test(image: str, new: str) -> None:
    """Start the image the way compose does and require a clean life cycle.

    A build that succeeds proves nothing: the failure this guards against is an
    image that starts, answers nothing and cannot be stopped, which the
    operator discovers on the server.
    """
    name = f"ezkea-smoke-{os.getpid()}"
    probe = [
        "docker", "exec", name, "python", "-c",
        "import urllib.request,sys;"
        "sys.exit(0 if urllib.request.urlopen("
        "'http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)",
    ]
    print(f"\nStarting {image}:{new} to prove it comes up ...")
    run(["docker", "rm", "-f", name], capture=True)
    try:
        # No --init on purpose: this is how "docker run" starts the image, with
        # the app as PID 1, which is the arrangement that hides signals.
        started = run([
            "docker", "run", "-d", "--name", name,
            "--tmpfs", "/data",
            "-e", "SECRET_KEY=" + "0" * 64,
            "-e", "HOST=0.0.0.0", "-e", "PORT=8080",
            "-e", "DATABASE_URL=sqlite:////data/smoke.db",
            "-e", "SETTINGS_FILE=/data/smoke-settings.json",
            "-e", "LOG_INDEX_DB=/data/smoke-log.db",
            "-e", "STATE_INDEX_DB=/data/smoke-state.db",
            "-e", "BACKUP_DIR=/data/backups",
            "-e", "INSTANCE_DIR=/data/instances",
            f"{image}:{new}",
        ], capture=True)
        if started.returncode != 0:
            die(f"the image would not start:\n{(started.stderr or started.stdout).strip()}")

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                        capture=True).stdout.strip()
            if state != "true":
                logs = run(["docker", "logs", name], capture=True)
                die(
                    "the image started and exited on its own; its logs are:\n"
                    + ((logs.stdout or "") + (logs.stderr or "")).strip()
                )
            if run(probe, capture=True).returncode == 0:
                print("  = /healthz answered")
                break
            time.sleep(1)
        else:
            logs = run(["docker", "logs", name], capture=True)
            die(
                "the image never answered /healthz within 60s; its logs are:\n"
                + ((logs.stdout or "") + (logs.stderr or "")).strip()
            )

        began = time.monotonic()
        run(["docker", "stop", "-t", "15", name], capture=True)
        took = time.monotonic() - began
        code = run(["docker", "inspect", "-f", "{{.State.ExitCode}}", name],
                   capture=True).stdout.strip()
        if code != "0":
            die(
                f"docker stop handed the container {took:.0f}s and exit code {code}. "
                "Only 0 is a clean shutdown: 137 means SIGTERM was ignored until "
                "the grace period ran out, 143 means it was killed rather than "
                "handled. The app is PID 1, so it needs its own SIGTERM handler."
            )
        print(f"  = stopped cleanly in {took:.1f}s (exit 0)")
    finally:
        run(["docker", "rm", "-f", name], capture=True)


def summary(root: Path, new: str, args) -> None:
    image = image_repo(root, args)
    print(f"\nEZ-KEA is at v{new}:")
    print(f"  ez_kea/__about__.py   {new}")
    print("  UI footer             derived from __about__.py at request time")
    print(f"  docker-compose        {image}:{new}")
    if args.no_tag:
        print("  git tag               not created (--no-tag)")
    elif args.no_push:
        print(f"  git tag               v{new} (local only, --no-push)")
    else:
        print(f"  git tag               v{new} (pushed to origin)")
    if args.docker:
        print(f"  image                 {image}:{new}"
              + (" + :latest" if args.latest else ""))


# -------------------------------------------------------------------- main --


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="version_bump.py",
        description="Bump EZ-KEA's version everywhere it is pinned, then cut the release.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 scripts/version_bump.py 0.10.2.1\n"
            "      rewrite every anchor, commit, tag v0.10.2.1, push both\n"
            "  python3 scripts/version_bump.py 0.10.2.1 --docker --latest --yes\n"
            "      the same, then build from the pushed tag, verify the image\n"
            "      really carries that version, and publish it (+ :latest)\n"
            "  python3 scripts/version_bump.py 0.10.2.1 --dry-run\n"
            "      print every edit and every command, change nothing\n"
            "  python3 scripts/version_bump.py 0.10.2.1 --no-commit\n"
            "      files only; commit and publish by hand afterwards\n"
            "  python3 scripts/version_bump.py --check\n"
            "      exit 1 if any anchor disagrees with __about__.py\n"
        ),
    )
    parser.add_argument("version", nargs="?", help="new version, e.g. 0.10.3 or 0.10.2.1")
    parser.add_argument("--check", action="store_true",
                        help="verify the working tree is internally consistent, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the planned edits and commands without running them")
    parser.add_argument("--date", default=None,
                        help="release date for the CHANGELOG heading (default: today)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="do not ask for confirmation")
    parser.add_argument("--allow-empty-changelog", action="store_true",
                        help="insert a placeholder when [Unreleased] has no entries")
    parser.add_argument("--allow-downgrade", action="store_true",
                        help="permit a version lower than the current one")
    parser.add_argument("--keep-files", action="store_true",
                        help="leave the rewritten files in place when a check fails")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="tag even though other tracked files are still modified")
    parser.add_argument("--no-tests", action="store_true",
                        help="skip tests/test_version.py")
    parser.add_argument("--no-commit", action="store_true", help="do not commit")
    parser.add_argument("--no-tag", action="store_true", help="do not tag")
    parser.add_argument("--no-push", action="store_true", help="do not push to origin")
    parser.add_argument("--no-docker", action="store_true",
                        help="do not build or publish the image (the default)")
    parser.add_argument("--docker", action="store_true",
                        help="build, verify and publish the image after the push")
    parser.add_argument("--latest", action="store_true",
                        help="also publish :latest (implies --docker)")
    parser.add_argument("--no-smoke", action="store_true",
                        help="skip the start/health/stop test of the built image")
    parser.add_argument("--platform", default=None,
                        help="value for docker build --platform, e.g. linux/amd64")
    parser.add_argument("--image", default=None,
                        help="image repository (default: from deploy/docker-compose.yml)")
    parser.add_argument("--docker-src", default=None,
                        help="EZK_SRC build arg (default: the origin remote URL)")
    parser.add_argument("--docker-ref", default=None,
                        help="EZK_REF build arg (default: v<version>)")
    return parser


def do_check(root: Path) -> int:
    version = read_version(root)
    print(f"version {version} (from {ABOUT})\n")
    problems = check_anchors(root, version)
    for problem in problems:
        print(f"  ! {problem}")
    if not problems:
        print("  = every anchor agrees")

    tagref = f"v{version}"
    local = git(root, "tag", "--list", tagref).strip()
    if not local:
        problems.append(f"no local tag {tagref}")
        print(f"  ! no local tag {tagref}")
    else:
        print(f"  = local tag {tagref} exists")
        try:
            proc = run(["git", "-C", str(root), "ls-remote", "--tags", "origin",
                        f"refs/tags/{tagref}"], capture=True, timeout=20)
            if proc.returncode != 0:
                print("  ? could not reach origin (offline?)")
            elif (proc.stdout or "").strip():
                print(f"  = tag {tagref} exists on origin")
            else:
                problems.append(f"tag {tagref} is not on origin")
                print(f"  ! tag {tagref} is missing on origin")
        except subprocess.TimeoutExpired:
            print("  ? origin did not answer (offline?)")

    print()
    if problems:
        print(f"{len(problems)} problem(s).")
        return 1
    print("Everything agrees.")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - exotic streams
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    root = REPO_ROOT

    if args.check:
        return do_check(root)
    if not args.version:
        parser.error("a version is required (or --check)")

    new = args.version.strip().lstrip("v")
    new_key = validate_version(new)
    old = read_version(root)
    old_key = parse_version(old)

    if not (root / ".git").exists():
        die(f"{root} is not a git checkout; this script commits and tags")

    # Running the same command again is how a release is resumed. A push can fail
    # on the network long after the files were rewritten, committed and tagged;
    # refusing with "the version is already X" would leave the operator no way
    # forward except by hand.
    resuming = new == old
    if resuming:
        problems = check_anchors(root, new)
        if problems:
            die(
                f"the tree already says {new}, but it is not a consistent "
                "release:\n" + "\n".join("  - " + p for p in problems)
            )
    elif new_key <= old_key and not args.allow_downgrade:
        die(f"{new} is not newer than {old}; pass --allow-downgrade to force it")

    # Fail before anything is written, not after the commit and the push.
    if args.latest:
        args.docker = True
    if args.docker:
        if args.no_push:
            die(
                f"the image is built from the pushed tag (EZK_REF=v{new}), so "
                "--docker needs the push. Drop --no-push, or drop --docker and "
                "build by hand."
            )
        if args.no_tag and not args.docker_ref:
            die(
                f"--docker builds from EZK_REF=v{new}, which --no-tag would never "
                "create. Drop --no-tag, or pass --docker-ref."
            )
        if shutil.which("docker") is None:
            die("docker is not on PATH but --docker was given")

    # The tag is what the image is built from, so anything still uncommitted at
    # this point would simply not be in the release -- the classic way to ship a
    # tag that does not contain the fix that prompted it.
    if not args.no_tag and not args.allow_dirty:
        stray = sorted(tracked_changes(root) - set(RELEASE_FILES))
        if stray:
            die(
                "these tracked files are modified but are not part of the "
                "release:\n" + "\n".join("  - " + path for path in stray)
                + f"\n\nTagging v{new} now would leave them out of the tag, and "
                "the image is built from the tag. Commit them first, or pass "
                "--allow-dirty to tag anyway."
            )

    date = args.date or dt.date.today().isoformat()
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() or "?"

    print("EZ-KEA release\n" if resuming else "EZ-KEA version bump\n")
    print(f"  repo    {root}")
    if resuming:
        print(f"  version {new}  (already in the tree; resuming the release)")
    else:
        print(f"  from    {old}  (tag v{old})")
        print(f"  to      {new}  (tag v{new})")
        print(f"  date    {date}")
    print(f"  branch  {branch}")
    print("  git     "
          + ("commit + " if not args.no_commit else "")
          + ("tag + " if not args.no_tag else "")
          + ("push origin" if not args.no_push else "no push"))

    dirty = [
        line for line in git(root, "status", "--porcelain").splitlines()
        if not line.startswith("??")
    ]
    if dirty:
        print("  note    the working tree has uncommitted changes:")
        for line in dirty[:10]:
            print("          " + line)
    print()

    # ---- plan ------------------------------------------------------------
    plan = {}
    if resuming:
        print("\nEvery file is already at the new version; nothing to rewrite.")
        files = list(RELEASE_FILES)
    else:
        print("Planning edits ...")
        stage(plan, root, ABOUT, bump_about(read_text(root / ABOUT), old, new),
              f"__version__ {old} -> {new}")
        stage(plan, root, COMPOSE, bump_compose(read_text(root / COMPOSE), new),
              f"image tag -> {new}")
        stage(plan, root, DOCKERFILE,
              bump_dockerfile(read_text(root / DOCKERFILE), new),
              f"build examples -> {new}")
        for rel in DOC_FILES:
            after, count = bump_docs(read_text(root / rel), old, new)
            stage(plan, root, rel, after,
                  f"{count} example(s) -> {new}" if count
                  else "no version example to update")
        stage(plan, root, CHANGELOG,
              bump_changelog(read_text(root / CHANGELOG), old, new, date,
                             args.allow_empty_changelog),
              f"promote [Unreleased] to {new}, update compare links")

        print()
        for rel in sorted(plan):
            edit = plan[rel]
            if edit.changed:
                show_diff(rel, edit.before, edit.after)
        files = [rel for rel, edit in plan.items() if edit.changed]

    if args.dry_run:
        print("\nDry run: nothing was written. The commands would be:")
        print(f"  git add -- {' '.join(files) or '(nothing)'}")
        print(f"  git commit -m 'release: v{new}'")
        if not args.no_tag:
            print(f"  git tag -a v{new} -m 'EZ-KEA v{new}'")
            print(f"  git push origin HEAD:refs/heads/{branch}")
            print(f"  git push origin refs/tags/v{new}")
        if args.docker:
            image = image_repo(root, args)
            print(f"  docker build --build-arg EZK_SRC=<origin> --build-arg "
                  f"EZK_REF=v{new} -t {image}:{new} deploy/ezkea")
            print(f"  docker push {image}:{new}"
                  + (f" && docker push {image}:latest" if args.latest else ""))
        return 0

    # ---- confirm ---------------------------------------------------------
    if not args.yes:
        if not sys.stdin.isatty():
            die("refusing to write without confirmation; re-run with --yes")
        question = (
            f"\nResume the release of v{new} (commit, tag, push)? [y/N] "
            if resuming
            else f"\nWrite {len(files)} file(s) and cut v{new}? [y/N] "
        )
        if input(question).strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    # ---- write -----------------------------------------------------------
    if not resuming:
        print("\nWriting files ...")
        for rel in files:
            plan[rel].write(root / rel)
            print(f"  + {rel}")

    def rollback(reason: str):
        if resuming or args.keep_files:
            die(reason + ("" if resuming
                          else "\n(--keep-files: the rewritten files were left in place)"))
        for rel in files:
            plan[rel].restore(root / rel)
        die(reason + "\nThe rewritten files were restored; nothing was committed.")

    # ---- verify ----------------------------------------------------------
    problems = check_anchors(root, new)
    if not resuming:
        problems += [f"still pins {old}: {line}" for line in leftovers(root, old)]
    if problems:
        rollback("verification failed:\n" + "\n".join("  - " + p for p in problems))
    print("All anchors agree"
          + ("" if resuming else " and nothing still pins the old version") + ".")

    if not args.no_tests:
        try:
            print(f"version guard: {run_version_tests(root)}")
        except Failure as exc:
            rollback(f"the version guard failed: {exc}")

    # ---- git -------------------------------------------------------------
    do_git(root, new, files, commit=not args.no_commit, tag=not args.no_tag,
           push=not args.no_push)

    # ---- docker ----------------------------------------------------------
    do_docker(root, args, new)

    summary(root, new, args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
