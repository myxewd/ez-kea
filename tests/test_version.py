# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
tests/test_version.py

The version string is a support tool: when someone reports a bug, the answer
to "what are you running?" has to come from the install itself, since a
deployment is usually an rsync or a tarball with no git metadata. These pin
the two things that make that work — the string is a real version, and it
actually reaches the rendered page.
"""
import json
import pathlib
import re

import pytest
from ez_kea import create_app
from ez_kea.__about__ import __version__
from conftest import login

# Three components is SemVer; a fourth is the hotfix counter EZ-KEA uses for
# re-cuts of a release that is already tagged (0.10.2 -> 0.10.2.1). Both are
# accepted here so a hotfix is a one-line bump, not a test edit.
SEMVER = re.compile(r"^\d+(?:\.\d+){2,3}(?:-[0-9A-Za-z.-]+)?$")
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def app(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text(json.dumps({"Dhcp4": {"shared-networks": []}}))

    app = create_app(config_overrides={
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/test.db",
    })
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SETTINGS_FILE"] = str(settings_file)
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    yield app


@pytest.fixture
def client(app):
    return login(app.test_client(), app)


def test_version_is_semver():
    assert SEMVER.match(__version__), f"{__version__!r} is not a semver string"


def test_changelog_documents_the_current_version():
    """A release that isn't in the changelog is a release nobody can read the
    notes for. Cheap guard against bumping __about__.py and forgetting."""
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in changelog


def test_version_is_exposed_to_templates(client):
    """The footer renders it, so it has to reach the template context on an
    ordinary authenticated page."""
    response = client.get("/")
    assert response.status_code == 200
    assert __version__ in response.get_data(as_text=True)
