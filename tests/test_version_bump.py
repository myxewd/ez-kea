# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
tests/test_version_bump.py

scripts/version_bump.py edits real release files, so the helpers that decide
what counts as a pinned version are worth pinning themselves. Two rules matter
and both are easy to get subtly wrong: a version example must be replaced even
when it is glued to punctuation (v0.10.2, ez-kea-0.10.2.tgz) but not when it is
a prefix of a longer one (0.10.20, 0.10.2.1), and a release must not be cut
without notes.
"""
import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "version_bump", REPO_ROOT / "scripts" / "version_bump.py"
)
version_bump = importlib.util.module_from_spec(_SPEC)
sys.modules["version_bump"] = version_bump
_SPEC.loader.exec_module(version_bump)

DASH = version_bump.DASH

CHANGELOG = """# Changelog

## [Unreleased]

### Fixed

- something was fixed.

## [0.9.0] \u2014 2024-01-01

### Added

- something older.

[Unreleased]: https://example.test/r/compare/v0.9.0...HEAD
[0.9.0]: https://example.test/r/compare/v0.8.0...v0.9.0
"""


class TestVersionParsing:
    def test_accepts_three_and_four_components(self):
        assert version_bump.parse_version("0.10.2") == (0, 10, 2)
        assert version_bump.parse_version("0.10.2.1") == (0, 10, 2, 1)

    @pytest.mark.parametrize("value", ["0.10", "0.10.2.1.1", "v", "0.10.2 "])
    def test_rejects_anything_else(self, value):
        with pytest.raises(version_bump.Failure):
            version_bump.parse_version(value)


class TestStandaloneVersion:
    def test_replaces_every_pinned_example_in_a_line(self):
        text = "docker build -t ez-kea:0.10.2 --build-arg EZK_REF=v0.10.2 ./ezkea"
        new, count = version_bump.bump_docs(text, "0.10.2", "0.10.2.1")
        assert count == 2
        assert "ez-kea:0.10.2.1" in new
        assert "EZK_REF=v0.10.2.1" in new

    def test_replaces_a_tarball_name(self):
        text = "docker save ez-kea:0.10.2 | gzip > ez-kea-0.10.2.tgz"
        new, count = version_bump.bump_docs(text, "0.10.2", "0.10.2.1")
        assert count == 2
        assert new == "docker save ez-kea:0.10.2.1 | gzip > ez-kea-0.10.2.1.tgz"

    @pytest.mark.parametrize("sample", ["0.10.20", "1.0.10.2", "0.10.2.1"])
    def test_leaves_a_longer_version_alone(self, sample):
        assert version_bump.standalone("0.10.2").search(sample) is None


class TestComposeAnchor:
    def test_reads_the_uncommented_image_line(self):
        text = (
            "services:\n"
            "  ezkea:\n"
            "    image: dreamlord2/ezkea:0.9.0\n"
            "  # image: " + "$" + "{KEA_IMAGE:-ez-kea-kea:2.2.0}\n"
        )
        assert version_bump.compose_image(text) == ("dreamlord2/ezkea", "0.9.0")
        assert "image: dreamlord2/ezkea:0.9.1" in version_bump.bump_compose(text, "0.9.1")

    def test_a_missing_anchor_is_an_error(self):
        with pytest.raises(version_bump.Failure):
            version_bump.compose_image("services: {}\n")


class TestChangelog:
    def test_promotes_unreleased_into_the_new_release(self):
        text = version_bump.bump_changelog(
            CHANGELOG, "0.9.0", "0.9.1", "2024-02-02", allow_empty=False
        )
        assert "## [Unreleased]\n\n## [0.9.1] " + DASH + " 2024-02-02" in text
        # the notes move with the heading instead of being left behind
        assert text.index("something was fixed.") < text.index("## [0.9.0]")
        assert "[Unreleased]: https://example.test/r/compare/v0.9.1...HEAD" in text
        assert "[0.9.1]: https://example.test/r/compare/v0.9.0...v0.9.1" in text

    def test_is_idempotent(self):
        once = version_bump.bump_changelog(
            CHANGELOG, "0.9.0", "0.9.1", "2024-02-02", allow_empty=False
        )
        twice = version_bump.bump_changelog(
            once, "0.9.0", "0.9.1", "2024-02-02", allow_empty=False
        )
        assert twice == once

    def test_refuses_to_release_without_notes(self):
        empty = CHANGELOG.replace("### Fixed\n\n- something was fixed.\n\n", "")
        with pytest.raises(version_bump.Failure):
            version_bump.bump_changelog(empty, "0.9.0", "0.9.1", "2024-02-02", False)
        allowed = version_bump.bump_changelog(
            empty, "0.9.0", "0.9.1", "2024-02-02", True
        )
        assert "## [0.9.1]" in allowed

    def test_refuses_a_link_it_cannot_rewrite(self):
        broken = CHANGELOG.replace("compare/v0.9.0...HEAD", "compare/main...HEAD")
        with pytest.raises(version_bump.Failure):
            version_bump.bump_changelog(broken, "0.9.0", "0.9.1", "2024-02-02", False)
