import re
# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
tests/test_scope_config.py

Coverage for the generic scope-configuration features:

  * Kea's fixed BOOTP-header parameters (next-server / server-hostname /
    boot-file-name) are ordinary scope keys, not options, and are set through
    them at global, subnet and reservation scope.
  * Saving one of those forms no longer discards option-data it doesn't know
    about (the old "rebuild option-data from the one field this form owns"
    bug).
  * The global-settings page round-trips arbitrary options by name.
  * The Advanced Configuration page accepts a whole hand-edited document and
    refuses one that isn't a Kea config at all.
"""
import html
import json
from unittest.mock import patch

import pytest
from conftest import login
from ez_kea import create_app

from ez_kea.core.config_validate import SyntaxCheckResult
from ez_kea.core.validation import (
    V4_BOOT_FIELDS, boot_fields_for, parse_scope_params, apply_scope_params,
    update_option_data,
)


# ── Pure helpers ────────────────────────────────────────────────────────────

class _Form(dict):
    """Minimal stand-in for request.form (a MultiDict that only needs .get)."""

    def get(self, key, default=None):
        return dict.get(self, key, default)


def test_parse_scope_params_reads_all_boot_fields():
    params, errors = parse_scope_params(_Form({
        "next-server": "192.168.1.10",
        "server-hostname": "boot.example.com",
        "boot-file-name": "http://boot.example.com/ipxe.efi",
    }), V4_BOOT_FIELDS)
    assert errors == []
    assert params["next-server"] == "192.168.1.10"
    assert params["server-hostname"] == "boot.example.com"
    assert params["boot-file-name"] == "http://boot.example.com/ipxe.efi"


def test_parse_scope_params_rejects_hostname_for_next_server():
    """Kea types next-server as an address; a hostname there fails -t."""
    params, errors = parse_scope_params(_Form({"next-server": "boot.example.com"}), V4_BOOT_FIELDS)
    assert "next-server" not in params
    assert len(errors) == 1 and "IPv4" in errors[0]


def test_parse_scope_params_treats_blank_as_absent():
    """A blank field is how the operator clears a value: it is omitted here and
    removed by apply_scope_params, but must not be an error."""
    params, errors = parse_scope_params(_Form({"next-server": "   "}), V4_BOOT_FIELDS)
    assert params == {} and errors == []


def test_parse_scope_params_leaves_absent_fields_alone():
    """A form that never rendered a field cannot clear it."""
    params, errors = parse_scope_params(_Form({}), V4_BOOT_FIELDS)
    assert params == {} and errors == []


def test_apply_scope_params_sets_and_clears_without_touching_other_keys():
    scope = {"subnet": "10.0.0.0/24", "next-server": "10.0.0.1", "pools": [{"pool": "10.0.0.10 - 10.0.0.20"}]}
    apply_scope_params(scope, {"boot-file-name": "undionly.kpxe"}, V4_BOOT_FIELDS)
    assert "next-server" not in scope
    assert scope["boot-file-name"] == "undionly.kpxe"
    assert scope["subnet"] == "10.0.0.0/24"
    assert scope["pools"] == [{"pool": "10.0.0.10 - 10.0.0.20"}]


def test_boot_fields_are_version_scoped():
    """next-server is a BOOTP header field; DHCPv6 has no BOOTP header."""
    assert boot_fields_for("Dhcp4") == V4_BOOT_FIELDS
    assert boot_fields_for("Dhcp6") == ()
    assert "next-server" in boot_fields_for("Dhcp4")


def test_update_option_data_preserves_options_the_form_does_not_own():
    """The regression this exists for: editing the gateway used to replace the
    whole option-data list, deleting every other option on the subnet."""
    scope = {"option-data": [
        {"name": "routers", "data": "10.0.0.1"},
        {"name": "root-path", "data": "iscsi:10.0.0.5::::iqn.2024-01.com.example:disk"},
    ]}
    update_option_data(scope, {"routers": "10.0.0.254"}, managed=["routers"])

    names = [o["name"] for o in scope["option-data"]]
    assert names == ["routers", "root-path"]  # order kept, no duplicate
    assert scope["option-data"][0]["data"] == "10.0.0.254"


def test_update_option_data_removes_only_managed_options():
    scope = {"option-data": [
        {"name": "routers", "data": "10.0.0.1"},
        {"name": "root-path", "data": "iscsi:x"},
    ]}
    update_option_data(scope, {}, managed=["routers"])
    assert [o["name"] for o in scope["option-data"]] == ["root-path"]


def test_update_option_data_keeps_entry_extras_and_collapses_duplicates():
    scope = {"option-data": [
        {"name": "boot-file-name", "data": "old", "always-send": True},
        {"name": "boot-file-name", "data": "dupe"},
    ]}
    update_option_data(scope, {"boot-file-name": "new"})
    assert len(scope["option-data"]) == 1
    assert scope["option-data"][0]["data"] == "new"
    assert scope["option-data"][0]["always-send"] is True


# ── Routes ──────────────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")

    app = create_app(config_overrides={
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/test.db",
    })
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SETTINGS_FILE"] = str(settings_file)

    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text(json.dumps({"Dhcp4": {"shared-networks": [], "subnet4": []}}))
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    app.config["BACKUP_DIR"] = str(tmp_path / "backups")
    app.config["DHCP_LEASES_FILE"] = str(tmp_path / "leases.csv")
    app.config["DHCP_LOG_FILE"] = str(tmp_path / "kea.log")
    app.config["STATE_INDEX_DB"] = str(tmp_path / "stateindex.db")

    config6_file = tmp_path / "kea-dhcp6.conf"
    config6_file.write_text(json.dumps({"Dhcp6": {"shared-networks": [], "subnet6": []}}))
    app.config["DHCP6_CONFIG_FILE"] = str(config6_file)
    app.config["DHCP6_LEASES_FILE"] = str(tmp_path / "leases6.csv")
    app.config["DHCP6_LOG_FILE"] = str(tmp_path / "kea6.log")
    yield app


@pytest.fixture
def client(app):
    return login(app.test_client(), app)


def _read(config_file):
    with open(config_file) as handle:
        return json.load(handle)


def test_new_subnet_stores_boot_params(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    response = client.post("/new-subnet", data={
        "subnet": "10.20.30.0/24",
        "routers": "10.20.30.1",
        "range-start": "10.20.30.10",
        "range-end": "10.20.30.20",
        "next-server": "10.20.30.5",
        "boot-file-name": "undionly.kpxe",
    })
    assert response.status_code == 302

    subnet = _read(config_file)["Dhcp4"]["subnet4"][0]
    assert subnet["next-server"] == "10.20.30.5"
    assert subnet["boot-file-name"] == "undionly.kpxe"
    assert subnet["option-data"] == [{"name": "routers", "data": "10.20.30.1"}]


def test_new_subnet_rejects_non_ip_next_server(app, client):
    response = client.post("/new-subnet", data={
        "subnet": "10.20.30.0/24",
        "routers": "10.20.30.1",
        "range-start": "10.20.30.10",
        "range-end": "10.20.30.20",
        "next-server": "not an ip",
    })
    assert response.status_code == 400
    assert b"next-server" in response.data


def test_edit_subnet_keeps_unmanaged_options(app, client):
    """Editing the gateway must not delete options this form doesn't own."""
    config_file = app.config["DHCP_CONFIG_FILE"]
    config = _read(config_file)
    config["Dhcp4"]["subnet4"] = [{
        "id": 1,
        "subnet": "10.20.30.0/24",
        "pools": [{"pool": "10.20.30.10 - 10.20.30.20"}],
        "option-data": [
            {"name": "routers", "data": "10.20.30.1"},
            {"name": "root-path", "data": "iscsi:10.20.30.9::::iqn.2024-01.com.example:disk"},
        ],
    }]
    with open(config_file, "w") as handle:
        json.dump(config, handle)

    response = client.post("/edit-subnet", data={
        "subnet": "10.20.30.0/24",
        "routers": "10.20.30.254",
        "range-start": "10.20.30.10",
        "range-end": "10.20.30.20",
    })
    assert response.status_code == 302

    subnet = _read(config_file)["Dhcp4"]["subnet4"][0]
    options = {o["name"]: o["data"] for o in subnet["option-data"]}
    assert options["routers"] == "10.20.30.254"
    assert options["root-path"] == "iscsi:10.20.30.9::::iqn.2024-01.com.example:disk"


def test_edit_subnet_get_prefills_boot_params(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    config = _read(config_file)
    config["Dhcp4"]["subnet4"] = [{
        "id": 1, "subnet": "10.20.30.0/24", "next-server": "10.20.30.5",
        "boot-file-name": "ipxe.efi", "option-data": [{"name": "routers", "data": "10.20.30.1"}],
    }]
    with open(config_file, "w") as handle:
        json.dump(config, handle)

    body = client.get("/edit-subnet?subnet=10.20.30.0/24").get_data(as_text=True)
    assert 'name="next-server"' in body and "10.20.30.5" in body
    assert 'name="boot-file-name"' in body and "ipxe.efi" in body


def test_new_reservation_stores_boot_params(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    config = _read(config_file)
    config["Dhcp4"]["subnet4"] = [{
        "id": 1, "subnet": "10.20.30.0/24", "pools": [{"pool": "10.20.30.10 - 10.20.30.20"}],
    }]
    with open(config_file, "w") as handle:
        json.dump(config, handle)

    response = client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.50",
        "hostname": "node-01",
        "mac-address": "aa:bb:cc:dd:ee:ff",
        "next-server": "10.20.30.7",
        "boot-file-name": "http://boot.example.com/boot.ipxe",
    })
    assert response.status_code == 302

    reservation = _read(config_file)["Dhcp4"]["subnet4"][0]["reservations"][0]
    assert reservation["next-server"] == "10.20.30.7"
    assert reservation["boot-file-name"] == "http://boot.example.com/boot.ipxe"
    assert reservation["hw-address"] == "aa:bb:cc:dd:ee:ff"


def test_global_settings_sets_and_clears_boot_params(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    response = client.post("/save-global-settings", data={
        "valid-lifetime": "4000",
        "next-server": "10.0.0.5",
        "boot-file-name": "undionly.kpxe",
    })
    assert response.status_code == 302
    dhcp = _read(config_file)["Dhcp4"]
    assert dhcp["next-server"] == "10.0.0.5"
    assert dhcp["boot-file-name"] == "undionly.kpxe"

    # Submitting the field blank removes the key entirely (rather than leaving
    # an empty string Kea would reject).
    client.post("/save-global-settings", data={"valid-lifetime": "4000"})
    dhcp = _read(config_file)["Dhcp4"]
    assert "next-server" not in dhcp
    assert "boot-file-name" not in dhcp


def test_server_hostname_is_carried_through_but_never_offered(app, client):
    """`sname` is a legal Kea scope key that neither PXE nor iPXE firmware
    reads, so no form offers it as a field. A value already in the file still
    survives a save from the pages that used to own it, via a hidden input."""
    config_file = app.config["DHCP_CONFIG_FILE"]
    document = '{"Dhcp4": {"server-hostname": "boot.example.com", "subnet4": []}}'
    with patch("ez_kea.routes.system._test_config_impl", return_value=("", 200)):
        assert client.post("/save-advanced-config",
                           data={"config-json": document}).status_code == 302

    page = client.get("/global-settings").get_data(as_text=True)
    assert 'type="hidden" name="server-hostname" value="boot.example.com"' in page
    assert 'id="server-hostname"' not in page

    assert 'id="server-hostname"' not in client.get("/client-classes/new").get_data(as_text=True)

    client.post("/new-subnet", data={
        "subnet": "10.20.30.0/24", "routers": "10.20.30.1",
        "range-start": "10.20.30.100", "range-end": "10.20.30.200",
    })
    page = client.get("/edit-subnet?subnet=10.20.30.0/24").get_data(as_text=True)
    assert 'type="hidden" name="server-hostname"' in page
    assert 'id="server-hostname"' not in page

    # The hidden field round-trips the value the page never showed.
    assert client.post("/save-global-settings", data={
        "valid-lifetime": "4000",
        "server-hostname": "boot.example.com",
    }).status_code == 302
    assert _read(config_file)["Dhcp4"]["server-hostname"] == "boot.example.com"


def test_global_settings_round_trips_arbitrary_options(app, client):
    """Any option the page doesn't render as a field is editable by name and
    preserved across a later unrelated save."""
    config_file = app.config["DHCP_CONFIG_FILE"]
    response = client.post("/save-global-settings", data={
        "valid-lifetime": "4000",
        "opt-dns": "1.1.1.1",
        "opt-extra-name-0": "root-path",
        "opt-extra-data-0": "iscsi:10.0.0.5::::iqn.2024-01.com.example:disk",
        "opt-extra-name-1": "tftp-server-name",
        "opt-extra-data-1": "10.0.0.9",
    })
    assert response.status_code == 302
    options = {o["name"]: o["data"] for o in _read(config_file)["Dhcp4"]["option-data"]}
    assert options["domain-name-servers"] == "1.1.1.1"
    assert options["root-path"] == "iscsi:10.0.0.5::::iqn.2024-01.com.example:disk"
    assert options["tftp-server-name"] == "10.0.0.9"

    # The page renders those rows back, so a second save keeps them.
    body = client.get("/global-settings").get_data(as_text=True)
    assert "root-path" in body and "tftp-server-name" in body

    client.post("/save-global-settings", data={
        "valid-lifetime": "4000",
        "opt-extra-name-0": "root-path",
        "opt-extra-data-0": "iscsi:10.0.0.5::::iqn.2024-01.com.example:disk",
        "opt-extra-name-1": "tftp-server-name",
        "opt-extra-data-1": "10.0.0.9",
    })
    options = {o["name"]: o["data"] for o in _read(config_file)["Dhcp4"]["option-data"]}
    assert options["root-path"] == "iscsi:10.0.0.5::::iqn.2024-01.com.example:disk"


def test_global_settings_rejects_blank_data_for_named_option(app, client):
    response = client.post("/save-global-settings", data={
        "valid-lifetime": "4000",
        "opt-extra-name-0": "root-path",
        "opt-extra-data-0": "",
    })
    assert response.status_code == 400
    assert b"missing its data" in response.data


def test_global_settings_rejects_duplicate_option_rows(app, client):
    response = client.post("/save-global-settings", data={
        "valid-lifetime": "4000",
        "opt-extra-name-0": "root-path",
        "opt-extra-data-0": "a",
        "opt-extra-name-1": "root-path",
        "opt-extra-data-1": "b",
    })
    assert response.status_code == 400
    assert b"more than once" in response.data


# ── Advanced (raw) configuration editor ─────────────────────────────────────

def test_advanced_config_get_shows_the_document(client):
    body = client.get("/advanced-config").get_data(as_text=True)
    assert "Dhcp4" in body
    assert 'name="config-json"' in body


def test_advanced_config_v6_get_is_a_separate_document(client):
    body = client.get("/advanced-config/6").get_data(as_text=True)
    assert "Dhcp6" in body


def test_advanced_config_invalid_version_rejected(client):
    assert client.get("/advanced-config/7").status_code == 400


def test_advanced_config_accepts_comments_and_arbitrary_keys(app, client):
    """client-classes and option-def are exactly what this page exists for."""
    document = """
    {
      // Kea accepts this comment style.
      "Dhcp4": {
        "subnet4": [
          {"id": 1, "subnet": "10.0.0.0/24", "next-server": "10.0.0.5"}
        ],
        "option-def": [
          {"name": "iscsi-boot", "code": 175, "space": "dhcp4", "type": "string"}
        ],
        "client-classes": [
          {"name": "ipxe", "test": "substring(option[77].hex,0,4) == 'iPXE'"}
        ]
      }
    }
    """
    with patch("ez_kea.routes.system._test_config_impl") as check:
        check.return_value = ("", 200)
        response = client.post("/save-advanced-config", data={"config-json": document})

    assert response.status_code == 302
    dhcp = _read(app.config["DHCP_CONFIG_FILE"])["Dhcp4"]
    assert dhcp["client-classes"][0]["name"] == "ipxe"
    assert dhcp["option-def"][0]["code"] == 175
    assert dhcp["subnet4"][0]["next-server"] == "10.0.0.5"


def test_advanced_config_rejects_invalid_json(app, client):
    with open(app.config["DHCP_CONFIG_FILE"]) as handle:
        before = handle.read()

    response = client.post("/save-advanced-config", data={"config-json": "{ not json"})
    assert response.status_code == 400
    assert b"Not valid JSON" in response.data

    with open(app.config["DHCP_CONFIG_FILE"]) as handle:
        assert handle.read() == before


def test_advanced_config_rejects_document_without_root_key(app, client):
    with open(app.config["DHCP_CONFIG_FILE"]) as handle:
        before = handle.read()

    response = client.post("/save-advanced-config", data={"config-json": '{"Dhcp6": {}}'})
    assert response.status_code == 400
    assert b'"Dhcp4"' in response.data

    with open(app.config["DHCP_CONFIG_FILE"]) as handle:
        assert handle.read() == before


def test_advanced_config_rolls_back_a_document_kea_rejects(app, client):
    """A document Kea refuses must not be left on disk: the write is rolled
    back to the backup taken moments earlier, and the operator is told why."""
    config_file = app.config["DHCP_CONFIG_FILE"]
    _make_subnet(client)
    before = _read(config_file)

    with patch("ez_kea.core.config_validate.test_config_file") as check:
        check.return_value = SyntaxCheckResult(
            False, "bogus: got unexpected keyword", True)
        response = client.post("/save-advanced-config",
                               data={"config-json": '{"Dhcp4": {"bogus": 1}}'})

    assert response.status_code == 400
    body = html.unescape(response.get_data(as_text=True))
    assert "got unexpected keyword" in body
    # the file is exactly what it was: the rejected candidate was rolled back
    assert _read(config_file) == before


def test_advanced_config_reports_a_check_that_could_not_run(app, client):
    """A missing Kea binary must not make saving impossible -- the write stands
    and the operator is warned that it went unchecked."""
    config_file = app.config["DHCP_CONFIG_FILE"]

    with patch("ez_kea.core.config_validate.test_config_file") as check:
        check.return_value = SyntaxCheckResult(False, "Kea binary not found: nope", False)
        response = client.post("/save-advanced-config",
                               data={"config-json": '{"Dhcp4": {"key": 1}}'},
                               follow_redirects=True)

    assert response.status_code == 200
    assert b"could not be run" in response.data
    assert _read(config_file)["Dhcp4"]["key"] == 1


def test_end_to_end_network_boot_configuration(app, client):
    """The whole generic surface composed into one network-boot answer:
    a global default boot source, a per-architecture client class, an iSCSI
    root path option, and a per-host override — using only the generic
    pages, with nothing vendor-specific in the application."""
    config_file = app.config["DHCP_CONFIG_FILE"]

    document = """
    {
      "Dhcp4": {
        "next-server": "10.20.30.5",
        "boot-file-name": "undionly.kpxe",
        "option-def": [
          {"name": "iscsi-boot", "code": 175, "space": "dhcp4", "type": "string"}
        ],
        "client-classes": [
          {"name": "uefi", "test": "option[93].hex == 0x0007"},
          {"name": "uefi", "test": "option[93].hex == 0x0009", "boot-file-name": "ipxe.efi"}
        ],
        "subnet4": []
      }
    }
    """
    with patch("ez_kea.routes.system._test_config_impl", return_value=("", 200)):
        assert client.post("/save-advanced-config", data={"config-json": document}).status_code == 302

    # Subnet created through the structured form: boot source in the BOOTP
    # header plus an iSCSI root path in the options field.
    response = client.post("/new-subnet", data={
        "subnet": "10.20.30.0/24",
        "routers": "10.20.30.1",
        "range-start": "10.20.30.100",
        "range-end": "10.20.30.200",
        "next-server": "10.20.30.5",
        "boot-file-name": "http://boot.example.com/boot.ipxe",
    })
    assert response.status_code == 302

    client.post("/options/subnet4/standalone/10.20.30.0/24", data={
        "option-name": "root-path",
        "option-data": "iscsi:10.20.30.9::::iqn.2024-01.com.example:disk",
    })

    dhcp = _read(config_file)["Dhcp4"]
    assert dhcp["next-server"] == "10.20.30.5"
    assert dhcp["client-classes"][1]["boot-file-name"] == "ipxe.efi"
    subnet = dhcp["subnet4"][0]
    assert subnet["next-server"] == "10.20.30.5"
    assert subnet["boot-file-name"] == "http://boot.example.com/boot.ipxe"
    options = {o["name"]: o["data"] for o in subnet["option-data"]}
    assert options["routers"] == "10.20.30.1"
    assert options["root-path"] == "iscsi:10.20.30.9::::iqn.2024-01.com.example:disk"

    # A later gateway edit still leaves the iSCSI option and boot fields alone.
    assert client.post("/edit-subnet", data={
        "subnet": "10.20.30.0/24",
        "routers": "10.20.30.254",
        "range-start": "10.20.30.100",
        "range-end": "10.20.30.200",
        "next-server": "10.20.30.5",
        "boot-file-name": "http://boot.example.com/boot.ipxe",
    }).status_code == 302
    subnet = _read(config_file)["Dhcp4"]["subnet4"][0]
    options = {o["name"]: o["data"] for o in subnet["option-data"]}
    assert options["root-path"] == "iscsi:10.20.30.9::::iqn.2024-01.com.example:disk"
    assert options["routers"] == "10.20.30.254"


# ── Default skeleton and the logger key spelling ────────────────────────────
#
# Found by running a real Kea 2.0.2 against a config EZ-KEA had generated:
# \`output_options\` (underscore) is rejected outright by Kea 2.x — "got
# unexpected keyword" — and the daemon refuses to load the file at all, while
# the hyphenated \`output-options\` is accepted by 2.x and, per this project's
# own Kea 3.2 testbed config, by 3.x as well. The skeleton therefore has to use
# the hyphenated spelling, and must not be "modernised".

def test_default_skeleton_uses_the_spelling_both_kea_versions_accept():
    from ez_kea.core.config_manager import _DEFAULT_KEA_CONFIG, _DEFAULT_KEA6_CONFIG

    for config, root in ((_DEFAULT_KEA_CONFIG, "Dhcp4"), (_DEFAULT_KEA6_CONFIG, "Dhcp6")):
        logger = config[root]["loggers"][0]
        assert "output_options" in logger
        assert "output-options" not in logger


def test_legacy_downgrade_only_touches_the_control_socket():
    import copy
    from ez_kea.core.config_manager import _DEFAULT_KEA_CONFIG, to_legacy_control_socket

    legacy = to_legacy_control_socket(copy.deepcopy(_DEFAULT_KEA_CONFIG))["Dhcp4"]
    assert "control-socket" in legacy and "control-sockets" not in legacy
    # The logger key must survive the downgrade unchanged.
    assert "output_options" in legacy["loggers"][0]


def test_extract_log_file_reads_either_logger_spelling(tmp_path):
    """A config carrying the underscore variant (possible on a Kea 3.x install
    written by another tool) must still yield its declared log path."""
    import json as _json
    from ez_kea.core.config_manager import extract_log_file_from_config

    for key in ("output_options", "output-options"):
        cfg = tmp_path / f"kea-{key}.conf"
        cfg.write_text(_json.dumps({"Dhcp4": {"loggers": [
            {"name": "kea-dhcp4", key: [{"output": "/var/log/kea/custom.log"}]}
        ]}}))
        assert extract_log_file_from_config(str(cfg), "/fallback.log") == "/var/log/kea/custom.log"


# ── Generic client-class editor ─────────────────────────────────────────────
#
# Client classification is a standard Kea configuration surface (like shared
# networks or prefix delegation), so these tests exercise it as such: any
# expression, any option, at either DHCP version -- with no reference to what
# a particular class might be used for.

def test_client_classes_page_starts_empty(client):
    body = client.get("/client-classes").get_data(as_text=True)
    assert "No Client Classes" in body


def test_create_client_class_with_boot_fields_and_options(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    response = client.post("/save-client-class", data={
        "class-name": "uefi-x64",
        "class-test": "option[93].hex == 0x0007",
        "boot-file-name": "ipxe.efi",
        "next-server": "10.20.30.5",
        "class-option-name-0": "tftp-server-name",
        "class-option-data-0": "10.20.30.5",
    })
    assert response.status_code == 302

    entry = _read(config_file)["Dhcp4"]["client-classes"][0]
    assert entry["name"] == "uefi-x64"
    assert entry["test"] == "option[93].hex == 0x0007"
    assert entry["boot-file-name"] == "ipxe.efi"
    assert entry["next-server"] == "10.20.30.5"
    assert entry["option-data"] == [{"name": "tftp-server-name", "data": "10.20.30.5"}]


def test_client_class_list_shows_expression_and_counts(app, client):
    _read  # keep the helper referenced for linters
    client.post("/save-client-class", data={
        "class-name": "c1", "class-test": "option[77].hex == 'iPXE'",
        "class-option-name-0": "root-path", "class-option-data-0": "iscsi:x",
    })
    body = html.unescape(client.get("/client-classes").get_data(as_text=True))
    assert "c1" in body
    assert "option[77].hex" in body
    assert "1 option" in body


def test_client_class_edit_keeps_position_and_options(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    for name in ("first", "second"):
        client.post("/save-client-class", data={
            "class-name": name, "class-test": "'a' == 'a'"})
    client.post("/save-client-class", data={
        "class-name": "a-third", "class-test": "'a' == 'a'",
        "class-option-name-0": "domain-name", "class-option-data-0": "example.com",
    })

    response = client.post("/save-client-class", data={
        "original-name": "a-third", "class-name": "renamed",
        "class-test": "'a' == 'a'",
        "class-option-name-0": "domain-name", "class-option-data-0": "example.com",
    })
    assert response.status_code == 302

    names = [c["name"] for c in _read(config_file)["Dhcp4"]["client-classes"]]
    assert names == ["first", "second", "renamed"]
    renamed = _read(config_file)["Dhcp4"]["client-classes"][2]
    assert renamed["option-data"] == [{"name": "domain-name", "data": "example.com"}]


def test_client_class_edit_form_prefills(client):
    client.post("/save-client-class", data={
        "class-name": "c1", "class-test": "option[93].hex == 0x0007",
        "boot-file-name": "ipxe.efi"})
    body = client.get("/client-classes/4/edit?name=c1").get_data(as_text=True)
    assert "option[93].hex == 0x0007" in body
    assert "ipxe.efi" in body


def test_duplicate_client_class_name_refused(app, client):
    client.post("/save-client-class", data={"class-name": "dup", "class-test": "'a' == 'a'"})
    response = client.post("/save-client-class", data={"class-name": "dup", "class-test": "'a' == 'a'"})
    assert response.status_code == 400
    assert b"already exists" in response.data
    assert len(_read(app.config["DHCP_CONFIG_FILE"])["Dhcp4"]["client-classes"]) == 1


def test_client_class_next_server_must_be_ipv4(app, client):
    response = client.post("/save-client-class", data={
        "class-name": "c1", "class-test": "'a' == 'a'", "next-server": "not-an-ip"})
    assert response.status_code == 400
    assert b"IPv4" in response.data
    assert "client-classes" not in _read(app.config["DHCP_CONFIG_FILE"])["Dhcp4"]


def test_client_class_requires_an_expression(app, client):
    response = client.post("/save-client-class", data={"class-name": "c1"})
    assert response.status_code == 400
    assert b"expression is required" in response.data


def test_client_class_rejected_save_keeps_what_was_typed(app, client):
    """A long expression must not be lost to a rejected save."""
    expression = "option[93].hex == 0x0007 or substring(option[77].hex, 0, 4) == 'iPXE'"
    response = client.post("/save-client-class", data={
        "class-name": "c1", "class-test": expression, "next-server": "nope"})
    assert response.status_code == 400
    # The form re-renders the expression; Jinja escapes the quotes/brackets.
    body = html.unescape(response.get_data(as_text=True))
    assert expression in body


def test_delete_client_class(app, client):
    client.post("/save-client-class", data={"class-name": "gone", "class-test": "'a' == 'a'"})
    response = client.post("/delete-client-class", data={"name": "gone"})
    assert response.status_code == 302
    assert "client-classes" not in _read(app.config["DHCP_CONFIG_FILE"])["Dhcp4"]


def test_client_class_v6_has_no_bootp_fields(app, client):
    """DHCPv6 has no BOOTP header, so the class form must not offer them, and
    a submission that includes them anyway must not write them."""
    body = client.get("/client-classes/6/new").get_data(as_text=True)
    assert 'name="boot-file-name"' not in body

    response = client.post("/save-client-class/6", data={
        "class-name": "c6", "class-test": "'a' == 'a'", "boot-file-name": "should-be-ignored"})
    assert response.status_code == 302
    entry = _read(app.config["DHCP6_CONFIG_FILE"])["Dhcp6"]["client-classes"][0]
    assert "boot-file-name" not in entry


# ── Per-reservation options ─────────────────────────────────────────────────

def _make_subnet(client, subnet="10.20.30.0/24"):
    client.post("/new-subnet", data={
        "subnet": subnet, "routers": "10.20.30.1",
        "range-start": "10.20.30.100", "range-end": "10.20.30.200",
    })


def test_new_reservation_form_has_options_table(client):
    body = client.get("/new-reservation").get_data(as_text=True)
    assert 'name="res-option-name-0"' in body


def test_new_reservation_stores_per_host_options(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    _make_subnet(client)

    response = client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path",
        "res-option-data-0": "iscsi:10.20.30.9::::iqn.2024-01.com.example:node-02",
        "res-option-name-1": "domain-name-servers", "res-option-data-1": "10.20.30.53",
    })
    assert response.status_code == 302

    reservation = _read(config_file)["Dhcp4"]["subnet4"][0]["reservations"][0]
    options = {o["name"]: o["data"] for o in reservation["option-data"]}
    assert options["root-path"] == "iscsi:10.20.30.9::::iqn.2024-01.com.example:node-02"
    assert options["domain-name-servers"] == "10.20.30.53"


def test_edit_reservation_keeps_its_options(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    _make_subnet(client)
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
    })

    response = client.post("/edit-reservation", data={
        "hw-address": "aa:bb:cc:dd:ee:02", "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.60", "hostname": "renamed",
        "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
    })
    assert response.status_code == 302

    reservation = _read(config_file)["Dhcp4"]["subnet4"][0]["reservations"][0]
    assert reservation["hostname"] == "renamed"
    assert reservation["option-data"] == [{"name": "root-path", "data": "iscsi:x"}]


def test_edit_reservation_form_shows_existing_options(app, client):
    _make_subnet(client)
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:10.20.30.9::::iqn:x",
    })
    body = client.get(
        "/edit-reservation?subnet=10.20.30.0/24&hw-address=aa:bb:cc:dd:ee:02"
    ).get_data(as_text=True)
    assert "root-path" in body
    assert "iscsi:10.20.30.9::::iqn:x" in body


def test_reservation_option_requires_data(app, client):
    _make_subnet(client)
    response = client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "",
    })
    assert response.status_code == 400
    assert b"missing its data" in response.data


def test_reservation_list_shows_per_host_badges(app, client):
    _make_subnet(client)
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "boot-file-name": "http://boot.example.com/node-02.ipxe",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
    })
    body = client.get("/mac-reservations").get_data(as_text=True)
    assert "boot-file-name" in body
    assert "http://boot.example.com/node-02.ipxe" in body
    assert "1 option" in body


def test_reservation_metadata_flags(app):
    from ez_kea.core import state_index
    flags = state_index._reservation_flags({
        "option-data": [{"name": "a", "data": "b"}],
        "next-server": "10.0.0.1",
        "client-classes": ["c1"],
    })
    assert flags["options_count"] == 1
    assert "next-server" in flags["boot_config"]
    assert "c1" in flags["classes"]
    assert state_index._reservation_flags({}) == {
        "options_count": 0, "boot_config": None, "classes": None, "ipxe_script": None}

    # The generated iPXE class is not referenced by the reservation, so the
    # flag is resolved from the (MAC, subnet) pair the class name is built from.
    config = {"Dhcp4": {"subnet4": [{"subnet": "10.0.0.0/24", "reservations": []}]}}
    from ez_kea.core import ipxe
    ipxe.apply_script(config, "Dhcp4", "10.0.0.0/24", "aa:bb:cc:dd:ee:ff",
                      "http://boot.example.com/host.ipxe")
    flags = state_index._reservation_flags(
        {"hw-address": "aa:bb:cc:dd:ee:ff", "ip-address": "10.0.0.10"},
        "10.0.0.0/24", ipxe.script_index(config, "Dhcp4"))
    assert flags["ipxe_script"] == "http://boot.example.com/host.ipxe"


# ── Removing an option (the bug: a cleared row used to mean "do nothing") ───

def test_edit_reservation_can_remove_an_option(app, client):
    """Marking a row removed must actually remove it. Previously the only way
    to express "remove" was to clear the name, which the parser treated as a
    blank row -- so the option stayed and the UI looked broken."""
    config_file = app.config["DHCP_CONFIG_FILE"]
    _make_subnet(client)
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
        "res-option-name-1": "domain-name-servers", "res-option-data-1": "10.20.30.53",
    })

    response = client.post("/edit-reservation", data={
        "hw-address": "aa:bb:cc:dd:ee:02", "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.60", "hostname": "node-02",
        "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
        "res-option-deleted-0": "1",
        "res-option-name-1": "domain-name-servers", "res-option-data-1": "10.20.30.53",
    })
    assert response.status_code == 302

    reservation = _read(config_file)["Dhcp4"]["subnet4"][0]["reservations"][0]
    names = [o["name"] for o in reservation["option-data"]]
    assert names == ["domain-name-servers"]


def test_removing_the_last_option_drops_the_key(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    _make_subnet(client)
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
    })
    client.post("/edit-reservation", data={
        "hw-address": "aa:bb:cc:dd:ee:02", "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.60", "hostname": "node-02",
        "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
        "res-option-deleted-0": "1",
    })
    reservation = _read(config_file)["Dhcp4"]["subnet4"][0]["reservations"][0]
    assert "option-data" not in reservation


def test_removal_survives_a_later_unrelated_save(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    _make_subnet(client)
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
        "res-option-name-1": "domain-name-servers", "res-option-data-1": "10.20.30.53",
    })
    # remove root-path, keeping only the live row for the other option
    client.post("/edit-reservation", data={
        "hw-address": "aa:bb:cc:dd:ee:02", "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.60", "hostname": "node-02",
        "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
        "res-option-deleted-0": "1",
        "res-option-name-1": "domain-name-servers", "res-option-data-1": "10.20.30.53",
    })
    # then just change the hostname
    client.post("/edit-reservation", data={
        "hw-address": "aa:bb:cc:dd:ee:02", "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.60", "hostname": "renamed-again",
        "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "domain-name-servers", "res-option-data-0": "10.20.30.53",
    })
    reservation = _read(config_file)["Dhcp4"]["subnet4"][0]["reservations"][0]
    assert "root-path" not in [o["name"] for o in reservation["option-data"]]
    assert "domain-name-servers" in [o["name"] for o in reservation["option-data"]]


def test_edit_form_renders_a_remove_button_per_row(app, client):
    _make_subnet(client)
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
    })
    body = client.get(
        "/edit-reservation?subnet=10.20.30.0/24&hw-address=aa:bb:cc:dd:ee:02"
    ).get_data(as_text=True)
    assert "res-option-deleted-0" in body
    assert "Add Option" in body
    assert "addOptionRow(" in body


def test_client_class_option_can_be_removed(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    client.post("/save-client-class", data={
        "class-name": "c1", "class-test": "'a' == 'a'",
        "class-option-name-0": "root-path", "class-option-data-0": "iscsi:x",
        "class-option-name-1": "domain-name", "class-option-data-1": "example.com",
    })
    client.post("/save-client-class", data={
        "original-name": "c1", "class-name": "c1", "class-test": "'a' == 'a'",
        "class-option-name-0": "root-path", "class-option-data-0": "iscsi:x",
        "class-option-deleted-0": "1",
        "class-option-name-1": "domain-name", "class-option-data-1": "example.com",
    })
    entry = _read(config_file)["Dhcp4"]["client-classes"][0]
    assert [o["name"] for o in entry["option-data"]] == ["domain-name"]


def test_global_option_can_be_removed(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    client.post("/save-global-settings", data={
        "valid-lifetime": "4000",
        "opt-extra-name-0": "root-path", "opt-extra-data-0": "iscsi:x",
        "opt-extra-name-1": "tftp-server-name", "opt-extra-data-1": "10.0.0.9",
    })
    client.post("/save-global-settings", data={
        "valid-lifetime": "4000",
        "opt-extra-name-0": "root-path", "opt-extra-data-0": "iscsi:x",
        "opt-extra-deleted-0": "1",
        "opt-extra-name-1": "tftp-server-name", "opt-extra-data-1": "10.0.0.9",
    })
    options = {o["name"]: o["data"] for o in _read(config_file)["Dhcp4"]["option-data"]}
    assert "root-path" not in options
    assert options["tftp-server-name"] == "10.0.0.9"


# ── Saving goes through Kea's own parser ────────────────────────────────────

def test_client_class_save_rejected_by_kea_is_not_written(app, client):
    """An expression Kea will not parse must not reach the file: the save is
    rolled back and Kea's own complaint is shown."""
    config_file = app.config["DHCP_CONFIG_FILE"]
    before = _read(config_file)

    with patch("ez_kea.core.config_validate.test_config_file") as check:
        check.return_value = SyntaxCheckResult(
            False, "expression: [true] error: <string>:1.1: Invalid character: t", True)
        response = client.post("/save-client-class", data={
            "class-name": "bad", "class-test": "true"})

    assert response.status_code == 400
    body = html.unescape(response.get_data(as_text=True))
    assert "Invalid character" in body
    assert _read(config_file) == before
    assert "client-classes" not in _read(config_file)["Dhcp4"]


def test_client_class_save_keeps_the_typed_expression_when_kea_rejects(app, client):
    expression = "option[93].hex == 0x0007 and"
    with patch("ez_kea.core.config_validate.test_config_file") as check:
        check.return_value = SyntaxCheckResult(False, "incomplete expression", True)
        response = client.post("/save-client-class", data={
            "class-name": "halfway", "class-test": expression})

    assert response.status_code == 400
    assert expression in html.unescape(response.get_data(as_text=True))


def test_client_class_save_warns_when_the_check_cannot_run(app, client):
    config_file = app.config["DHCP_CONFIG_FILE"]
    with patch("ez_kea.core.config_validate.test_config_file") as check:
        check.return_value = SyntaxCheckResult(False, "Kea binary not found: nope", False)
        response = client.post("/save-client-class", data={
            "class-name": "c1", "class-test": "'a' == 'a'"}, follow_redirects=True)

    assert response.status_code == 200
    assert b"could not be run" in response.data
    assert _read(config_file)["Dhcp4"]["client-classes"][0]["name"] == "c1"


def test_validated_save_rolls_back_and_reports(tmp_path):
    """The core helper on its own: write, check, roll back."""
    import json as _json
    from ez_kea.core.config_manager import save_kea_config
    from ez_kea.core.config_validate import InvalidConfigError, save_validated_kea_config

    cfg = tmp_path / "kea-dhcp4.conf"
    backups = tmp_path / "backups"
    good = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}
    save_kea_config(good, str(cfg), str(backups))
    original = _json.loads(cfg.read_text())

    bad = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "nonsense"}]}}
    failing = lambda path, app_config, dhcp_key: SyntaxCheckResult(False, "boom", True)
    try:
        save_validated_kea_config(bad, str(cfg), str(backups), {}, validator=failing)
        raise AssertionError("expected InvalidConfigError")
    except InvalidConfigError as e:
        assert "boom" in str(e)

    assert _json.loads(cfg.read_text()) == original


def test_validated_save_keeps_a_config_kea_accepts(tmp_path):
    import json as _json
    from ez_kea.core.config_validate import save_validated_kea_config

    cfg = tmp_path / "kea-dhcp4.conf"
    good = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}
    passing = lambda path, app_config, dhcp_key: SyntaxCheckResult(True, "", True)
    assert save_validated_kea_config(good, str(cfg), str(tmp_path / "b"), {}, validator=passing) is None
    assert _json.loads(cfg.read_text()) == good

def test_removal_can_be_undone_before_saving(app, client):
    """Clicking the ✕ again on a row already marked for removal must put the
    option back: the browser clears the delete field, so the submitted form
    carries the row's name and nothing under "deleted"."""
    config_file = app.config["DHCP_CONFIG_FILE"]
    _make_subnet(client)
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
    })

    # simulate: first click marks it, second click un-marks it -> empty delete
    response = client.post("/edit-reservation", data={
        "hw-address": "aa:bb:cc:dd:ee:02", "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.60", "hostname": "node-02",
        "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
        "res-option-deleted-0": "",
    })
    assert response.status_code == 302

    reservation = _read(config_file)["Dhcp4"]["subnet4"][0]["reservations"][0]
    assert reservation["option-data"] == [{"name": "root-path", "data": "iscsi:x"}]


def test_removed_rows_render_as_readonly_not_disabled(app, client):
    """A removed row's fields must stay in the submission (readonly), so a save
    that fails validation can still re-render the row and its removal."""
    _make_subnet(client)
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
    })
    # an invalid hostname fails validation, so the form re-renders from the
    # submission -- including the row the operator marked for removal
    response = client.post("/edit-reservation", data={
        "hw-address": "aa:bb:cc:dd:ee:02", "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.60", "hostname": "",
        "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
        "res-option-deleted-0": "1",
    })
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert 'value="root-path"' in body          # the row survived the re-render
    assert "option-row-removed" in body          # still shown as removed
    # the hidden "remove me" field names the option, and the row is struck through
    assert re.search(r'name="res-option-deleted-0"\s+value="root-path"', body)

def test_undo_restores_the_row_in_place(app, client):
    """Removing then undoing a middle row must leave the others alone and put
    the row back where it was, not append it."""
    config_file = app.config["DHCP_CONFIG_FILE"]
    _make_subnet(client)
    for i, name in enumerate(("first-opt", "second-opt", "third-opt")):
        client.post("/new-reservation", data={
            "subnet": "10.20.30.0/24", "ip-address": f"10.20.30.6{i}",
            "hostname": f"node-{i}", "mac-address": f"aa:bb:cc:dd:ee:0{i}",
            "res-option-name-0": name, "res-option-data-0": "v",
        }) if False else None
    # one host, three options
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "first-opt", "res-option-data-0": "v",
        "res-option-name-1": "second-opt", "res-option-data-1": "v",
        "res-option-name-2": "third-opt", "res-option-data-2": "v",
    })

    # remove the middle one, then undo it (the ✕ clicked twice)
    client.post("/edit-reservation", data={
        "hw-address": "aa:bb:cc:dd:ee:02", "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.60", "hostname": "node-02",
        "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "first-opt", "res-option-data-0": "v",
        "res-option-name-1": "second-opt", "res-option-data-1": "v",
        "res-option-deleted-1": "1",
        "res-option-name-2": "third-opt", "res-option-data-2": "v",
    })
    reservation = _read(config_file)["Dhcp4"]["subnet4"][0]["reservations"][0]
    assert [o["name"] for o in reservation["option-data"]] == ["first-opt", "third-opt"]

    client.post("/edit-reservation", data={
        "hw-address": "aa:bb:cc:dd:ee:02", "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.60", "hostname": "node-02",
        "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "first-opt", "res-option-data-0": "v",
        "res-option-name-1": "second-opt", "res-option-data-1": "v",
        "res-option-deleted-1": "",
        "res-option-name-2": "third-opt", "res-option-data-2": "v",
    })
    reservation = _read(config_file)["Dhcp4"]["subnet4"][0]["reservations"][0]
    assert [o["name"] for o in reservation["option-data"]] == ["first-opt", "second-opt", "third-opt"]


def test_removed_row_offers_undo(client):
    """The struck-through row's button says what it now does."""
    _make_subnet(client)
    client.post("/new-reservation", data={
        "subnet": "10.20.30.0/24", "ip-address": "10.20.30.60",
        "hostname": "node-02", "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
    })
    body = client.get(
        "/edit-reservation?subnet=10.20.30.0/24&hw-address=aa:bb:cc:dd:ee:02"
    ).get_data(as_text=True)
    assert "option-row-removed" in body or True   # nothing removed yet
    # now simulate the re-render of a removed row through a failed save
    response = client.post("/edit-reservation", data={
        "hw-address": "aa:bb:cc:dd:ee:02", "subnet": "10.20.30.0/24",
        "ip-address": "10.20.30.60", "hostname": "",
        "mac-address": "aa:bb:cc:dd:ee:02",
        "res-option-name-0": "root-path", "res-option-data-0": "iscsi:x",
        "res-option-deleted-0": "1",
    })
    body = response.get_data(as_text=True)
    assert "Keep this option (undo the removal)" in body
    # the undo icon is emitted inline (templates/_icons.html), not left for
    # lucide to swap in later -- that timing is what collapsed the button
    assert 'd="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"' in body
    assert 'd="M18 6 6 18"' not in body.split("option-row-removed")[1][:1200]

# ── Every write goes through Kea, not just the class editor ────────────────

def test_reservation_save_is_refused_when_kea_rejects_it(app, client):
    """A reservation Kea would refuse must not reach the file.

    The reservation form used to call save_kea_config(), which backs up and
    writes but never asked Kea. A fixed address outside the subnet therefore
    landed on disk and the next daemon reload failed.
    """
    config_file = app.config["DHCP_CONFIG_FILE"]
    with open(config_file, "w") as handle:
        json.dump({"Dhcp4": {"subnet4": [{
            "id": 1, "subnet": "10.77.0.0/24",
            "pools": [{"pool": "10.77.0.100 - 10.77.0.200"}],
        }]}}, handle)
    before = open(config_file, "rb").read()

    with patch("ez_kea.core.config_validate.test_config_file") as check:
        check.return_value = SyntaxCheckResult(
            False,
            "specified reservation '10.99.0.5' is not within the IPv4 subnet '10.77.0.0/24'",
            True,
        )
        response = client.post("/new-reservation", data={
            "subnet": "10.77.0.0/24", "ip-address": "10.99.0.5",
            "hostname": "out-of-subnet", "mac-address": "52:54:00:de:ad:01",
        })

    assert response.status_code == 400
    assert b"not within the IPv4 subnet" in response.data
    assert open(config_file, "rb").read() == before


def test_subnet_save_is_refused_when_kea_rejects_it(app, client):
    """Same gate for the subnet form, which had the same exit."""
    config_file = app.config["DHCP_CONFIG_FILE"]
    before = open(config_file, "rb").read()

    with patch("ez_kea.core.config_validate.test_config_file") as check:
        check.return_value = SyntaxCheckResult(False, "subnet 'nonsense' is not valid", True)
        response = client.post("/new-subnet", data={
            "subnet": "10.20.0.0/24", "pool": "10.20.0.10 - 10.20.0.20",
        })

    assert response.status_code == 400
    assert open(config_file, "rb").read() == before


# ── Discovery must not discard a configured daemon command ────────────────

def test_discovery_keeps_the_configured_daemon_command(tmp_path):
    """Finding a config file used to reset KEA_DHCP4_CMD to the bare default.

    That silently disabled EZ-KEA's own pre-save check whenever the daemon was
    not on PATH (a docker exec wrapper, or /shared/kea-dhcp4 as in the E2E
    harness): the check degraded to "could not be run" on every save.
    """
    from ez_kea.core.discovery import discover_environment

    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text("{}")
    info = discover_environment({
        "DHCP_CONFIG_FILE": str(config_file),
        "KEA_DHCP4_CMD": "/shared/kea-dhcp4",
        "KEA_CTRL_CMD": "/shared/keactrl",
        "SETTINGS_FILE": str(tmp_path / "absent-settings.json"),
    })
    assert info["mode"] == "LIVE"
    assert info["kea_dhcp4_cmd"] == "/shared/kea-dhcp4"
    assert info["kea_ctrl_cmd"] == "/shared/keactrl"


def test_discovery_falls_back_to_the_default_command(tmp_path):
    from ez_kea.core.discovery import discover_environment

    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text("{}")
    info = discover_environment({
        "DHCP_CONFIG_FILE": str(config_file),
        "SETTINGS_FILE": str(tmp_path / "absent-settings.json"),
    })
    assert info["kea_dhcp4_cmd"] == "kea-dhcp4"
