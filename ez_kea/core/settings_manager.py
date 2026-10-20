# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
keage/core/settings_manager.py

Persists runtime-overridable settings (Kea command paths, file paths) to a local
JSON file so they survive restarts without requiring environment variables.
"""
import json
import os
from typing import Any, Dict

from flask import Flask

_DEFAULTS: Dict[str, str] = {
    "kea_dhcp4_cmd":    "kea-dhcp4",
    "kea_ctrl_cmd":     "keactrl",
    "dhcp_config_file": "./data/kea-dhcp4.conf",
    "dhcp_leases_file": "./data/kea-leases4.csv",
    "dhcp_log_file":    "./data/kea-dhcp4.log",
    # DHCPv6 equivalents — mirror the DHCPv4 settings above exactly.
    "kea_dhcp6_cmd":     "kea-dhcp6",
    "dhcp6_config_file": "./data/kea-dhcp6.conf",
    "dhcp6_leases_file": "./data/kea-leases6.csv",
    "dhcp6_log_file":    "./data/kea-dhcp6.log",
    # Docker-deployment settings — see ez_kea/config.py for full rationale.
    # Blank means "same as the host path above", which is correct for
    # bare-metal/non-Docker installs.
    "dhcp_config_file_in_container": "",
    "dhcp_log_file_in_container":    "",
    "dhcp6_config_file_in_container": "",
    "dhcp6_log_file_in_container":    "",
    "kea_reload_strategy":           "keactrl",
    "kea_docker_container":          "",
    "dhcp_http_url":                 "",
    "dhcp6_http_url":                "",
}


def load_settings(settings_file: str) -> Dict[str, str]:
    """
    Load settings from the given JSON file.

    Merges the loaded settings with the application defaults so that missing
    keys are always present.

    Args:
        settings_file (str): The path to the settings file.

    Returns:
        Dict[str, str]: The merged settings dictionary.
    """
    try:
        with open(settings_file, "r") as f:
            saved = json.load(f)
        # Merge with defaults so new keys are always present
        return {**_DEFAULTS, **saved}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULTS)


def save_settings(settings_file: str, data: Dict[str, Any]) -> None:
    """
    Save the given settings dictionary to a JSON file.

    Creates necessary directories if they do not exist.

    Args:
        settings_file (str): The path to the destination JSON file.
        data (Dict[str, Any]): The dictionary containing configuration data.
    """
    os.makedirs(os.path.dirname(os.path.abspath(settings_file)), exist_ok=True)
    with open(settings_file, "w") as f:
        json.dump(data, f, indent=2)


def apply_settings_to_app(app: Flask) -> None:
    """
    Load persisted settings and override Flask app.config values.
    Env-var values always take priority over persisted settings.
    
    Called once at startup after the app is created.

    Args:
        app (Flask): The Flask application instance.
    """
    settings = load_settings(app.config["SETTINGS_FILE"])

    # Only apply if the env var was NOT explicitly set (i.e. still using default)
    env_overrides = {
        "KEA_DHCP4_CMD":    ("kea_dhcp4_cmd",    "kea-dhcp4"),
        "KEA_DHCP6_CMD":    ("kea_dhcp6_cmd",    "kea-dhcp6"),
        "KEA_CTRL_CMD":     ("kea_ctrl_cmd",     "keactrl"),
        "DHCP_CONFIG_FILE": ("dhcp_config_file", "./data/kea-dhcp4.conf"),
        "DHCP_LEASES_FILE": ("dhcp_leases_file", "./data/kea-leases4.csv"),
        "DHCP_LOG_FILE":    ("dhcp_log_file",    "./data/kea-dhcp4.log"),
        "DHCP6_CONFIG_FILE": ("dhcp6_config_file", "./data/kea-dhcp6.conf"),
        "DHCP6_LEASES_FILE": ("dhcp6_leases_file", "./data/kea-leases6.csv"),
        "DHCP6_LOG_FILE":    ("dhcp6_log_file",    "./data/kea-dhcp6.log"),
        "KEA_RELOAD_STRATEGY":  ("kea_reload_strategy",  "keactrl"),
        "KEA_DOCKER_CONTAINER": ("kea_docker_container", ""),
        "KEA_HTTP_URL":         ("dhcp_http_url",        ""),
        "KEA_HTTP_URL6":        ("dhcp6_http_url",       ""),
        # DHCP_CONFIG_FILE_IN_CONTAINER/DHCP_LOG_FILE_IN_CONTAINER (and their
        # v6 equivalents) are handled separately below since their "unset"
        # default is the dynamic host path, not a fixed literal.
    }
    for app_key, (settings_key, default_val) in env_overrides.items():
        if app.config.get(app_key) == default_val and settings_key in settings:
            app.config[app_key] = settings[settings_key]

    for app_key, settings_key in (
        ("DHCP_CONFIG_FILE_IN_CONTAINER", "dhcp_config_file_in_container"),
        ("DHCP_LOG_FILE_IN_CONTAINER",    "dhcp_log_file_in_container"),
        ("DHCP6_CONFIG_FILE_IN_CONTAINER", "dhcp6_config_file_in_container"),
        ("DHCP6_LOG_FILE_IN_CONTAINER",    "dhcp6_log_file_in_container"),
    ):
        if not os.getenv(app_key) and settings_key in settings:
            app.config[app_key] = settings[settings_key]
