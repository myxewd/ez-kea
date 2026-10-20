# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
tests/test_license_nag.py

EZ-KEA's licensing is a license *term*, not a runtime gate (see
ez_kea/license.py). These tests pin that down from both directions:

  - Nothing is ever blocked. Config writes must succeed at any lease count,
    licensed or not. This is the regression that matters — an enforcement
    check reintroduced anywhere in the write path would fail these.
  - The reminder is computed correctly: a quiet notice whenever unlicensed,
    escalating to a banner on installs large enough to probably be commercial.
"""
import csv
import json
import time

import pytest
from ez_kea import create_app
from ez_kea.license import NAG_LEASE_THRESHOLD, license_status
from conftest import login


def _write_leases(path, count, expired=False):
    now = int(time.time())
    expire = now - 3600 if expired else now + 3600
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["address", "hwaddr", "expire"])
        for i in range(count):
            writer.writerow([f"10.0.{i // 256}.{i % 256}", f"00:00:00:00:{i:02x}:00", expire])


@pytest.fixture
def app(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text(json.dumps({"Dhcp4": {"shared-networks": []}}))
    leases_file = tmp_path / "leases.csv"

    app = create_app(config_overrides={
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/test.db",
    })
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SETTINGS_FILE"] = str(settings_file)
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    app.config["DHCP_LEASES_FILE"] = str(leases_file)
    yield app


@pytest.fixture
def client(app):
    return login(app.test_client(), app)


def _created(app, name):
    with open(app.config["DHCP_CONFIG_FILE"]) as f:
        config = json.load(f)
    return any(n["name"] == name for n in config["Dhcp4"]["shared-networks"])


# ── Nothing is gated ────────────────────────────────────────────────────────

@pytest.mark.parametrize("lease_count", [5, NAG_LEASE_THRESHOLD + 50, 5000])
def test_writes_succeed_at_any_lease_count(app, client, lease_count):
    """Unlicensed installs must stay fully writable no matter how large."""
    _write_leases(app.config["DHCP_LEASES_FILE"], lease_count)
    response = client.post("/new-shared-network", data={"shared-network-name": "net-x"})
    assert response.status_code == 302
    assert _created(app, "net-x")


def test_writes_succeed_with_unreadable_leases_file(app, client):
    """A missing/bogus leases path must not affect writability either way."""
    app.config["DHCP_LEASES_FILE"] = "/nonexistent/path/leases.csv"
    response = client.post("/new-shared-network", data={"shared-network-name": "net-y"})
    assert response.status_code == 302
    assert _created(app, "net-y")


def test_get_requests_work_over_threshold(app, client):
    _write_leases(app.config["DHCP_LEASES_FILE"], NAG_LEASE_THRESHOLD + 800)
    assert client.get("/pools").status_code == 200


# ── The reminder itself ─────────────────────────────────────────────────────

def test_unlicensed_small_install_gets_no_nag(app):
    """The footer reminder is gone: a small unlicensed install is silent."""
    with app.app_context():
        state = license_status(12)
    assert state["licensed"] is False
    assert state["notice"] == ""
    assert state["banner"] == ""


def test_unlicensed_large_install_gets_banner(app):
    with app.app_context():
        state = license_status(NAG_LEASE_THRESHOLD + 1)
    assert state["licensed"] is False
    assert str(NAG_LEASE_THRESHOLD + 1) in state["banner"]
    assert "commercial use requires a license" in state["banner"].lower()


def test_licensed_install_is_never_nagged(app, monkeypatch):
    monkeypatch.setattr("ez_kea.license.is_licensed", lambda: True)
    with app.app_context():
        state = license_status(9000)
    assert state == {"licensed": True, "notice": "", "banner": ""}


# ── Rendering ───────────────────────────────────────────────────────────────

def test_footer_notice_gone_when_unlicensed(app, client):
    """The removed footer note must not come back from the context processor."""
    _write_leases(app.config["DHCP_LEASES_FILE"], 10)
    body = client.get("/pools").get_data(as_text=True)
    assert "Commercial use requires a license" not in body
    assert "Unlicensed" not in body


def test_footer_still_carries_version_when_unlicensed(app, client):
    """Only the notice went; the version line is still rendered."""
    body = client.get("/pools").get_data(as_text=True)
    assert "EZ-KEA v" in body


def test_banner_rendered_over_threshold(app, client):
    _write_leases(app.config["DHCP_LEASES_FILE"], NAG_LEASE_THRESHOLD + 5)
    body = client.get("/pools").get_data(as_text=True)
    assert "active leases" in body
    assert "commercial use requires a license" in body.lower()
