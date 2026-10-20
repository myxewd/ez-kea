# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
The multi-endpoint registry: persistence, the per-request scope switch, and
pulling a remote daemon's live configuration into its mirror.
"""
import json

import pytest
from flask import current_app

from test_kea_ctrl import fake_kea_http


@pytest.fixture
def app(tmp_path):
    from ez_kea import create_app

    config4 = tmp_path / "local-kea-dhcp4.conf"
    config4.write_text(json.dumps({"Dhcp4": {"subnet4": [], "comment": "LOCAL-DEFAULT"}}))
    config6 = tmp_path / "local-kea-dhcp6.conf"
    config6.write_text(json.dumps({"Dhcp6": {"subnet6": []}}))

    app = create_app(config_overrides={
        "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(tmp_path / "test.db"),
    })
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SETTINGS_FILE"] = str(tmp_path / "settings.json")
    app.config["DHCP_CONFIG_FILE"] = str(config4)
    app.config["DHCP6_CONFIG_FILE"] = str(config6)
    app.config["DHCP_CONFIG_FILE_EXPLICIT"] = True
    app.config["DHCP6_CONFIG_FILE_EXPLICIT"] = True
    app.config["BACKUP_DIR"] = str(tmp_path / "backups")
    app.config["INSTANCE_DIR"] = str(tmp_path / "instances")
    app.config["DHCP_LEASES_FILE"] = str(tmp_path / "leases.csv")
    app.config["DHCP_LOG_FILE"] = str(tmp_path / "kea.log")
    app.config["DHCP6_LEASES_FILE"] = str(tmp_path / "leases6.csv")
    app.config["DHCP6_LOG_FILE"] = str(tmp_path / "kea6.log")
    # This fixture stands for an installation that does have a local Kea (the
    # real value comes from startup discovery). Tests that care about a
    # standalone deployment flip these off.
    app.config["LOCAL_KEA4_PRESENT"] = True
    app.config["LOCAL_KEA6_PRESENT"] = True
    return app


@pytest.fixture
def client(app):
    from conftest import login
    return login(app.test_client(), app)


def _add_http_instance(client, **overrides):
    data = {
        "name": "edge-1",
        "mode": "http",
        "http_url": "http://127.0.0.1:8000/",
        "http_user": "admin",
        "http_password": "s3cret",
    }
    data.update(overrides)
    return client.post("/instances/add", data=data)


def test_adding_an_http_instance_persists_it_and_seeds_its_mirrors(app, client):
    from ez_kea.core import instances

    with app.app_context():
        response = _add_http_instance(client)
        assert response.status_code == 302

        [instance] = instances.all_instances()
        assert instance.name == "edge-1"
        assert instance.mode == "http"
        # Normalised on the way in, so the dropdown and the request agree.
        assert instance.http_url == "http://127.0.0.1:8000/"

        paths = instances.mirror_paths(instance)
        assert json.load(open(paths["4"]))["Dhcp4"]["subnet4"] == []
        assert json.load(open(paths["6"]))["Dhcp6"]["subnet6"] == []


def test_a_local_instance_points_at_the_files_it_was_given(app, client):
    from ez_kea.core import instances

    client.post("/instances/add", data={
        "name": "site-b",
        "mode": "local",
        "dhcp4_config_file": "/srv/site-b/kea-dhcp4.conf",
        "dhcp6_config_file": "/srv/site-b/kea-dhcp6.conf",
    })

    with app.app_context():
        [instance] = instances.all_instances()
        overrides = instances.overrides_for(instance)
    assert overrides["DHCP_CONFIG_FILE"] == "/srv/site-b/kea-dhcp4.conf"
    assert overrides["DHCP6_CONFIG_FILE"] == "/srv/site-b/kea-dhcp6.conf"


def test_an_http_instance_needs_a_usable_url(app, client):
    from ez_kea.core import instances

    _add_http_instance(client, http_url="ftp://kea.example.org")
    with app.app_context():
        assert instances.all_instances() == []


def test_selecting_an_endpoint_re_points_the_whole_app(app, client):
    """The switch has to reach routes that know nothing about endpoints, which
    it does by overriding current_app.config for the request only."""
    from ez_kea.core import instances

    _add_http_instance(client)
    with app.app_context():
        [instance] = instances.all_instances()
        mirror = instances.mirror_paths(instance)["4"]
        instance_id = instance.id

    # Outside a request (or with nothing selected) the installation default wins.
    with app.app_context():
        assert current_app.config["DHCP_CONFIG_FILE"] != mirror

    with app.test_request_context():
        from flask import session
        session[instances.SESSION_KEY] = instance_id
        instances.apply_selected_instance()
        assert current_app.config["DHCP_CONFIG_FILE"] == mirror
        assert current_app.config["KEA_RELOAD_STRATEGY"] == "http"
        assert current_app.config["KEA_HTTP_URL"] == "http://127.0.0.1:8000/"
        assert current_app.config["KEA_HTTP_USER"] == "admin"

    # ...and it is gone again once the request ends.
    with app.app_context():
        assert current_app.config["DHCP_CONFIG_FILE"] != mirror


def test_the_dashboard_reads_the_selected_endpoints_config(app, client):
    """End-to-end: select an endpoint, then load a page that renders the config
    file. It must be that endpoint's file, not this installation's."""
    from ez_kea.core import instances

    _add_http_instance(client)
    with app.app_context():
        [instance] = instances.all_instances()
        paths = instances.mirror_paths(instance)
        instance_id = instance.id

    # Distinguish the mirror's content from the local default.
    with open(paths["4"], "w") as handle:
        json.dump({"Dhcp4": {"subnet4": [], "comment": "MIRROR-OF-EDGE-1"}}, handle)

    response = client.post("/instances/select", data={"instance_id": instance_id})
    assert response.status_code == 302

    page = client.get("/advanced-config")
    assert page.status_code == 200
    assert "MIRROR-OF-EDGE-1" in page.get_data(as_text=True)


def test_selecting_the_installation_default_clears_the_switch(app, client):
    from ez_kea.core import instances

    _add_http_instance(client)
    with app.app_context():
        [instance] = instances.all_instances()

    client.post("/instances/select", data={"instance_id": instance.id})
    client.post("/instances/select", data={"instance_id": "default"})

    with client.session_transaction() as sess:
        assert instances.SESSION_KEY not in sess

    page = client.get("/advanced-config")
    assert "LOCAL-DEFAULT" in page.get_data(as_text=True)


def test_pull_fills_the_mirror_from_the_daemon(app, client):
    """config-get is what makes 'EZ-KEA pulls the selected daemon's config'
    true: the mirror is replaced with exactly what the daemon reported."""
    from ez_kea.core import instances

    live_document = {
        "Dhcp4": {"subnet4": [{"subnet": "198.51.100.0/24", "id": 7, "pools": []}]},
        "Logging": {"loggers": [{"name": "kea-dhcp4"}]},
    }
    with fake_kea_http([{"result": 0, "arguments": live_document}]) as (endpoint, requests):
        _add_http_instance(client, http_url=endpoint, http_user="", http_password="")

        with app.app_context():
            [instance] = instances.all_instances()
            mirrored = json.load(open(instances.mirror_paths(instance)["4"]))

    assert requests[0]["body"] == {"command": "config-get"}
    assert mirrored["Dhcp4"]["subnet4"][0]["subnet"] == "198.51.100.0/24"
    # The logger block travels with the daemon section; dropping it would
    # describe a daemon with logging unconfigured.
    assert mirrored["Logging"] == live_document["Logging"]


def test_pull_reports_a_refusal_instead_of_clobbering_the_mirror(app, client):
    from ez_kea.core import instances

    # Two responses: adding an http endpoint pulls once, then the test pulls again.
    with fake_kea_http([{"result": 1, "text": "Forbidden"}],
                       [{"result": 1, "text": "Forbidden"}]) as (endpoint, _):
        _add_http_instance(client, http_url=endpoint)
        with app.app_context():
            [instance] = instances.all_instances()
            warnings = instances.pull_config(instance)
            mirrored = json.load(open(instances.mirror_paths(instance)["4"]))

    assert any("Forbidden" in w for w in warnings)
    assert mirrored["Dhcp4"]["subnet4"] == []


def test_deleting_an_endpoint_clears_the_selection(app, client):
    from ez_kea.core import instances

    _add_http_instance(client)
    with app.app_context():
        [instance] = instances.all_instances()
        instance_id = instance.id

    client.post("/instances/select", data={"instance_id": instance_id})
    client.post("/instances/%d/delete" % instance_id)

    with app.app_context():
        assert instances.all_instances() == []
    with client.session_transaction() as sess:
        assert instances.SESSION_KEY not in sess


def test_the_navbar_lists_every_endpoint(app, client):
    _add_http_instance(client, name="toronto-core")

    body = client.get("/").get_data(as_text=True)
    assert "toronto-core" in body
    # The installation itself stays selectable from the same dropdown.
    assert "This installation" in body


def test_a_standalone_installation_offers_no_local_entry(app, client):
    """EZ-KEA deployed without a local Kea must not offer "This installation":
    that entry would point at an empty sandbox file."""
    app.config["LOCAL_KEA4_PRESENT"] = False
    app.config["LOCAL_KEA6_PRESENT"] = False
    _add_http_instance(client)

    body = client.get("/instances").get_data(as_text=True)
    assert "This installation" not in body

    nav = client.get("/").get_data(as_text=True)
    assert "This installation" not in nav


def test_a_standalone_installation_falls_back_to_an_endpoint(app, client):
    """With no local Kea and nothing selected, the first endpoint is used, so
    no page describes a sandbox file nobody asked for."""
    app.config["LOCAL_KEA4_PRESENT"] = False
    app.config["LOCAL_KEA6_PRESENT"] = False
    _add_http_instance(client)

    with app.app_context():
        from ez_kea.core import instances
        [instance] = instances.all_instances()
        mirror = instances.mirror_paths(instance)["4"]

    with app.test_request_context():
        from ez_kea.core import instances
        instances.apply_selected_instance()
        assert current_app.config["DHCP_CONFIG_FILE"] == mirror


def test_ipv6_navigation_is_hidden_without_a_v6_endpoint(app, client):
    """An endpoint with only an IPv4 control channel has no DHCPv6 side, so the
    IPv6 menu (and its pages) must not be offered."""
    _add_http_instance(client, http_url6="")
    with app.app_context():
        from ez_kea.core import instances
        [instance] = instances.all_instances()

    client.post("/instances/select", data={"instance_id": instance.id})
    body = client.get("/").get_data(as_text=True)
    # The dashboard still counts what the mirror holds (and links to v6 pages
    # of its own); the *menu element* is what must be gone.
    assert 'id="navIpv6Menu"' not in body

    # ...and a direct hit on a v6 page is refused rather than rendered empty.
    response = client.get("/global-settings/6")
    assert response.status_code == 302


def test_ipv6_navigation_returns_when_the_endpoint_has_a_v6_url(app, client):
    _add_http_instance(client, http_url6="http://127.0.0.1:8001/")
    with app.app_context():
        from ez_kea.core import instances
        [instance] = instances.all_instances()

    client.post("/instances/select", data={"instance_id": instance.id})
    assert 'id="navIpv6Menu"' in client.get("/").get_data(as_text=True)


def test_log_search_is_hidden_for_a_remote_endpoint(app, client):
    """Logs live on the daemon's host; the page would otherwise read this
    installation's log files and look plausible."""
    _add_http_instance(client)
    with app.app_context():
        from ez_kea.core import instances
        [instance] = instances.all_instances()

    client.post("/instances/select", data={"instance_id": instance.id})
    assert "Log Search" not in client.get("/").get_data(as_text=True)
    assert client.get("/logs").status_code == 302


def test_remote_endpoint_settings_replace_the_local_file_settings(app, client):
    """Global Settings must not offer local file/binary paths for a daemon on
    another host: it shows that endpoint's own settings instead."""
    _add_http_instance(client)
    with app.app_context():
        from ez_kea.core import instances
        [instance] = instances.all_instances()

    client.post("/instances/select", data={"instance_id": instance.id})
    body = client.get("/app-settings").get_data(as_text=True)

    assert "Endpoint Settings" in body
    assert 'name="http_url"' in body          # the endpoint's own field
    assert 'name="dhcp-config-file"' not in body   # the local path field
    assert 'name="kea-ctrl-cmd"' not in body       # the local binary field
    # The mirror path it is actually editing is named, so the operator knows.
    assert "instance-%d" % instance.id in body


def test_editing_an_endpoint_keeps_the_password_when_left_blank(app, client):
    _add_http_instance(client, http_password="super-secret-value")
    with app.app_context():
        from ez_kea.core import instances
        [instance] = instances.all_instances()
        instance_id = instance.id

    client.post("/instances/%d/edit" % instance_id, data={
        "name": "renamed",
        "http_url": "http://127.0.0.1:9000/",
        "http_user": "admin",
        "http_password": "",
    })

    with app.app_context():
        from ez_kea.core import instances
        updated = instances.get_instance(instance_id)
        assert updated.name == "renamed"
        assert updated.http_url == "http://127.0.0.1:9000/"
        assert updated.http_password == "super-secret-value"


def test_editing_an_endpoint_rejects_an_unusable_url(app, client):
    _add_http_instance(client)
    with app.app_context():
        from ez_kea.core import instances
        [instance] = instances.all_instances()
        instance_id = instance.id

    client.post("/instances/%d/edit" % instance_id, data={
        "name": "edge-1", "http_url": "ftp://kea", "http_url6": "",
    })

    with app.app_context():
        from ez_kea.core import instances
        assert instances.get_instance(instance_id).http_url == "http://127.0.0.1:8000/"


# ── pulling a remote endpoint's lease table ─────────────────────────────────

LEASE4 = {
    "ip-address": "192.0.2.50",
    "hw-address": "00:11:22:33:44:55",
    "client-id": "01:00:11:22:33:44:55",
    "valid-lifetime": 4000,
    "cltt": 1000,
    "subnet-id": 1,
    "hostname": "laptop-01",
    "state": 0,
    "fqdn-fwd": False,
    "fqdn-rev": False,
    "user-context": "",
    "pool-id": 0,
}


def test_pull_leases_writes_a_memfile_csv(app):
    """The pulled table is written in Kea's own memfile shape, so every
    existing reader (Leases page, search index, dashboard count) is unchanged."""
    import csv

    with fake_kea_http([{"result": 0, "arguments": {"leases": [LEASE4]}}]) as (endpoint, requests):
        with app.app_context():
            from ez_kea.core import instances
            instance = instances.create_instance("remote", "http", http_url=endpoint)
            warnings = instances.pull_leases(instance, "4")
            path = instances.mirror_paths(instance)["leases4"]
            rows = list(csv.DictReader(open(path)))

    assert requests[0]["body"] == {"command": "lease4-get-all"}
    assert warnings == []
    assert len(rows) == 1
    assert rows[0]["address"] == "192.0.2.50"
    assert rows[0]["hwaddr"] == "00:11:22:33:44:55"
    assert rows[0]["hostname"] == "laptop-01"
    # Kea reports cltt + valid-lifetime, the CSV wants the absolute expiry.
    assert rows[0]["expire"] == "5000"


def test_pull_leases_treats_result_3_as_an_empty_table(app):
    """Kea answers result=3 ("0 leases found") for a daemon that has none --
    that is a successful pull, not an error."""
    with fake_kea_http([{"result": 3, "text": "0 IPv4 lease(s) found."}]) as (endpoint, _):
        with app.app_context():
            from ez_kea.core import instances
            instance = instances.create_instance("remote", "http", http_url=endpoint)
            assert instances.pull_leases(instance, "4") == []


def test_ensure_leases_pulls_only_when_the_mirror_is_stale(app):
    """The freshness window is what keeps a full-table pull off every render."""
    with fake_kea_http([{"result": 0, "arguments": {"leases": [LEASE4]}}],
                       [{"result": 0, "arguments": {"leases": []}}]) as (endpoint, requests):
        with app.app_context():
            from ez_kea.core import instances
            instance = instances.create_instance("remote", "http", http_url=endpoint)
            from flask import g, session
            with app.test_request_context():
                session[instances.SESSION_KEY] = instance.id
                instances.apply_selected_instance()
                assert instances.ensure_leases("4") == []      # first view pulls
                assert len(requests) == 1
                assert instances.ensure_leases("4") == []      # second view reuses it
                assert len(requests) == 1


def test_the_leases_page_shows_a_remote_endpoints_leases(app, client):
    """End-to-end read path: select the endpoint, open /leases, see the lease
    that came off the daemon (not an empty local file)."""
    with fake_kea_http([{"result": 0, "arguments": {"leases": [LEASE4]}}]) as (endpoint, requests):
        with app.app_context():
            from ez_kea.core import instances
            instance = instances.create_instance("remote", "http", http_url=endpoint)
            instance_id = instance.id

        with client.session_transaction() as sess:
            sess[instances.SESSION_KEY] = instance_id

        page = client.get("/leases").get_data(as_text=True)

    assert [r["body"]["command"] for r in requests] == ["lease4-get-all"]
    assert "192.0.2.50" in page
    assert "laptop-01" in page


def test_config_snapshot_carries_the_overrides(app):
    """dict(current_app.config) bypasses __getitem__ on a dict subclass, which
    is exactly why every request-path snapshot has to go through this helper."""
    with app.app_context():
        from ez_kea.core import instances
        instance = instances.create_instance("remote", "http", http_url="http://127.0.0.1:8000/")

    with app.test_request_context():
        from flask import current_app, session
        from ez_kea.core import instances as inst
        session[inst.SESSION_KEY] = instance.id
        inst.apply_selected_instance()

        snapshot = inst.config_snapshot(current_app.config)
        assert snapshot["DHCP_CONFIG_FILE"] == inst.mirror_paths(instance)["4"]
        assert snapshot["STATE_INDEX_DB"].endswith("stateindex.db")
        # The trap this exists for: a plain dict() copy loses the override.
        assert dict(current_app.config)["DHCP_CONFIG_FILE"] != snapshot["DHCP_CONFIG_FILE"]


def test_the_local_syntax_check_is_skipped_for_a_remote_endpoint(app):
    """kea-dhcpN -t runs against a mirror of another host's config: its hook
    libraries and auth files may not exist here, so the daemon's own
    config-test is the authority (and the check must not block saves)."""
    from ez_kea.core.config_validate import test_config_file
    from ez_kea.core import instances

    with app.app_context():
        instance = instances.create_instance("remote", "http", http_url="http://127.0.0.1:8000/")

    with app.test_request_context():
        from flask import session
        session[instances.SESSION_KEY] = instance.id
        instances.apply_selected_instance()

        result = test_config_file("/nonexistent/kea-dhcp4.conf",
                                  {"KEA_DHCP4_CMD": "kea-dhcp4"}, "Dhcp4")

    assert result.ok is False
    assert result.ran is False           # "could not run", not "Kea refused"
    assert "remote endpoint" in result.output


def test_the_local_syntax_check_still_runs_for_the_installation(app):
    """With nothing remote selected the check is exactly what it always was."""
    from ez_kea.core import instances

    with app.test_request_context():
        instances.apply_selected_instance()
        assert instances.local_syntax_check_applicable() is True


def test_the_endpoints_page_never_renders_the_password(app, client):
    _add_http_instance(client, http_password="super-secret-value")

    body = client.get("/instances").get_data(as_text=True)
    assert "super-secret-value" not in body
