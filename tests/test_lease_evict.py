# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
tests/test_lease_evict.py

The Leases page's Evict action: offered only while a remote endpoint is
selected, one lease4-del per click, and the row gone from the table straight
away.

Also pins the Kea 3.x response field name for a lease's lifetime
("valid-lft"): reading only 2.x's "valid-lifetime" left the lifetime at zero,
so a pulled lease's expiration came out equal to its allocation time -- which
is why a lease that had just been handed out rendered as "Expired".
"""
import time

import pytest

from test_kea_ctrl import fake_kea_http


def _lease(**overrides):
    """One lease4-get-all entry, in Kea 3.x's shape and always currently valid."""
    lease = {
        "ip-address": "10.195.1.32",
        "hw-address": "00:a0:98:2e:cc:74",
        "client-id": "01:00:a0:98:2e:cc:74",
        "cltt": int(time.time()) - 60,
        "valid-lft": 4000,
        "subnet-id": 3,
        "state": 0,
        "hostname": "infra-db-pluto.vm.lab.coxre.net",
        "fqdn-fwd": False,
        "fqdn-rev": False,
    }
    lease.update(overrides)
    return lease


def _leases_response(*leases):
    """A lease4-get-all answer, in the list Kea's HTTP channel wraps it in."""
    return [{
        "result": 0,
        "arguments": {"leases": list(leases)},
        "text": "%d IPv4 lease(s) found." % len(leases),
    }]


def _command_response(result, text):
    return [{"result": result, "text": text}]


@pytest.fixture
def app(tmp_path):
    from ez_kea import create_app

    config4 = tmp_path / "local-kea-dhcp4.conf"
    config4.write_text('{"Dhcp4": {"subnet4": []}}')
    (tmp_path / "backups").mkdir()

    app = create_app(config_overrides={
        "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(tmp_path / "test.db"),
    })
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SETTINGS_FILE"] = str(tmp_path / "settings.json")
    app.config["DHCP_CONFIG_FILE"] = str(config4)
    app.config["DHCP_CONFIG_FILE_EXPLICIT"] = True
    app.config["BACKUP_DIR"] = str(tmp_path / "backups")
    app.config["INSTANCE_DIR"] = str(tmp_path / "instances")
    app.config["DHCP_LEASES_FILE"] = str(tmp_path / "leases.csv")
    app.config["STATE_INDEX_DB"] = str(tmp_path / "stateindex.db")
    app.config["LOCAL_KEA4_PRESENT"] = True
    return app


@pytest.fixture
def client(app):
    from conftest import login
    return login(app.test_client(), app)


def _select_remote_endpoint(app, client, url="http://127.0.0.1:8000/"):
    """Register an http endpoint and make it the one this session acts on."""
    from ez_kea.core import instances

    with app.app_context():
        instance = instances.create_instance(
            "edge-1", instances.MODE_HTTP, http_url=url,
            http_user="admin", http_password="s3cret",
        )
        instance_id = instance.id
    with client.session_transaction() as sess:
        sess[instances.SESSION_KEY] = instance_id
    return instance_id


def test_a_local_daemons_lease_table_offers_no_evict(app, client):
    """No remote endpoint selected: no Operation column, and the route refuses."""
    open(app.config["DHCP_LEASES_FILE"], "w").close()

    page = client.get("/leases")
    assert page.status_code == 200
    assert b"Operation" not in page.data

    response = client.post("/leases/evict", data={"ip-address": "10.195.1.32"})
    assert response.status_code == 400
    assert b"remote Kea endpoint" in response.data


def test_evict_posts_lease4_del_and_refreshes_the_table(app, client):
    with fake_kea_http(
        _leases_response(_lease()),
        _command_response(0, "IPv4 lease deleted."),
        _leases_response(),
    ) as (endpoint, sent):
        _select_remote_endpoint(app, client, endpoint)

        page = client.get("/leases")
        assert page.status_code == 200
        body = page.data.decode()
        assert "Operation" in body
        assert "10.195.1.32" in body
        assert "Active" in body  # the lifetime is read, so it is not "Expired"

        response = client.post("/leases/evict", data={"ip-address": "10.195.1.32"})

        assert response.status_code == 302
        assert response.headers["Location"].startswith("/leases")

        commands = [request["body"]["command"] for request in sent]
        assert commands == ["lease4-get-all", "lease4-del", "lease4-get-all"]
        assert sent[1]["body"]["arguments"] == {"ip-address": "10.195.1.32"}
        # Credentials the endpoint was registered with are what reach the daemon.
        assert sent[1]["authorization"].startswith("Basic ")

        # The row is gone from the table itself (the success flash names the
        # address, so look for the empty state rather than for the string).
        after = client.get("/leases")
        assert "No Matching Leases" in after.data.decode()


def test_evict_filters_survive_the_redirect(app, client):
    with fake_kea_http(
        _leases_response(_lease()),
        _command_response(0, "IPv4 lease deleted."),
        _leases_response(),
    ) as (endpoint, sent):
        _select_remote_endpoint(app, client, endpoint)
        client.get("/leases")

        response = client.post("/leases/evict", data={
            "ip-address": "10.195.1.32", "q": "pluto", "status": "active", "page": "2",
        })

        assert response.status_code == 302
        assert "q=pluto" in response.headers["Location"]
        assert "status=active" in response.headers["Location"]
        assert "page=2" in response.headers["Location"]


def test_a_lease_that_is_already_gone_is_not_reported_as_a_failure(app, client):
    with fake_kea_http(
        _leases_response(_lease()),
        _command_response(3, "IPv4 lease not found."),
        _leases_response(),
    ) as (endpoint, sent):
        _select_remote_endpoint(app, client, endpoint)
        client.get("/leases")

        response = client.post("/leases/evict", data={"ip-address": "10.195.1.32"},
                               follow_redirects=True)

        assert response.status_code == 200
        assert "already gone" in response.data.decode()
        # The stale row is cleared rather than left there to be clicked again.
        assert [request["body"]["command"] for request in sent].count("lease4-get-all") == 2


def test_a_refused_evict_reaches_the_operator(app, client):
    with fake_kea_http(
        _leases_response(_lease()),
        _command_response(1, "unable to delete the lease"),
    ) as (endpoint, sent):
        _select_remote_endpoint(app, client, endpoint)
        client.get("/leases")

        response = client.post("/leases/evict", data={"ip-address": "10.195.1.32"},
                               follow_redirects=True)

        assert response.status_code == 200
        assert "unable to delete the lease" in response.data.decode()
        # A refused delete must not be papered over with a success flash.
        assert "evicted from" not in response.data.decode()


def test_a_malformed_address_never_reaches_the_daemon(app, client):
    with fake_kea_http(_leases_response(_lease())) as (endpoint, sent):
        _select_remote_endpoint(app, client, endpoint)
        client.get("/leases")

        before = len(sent)
        response = client.post("/leases/evict", data={"ip-address": "not-an-address"})

        assert response.status_code == 302
        assert len(sent) == before
        assert "not a valid IPv4 address" in client.get("/leases").data.decode()


# -- the pulled-lease expiration --------------------------------------------

def test_kea3_valid_lft_is_read_as_the_lifetime():
    """Regression: "valid-lft" (3.x) must produce expire, not just cltt."""
    from ez_kea.core.instances import _lease_row

    cltt = int(time.time()) - 60
    row = _lease_row(_lease(cltt=cltt, **{"valid-lft": 4000}), "4")
    assert row["expire"] == str(cltt + 4000)
    assert row["valid_lifetime"] == "4000"


def test_kea2_valid_lifetime_is_still_read():
    """The 2.x spelling keeps working for daemons that have not been upgraded."""
    from ez_kea.core.instances import _lease_row

    cltt = int(time.time()) - 60
    row = _lease_row({"ip-address": "10.0.0.5", "valid-lifetime": 600, "cltt": cltt}, "4")
    assert row["expire"] == str(cltt + 600)


def test_a_pulled_lease_is_active_rather_than_expired(app, client):
    """The user-visible symptom: a lease that was just issued shows as expired."""
    from ez_kea.core import instances, state_index

    with fake_kea_http(_leases_response(_lease())) as (endpoint, sent):
        instance_id = _select_remote_endpoint(app, client, endpoint)
        client.get("/leases")

        with app.app_context():
            instance = instances.get_instance(instance_id)
            mirror = instances.mirror_paths(instance)["leases4"]
            [row] = state_index.parse_lease4(mirror, {3: "10.195.1.0/24"})
            assert row["expire"] - row["valid_lifetime"] == _lease()["cltt"]
            assert state_index.status_label(row["state"], row["expire"]) == "active"
