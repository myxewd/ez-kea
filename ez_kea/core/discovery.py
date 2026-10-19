# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/core/discovery.py

Handles auto-discovery of live ISC-Kea processes and configurations.
Allows EZ-KEA to be purely plug-and-play by sniffing /proc to see if Kea
is already running, and what config files it was started with.
"""
import os
import glob
from typing import Dict, List, Any
from .settings_manager import load_settings

def _read_cmdline(pid: str) -> List[str]:
    """Read and parse the null-byte separated cmdline file for a given pid."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode('utf-8', errors='ignore').split('\x00')
            # Remove empty strings from the end
            return [arg for arg in cmdline if arg]
    except (FileNotFoundError, PermissionError):
        return []

def find_kea_process(process_name: str = "kea-dhcp4") -> Dict[str, str]:
    """
    Scans /proc for a process matching the given name and extracts its config path.
    Returns a dict with 'pid' and 'config_file' if found, else {}.
    """
    for pid_dir in glob.glob("/proc/[0-9]*"):
        pid = os.path.basename(pid_dir)
        try:
            with open(os.path.join(pid_dir, "comm"), "r") as f:
                comm = f.read().strip()
            
            cmdline = _read_cmdline(pid)
            cmd_zero = cmdline[0] if cmdline else ""
            
            if process_name in comm or process_name in cmd_zero:
                # Look for -c or --config
                config_path = None
                for i, arg in enumerate(cmdline):
                    if (arg == "-c" or arg == "--config") and i + 1 < len(cmdline):
                        config_path = cmdline[i + 1]
                        break
                
                # If Kea is running but no explicit -c was provided, it uses its compiled-in default.
                # In standard packages, this is /etc/kea/kea-dhcp4.conf
                if not config_path:
                    config_path = f"/etc/kea/{process_name}.conf"
                
                return {"pid": pid, "config_file": os.path.abspath(config_path)}
        except Exception:
            continue
            
    return {}

def discover_environment(app_config: Dict[str, Any]) -> Dict[str, str]:
    """
    Determines the operational mode (LIVE or DEMO) and config paths.
    1. If user explicitly overrode settings in UI (ez-kea-settings.json) -> Use those (LIVE/DEMO depends on path).
    1b. If DHCP_CONFIG_FILE was set explicitly (an environment path, not the
        built-in default) -> manage exactly that file.
    2. If kea-dhcp4 process is running -> Use its config (LIVE).
    3. If standard config file exists (/etc/kea/) -> Use it (LIVE).
    4. Otherwise -> Fallback to ./data/ (DEMO).
    """
    # 1. Check if user explicitly saved settings via the UI. If so, those win.
    settings = load_settings(app_config.get("SETTINGS_FILE", ""))
    # If the settings file has been explicitly saved, it will contain these keys.
    # The default settings generator merges defaults, but we can check if the file actually exists
    # to see if the user made explicit choices.
    if os.path.exists(app_config.get("SETTINGS_FILE", "")):
        # We assume if the file exists, the user took control. 
        # But we still want to classify the mode.
        mode = "DEMO" if "data/kea" in settings.get("dhcp_config_file", "") else "LIVE"
        return {
            "mode": mode,
            "dhcp_config_file": settings["dhcp_config_file"],
            "kea_dhcp4_cmd": settings["kea_dhcp4_cmd"],
            "kea_ctrl_cmd": settings["kea_ctrl_cmd"]
        }

    # 1b. A path the operator configured explicitly wins over anything found by
    # discovery: if they pointed EZ-KEA at a file, that is the file to manage,
    # even when the Kea package left its own /etc/kea/kea-dhcp4.conf behind.
    custom_path = app_config.get("DHCP_CONFIG_FILE")
    if custom_path and app_config.get("DHCP_CONFIG_FILE_EXPLICIT"):
        return {
            "mode": "DEMO" if "data/kea" in custom_path else "LIVE",
            "dhcp_config_file": custom_path,
            "kea_dhcp4_cmd": app_config.get("KEA_DHCP4_CMD", "kea-dhcp4"),
            "kea_ctrl_cmd": app_config.get("KEA_CTRL_CMD", "keactrl"),
        }

    # 2. Check for running processes (Auto-Discovery)
    process_info = find_kea_process("kea-dhcp4")
    if process_info and process_info.get("config_file"):
        # an operator-configured command wins even when a process is found: it
        # may be a docker exec wrapper, or a binary that is not on PATH
        kea_bin = app_config.get("KEA_DHCP4_CMD", "kea-dhcp4")
        return {
            "mode": "LIVE",
            "dhcp_config_file": process_info["config_file"],
            "kea_dhcp4_cmd": kea_bin,
            "kea_ctrl_cmd": app_config.get("KEA_CTRL_CMD", "keactrl") # Assume standard
        }

    # 3. Check for standard installation paths (even if not currently running)
    custom_path = app_config.get("DHCP_CONFIG_FILE")
    standard_paths = [
        "/etc/kea/kea-dhcp4.conf",
        "/usr/local/etc/kea/kea-dhcp4.conf"
    ]
    if custom_path and custom_path not in standard_paths:
        standard_paths.insert(0, custom_path)

    for path in standard_paths:
        if os.path.isfile(path):
            return {
                "mode": "LIVE",
                "dhcp_config_file": path,
                "kea_dhcp4_cmd": app_config.get("KEA_DHCP4_CMD", "kea-dhcp4"),
                "kea_ctrl_cmd": app_config.get("KEA_CTRL_CMD", "keactrl")
            }

    # 4. Fallback to Demo / Local mode
    return {
        "mode": "DEMO",
        "dhcp_config_file": app_config.get("DHCP_CONFIG_FILE", "./data/kea-dhcp4.conf"),
        "kea_dhcp4_cmd": app_config.get("KEA_DHCP4_CMD", "kea-dhcp4"),
        "kea_ctrl_cmd": app_config.get("KEA_CTRL_CMD", "keactrl")
    }


def discover_environment6(app_config: Dict[str, Any]) -> Dict[str, str]:
    """
    discover_environment(), scoped to DHCPv6.

    Kept as a separate function/mode rather than folded into
    discover_environment(): a deployment can easily have only one of the two
    daemons installed/running, so v4 and v6 must be able to independently
    land in LIVE or DEMO mode instead of one shared "mode" label papering
    over that.
    """
    settings = load_settings(app_config.get("SETTINGS_FILE", ""))
    if os.path.exists(app_config.get("SETTINGS_FILE", "")):
        mode = "DEMO" if "data/kea" in settings.get("dhcp6_config_file", "") else "LIVE"
        return {
            "mode": mode,
            "dhcp6_config_file": settings["dhcp6_config_file"],
            "kea_dhcp6_cmd": settings["kea_dhcp6_cmd"],
            "kea_ctrl_cmd": settings["kea_ctrl_cmd"]
        }

    # 1b. Same rule as v4: an explicitly configured path is the path.
    custom_path = app_config.get("DHCP6_CONFIG_FILE")
    if custom_path and app_config.get("DHCP6_CONFIG_FILE_EXPLICIT"):
        return {
            "mode": "DEMO" if "data/kea" in custom_path else "LIVE",
            "dhcp6_config_file": custom_path,
            "kea_dhcp6_cmd": app_config.get("KEA_DHCP6_CMD", "kea-dhcp6"),
            "kea_ctrl_cmd": app_config.get("KEA_CTRL_CMD", "keactrl"),
        }

    process_info = find_kea_process("kea-dhcp6")
    if process_info and process_info.get("config_file"):
        return {
            "mode": "LIVE",
            "dhcp6_config_file": process_info["config_file"],
            "kea_dhcp6_cmd": app_config.get("KEA_DHCP6_CMD", "kea-dhcp6"),
            "kea_ctrl_cmd": app_config.get("KEA_CTRL_CMD", "keactrl")
        }

    custom_path = app_config.get("DHCP6_CONFIG_FILE")
    standard_paths = [
        "/etc/kea/kea-dhcp6.conf",
        "/usr/local/etc/kea/kea-dhcp6.conf"
    ]
    if custom_path and custom_path not in standard_paths:
        standard_paths.insert(0, custom_path)

    for path in standard_paths:
        if os.path.isfile(path):
            return {
                "mode": "LIVE",
                "dhcp6_config_file": path,
                "kea_dhcp6_cmd": app_config.get("KEA_DHCP6_CMD", "kea-dhcp6"),
                "kea_ctrl_cmd": app_config.get("KEA_CTRL_CMD", "keactrl")
            }

    return {
        "mode": "DEMO",
        "dhcp6_config_file": app_config.get("DHCP6_CONFIG_FILE", "./data/kea-dhcp6.conf"),
        "kea_dhcp6_cmd": app_config.get("KEA_DHCP6_CMD", "kea-dhcp6"),
        "kea_ctrl_cmd": app_config.get("KEA_CTRL_CMD", "keactrl")
    }
