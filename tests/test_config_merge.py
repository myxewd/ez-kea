# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
Three-way merge of Kea configuration documents: replay EZ-KEA's own edits onto
a configuration the daemon changed underneath the mirror, and refuse to guess
when both sides changed the same setting.

These are pure-data tests; the end-to-end Apply path is covered in
test_instances.py (see "remote apply").
"""
import copy

from ez_kea.core.config_merge import merge_documents


def _config(**dhcp4):
    return {"Dhcp4": dict(dhcp4)}


def test_an_edit_only_ez_kea_made_is_applied():
    base = _config(**{"valid-lifetime": 4000, "authoritative": False})
    local = _config(**{"valid-lifetime": 6000, "authoritative": False})
    merged, conflicts = merge_documents(base, local, base)

    assert conflicts == []
    assert merged["Dhcp4"]["valid-lifetime"] == 6000
    # Untouched keys keep the daemon's value.
    assert merged["Dhcp4"]["authoritative"] is False


def test_a_change_only_the_daemon_made_is_kept():
    base = _config(**{"valid-lifetime": 4000, "hooks-libraries": []})
    remote = _config(**{"valid-lifetime": 4000,
                        "hooks-libraries": [{"library": "/usr/lib/kea/libdhcp_lease_cmds.so"}]})
    merged, conflicts = merge_documents(base, base, remote)

    assert conflicts == []
    # The regression this module exists for: a stale mirror must not be able to
    # unload a hook the operator enabled after the pull.
    assert merged["Dhcp4"]["hooks-libraries"] == remote["Dhcp4"]["hooks-libraries"]


def test_identical_changes_on_both_sides_are_not_a_conflict():
    base = _config(**{"valid-lifetime": 4000})
    changed = _config(**{"valid-lifetime": 6000})
    merged, conflicts = merge_documents(base, changed, changed)

    assert conflicts == []
    assert merged["Dhcp4"]["valid-lifetime"] == 6000


def test_different_changes_to_the_same_setting_are_a_conflict():
    base = _config(**{"valid-lifetime": 4000})
    local = _config(**{"valid-lifetime": 6000})
    remote = _config(**{"valid-lifetime": 8000})
    merged, conflicts = merge_documents(base, local, remote)

    assert conflicts == ["Dhcp4.valid-lifetime"]
    # The local value is what a caller would have pushed; it must not be
    # mistaken for an agreement.
    assert merged["Dhcp4"]["valid-lifetime"] == 6000


def test_a_key_added_locally_is_kept():
    base = _config(**{"valid-lifetime": 4000})
    local = _config(**{"valid-lifetime": 4000, "server-tag": "edge-1"})
    merged, conflicts = merge_documents(base, local, base)

    assert conflicts == []
    assert merged["Dhcp4"]["server-tag"] == "edge-1"


def test_a_key_added_on_the_daemon_is_kept():
    base = _config(**{"valid-lifetime": 4000})
    remote = _config(**{"valid-lifetime": 4000, "server-tag": "edge-1"})
    merged, conflicts = merge_documents(base, base, remote)

    assert conflicts == []
    assert merged["Dhcp4"]["server-tag"] == "edge-1"


def test_a_key_deleted_locally_is_deleted():
    base = _config(**{"valid-lifetime": 4000, "echo-client-id": True})
    local = _config(**{"valid-lifetime": 4000})
    merged, conflicts = merge_documents(base, local, base)

    assert conflicts == []
    assert "echo-client-id" not in merged["Dhcp4"]


def test_delete_locally_versus_edit_remotely_is_a_conflict():
    base = {"Dhcp4": {"option-data": [{"name": "domain-name", "data": "old.example"}]}}
    local = {"Dhcp4": {}}
    remote = {"Dhcp4": {"option-data": [{"name": "domain-name", "data": "new.example"}]}}
    _, conflicts = merge_documents(base, local, remote)

    assert conflicts == ["Dhcp4.option-data"]


def test_edit_locally_versus_delete_remotely_is_a_conflict():
    base = {"Dhcp4": {"option-data": [{"name": "domain-name", "data": "old.example"}]}}
    local = {"Dhcp4": {"option-data": [{"name": "domain-name", "data": "local.example"}]}}
    remote = {"Dhcp4": {}}
    _, conflicts = merge_documents(base, local, remote)

    assert conflicts == ["Dhcp4.option-data"]


def test_a_local_subnet_edit_and_a_remote_hook_both_survive():
    """The exact incident: a subnet edit must not revert hooks-libraries."""
    base = {"Dhcp4": {
        "hooks-libraries": [],
        "subnet4": [{"id": 1, "subnet": "192.0.2.0/24", "pools": []}],
    }}
    local = copy.deepcopy(base)
    local["Dhcp4"]["subnet4"][0]["pools"] = [{"pool": "192.0.2.10 - 192.0.2.20"}]
    remote = copy.deepcopy(base)
    remote["Dhcp4"]["hooks-libraries"] = [{"library": "/usr/lib/kea/libdhcp_lease_cmds.so"}]
    remote["Dhcp4"]["subnet4"].append({"id": 2, "subnet": "198.51.100.0/24", "pools": []})

    merged, conflicts = merge_documents(base, local, remote)

    assert conflicts == []
    assert merged["Dhcp4"]["hooks-libraries"] == remote["Dhcp4"]["hooks-libraries"]
    assert merged["Dhcp4"]["subnet4"][0]["pools"] == [{"pool": "192.0.2.10 - 192.0.2.20"}]
    assert [subnet["id"] for subnet in merged["Dhcp4"]["subnet4"]] == [1, 2]


def test_list_elements_are_merged_by_their_identifier():
    base = {"Dhcp4": {"subnet4": [
        {"id": 1, "subnet": "192.0.2.0/24", "pools": []},
        {"id": 2, "subnet": "198.51.100.0/24", "pools": []},
    ]}}
    local = copy.deepcopy(base)
    local["Dhcp4"]["subnet4"][1]["pools"] = [{"pool": "198.51.100.10 - 198.51.100.20"}]
    remote = copy.deepcopy(base)
    remote["Dhcp4"]["subnet4"][0]["pools"] = [{"pool": "192.0.2.10 - 192.0.2.20"}]

    merged, conflicts = merge_documents(base, local, remote)

    assert conflicts == []
    by_id = {subnet["id"]: subnet for subnet in merged["Dhcp4"]["subnet4"]}
    assert by_id[1]["pools"] == [{"pool": "192.0.2.10 - 192.0.2.20"}]
    assert by_id[2]["pools"] == [{"pool": "198.51.100.10 - 198.51.100.20"}]


def test_the_daemons_list_order_is_preserved_and_local_additions_follow():
    base = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "192.0.2.0/24"}]}}
    local = {"Dhcp4": {"subnet4": [
        {"id": 1, "subnet": "192.0.2.0/24"},
        {"id": 3, "subnet": "203.0.113.0/24"},
    ]}}
    remote = {"Dhcp4": {"subnet4": [
        {"id": 2, "subnet": "198.51.100.0/24"},
        {"id": 1, "subnet": "192.0.2.0/24"},
    ]}}
    merged, conflicts = merge_documents(base, local, remote)

    assert conflicts == []
    assert [subnet["id"] for subnet in merged["Dhcp4"]["subnet4"]] == [2, 1, 3]


def test_an_unrelated_subnet_edit_and_a_remote_key_do_not_conflict():
    base = {"Dhcp4": {"subnet4": [{"id": 2, "subnet": "198.51.100.0/24", "routers": "198.51.100.1"}]}}
    local = {"Dhcp4": {"subnet4": [{"id": 2, "subnet": "198.51.100.0/24", "routers": "198.51.100.254"}]}}
    remote = {"Dhcp4": {"subnet4": [{"id": 2, "subnet": "198.51.100.0/24", "routers": "198.51.100.1",
                                     "comment": "set by hand"}]}}
    merged, conflicts = merge_documents(base, local, remote)

    # The routers edit is applied, the unrelated remote key is kept.
    assert conflicts == []
    assert merged["Dhcp4"]["subnet4"][0]["routers"] == "198.51.100.254"
    assert merged["Dhcp4"]["subnet4"][0]["comment"] == "set by hand"


def test_the_same_subnet_field_changed_differently_conflicts_with_its_path():
    base = {"Dhcp4": {"subnet4": [{"id": 2, "subnet": "198.51.100.0/24", "routers": "198.51.100.1"}]}}
    local = {"Dhcp4": {"subnet4": [{"id": 2, "subnet": "198.51.100.0/24", "routers": "198.51.100.254"}]}}
    remote = {"Dhcp4": {"subnet4": [{"id": 2, "subnet": "198.51.100.0/24", "routers": "198.51.100.253"}]}}
    _, conflicts = merge_documents(base, local, remote)

    assert conflicts == ["Dhcp4.subnet4[id=2].routers"]


def test_both_sides_adding_the_same_subnet_differently_conflicts():
    base = {"Dhcp4": {"subnet4": []}}
    local = {"Dhcp4": {"subnet4": [{"id": 9, "subnet": "192.0.2.0/24"}]}}
    remote = {"Dhcp4": {"subnet4": [{"id": 9, "subnet": "198.51.100.0/24"}]}}
    _, conflicts = merge_documents(base, local, remote)

    # An add/add conflict is reported at the element both sides created.
    assert conflicts == ["Dhcp4.subnet4[id=9]"]


def test_pool_changes_are_all_or_nothing():
    """A pool's string is both its identity and what an edit changes, so a
    change on both sides is a conflict, never a silent union of both ranges."""
    base = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "192.0.2.0/24",
                                    "pools": [{"pool": "192.0.2.10 - 192.0.2.20"}]}]}}
    local = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "192.0.2.0/24",
                                     "pools": [{"pool": "192.0.2.30 - 192.0.2.40"}]}]}}
    remote = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "192.0.2.0/24",
                                      "pools": [{"pool": "192.0.2.50 - 192.0.2.60"}]}]}}
    _, conflicts = merge_documents(base, local, remote)

    assert conflicts == ["Dhcp4.subnet4[id=1].pools"]


def test_a_pool_change_on_one_side_is_taken():
    base = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "192.0.2.0/24", "pools": []}]}}
    local = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "192.0.2.0/24",
                                     "pools": [{"pool": "192.0.2.10 - 192.0.2.20"}]}]}}
    merged, conflicts = merge_documents(base, local, base)

    assert conflicts == []
    assert merged["Dhcp4"]["subnet4"][0]["pools"] == [{"pool": "192.0.2.10 - 192.0.2.20"}]


def test_options_are_merged_by_name_so_an_unrelated_data_edit_survives():
    base = {"Dhcp4": {"option-data": [
        {"name": "routers", "data": "192.0.2.1"},
        {"name": "domain-name", "data": "old.example"},
    ]}}
    local = copy.deepcopy(base)
    local["Dhcp4"]["option-data"][1]["data"] = "local.example"
    remote = copy.deepcopy(base)
    remote["Dhcp4"]["option-data"][0]["data"] = "192.0.2.254"

    merged, conflicts = merge_documents(base, local, remote)

    assert conflicts == []
    by_name = {option["name"]: option["data"] for option in merged["Dhcp4"]["option-data"]}
    assert by_name == {"routers": "192.0.2.254", "domain-name": "local.example"}


def test_an_unkeyable_list_changed_on_both_sides_conflicts():
    base = _config(**{"interfaces-config": {"interfaces": ["eth0"]}})
    local = _config(**{"interfaces-config": {"interfaces": ["eth1"]}})
    remote = _config(**{"interfaces-config": {"interfaces": ["eth2"]}})
    _, conflicts = merge_documents(base, local, remote)

    assert conflicts == ["Dhcp4.interfaces-config.interfaces"]


def test_duplicate_identifiers_fall_back_to_all_or_nothing():
    base = {"Dhcp4": {"option-data": [{"name": "routers", "data": "192.0.2.1"}]}}
    local = {"Dhcp4": {"option-data": [{"name": "routers", "data": "192.0.2.2"},
                                       {"name": "routers", "data": "192.0.2.3"}]}}
    remote = {"Dhcp4": {"option-data": [{"name": "routers", "data": "192.0.2.4"}]}}
    _, conflicts = merge_documents(base, local, remote)

    assert conflicts == ["Dhcp4.option-data"]


def test_the_logging_block_travels_with_the_daemon_section():
    base = {"Dhcp4": {"valid-lifetime": 4000}}
    remote = {"Dhcp4": {"valid-lifetime": 4000},
              "Logging": {"loggers": [{"name": "kea-dhcp4"}]}}
    merged, conflicts = merge_documents(base, base, remote)

    assert conflicts == []
    assert merged["Logging"] == remote["Logging"]


def test_a_merged_document_never_shares_mutable_state_with_its_inputs():
    base = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "192.0.2.0/24"}]}}
    remote = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "192.0.2.0/24"}]}}
    merged, _ = merge_documents(base, base, remote)

    merged["Dhcp4"]["subnet4"][0]["subnet"] = "203.0.113.0/24"
    assert remote["Dhcp4"]["subnet4"][0]["subnet"] == "192.0.2.0/24"
