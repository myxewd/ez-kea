# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
tests/test_reservation_ipxe.py

The per-reservation iPXE quick configuration (core/ipxe.py and the reservation
routes): one checkbox and one script address, from which EZ-KEA maintains a
client-class bound to that reservation.

The class has to be two things at once, and the tests pin both:

  * specific to this host *and* to a client that is already running iPXE,
    because the firmware and iPXE share a MAC and only iPXE can run a script;
  * ahead of every other class, because Kea uses the first matching class's
    boot fields -- which is what makes it outrank a general iPXE class.

They also pin the lifecycle (edited, unchecked, MAC changed, reservation or
subnet deleted) and the rule that the class cannot be edited or deleted from
the client-class pages while its reservation exists.
"""
import json

import pytest
from conftest import login
from ez_kea import create_app
from ez_kea.core import ipxe, state_index

MAC = "52:54:00:12:34:56"
SUBNET = "10.77.0.0/24"
IP = "10.77.0.150"
SCRIPT = "http://10.77.0.12:8080/hosts/52-54-00-12-34-56.ipxe"
CLASS_NAME = "ez-kea-ipxe-525400123456-10-77-0-0-24"


def _config():
    """A config with one usable subnet and a general iPXE class, the class a
    per-host script has to outrank."""
    return {
        "Dhcp4": {
            "shared-networks": [],
            "client-classes": [
                {
                    "name": "ipxe",
                    "test": "substring(option[77].hex, 0, 4) == 'iPXE'",
                    "boot-file-name": "boot.ipxe",
                }
            ],
            "subnet4": [
                {
                    "id": 1,
                    "subnet": SUBNET,
                    "pools": [{"pool": "10.77.0.100 - 10.77.0.200"}],
                    "reservations": [],
                }
            ],
        }
    }


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
    config_file.write_text(json.dumps(_config()))
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    app.config["BACKUP_DIR"] = str(tmp_path / "backups")
    app.config["DHCP_LEASES_FILE"] = str(tmp_path / "leases.csv")
    app.config["DHCP_LOG_FILE"] = str(tmp_path / "kea.log")
    app.config["STATE_INDEX_DB"] = str(tmp_path / "stateindex.db")
    app.config["DHCP6_CONFIG_FILE"] = str(tmp_path / "kea-dhcp6.conf")
    yield app


@pytest.fixture
def client(app):
    return login(app.test_client(), app)


def _read(app):
    with open(app.config["DHCP_CONFIG_FILE"]) as handle:
        return json.load(handle)


def _write(app, config):
    with open(app.config["DHCP_CONFIG_FILE"], "w") as handle:
        json.dump(config, handle)


def _reservation(config):
    return config["Dhcp4"]["subnet4"][0]["reservations"][0]


def _classes(config):
    return config["Dhcp4"].get("client-classes", [])


def _names(config):
    return [entry.get("name") for entry in _classes(config)]


def _add_reservation(app, *, mac=MAC, ip=IP, hostname="san-node-1"):
    config = _read(app)
    config["Dhcp4"]["subnet4"][0]["reservations"].append({
        "hostname": hostname, "hw-address": mac, "ip-address": ip,
    })
    _write(app, config)
    return config


def _form(**overrides):
    data = {
        "subnet": SUBNET, "ip-address": IP, "mac-address": MAC,
        "hostname": "san-node-1",
    }
    data.update(overrides)
    return data


# ── The generated class itself ──────────────────────────────────────────────

def test_managed_class_name_is_deterministic_and_parsable():
    assert ipxe.managed_class_name(MAC, SUBNET) == CLASS_NAME
    assert ipxe.managed_owner(CLASS_NAME) == {"mac": MAC, "slug": "10-77-0-0-24"}
    assert ipxe.is_managed_name(CLASS_NAME)
    assert not ipxe.is_managed_name("ipxe")


def test_managed_class_name_normalizes_mac_spelling():
    """A hyphenated or upper-case MAC must not produce a second class for the
    same host (and hexstring(pkt4.mac, ':') always reports lower-case colons)."""
    assert ipxe.managed_class_name("52-54-00-12-34-56", SUBNET) == CLASS_NAME
    assert ipxe.managed_class_name("52:54:00:12:34:56".upper(), SUBNET) == CLASS_NAME
    assert ipxe.mac_colon("52-54-00-12-34-56") == MAC
    assert ipxe.managed_class_name("not-a-mac", SUBNET) == ""


def test_test_expression_requires_both_this_mac_and_ipxe():
    expression = ipxe.test_expression(MAC)
    assert "hexstring(pkt4.mac, ':') == '" + MAC + "'" in expression
    assert "substring(option[77].hex, 0, 4) == 'iPXE'" in expression
    assert " and " in expression


def test_script_validation_accepts_urls_and_paths():
    for value in (SCRIPT, "boot.ipxe", "hosts/52-54-00-12-34-56.ipxe",
                  "tftp://10.77.0.12/san-node-1.ipxe",
                  "http://boot.example.com/x.ipxe?mac=525400123456"):
        assert ipxe.validate_script(value) == [], value


def test_script_validation_rejects_blank_and_unfetchable_values():
    assert ipxe.validate_script("")
    for value in ("a b.ipxe", "http://x/a<b.ipxe", "http://x/a\"b.ipxe",
                  "x" * (ipxe.MAX_SCRIPT_LENGTH + 1)):
        assert ipxe.validate_script(value), value


def test_selection_errors_ignore_the_field_when_unchecked():
    class _Form(dict):
        pass

    assert ipxe.selection_errors(_Form({"ipxe-script": ""})) == []
    assert ipxe.selection_errors(_Form({"boot-with-ipxe": "on"}))
    assert ipxe.selection_errors(_Form({"boot-with-ipxe": "on", "ipxe-script": SCRIPT})) == []


# ── The class in the config ─────────────────────────────────────────────────

def test_apply_script_puts_the_class_first_and_preserves_other_classes():
    config = _config()
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)

    names = _names(config)
    assert names == [CLASS_NAME, "ipxe"]
    generated = _classes(config)[0]
    assert generated["boot-file-name"] == SCRIPT
    assert "pkt4.mac" in generated["test"]

    # Updating rewrites in place rather than appending a duplicate, and it is
    # still the first class -- that position is what outranks the general one.
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, "http://x/node-1.ipxe")
    assert _names(config) == [CLASS_NAME, "ipxe"]
    assert _classes(config)[0]["boot-file-name"] == "http://x/node-1.ipxe"


def test_apply_script_strips_a_host_assignment_of_the_generated_class():
    """Assigned through a reservation, a class is a member whatever its test
    says, so the firmware would be handed the iPXE script. The write path
    removes that assignment."""
    config = _config()
    config["Dhcp4"]["subnet4"][0]["reservations"].append({
        "hostname": "san-node-1", "hw-address": MAC, "ip-address": IP,
        "client-classes": [CLASS_NAME, "uefi-x64"],
    })
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    assert _reservation(config)["client-classes"] == ["uefi-x64"]


def test_remove_script_drops_the_class_and_any_assignment():
    config = _config()
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    config["Dhcp4"]["subnet4"][0]["reservations"].append({
        "hostname": "san-node-1", "hw-address": MAC, "ip-address": IP,
        "client-classes": [CLASS_NAME],
    })

    assert ipxe.remove_script(config, "Dhcp4", SUBNET, MAC) is True
    assert _names(config) == ["ipxe"]
    assert "client-classes" not in _reservation(config)
    assert ipxe.remove_script(config, "Dhcp4", SUBNET, MAC) is False


def test_script_for_returns_the_reservations_script():
    config = _config()
    assert ipxe.script_for(config, "Dhcp4", SUBNET, MAC) == ""
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    assert ipxe.script_for(config, "Dhcp4", SUBNET, MAC) == SCRIPT


def test_parse_reservation4_reports_the_ipxe_script():
    """The reservations list/export get the script through the state index,
    which has to associate the host with a class it never references."""
    config = _config()
    config["Dhcp4"]["subnet4"][0]["reservations"].append({
        "hostname": "san-node-1", "hw-address": MAC, "ip-address": IP,
    })
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)

    rows = state_index.parse_reservation4(config)
    assert len(rows) == 1
    assert rows[0]["ipxe_script"] == SCRIPT

    # A host without a generated class reports none, not somebody else's.
    no_ipxe = _config()
    no_ipxe["Dhcp4"]["subnet4"][0]["reservations"].append({
        "hostname": "san-node-1", "hw-address": MAC, "ip-address": IP,
    })
    rows = state_index.parse_reservation4(no_ipxe)
    assert rows[0]["ipxe_script"] is None


def test_managed_class_works_for_a_shared_network_reservation():
    """The reservation walk covers subnets nested in shared networks too, for
    both the class and the state index's script association."""
    config = {
        "Dhcp4": {
            "shared-networks": [{
                "name": "lab",
                "subnet4": [{
                    "id": 1,
                    "subnet": SUBNET,
                    "reservations": [{"hw-address": MAC, "ip-address": IP}],
                }],
            }],
        }
    }
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)

    owners = ipxe.owners(config, "Dhcp4")
    assert owners[CLASS_NAME]["shared-network-name"] == "lab"
    assert owners[CLASS_NAME]["subnet"] == SUBNET
    assert state_index.parse_reservation4(config)[0]["ipxe_script"] == SCRIPT


# ── Creating a reservation ──────────────────────────────────────────────────

def test_new_reservation_with_ipxe_creates_the_class(app, client):
    response = client.post("/new-reservation", data=_form(
        **{"boot-with-ipxe": "on", "ipxe-script": SCRIPT,
           "boot-file-name": "old-firmware.kpxe"}))
    assert response.status_code == 302

    config = _read(app)
    assert _names(config) == [CLASS_NAME, "ipxe"]
    generated = _classes(config)[0]
    assert generated["boot-file-name"] == SCRIPT
    assert "hexstring(pkt4.mac, ':') == '" + MAC + "'" in generated["test"]

    reservation = _reservation(config)
    assert reservation["hw-address"] == MAC
    # The class supplies boot-file-name; a reservation-level value would be a
    # second source for the same BOOTP field.
    assert "boot-file-name" not in reservation
    assert "client-classes" not in reservation


def test_new_reservation_without_ipxe_leaves_classes_alone(app, client):
    response = client.post("/new-reservation", data=_form(**{
        "boot-file-name": "undionly.kpxe",
        "ipxe-script": "stale-but-ignored.ipxe",
    }))
    assert response.status_code == 302

    config = _read(app)
    assert _names(config) == ["ipxe"]
    assert _reservation(config)["boot-file-name"] == "undionly.kpxe"


def test_new_reservation_ipxe_without_a_script_is_rejected(app, client):
    response = client.post("/new-reservation", data=_form(
        **{"boot-with-ipxe": "on", "ipxe-script": "  "}))
    assert response.status_code == 400
    assert b"iPXE script address is required" in response.data

    config = _read(app)
    assert _names(config) == ["ipxe"]
    assert config["Dhcp4"]["subnet4"][0]["reservations"] == []


def test_new_reservation_ipxe_with_an_unfetchable_script_is_rejected(app, client):
    response = client.post("/new-reservation", data=_form(
        **{"boot-with-ipxe": "on", "ipxe-script": "http://boot/a b.ipxe"}))
    assert response.status_code == 400
    assert b"characters that are not allowed" in response.data
    assert _names(_read(app)) == ["ipxe"]


def test_new_reservation_form_offers_the_checkbox(client):
    body = client.get("/new-reservation").get_data(as_text=True)
    assert 'name="boot-with-ipxe"' in body
    assert 'name="ipxe-script"' in body


# ── Editing a reservation ───────────────────────────────────────────────────

def _edit_form(**overrides):
    data = {
        "hw-address": MAC, "subnet": SUBNET, "ip-address": IP,
        "hostname": "san-node-1", "mac-address": MAC,
    }
    data.update(overrides)
    return data


def test_edit_reservation_prefills_the_ipxe_fields(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    body = client.get(
        "/edit-reservation?subnet=" + SUBNET + "&hw-address=" + MAC
    ).get_data(as_text=True)
    assert "checked" in body
    assert SCRIPT in body
    # The plain boot-file-name field is disabled while the script is in charge.
    assert "disabled" in body


def test_edit_reservation_updates_the_generated_class(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    new_script = "http://10.77.0.12:8080/hosts/san-node-1.ipxe"
    response = client.post("/edit-reservation", data=_edit_form(
        **{"boot-with-ipxe": "on", "ipxe-script": new_script}))
    assert response.status_code == 302

    config = _read(app)
    assert _names(config) == [CLASS_NAME, "ipxe"]
    assert _classes(config)[0]["boot-file-name"] == new_script
    assert "boot-file-name" not in _reservation(config)


def test_edit_reservation_unchecking_removes_the_class(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    response = client.post("/edit-reservation", data=_edit_form())
    assert response.status_code == 302

    config = _read(app)
    assert _names(config) == ["ipxe"]
    assert _reservation(config)["hostname"] == "san-node-1"


def test_edit_reservation_mac_change_moves_the_class(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    new_mac = "52:54:00:aa:bb:cc"
    response = client.post("/edit-reservation", data=_edit_form(
        **{"mac-address": new_mac, "ip-address": "10.77.0.151",
           "boot-with-ipxe": "on", "ipxe-script": SCRIPT}))
    assert response.status_code == 302

    config = _read(app)
    new_name = ipxe.managed_class_name(new_mac, SUBNET)
    assert _names(config) == [new_name, "ipxe"]
    assert CLASS_NAME not in _names(config)
    assert ipxe.script_for(config, "Dhcp4", SUBNET, new_mac) == SCRIPT


def test_edit_reservation_respelling_the_mac_keeps_the_class(app, client):
    """A hyphenated or upper-case re-entry is the same host, so the class must
    survive rather than being removed as "the old MAC"."""
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    response = client.post("/edit-reservation", data=_edit_form(**{
        "mac-address": "52-54-00-12-34-56",
        "boot-with-ipxe": "on", "ipxe-script": SCRIPT}))
    assert response.status_code == 302

    config = _read(app)
    assert _names(config) == [CLASS_NAME, "ipxe"]
    assert _reservation(config)["hw-address"] == "52-54-00-12-34-56"


def test_edit_reservation_rejects_a_missing_script_without_losing_state(app, client):
    _add_reservation(app)
    response = client.post("/edit-reservation", data=_edit_form(
        **{"boot-with-ipxe": "on", "ipxe-script": ""}))
    assert response.status_code == 400
    assert b"iPXE script address is required" in response.data
    # The re-rendered form still has the box ticked and nothing was written.
    assert _names(_read(app)) == ["ipxe"]


# ── Deleting what owns the class ────────────────────────────────────────────

def test_delete_reservation_removes_the_generated_class(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    response = client.post("/delete-reservation", data={
        "subnet": SUBNET, "hw-address": MAC})
    assert response.status_code == 302

    config = _read(app)
    assert _names(config) == ["ipxe"]
    assert config["Dhcp4"]["subnet4"][0]["reservations"] == []


def test_delete_subnet_removes_the_generated_classes_it_held(app, client):
    config = _config()
    config["Dhcp4"]["subnet4"][0]["reservations"].append({
        "hostname": "san-node-1", "hw-address": MAC, "ip-address": IP})
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    response = client.post("/delete-subnet", data={"subnet": SUBNET})
    assert response.status_code == 302

    config = _read(app)
    assert _names(config) == ["ipxe"]
    assert config["Dhcp4"]["subnet4"] == []


# ── The generated class is not an ordinary class ────────────────────────────

def test_managed_class_is_read_only_on_the_class_list(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    body = client.get("/client-classes").get_data(as_text=True)
    assert "Managed" in body
    assert "edit-reservation" in body            # link to its owner
    assert "/client-classes/4/edit?name=" + CLASS_NAME not in body
    assert "<td style=\"color:var(--text-secondary);font-variant-numeric:tabular-nums;\">1</td>" in body


def test_editing_a_managed_class_redirects_to_its_reservation(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    response = client.get("/client-classes/4/edit?name=" + CLASS_NAME)
    assert response.status_code == 302
    assert "/edit-reservation" in response.headers["Location"]


def test_saving_a_managed_class_is_refused(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    response = client.post("/save-client-class", data={
        "original-name": CLASS_NAME, "class-name": CLASS_NAME,
        "class-test": "true", "boot-file-name": "hijacked.ipxe"})
    assert response.status_code == 400
    assert b"cannot be edited" in response.data
    # Nothing was written.
    assert _classes(_read(app))[0]["boot-file-name"] == SCRIPT


def test_deleting_a_managed_class_is_refused(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    response = client.post("/delete-client-class", data={"name": CLASS_NAME})
    assert response.status_code == 302
    assert CLASS_NAME in _names(_read(app))


def test_orphaned_managed_class_can_be_deleted(app, client):
    """No reservation owns it any more, so removing the leftover is cleanup
    rather than overriding the binding."""
    config = _config()
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    body = client.get("/client-classes").get_data(as_text=True)
    assert "Orphaned" in body

    response = client.post("/delete-client-class", data={"name": CLASS_NAME})
    assert response.status_code == 302
    assert _names(_read(app)) == ["ipxe"]


def test_the_reserved_namespace_cannot_be_created_from_the_class_form(app, client):
    response = client.post("/save-client-class", data={
        "class-name": "ez-kea-ipxe-001122334455-10-0-0-0-24",
        "class-test": "true", "boot-file-name": "x.ipxe"})
    assert response.status_code == 400
    assert b"reserved" in response.data
    assert _names(_read(app)) == ["ipxe"]


# ── The reservations list and export ────────────────────────────────────────

def test_reservations_list_shows_the_ipxe_script(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    body = client.get("/mac-reservations").get_data(as_text=True)
    assert "iPXE" in body
    assert SCRIPT in body


def test_reservations_csv_includes_the_ipxe_script(app, client):
    _add_reservation(app)
    config = _read(app)
    ipxe.apply_script(config, "Dhcp4", SUBNET, MAC, SCRIPT)
    _write(app, config)

    response = client.get("/mac-reservations/export.csv")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "ipxe_script" in body.splitlines()[0]
    assert SCRIPT in body
