# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import os
import json
from unittest.mock import patch
from ez_kea.core.discovery import discover_environment, discover_environment6


def _app_config(tmp_path, settings_file_exists=False, **overrides):
    settings_file = os.path.join(tmp_path, "settings.json")
    if settings_file_exists:
        with open(settings_file, "w") as f:
            json.dump({}, f)
    config = {"SETTINGS_FILE": settings_file}
    config.update(overrides)
    return config


def test_discover_environment6_demo_fallback(tmp_path):
    """With no saved settings, no running kea-dhcp6 process, and no standard
    install path present, v6 discovery must fall back to its own DEMO path —
    independently of whatever v4 discovery decides."""
    config = _app_config(tmp_path, DHCP6_CONFIG_FILE="./data/kea-dhcp6.conf")
    with patch("ez_kea.core.discovery.find_kea_process", return_value={}):
        with patch("os.path.isfile", return_value=False):
            result = discover_environment6(config)
    assert result["mode"] == "DEMO"
    assert result["dhcp6_config_file"] == "./data/kea-dhcp6.conf"
    assert result["kea_dhcp6_cmd"] == "kea-dhcp6"


def test_discover_environment6_live_via_running_process(tmp_path):
    config = _app_config(tmp_path)
    with patch(
        "ez_kea.core.discovery.find_kea_process",
        return_value={"pid": "1234", "config_file": "/etc/kea/kea-dhcp6.conf"},
    ):
        result = discover_environment6(config)
    assert result["mode"] == "LIVE"
    assert result["dhcp6_config_file"] == "/etc/kea/kea-dhcp6.conf"
    assert result["kea_dhcp6_cmd"] == "kea-dhcp6"


def test_discover_environment6_respects_saved_settings(tmp_path):
    settings_file = os.path.join(tmp_path, "settings.json")
    with open(settings_file, "w") as f:
        json.dump({
            "dhcp6_config_file": "/etc/kea/kea-dhcp6.conf",
            "kea_dhcp6_cmd": "/opt/kea/sbin/kea-dhcp6",
            "kea_ctrl_cmd": "keactrl",
        }, f)
    config = {"SETTINGS_FILE": settings_file}
    result = discover_environment6(config)
    assert result["mode"] == "LIVE"
    assert result["dhcp6_config_file"] == "/etc/kea/kea-dhcp6.conf"
    assert result["kea_dhcp6_cmd"] == "/opt/kea/sbin/kea-dhcp6"


def test_discover_environment6_independent_of_v4_mode(tmp_path):
    """v4 can be LIVE (a real kea-dhcp4 process running) while v6 is still
    DEMO (no kea-dhcp6 installed) — the two must not be conflated into one
    shared mode."""
    config = _app_config(tmp_path, DHCP6_CONFIG_FILE="./data/kea-dhcp6.conf")

    def fake_find(process_name="kea-dhcp4"):
        if process_name == "kea-dhcp4":
            return {"pid": "1", "config_file": "/etc/kea/kea-dhcp4.conf"}
        return {}

    with patch("ez_kea.core.discovery.find_kea_process", side_effect=fake_find):
        with patch("os.path.isfile", return_value=False):
            v4_result = discover_environment(config)
            v6_result = discover_environment6(config)

    assert v4_result["mode"] == "LIVE"
    assert v6_result["mode"] == "DEMO"

def test_explicit_config_path_beats_a_packaged_default(tmp_path):
    """DHCP_CONFIG_FILE set in the environment is the file to manage, even when
    the Kea package left /etc/kea/kea-dhcp4.conf behind. Silently managing the
    package default instead would send every edit somewhere the operator is not
    looking."""
    config = _app_config(
        tmp_path,
        DHCP_CONFIG_FILE="/srv/kea/kea-dhcp4.conf",
        DHCP_CONFIG_FILE_EXPLICIT=True,
    )
    with patch("ez_kea.core.discovery.find_kea_process", return_value={}):
        with patch("os.path.isfile", return_value=True):
            result = discover_environment(config)
    assert result["dhcp_config_file"] == "/srv/kea/kea-dhcp4.conf"
    assert result["mode"] == "LIVE"


def test_explicit_config_path_is_honoured_before_the_file_exists(tmp_path):
    """A path the operator configured is also where the skeleton must be
    bootstrapped, so an existing /etc/kea config must not win just by existing."""
    config = _app_config(
        tmp_path,
        DHCP_CONFIG_FILE="/srv/kea/kea-dhcp4.conf",
        DHCP_CONFIG_FILE_EXPLICIT=True,
    )
    with patch("ez_kea.core.discovery.find_kea_process", return_value={}):
        with patch("os.path.isfile", return_value=False):
            result = discover_environment(config)
    assert result["dhcp_config_file"] == "/srv/kea/kea-dhcp4.conf"


def test_unset_config_path_falls_back_to_a_packaged_install(tmp_path):
    """When nothing was configured explicitly and the configured path is not on
    disk, discovery must still find a packaged Kea install -- that is the whole
    point of auto-discovery."""
    config = _app_config(tmp_path, DHCP_CONFIG_FILE="/srv/kea/kea-dhcp4.conf")
    standard = {"/etc/kea/kea-dhcp4.conf", "/usr/local/etc/kea/kea-dhcp4.conf"}

    def only_standard_on_disk(path):
        return str(path) in standard

    with patch("ez_kea.core.discovery.find_kea_process", return_value={}):
        with patch("os.path.isfile", side_effect=only_standard_on_disk):
            result = discover_environment(config)
    assert result["dhcp_config_file"] == "/etc/kea/kea-dhcp4.conf"


def test_explicit_config_path6_beats_a_packaged_default(tmp_path):
    config = _app_config(
        tmp_path,
        DHCP6_CONFIG_FILE="/srv/kea/kea-dhcp6.conf",
        DHCP6_CONFIG_FILE_EXPLICIT=True,
    )
    with patch("ez_kea.core.discovery.find_kea_process", return_value={}):
        with patch("os.path.isfile", return_value=True):
            result = discover_environment6(config)
    assert result["dhcp6_config_file"] == "/srv/kea/kea-dhcp6.conf"
