# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import csv
import io
import ipaddress
import shlex
import subprocess
import os
import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from flask_login import login_required
from flask import (
    Blueprint, render_template, request, redirect, url_for, jsonify, current_app,
    flash, stream_with_context,
)
from werkzeug.wrappers import Response
from ..core.config_manager import (
    load_json, save_json, save_kea_config, copy_file, extract_log_file_from_config,
    with_config_lock, config_lock, bootstrap_config, bootstrap_config6, _DEFAULT_KEA6_CONFIG,
)
from ..core.settings_manager import load_settings, save_settings
from ..core.instances import config_snapshot
from ..core.security import validate_kea_command, validate_log_file_path, InvalidKeaCommandError, InvalidLogPathError
from ..core.kea_ctrl import (
    send_command, find_unix_socket_path, find_http_endpoint, normalize_http_endpoint,
    push_config, config_document_for_push, send_command_any,
    ConfigRejectedError, ControlChannelError,
)
from ..core import state_index
from ..core.ha_manager import get_ha_params
from ..core.validation import (
    boot_fields_for, parse_scope_params, apply_scope_params, update_option_data,
    validate_option_data, _parse_option_rows,
)
from ..core.config_validate import test_config_file

system_bp = Blueprint('system', __name__)

# DHCP options this page renders as first-class form fields rather than as
# free-form rows. Everything else in the scope's option-data is presented in
# the generic "additional options" table and round-tripped verbatim, so no
# option is unreachable just because it isn't in this list.
_MANAGED_V4_OPTIONS = ("domain-name-servers", "ntp-servers", "domain-name")
_MANAGED_V6_OPTIONS = ("dns-servers", "sntp-servers", "domain-search")
# Upper bound on free-form option rows accepted from one submission. The form
# renders one row per configured option plus a few blank ones, so this is far
# above any realistic config and exists only to bound a hand-crafted POST.
_MAX_EXTRA_OPTION_ROWS = 100
# Blank rows the global-settings form always renders, so an operator can add a
# new option without JavaScript.
_EXTRA_OPTION_BLANK_ROWS = 3


def _boot_fields(version: str) -> tuple:
    """The Kea fixed-header scope parameters valid for the given DHCP version.
    DHCPv6 has no BOOTP header, so its tuple is empty."""
    return boot_fields_for("Dhcp6" if version == "6" else "Dhcp4")


def _reject_json_constant(token: str):
    """json.loads(parse_constant=...) hook: Kea's parser has no NaN/Infinity,
    but Python's json module accepts both by default. Refusing them here keeps
    a document that Kea would reject from being written as if it were fine."""
    raise ValueError(f"unsupported literal '{token}'")

# Global timers are Kea uint32 fields — reject non-positive and absurdly
# large values instead of letting an invalid value reach the Kea config.
TIMER_FIELDS = ("valid-lifetime", "max-valid-lifetime", "renew-timer", "rebind-timer")
# DHCPv6 has every v4 timer plus preferred-lifetime (no v4 equivalent).
TIMER_FIELDS6 = TIMER_FIELDS + ("preferred-lifetime",)
MIN_TIMER_VALUE = 1
MAX_TIMER_VALUE = 4294967295  # 2**32 - 1

# DHCPv6 global option-data presets, parallel to v4's GLOBAL_OPTIONS below —
# different option namespace (dns-servers/sntp-servers/domain-search rather
# than domain-name-servers/ntp-servers/domain-name).
def _global_settings_field_names(version: str) -> Dict[str, str]:
    """Form-field names and settings-key prefix for the given DHCP version's
    global-settings form. v4 names are unchanged from before version-aware
    routes existed, so the original template/form keeps working verbatim."""
    if version == "6":
        return {
            "cmd_field": "kea-dhcp6-cmd",
            "log_file_field": "dhcp6-log-file",
            "config_file_field": "dhcp6-config-file",
            "leases_file_field": "dhcp6-leases-file",
            "config_file_in_container_field": "dhcp6-config-file-in-container",
            "log_file_in_container_field": "dhcp6-log-file-in-container",
            "http_url_field": "dhcp6-http-url",
            "http_url_config_key": "KEA_HTTP_URL6",
            "settings_prefix": "dhcp6",
            "cmd_settings_key": "kea_dhcp6_cmd",
        }
    return {
        "cmd_field": "kea-dhcp4-cmd",
        "log_file_field": "dhcp-log-file",
        "config_file_field": "dhcp-config-file",
        "leases_file_field": "dhcp-leases-file",
        "config_file_in_container_field": "dhcp-config-file-in-container",
        "log_file_in_container_field": "dhcp-log-file-in-container",
        "http_url_field": "dhcp-http-url",
        "http_url_config_key": "KEA_HTTP_URL",
        "settings_prefix": "dhcp",
        "cmd_settings_key": "kea_dhcp4_cmd",
    }

# --- Docker-deployment helpers ----------------------------------------------
# These centralize the "host path vs. in-container path" and "which container"
# concerns so /test-config, /apply-config, and /save-global-settings agree on
# them.

def _resolve_daemon_config(version: str) -> Dict[str, str]:
    """
    Centralizes the version(4/6)->config-key mapping so /test-config,
    /apply-config, /backup-config, and /restore-config all agree on which
    file/command/logger they're acting on, instead of each hardcoding v4.
    """
    # leases_file/log_file are only actually read by the logs/global-settings
    # code paths, not by test/apply/backup/restore — looked up via .get() so
    # callers that only need config_file/cmd_key aren't forced to also
    # configure DHCP(6)_LEASES_FILE/DHCP(6)_LOG_FILE.
    if version == "6":
        return {
            "config_file": current_app.config["DHCP6_CONFIG_FILE"],
            "config_file_in_container": current_app.config.get("DHCP6_CONFIG_FILE_IN_CONTAINER", ""),
            "cmd_key": "KEA_DHCP6_CMD",
            "leases_file": current_app.config.get("DHCP6_LEASES_FILE", ""),
            "log_file": current_app.config.get("DHCP6_LOG_FILE", ""),
            "log_file_in_container": current_app.config.get("DHCP6_LOG_FILE_IN_CONTAINER", ""),
            "dhcp_root_key": "Dhcp6",
            "logger_name": "kea-dhcp6",
            "http_url_key": "KEA_HTTP_URL6",
        }
    return {
        "config_file": current_app.config["DHCP_CONFIG_FILE"],
        "config_file_in_container": current_app.config.get("DHCP_CONFIG_FILE_IN_CONTAINER", ""),
        "cmd_key": "KEA_DHCP4_CMD",
        "leases_file": current_app.config.get("DHCP_LEASES_FILE", ""),
        "log_file": current_app.config.get("DHCP_LOG_FILE", ""),
        "log_file_in_container": current_app.config.get("DHCP_LOG_FILE_IN_CONTAINER", ""),
        "dhcp_root_key": "Dhcp4",
        "logger_name": "kea-dhcp4",
        "http_url_key": "KEA_HTTP_URL",
    }


def _in_container_config_path(daemon: Dict[str, str]) -> str:
    """
    Path to pass to `-t` when invoking a Kea DHCP daemon command.

    Distinct from the host-side config path EZ-KEA itself reads/writes
    because when the command execs into a Docker container (`docker exec
    <container> kea-dhcp4|kea-dhcp6`), the binary runs inside the container's
    own filesystem namespace, where the host path may not exist. Falls back
    to the host path, which is correct for bare-metal/non-Docker deployments.
    """
    in_container = daemon["config_file_in_container"].strip()
    return in_container or daemon["config_file"]


def _parse_docker_container(cmd: str) -> str:
    """
    Best-effort extraction of the container name from a `docker exec
    <container> ...`-style command string.

    Read-only-validation use only (e.g. checking a directory exists before
    writing a path into Kea's config) — NEVER used to decide which command to
    actually execute for a reload. That decision is always the explicit
    KEA_RELOAD_STRATEGY / KEA_DOCKER_CONTAINER setting; guessing wrong there
    is worse than requiring the extra field.
    """
    try:
        tokens = shlex.split(cmd or "")
    except ValueError:
        return ""
    if len(tokens) >= 3 and tokens[0] == "docker" and tokens[1] == "exec":
        for tok in tokens[2:]:
            if not tok.startswith("-"):
                return tok
    return ""


def _docker_container_hint() -> str:
    """Best-effort container name for read-only validation (see _parse_docker_container)."""
    explicit = current_app.config.get("KEA_DOCKER_CONTAINER", "").strip()
    if explicit:
        return explicit
    for cmd_key in ("KEA_DHCP4_CMD", "KEA_CTRL_CMD"):
        container = _parse_docker_container(current_app.config.get(cmd_key, ""))
        if container:
            return container
    return ""


def _dir_exists(path: str) -> bool:
    """
    Check whether the parent directory of `path` exists, from the correct
    filesystem perspective: inside the Kea container if one is configured or
    discoverable (Docker deployments), else on the host running EZ-KEA.
    """
    directory = os.path.dirname(path) or "."
    container = _docker_container_hint()
    if container:
        try:
            probe = subprocess.run(
                ["docker", "exec", container, "test", "-d", directory],
                capture_output=True, timeout=5,
            )
            return probe.returncode == 0
        except Exception:
            return False
    return os.path.isdir(directory)


def _log_file_for_viewing(version: str = "4") -> str:
    """
    Path EZ-KEA's own (host-side) /logs viewer should open, for the given
    DHCP version (4 or 6; defaults to 4 for the existing /logs route).

    When a distinct in-container log path is configured (Docker deployments —
    see DHCP_LOG_FILE_IN_CONTAINER), the value written into Kea's own config
    is a container-namespace path the host Flask process cannot open, so we
    trust our own DHCP_LOG_FILE setting instead of re-reading it back out of
    the Kea config. Bare-metal/non-Docker installs (no in-container path
    configured) keep the old behavior of trusting whatever's actually in the
    Kea config, in case it was hand-edited outside EZ-KEA.
    """
    daemon = _resolve_daemon_config(version)
    if daemon["log_file_in_container"].strip():
        return daemon["log_file"]
    return extract_log_file_from_config(
        daemon["config_file"], daemon["log_file"],
        dhcp_key=daemon["dhcp_root_key"], logger_name=daemon["logger_name"],
    )

def _count_subnets(config: Dict[str, Any], dhcp_key: str, subnet_key: str) -> int:
    """Total subnet count for a DHCP version: standalone subnets plus any
    nested inside shared networks (mirrors how pools()/pools6() read the
    same config, just counting instead of rendering)."""
    dhcp = config.get(dhcp_key, {})
    count = len(dhcp.get(subnet_key, []))
    for network in dhcp.get("shared-networks", []):
        count += len(network.get(subnet_key, []))
    return count


def _pool_size(pool_str: str) -> int:
    """Address count for one Kea pool string: either "start - end" (how
    EZ-KEA itself writes pools — see new_subnet()/edit_subnet() in
    routes/dhcp4.py and dhcp6.py) or bare CIDR (how Kea, or a hand-edited
    config, might). An unparseable entry contributes 0 rather than failing
    the whole dashboard over one bad pool."""
    pool_str = (pool_str or "").strip()
    try:
        if "-" in pool_str:
            start_str, end_str = (p.strip() for p in pool_str.split("-", 1))
            start = ipaddress.ip_address(start_str)
            end = ipaddress.ip_address(end_str)
            return max(0, int(end) - int(start) + 1)
        return ipaddress.ip_network(pool_str, strict=False).num_addresses
    except ValueError:
        return 0


def _subnet_pool_capacity(subnet: Dict[str, Any]) -> int:
    """Total addresses across one subnet's regular address pools.
    Deliberately excludes DHCPv6 pd-pools (prefix delegation): those hand
    out prefixes, not individual addresses, a different unit that can't be
    summed into the same capacity figure."""
    return sum(_pool_size(pool.get("pool", "")) for pool in subnet.get("pools", []))


def _utilization_pct(used: int, capacity: int) -> Optional[float]:
    """Percentage of pool capacity currently leased, or None when no pools
    are configured at all (division has nothing meaningful to report)."""
    if capacity <= 0:
        return None
    return min(100.0, (used / capacity) * 100)


def _format_pct(pct: Optional[float]) -> str:
    """Display text for a utilization percentage. A non-zero figure that
    rounds to "0.0%" (routine on DHCPv6's enormous address pools) would read
    as "no leases at all" — flag it as a nonzero sliver instead."""
    if pct is None:
        return "No pools configured"
    if 0 < pct < 0.1:
        return "<0.1%"
    return f"{pct:.1f}%"


def _subnet_utilization(subnet: Dict[str, Any], lease_counts: Dict[str, int]) -> Dict[str, Any]:
    """Capacity/used/pct for one subnet, keyed against active-lease counts
    already grouped by subnet CIDR (state_index.active_lease_counts_by_subnet)."""
    cidr = subnet.get("subnet", "")
    capacity = _subnet_pool_capacity(subnet)
    used = lease_counts.get(cidr, 0)
    pct = _utilization_pct(used, capacity)
    return {"subnet": cidr, "capacity": capacity, "used": used, "pct": pct, "pct_text": _format_pct(pct)}


def _pool_groups(config: Dict[str, Any], dhcp_key: str, subnet_key: str, lease_counts: Dict[str, int]) -> List[Dict[str, Any]]:
    """One entry per "Pool" in EZ-KEA's own UI language — a shared network
    (Kea's `shared-networks`; ISC DHCPD called the same thing a "shared
    network", effectively a VLAN grouping multiple subnets — see pools.html)
    or a standalone subnet acting as its own single-subnet pool. Each
    aggregates its subnet(s)' pool capacity/usage, mirroring exactly how
    pools()/pools6() already group shared_networks vs standalone_subnets for
    the Pools page, so the homepage's grouping matches what clicking through
    to Pools actually shows."""
    dhcp = config.get(dhcp_key, {})
    groups: List[Dict[str, Any]] = []

    for network in dhcp.get("shared-networks", []):
        subnets = [_subnet_utilization(s, lease_counts) for s in network.get(subnet_key, [])]
        capacity = sum(s["capacity"] for s in subnets)
        used = sum(s["used"] for s in subnets)
        pct = _utilization_pct(used, capacity)
        groups.append({
            "name": network.get("name") or "Unnamed shared network",
            "capacity": capacity, "used": used, "pct": pct, "pct_text": _format_pct(pct),
            "subnets": subnets,
        })

    for subnet in dhcp.get(subnet_key, []):
        util = _subnet_utilization(subnet, lease_counts)
        groups.append({
            "name": None,  # standalone — template falls back to the subnet CIDR as the label
            "capacity": util["capacity"], "used": util["used"], "pct": util["pct"], "pct_text": util["pct_text"],
            "subnets": [util],
        })

    return groups


@system_bp.route("/healthz")
def healthz() -> Union[Response, Tuple[Response, int]]:
    """Unauthenticated readiness probe for the container healthcheck.

    Answers only the two questions that make every page fail at once: the
    database can be reached, and the Kea config EZ-KEA manages can be read and
    parsed. Deliberately cheap -- it does not touch the daemon's control
    channel (that is /api/daemon-status, which is authenticated and can be
    slow or unreachable without meaning this process is broken).
    """
    from sqlalchemy import text
    from .. import db

    problems: List[str] = []
    try:
        with db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        problems.append("database")
    try:
        load_json(current_app.config["DHCP_CONFIG_FILE"])
    except Exception:
        problems.append("config")

    if problems:
        return jsonify({"status": "error", "problems": problems}), 503
    return jsonify({"status": "ok"})


@system_bp.route("/")
@login_required
def index() -> str:
    """Render the dashboard/index page: lease/reservation/subnet counts and
    HA state for both DHCP versions. Counts come from the state index
    (kept warm by the background indexer — see create_app()) and the Kea
    config files themselves, both cheap reads; live daemon health is fetched
    client-side afterwards (see api_daemon_status) so an unreachable socket
    can't slow down or fail the page render."""
    conn = state_index.connect(current_app.config["STATE_INDEX_DB"])
    try:
        lease_stats = state_index.index_stats(conn)
        lease_counts4 = state_index.active_lease_counts_by_subnet(conn, "lease4")
        lease_counts6 = state_index.active_lease_counts_by_subnet(conn, "lease6")
    finally:
        conn.close()

    config4 = load_json(current_app.config["DHCP_CONFIG_FILE"])
    config6 = load_json(current_app.config["DHCP6_CONFIG_FILE"], default=_DEFAULT_KEA6_CONFIG)

    return render_template(
        "index.html",
        lease4=lease_stats.get("lease4", 0),
        lease6=lease_stats.get("lease6", 0),
        reservation4=lease_stats.get("reservation4", 0),
        reservation6=lease_stats.get("reservation6", 0),
        subnet4=_count_subnets(config4, "Dhcp4", "subnet4"),
        subnet6=_count_subnets(config6, "Dhcp6", "subnet6"),
        # One utilization bar per "Pool" (shared network, or a standalone
        # subnet acting as its own pool — see _pool_groups), each expandable
        # to its individual subnets. Deliberately not a single aggregate bar:
        # that would average an exhausted pool against an empty one into a
        # figure that looks fine.
        pools4=_pool_groups(config4, "Dhcp4", "subnet4", lease_counts4),
        pools6=_pool_groups(config6, "Dhcp6", "subnet6", lease_counts6),
        # Per-version DEMO mode (no live daemon found for that version at
        # startup — see create_app()'s discover_environment()/discover_environment6()
        # calls) means there is nothing for a live status-get to ever reach.
        # The template skips the async fetch and shows a neutral "Demo Mode"
        # badge instead of a live check that's guaranteed to read as a
        # (misleading) daemon-down "Unreachable".
        demo4=current_app.config.get("EZ-KEA_MODE", "LIVE") == "DEMO",
        demo6=current_app.config.get("EZ-KEA6_MODE", "LIVE") == "DEMO",
        ha4_enabled=get_ha_params(config4, "Dhcp4") is not None,
        ha6_enabled=get_ha_params(config6, "Dhcp6") is not None,
    )


@system_bp.route("/api/daemon-status/<version>")
@login_required
def api_daemon_status(version: str) -> Union[Response, Tuple[Response, int]]:
    """Live DHCPv4/DHCPv6 daemon status via `status-get` on the daemon's own
    control channel (UNIX socket, or its HTTP listener when that is all it
    has), for the homepage's async health badge. Fetched client-side (not baked
    into index()'s render) so a slow or unreachable socket can't hold up the
    rest of the dashboard."""
    if version not in ("4", "6"):
        return _invalid_version_response()
    daemon = _resolve_daemon_config(version)
    config = load_json(daemon["config_file"], default=_DEFAULT_KEA6_CONFIG if version == "6" else None)
    http_override = str(current_app.config.get(daemon["http_url_key"], "") or "").strip()
    # Either channel answers status-get, so a daemon configured with only an
    # HTTP control socket is fully reachable — reporting it as unconfigured
    # would be exactly the false "down" badge this check exists to avoid.
    try:
        has_any_channel = bool(find_unix_socket_path(config, daemon["dhcp_root_key"])) or bool(
            http_override or find_http_endpoint(config, daemon["dhcp_root_key"])
        )
    except ControlChannelError as e:
        return jsonify({"status": "unreachable", "message": str(e)})
    if not has_any_channel:
        # Distinct from "unreachable": plenty of installs use the keactrl/
        # SIGHUP reload strategy and never configure a control socket at all
        # (see _reload_via_control_socket, which treats this the same way) —
        # that's a normal, unconfigured state, not a daemon that's down.
        return jsonify({"status": "not_configured", "message": "No control channel configured."})
    try:
        response = send_command_any(
            config, daemon["dhcp_root_key"], "status-get",
            timeout=3.0, http_endpoint=http_override, http_auth=_http_auth(),
        )
    except ControlChannelError as e:
        return jsonify({"status": "unreachable", "message": str(e)})
    if response.get("result") != 0:
        return jsonify({"status": "unreachable", "message": response.get("text", "Command failed.")})
    return jsonify({"status": "running", "message": "OK"})

def _invalid_version_response() -> Tuple[Response, int]:
    return jsonify({"error": "Unknown DHCP version — must be '4' or '6'"}), 400


@system_bp.route("/backup-config", methods=["POST"])
@system_bp.route("/backup-config/<version>", methods=["POST"])
@login_required
def backup_config(version: str = "4") -> Union[Response, Tuple[Response, int]]:
    """Backup the current Kea configuration (v4 or v6) to the backup directory."""
    if version not in ("4", "6"):
        return _invalid_version_response()
    daemon = _resolve_daemon_config(version)
    # Lock on the resolved config file, not a fixed key — with_config_lock()
    # can't do this itself since the file to lock depends on the `version`
    # route argument, which isn't known until the view is actually called.
    with config_lock(daemon["config_file"]):
        try:
            copy_file(daemon["config_file"], current_app.config["BACKUP_DIR"])
            return redirect(request.referrer or url_for("main.system.index"))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

@system_bp.route("/restore-config", methods=["POST"])
@system_bp.route("/restore-config/<version>", methods=["POST"])
@login_required
def restore_config(version: str = "4") -> Union[Response, Tuple[Response, int]]:
    """Restore Kea configuration (v4 or v6) from the most recent backup."""
    if version not in ("4", "6"):
        return _invalid_version_response()
    daemon = _resolve_daemon_config(version)
    with config_lock(daemon["config_file"]):
        try:
             copy_file(daemon["config_file"], current_app.config["BACKUP_DIR"], restore=True)
             return redirect(request.referrer or url_for("main.system.index"))
        except Exception as e:
             return jsonify({"error": str(e)}), 500

def _test_config_impl(version: str) -> Union[Response, Tuple[Response, int]]:
    """
    Test the syntactic validity of the Kea configuration file for `version`.

    Shares its implementation with the pre-save check (see
    core/config_validate.py) so the button and the save path can never disagree
    about what Kea accepts.
    """
    daemon = _resolve_daemon_config(version)
    result = test_config_file(daemon["config_file"], config_snapshot(current_app.config),
                              dhcp_key=daemon["dhcp_root_key"])
    if result.ok:
        return jsonify({"message": "Syntax check passed!"})
    if not result.ran and "remote endpoint" in result.output.lower():
        # A remote endpoint has no local daemon to ask; Apply runs the
        # daemon's own config-test, which is the check that matters there.
        # Reported as a pass so the Apply button proceeds.
        return jsonify({"message": result.output, "skipped": True})
    if not result.ran:
        # The check itself could not run: the operator's command configuration
        # is refused (their input -> 400) or the environment cannot run it
        # (missing binary, timeout -> 500).
        return jsonify({"error": result.output}), (400 if result.invalid_command else 500)
    detail = result.output
    if "Syntax check failed" in detail or "error" in detail.lower():
        detail = f"Syntax error in configuration file: {detail}"
    return jsonify({"error": detail}), 500

@system_bp.route("/test-config", methods=["POST"])
@login_required
def test_config() -> Union[Response, Tuple[Response, int]]:
    """Test the syntactic validity of the DHCPv4 Kea configuration file."""
    return _test_config_impl("4")

@system_bp.route("/test-config/<version>", methods=["POST"])
@login_required
def test_config_version(version: str) -> Union[Response, Tuple[Response, int]]:
    """Test the syntactic validity of the Kea configuration file for the given version (4 or 6)."""
    if version not in ("4", "6"):
        return _invalid_version_response()
    return _test_config_impl(version)

def _reload_via_control_socket(version: str) -> Union[Response, Tuple[Response, int]]:
    """
    Reload Kea by sending `config-reload` on the daemon's own UNIX control
    socket, with no external binary involved.

    This is the reload path for Kea 3.x installed from ISC's own packages,
    which ship no `keactrl` at all, and it is what ISC points at now that the
    Control Agent is gone: talk to each daemon's control socket directly.
    Unlike SIGHUP it reports back whether the reload actually succeeded.
    """
    config_key = "DHCP6_CONFIG_FILE" if version == "6" else "DHCP_CONFIG_FILE"
    dhcp_key = "Dhcp6" if version == "6" else "Dhcp4"

    socket_path = find_unix_socket_path(
        load_json(current_app.config[config_key], default=_DEFAULT_KEA6_CONFIG if version == "6" else None),
        dhcp_key,
    )
    if not socket_path:
        return jsonify({
            "error": f"Reload strategy is set to 'control socket' but {dhcp_key} has no UNIX "
                     "control socket configured. Add one under 'control-sockets' in the Kea "
                     "config before applying."
        }), 500

    try:
        response = send_command(socket_path, "config-reload")
    except ControlChannelError as e:
        return jsonify({"error": f"Could not reload Kea over its control socket: {e}"}), 500

    if response.get("result") != 0:
        return jsonify({
            "error": f"Kea refused the reload: {response.get('text', 'unknown error')}"
        }), 500

    return jsonify({"message": "KEA service reloaded successfully!"})


def _http_auth() -> Optional[Tuple[str, str]]:
    """
    Basic credentials for the HTTP control channel, from the environment.

    Kea 3.x will not start with an unauthenticated HTTP control channel, and it
    equally refuses a clear-text password in its own config, so the compliant
    deployment authenticates with user-file/password-file -- which means EZ-KEA
    has to send a credential. Half a credential is a configuration error worth
    saying out loud rather than silently sending an unauthenticated request
    that is guaranteed to be refused.
    """
    user = str(current_app.config.get("KEA_HTTP_USER", "") or "")
    password = str(current_app.config.get("KEA_HTTP_PASSWORD", "") or "")
    if not user and not password:
        return None
    if not user or not password:
        raise ControlChannelError(
            "KEA_HTTP_USER and KEA_HTTP_PASSWORD must be set together (the daemon "
            "requires both, since Kea 3.x refuses an unauthenticated HTTP control channel)"
        )
    return (user, password)


def _http_endpoint_for(daemon: Dict[str, str], config: Dict[str, Any]) -> str:
    """
    The HTTP control-channel URL for this daemon.

    The per-version override (KEA_HTTP_URL / KEA_HTTP_URL6) wins when set: it is
    the only way to reach a listener the edited config file does not describe
    (another host, a port published out of a container, or an HTTPS endpoint
    behind a proxy). Otherwise the URL is read out of the daemon's own
    control-sockets entry, which is the normal case for a Kea 3.x daemon that
    was given an http/https socket.

    The override is per version because the two daemons cannot share an
    address/port pair -- Kea refuses it -- so a single shared URL could only
    ever apply to one of them.

    Returns "" when neither is available; raises ControlChannelError when a
    configured value is not a usable URL.
    """
    override = str(current_app.config.get(daemon["http_url_key"], "") or "").strip()
    if override:
        return normalize_http_endpoint(override)
    return find_http_endpoint(config, daemon["dhcp_root_key"])


def _apply_config_over_http(version: str) -> Union[Response, Tuple[Response, int]]:
    """
    Apply by pushing the on-disk document over the daemon's own HTTP control
    channel: config-test first, config-set only if Kea accepted the test.

    This is the one strategy that needs no reload binary, no signal and no UNIX
    socket -- it asks the running daemon itself, and the daemon decides. The
    document pushed is the file EZ-KEA just wrote (already checked with -t),
    parsed and sent under the daemon's own section name, so a restart later
    lands on the same configuration that is now running.

    config-test is Kea's own check on that exact document, but the ARM is
    explicit that it does not actually load the configuration, so a refused
    config-set is still possible and is reported separately.
    """
    daemon = _resolve_daemon_config(version)
    dhcp_key = daemon["dhcp_root_key"]
    config = load_json(
        daemon["config_file"],
        default=_DEFAULT_KEA6_CONFIG if version == "6" else None,
    )

    try:
        endpoint = _http_endpoint_for(daemon, config)
        auth = _http_auth()
    except ControlChannelError as e:
        return jsonify({"error": f"The HTTP control channel is misconfigured: {e}"}), 500

    if not endpoint:
        return jsonify({
            "error": f"Reload strategy is set to 'HTTP API' but {dhcp_key} has no http or "
                     "https control socket configured. Add one under 'control-sockets' in the "
                     "Kea config, or set the EZ-KEA HTTP URL in Application Settings, before "
                     "applying."
        }), 500

    try:
        document = config_document_for_push(config, dhcp_key)
        response = push_config(endpoint, dhcp_key, document, auth=auth)
    except ConfigRejectedError as e:
        # Two different failures, because they leave the daemon in different
        # states and need different fixes.
        if e.phase == "config-test":
            return jsonify({"error": (
                f"Kea refused this configuration at config-test, so nothing was applied and "
                f"the running daemon still has its previous configuration:\n\n{e.detail}\n\n"
                "config-test is Kea's own parser judging this exact document, so its complaint "
                f"above is what to fix. The file on disk already passed kea-dhcp{version} -t "
                "and is unchanged."
            )}), 500
        return jsonify({"error": (
            f"Kea passed config-test but refused the config-set, so this configuration is not "
            f"running:\n\n{e.detail}\n\n"
            "Kea keeps its previous configuration in that case; its log says whether it is "
            "still serving. Nothing further was attempted."
        )}), 500
    except ControlChannelError as e:
        return jsonify({"error": f"Could not apply the configuration over Kea's HTTP API: {e}"}), 500

    message = "Configuration applied to the running Kea daemon (config-test, then config-set)."
    arguments = response.get("arguments")
    digest = arguments.get("hash") if isinstance(arguments, dict) else None
    if isinstance(digest, str) and digest:
        message += f" Kea is now running configuration {digest[:12]}."
    return jsonify({"message": message})


def _apply_config_impl(version: str) -> Union[Response, Tuple[Response, int]]:
    """Make the running daemon for `version` use the current config file.

    Every strategy first hands the file to `kea-dhcpN -t`; what happens after
    that is the operator's explicit choice: a reload (`keactrl`, the UNIX
    socket's `config-reload`, a container SIGHUP) or, with the "http" strategy,
    a push of the same document over the daemon's HTTP control channel through
    config-test and config-set.
    """
    # Attempt syntax test first
    test_result = _test_config_impl(version)
    status_code = test_result[1] if isinstance(test_result, tuple) else getattr(test_result, 'status_code', 200)
    if status_code != 200:
        return test_result

    strategy = current_app.config.get("KEA_RELOAD_STRATEGY", "keactrl")
    if strategy == "control-socket":
        return _reload_via_control_socket(version)
    if strategy == "http":
        return _apply_config_over_http(version)
    if strategy == "sighup":
        container = current_app.config.get("KEA_DOCKER_CONTAINER", "").strip()
        if not container:
            return jsonify({
                "error": "Reload strategy is set to 'Docker container SIGHUP' but no Docker "
                         "container name is configured. Set it in Global Settings before "
                         "applying — this is required explicitly and never guessed."
            }), 500
        command = ["docker", "kill", "-s", "HUP", container]
    else:
        try:
            command = validate_kea_command(current_app.config["KEA_CTRL_CMD"], "kea-ctrl-cmd")
        except InvalidKeaCommandError as e:
            return jsonify({"error": str(e)}), 400
        command = command + ["reload"]

    try:
        subprocess.run(command, capture_output=True, text=True, check=True)
        return jsonify({"message": "KEA service reloaded successfully!"})
    except subprocess.CalledProcessError as e:
        error_msg = f"{e.stdout}\n{e.stderr}".strip()
        return jsonify({"error": f"KEA service reload failed: {error_msg}"}), 500
    except FileNotFoundError as e:
        return jsonify({"error": f"Could not reload Kea — control binary not found: {e}"}), 500

@system_bp.route("/apply-config", methods=["POST"])
@login_required
def apply_config() -> Union[Response, Tuple[Response, int]]:
    """Validate functionality and reload the DHCPv4 Kea service with the current config."""
    return _apply_config_impl("4")

@system_bp.route("/apply-config/<version>", methods=["POST"])
@login_required
def apply_config_version(version: str) -> Union[Response, Tuple[Response, int]]:
    """Validate functionality and reload the Kea service for the given version (4 or 6)."""
    if version not in ("4", "6"):
        return _invalid_version_response()
    return _apply_config_impl(version)

# --- Log search --------------------------------------------------------------
# Searches run against the SQLite index built by core/log_index.py, not against
# the log files, so they cover the full retained history (including rotated and
# gzipped archives) at index-seek speed rather than the last 1000 lines.

# Relative windows offered in the UI, as (value, label, seconds).
LOG_TIME_RANGES = (
    ("1h",  "Last hour",    3600),
    ("24h", "Last 24 hours", 86400),
    ("7d",  "Last 7 days",   604800),
    ("30d", "Last 30 days",  2592000),
    ("90d", "Last 90 days",  7776000),
    ("all", "All history",   None),
)
_RANGE_SECONDS = {value: seconds for value, _label, seconds in LOG_TIME_RANGES}

# Hard ceiling on a single CSV export. An export is meant to be attached to a
# ticket, and an unbounded one is a way to accidentally serve gigabytes; when
# it bites, the file says so explicitly rather than quietly ending early.
LOG_EXPORT_MAX_ROWS = 500_000


def _parse_datetime_local(value: str) -> Optional[float]:
    """Epoch seconds for an <input type=datetime-local> value, or None.

    Interpreted as local time, matching how Kea writes its own timestamps and
    therefore how the index stores them.
    """
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    return None


def _format_epoch(value: Optional[float]) -> str:
    """Human-readable local time for an index timestamp, or an em dash."""
    if not value:
        return "—"
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"


def _log_search_params() -> Dict[str, Any]:
    """Read the Logs page's filters off the request.

    Accepts GET and POST identically (via request.values) so a search is a
    plain linkable URL — which is the point for compliance work: the query that
    produced an export can be pasted into the ticket alongside it.
    """
    from ..core import log_index

    values = request.values
    # `search_query` is the field name the pre-index Logs form used; still
    # honoured so old bookmarks and links keep working.
    raw_query = (values.get("q") or values.get("search_query") or "").strip()
    kind, term = log_index.classify_term(raw_query)

    filters: Dict[str, Any] = {
        "q": None, "mac": None, "ip": None, "cidr": None,
        "severity": values.get("severity", "").strip().upper() or None,
        "version": values.get("version", "").strip() or None,
        "msg_id": values.get("msg_id", "").strip().upper() or None,
    }
    if raw_query:
        if kind == "mac":
            filters["mac"] = term
        elif kind == "ip":
            filters["ip"] = term
        elif kind == "cidr":
            filters["cidr"] = term
        else:
            filters["q"] = term

    start = _parse_datetime_local(values.get("start", ""))
    end = _parse_datetime_local(values.get("end", ""))
    time_range = values.get("range", "").strip() or ("custom" if (start or end) else "all")
    if time_range not in ("custom",) and time_range in _RANGE_SECONDS:
        window = _RANGE_SECONDS[time_range]
        start = (time.time() - window) if window else None
        end = None
    filters["start"] = start
    filters["end"] = end

    return {
        "filters": filters,
        "raw_query": raw_query,
        "query_kind": kind if raw_query else None,
        "range": time_range,
        "start_text": values.get("start", "").strip(),
        "end_text": values.get("end", "").strip(),
    }


@system_bp.route("/logs", methods=["GET", "POST"])
@login_required
def logs() -> str:
    """
    Render the Kea DHCP log search.

    Every filter resolves to an indexed lookup in the log index, so the page
    costs the same whether the deployment has a thousand lines of history or a
    hundred million. Nothing here builds or refreshes the index — that happens
    on the background thread started in create_app() — so a first-run backfill
    never shows up as a slow page load.
    """
    from ..core import log_index

    parsed = _log_search_params()
    filters = parsed["filters"]

    try:
        page = max(1, int(request.values.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    page_size = log_index.DEFAULT_PAGE_SIZE

    conn = log_index.connect(current_app.config["LOG_INDEX_DB"])
    try:
        result = log_index.search(
            conn, limit=page_size, offset=(page - 1) * page_size, **filters
        )
        stats = log_index.index_stats(conn)
    finally:
        conn.close()

    # Formatted here rather than via a Jinja filter — these are the only two
    # timestamps the page renders outside the raw log text itself.
    stats["oldest_text"] = _format_epoch(stats["oldest"])
    stats["newest_text"] = _format_epoch(stats["newest"])

    return render_template(
        "logs.html",
        rows=result["rows"],
        total=result["total"],
        capped=result["capped"],
        page=page,
        page_size=page_size,
        has_next=(page * page_size) < result["total"] or result["capped"],
        stats=stats,
        search_query=parsed["raw_query"],
        query_kind=parsed["query_kind"],
        severity=filters["severity"] or "",
        version=filters["version"] or "",
        selected_range=parsed["range"],
        start_text=parsed["start_text"],
        end_text=parsed["end_text"],
        time_ranges=LOG_TIME_RANGES,
        severities=log_index.SEVERITIES,
        export_max_rows=LOG_EXPORT_MAX_ROWS,
    )


@system_bp.route("/logs/export.csv", methods=["GET"])
@login_required
def logs_export() -> Response:
    """
    Stream the current log search as CSV — the deliverable for an audit or
    abuse request.

    Rows are fetched a page at a time and written straight to the response, so
    a large export costs bounded memory instead of materialising every matching
    line first.
    """
    from ..core import log_index

    filters = _log_search_params()["filters"]
    db_path = current_app.config["LOG_INDEX_DB"]

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush() -> str:
            chunk = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return chunk

        writer.writerow([
            "timestamp", "epoch", "dhcp_version", "severity",
            "logger", "message_id", "message",
        ])
        yield flush()

        conn = log_index.connect(db_path)
        try:
            written = 0
            for row in log_index.iter_search(conn, **filters):
                if written >= LOG_EXPORT_MAX_ROWS:
                    # Never end an evidence file silently short.
                    writer.writerow([
                        f"# TRUNCATED at {LOG_EXPORT_MAX_ROWS} rows — narrow the "
                        "time range or filters and export again"
                    ])
                    yield flush()
                    break
                ts = row["ts"]
                writer.writerow([
                    datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="milliseconds") if ts else "",
                    f"{ts:.3f}" if ts else "",
                    row["version"], row["severity"] or "", row["logger"] or "",
                    row["msg_id"] or "", row["raw"],
                ])
                written += 1
                if written % 500 == 0:
                    yield flush()
            yield flush()
        finally:
            conn.close()

    filename = f"ez-kea-log-search-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return current_app.response_class(
        stream_with_context(generate()),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@system_bp.route("/logs/reindex", methods=["POST"])
@login_required
def logs_reindex() -> Response:
    """
    Discard and rebuild the log index from the files on disk.

    Wanted after the log path changes, or if the index is ever suspected of
    having drifted. The rebuild itself runs on a worker thread — the operator
    gets the page back immediately and watches the coverage figures refill.
    """
    from ..core import log_index

    config = config_snapshot(current_app.config)
    log_index.rebuild(config["LOG_INDEX_DB"])

    def refill() -> None:
        try:
            log_index.run_once(config)
        except Exception:  # pragma: no cover - defensive
            pass

    threading.Thread(target=refill, name="ez-kea-log-reindex", daemon=True).start()
    flash("Log index cleared — rebuilding from the log files now.", "success")
    return redirect(url_for("main.system.logs"))

def _parse_extra_option_rows() -> Tuple[List[str], Dict[str, str], set]:
    """
    Read the free-form "additional options" rows off the global-settings form.

    Each row is submitted as opt-extra-name-<i> / opt-extra-data-<i>, and a row
    the operator removed carries opt-extra-deleted-<i>. Returns
    (errors, {name: data}, {removed names}); rows with a blank name are
    ignored, a name without data is an error (Kea rejects empty option data),
    and a repeated name is an error rather than one silently winning over the
    other.

    Removals are named explicitly because "absent from this submission" cannot
    mean "delete": the table only renders options it already knows about.
    """
    options, row_order, errors = _parse_option_rows(
        request.form, "opt-extra", _MAX_EXTRA_OPTION_ROWS)
    return errors, options, row_order


def _managed_option_rows(prefix: str, presets: Dict[str, str]) -> list:
    """
    The global page's fixed preset fields (DNS, NTP, ...) as rows for the
    positional merge, placed before the free-form table (negative indices sort
    first). Without these the merge would treat "the form never showed this
    preset" as true and drop a preset the operator just filled in.
    """
    return [
        (-len(presets) + position, name, name not in presets)
        for position, name in enumerate(_MANAGED_V6_OPTIONS if prefix == "6" else _MANAGED_V4_OPTIONS)
    ]


def _build_dhcp_settings_context(version: str = "4") -> Dict[str, Any]:
    """Build the template context shared by the DHCP-settings GET view and
    the error-path re-render of the same page after a failed POST."""
    daemon = _resolve_daemon_config(version)

    config = load_json(daemon["config_file"], default=_DEFAULT_KEA6_CONFIG if version == "6" else None)
    dhcp = config.get(daemon["dhcp_root_key"], {})

    filtered_settings = {k: v for k, v in dhcp.items() if k != "shared-networks"}
    global_options = {opt["name"]: opt["data"] for opt in dhcp.get("option-data", [])}

    managed = _MANAGED_V6_OPTIONS if version == "6" else _MANAGED_V4_OPTIONS
    # Every option the preset fields don't own, as ordered (name, data) pairs.
    # Rendering these as editable rows is what makes the table lossless: an
    # option that appears here is re-submitted unchanged unless the operator
    # actually edits or deletes it.
    extra_options = [
        (opt.get("name", ""), opt.get("data", ""), True)
        for opt in dhcp.get("option-data", [])
        if isinstance(opt, dict) and opt.get("name") not in managed
    ]
    if not extra_options:
        # A fresh form starts usable without clicking "Add Option"; these are
        # not saved rows, so the ✕ just removes them (see the row macro).
        extra_options = [("", "", False)] * _EXTRA_OPTION_BLANK_ROWS

    return {
        "version": version,
        "global_settings": filtered_settings,
        "global_options": global_options,
        "extra_options": extra_options,
        "boot_values": {
            name: dhcp.get(name, "")
            for name in _boot_fields(version)
        },
    }

def _build_app_settings_context(version: str = "4") -> Dict[str, Any]:
    """Build the template context for the EZ-KEA application-settings page
    (config/log file paths, binary paths, Docker deployment, reload
    strategy) for the given DHCP version."""
    daemon = _resolve_daemon_config(version)
    names = _global_settings_field_names(version)
    prefix = names["settings_prefix"]

    ez_kea_settings = load_settings(current_app.config["SETTINGS_FILE"])
    # Overlay current app config values (env vars take priority over saved settings)
    ez_kea_settings[f"{prefix}_config_file"] = daemon["config_file"]
    ez_kea_settings[f"{prefix}_leases_file"] = daemon["leases_file"]
    ez_kea_settings[f"{prefix}_log_file"]    = _log_file_for_viewing(version)
    ez_kea_settings[names["cmd_settings_key"]] = current_app.config[daemon["cmd_key"]]
    ez_kea_settings["kea_ctrl_cmd"]     = current_app.config["KEA_CTRL_CMD"]
    ez_kea_settings[f"{prefix}_config_file_in_container"] = daemon["config_file_in_container"]
    ez_kea_settings[f"{prefix}_log_file_in_container"]    = daemon["log_file_in_container"]
    ez_kea_settings["kea_reload_strategy"]  = current_app.config.get("KEA_RELOAD_STRATEGY", "keactrl")
    ez_kea_settings["kea_docker_container"] = current_app.config.get("KEA_DOCKER_CONTAINER", "")
    ez_kea_settings[f"{prefix}_http_url"]   = current_app.config.get(names["http_url_config_key"], "")

    return {
        "version": version,
        "ez_kea_settings": ez_kea_settings,
    }

@system_bp.route("/global-settings")
@login_required
def global_settings() -> str:
    """Render the DHCPv4 Kea daemon global-parameters settings page."""
    return render_template("global_settings.html", **_build_dhcp_settings_context("4"))

@system_bp.route("/global-settings/<version>")
@login_required
def global_settings_version(version: str) -> Union[str, Tuple[str, int]]:
    """Render the Kea daemon global-parameters settings page for the given version (4 or 6)."""
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400
    return render_template("global_settings.html", **_build_dhcp_settings_context(version))

@system_bp.route("/app-settings")
@login_required
def app_settings() -> str:
    """Render the EZ-KEA application-settings page (v4 tab)."""
    return render_template("app_settings.html", **_build_app_settings_context("4"))

@system_bp.route("/app-settings/<version>")
@login_required
def app_settings_version(version: str) -> Union[str, Tuple[str, int]]:
    """Render the EZ-KEA application-settings page for the given version (4 or 6)."""
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400
    return render_template("app_settings.html", **_build_app_settings_context(version))

def _save_dhcp_settings_impl(version: str) -> Union[Response, Tuple[str, int]]:
    """Save user-updated Kea daemon global (non-subnet) DHCP parameters —
    interfaces, lease timers, DUID, global options, host-reservation
    identifiers — for the given DHCP version (4 or 6)."""
    daemon = _resolve_daemon_config(version)
    redirect_endpoint = "main.system.global_settings" if version == "4" else "main.system.global_settings_version"
    redirect_kwargs = {} if version == "4" else {"version": version}
    current_config_file = daemon["config_file"]

    # ── Validate timer fields up front so a bad value never partially
    # mutates the config before we know the whole submission is clean.
    timer_fields = TIMER_FIELDS6 if version == "6" else TIMER_FIELDS
    parsed_timers: Dict[str, int] = {}
    timer_errors = []
    for timer in timer_fields:
        raw_value = request.form.get(timer, "").strip()
        if not raw_value:
            continue
        try:
            value = int(raw_value)
        except ValueError:
            timer_errors.append(f"{timer.replace('-', ' ').title()} must be a whole number.")
            continue
        if value < MIN_TIMER_VALUE or value > MAX_TIMER_VALUE:
            timer_errors.append(
                f"{timer.replace('-', ' ').title()} must be between {MIN_TIMER_VALUE} and {MAX_TIMER_VALUE}."
            )
            continue
        parsed_timers[timer] = value

    # Fixed BOOTP-header parameters (DHCPv4 only — see core/validation.py) and
    # the free-form option rows are validated alongside the timers so a bad
    # submission never half-writes the config.
    boot_fields = _boot_fields(version)
    scope_params, scope_errors = parse_scope_params(request.form, boot_fields)
    timer_errors.extend(scope_errors)
    extra_errors, extra_options, option_rows = _parse_extra_option_rows()
    timer_errors.extend(extra_errors)

    if timer_errors:
        return render_template("global_settings.html", errors=timer_errors, **_build_dhcp_settings_context(version)), 400

    config = load_json(current_config_file, default=_DEFAULT_KEA6_CONFIG if version == "6" else None)
    if daemon["dhcp_root_key"] not in config: config[daemon["dhcp_root_key"]] = {}
    dhcp = config[daemon["dhcp_root_key"]]

    if "interfaces-config" in request.form:
        seen = set()
        interfaces = []
        for i in request.form["interfaces-config"].split(","):
            name = i.strip()
            if name and name not in seen:
                seen.add(name)
                interfaces.append(name)
        dhcp.setdefault("interfaces-config", {})["interfaces"] = interfaces

    for timer in timer_fields:
        if timer in parsed_timers:
            dhcp[timer] = parsed_timers[timer]
        elif timer in dhcp and not request.form.get(timer):
            # Clear field if user left it blank (optional timers)
            if timer in ("renew-timer", "rebind-timer"):
                dhcp.pop(timer, None)

    if "host-reservation-identifiers" in request.form:
        identifiers = [
            i.strip() for i in request.form["host-reservation-identifiers"].split(",") if i.strip()
        ]
        if identifiers:
            dhcp["host-reservation-identifiers"] = identifiers
        else:
            # Kea rejects an empty list outright ("syntax error, unexpected ]"),
            # so "none set" has to be represented by the key being absent.
            dhcp.pop("host-reservation-identifiers", None)

    # DHCPv6-only: server-id (Dhcp6.server-id). Only touched when the field
    # is present and non-blank — leaving it blank means "let Kea auto-generate
    # one," not "clear whatever's already there."
    if version == "6":
        server_id_type = request.form.get("server-id-type", "").strip()
        if server_id_type:
            server_id = dhcp.setdefault("server-id", {})
            server_id["type"] = server_id_type
            identifier = request.form.get("server-id-identifier", "").strip()
            if identifier:
                server_id["identifier"] = identifier

    # Global option-data: v4 gets DNS/NTP/domain-name as first-class fields,
    # v6 gets its own namespace (dns-servers/sntp-servers/domain-search).
    # Anything else the operator configured — including options this page has
    # no concept of, such as a custom option defined outside EZ-KEA — comes
    # back through the free-form table and is merged in by name.
    if version == "6":
        managed_names = _MANAGED_V6_OPTIONS
        managed_options = {
            "dns-servers": request.form.get("opt-dns6", "").strip(),
            "sntp-servers": request.form.get("opt-sntp6", "").strip(),
            "domain-search": request.form.get("opt-domain-search6", "").strip(),
        }
    else:
        managed_names = _MANAGED_V4_OPTIONS
        managed_options = {
            "domain-name-servers": request.form.get("opt-dns", "").strip(),
            "ntp-servers": request.form.get("opt-ntp", "").strip(),
            "domain-name": request.form.get("opt-domain", "").strip(),
        }
    managed_options = {name: data for name, data in managed_options.items() if data}
    managed_options.update(extra_options)
    update_option_data(
        dhcp, managed_options, managed=managed_names,
        row_order=_managed_option_rows(version, managed_options) + option_rows,
    )

    apply_scope_params(dhcp, scope_params, boot_fields)

    save_kea_config(config, current_config_file, current_app.config["BACKUP_DIR"])
    return redirect(url_for(redirect_endpoint, **redirect_kwargs))

def _save_app_settings_impl(version: str) -> Union[Response, Tuple[str, int]]:
    """Save user-updated EZ-KEA application settings (config/log file paths,
    Kea binary paths, Docker deployment, reload strategy) for the given DHCP
    version (4 or 6)."""
    daemon = _resolve_daemon_config(version)
    names = _global_settings_field_names(version)
    prefix = names["settings_prefix"]
    redirect_endpoint = "main.system.app_settings" if version == "4" else "main.system.app_settings_version"
    redirect_kwargs = {} if version == "4" else {"version": version}
    current_config_file = daemon["config_file"]

    # ── Validate security-sensitive fields up front. Any failure here aborts
    # the whole save with no changes written anywhere. ────────────────────
    errors = []

    # Only (re-)validate a command field when it's actually being changed. If we
    # revalidated the already-stored value on every save regardless, an
    # unrelated settings change (e.g. just updating the Docker container name)
    # would fail outright the moment the previously-configured kea-dhcp4/keactrl
    # binary stops resolving (host rebuilt, PATH changed, etc.) — that's a
    # usability regression, not a security requirement. The actual security
    # boundary (can't exec anything but an allowed, executable binary) is still
    # fully enforced at set-time here AND, independently, at execution time in
    # test_config()/apply_config() regardless of how the value got there.
    candidate_dhcp_cmd = request.form.get(names["cmd_field"], "").strip() or current_app.config[daemon["cmd_key"]]
    candidate_ctrl_cmd  = request.form.get("kea-ctrl-cmd", "").strip()  or current_app.config["KEA_CTRL_CMD"]
    if candidate_dhcp_cmd != current_app.config[daemon["cmd_key"]]:
        try:
            validate_kea_command(candidate_dhcp_cmd, names["cmd_field"])
        except InvalidKeaCommandError as e:
            errors.append(str(e))
    if candidate_ctrl_cmd != current_app.config["KEA_CTRL_CMD"]:
        try:
            validate_kea_command(candidate_ctrl_cmd, "kea-ctrl-cmd")
        except InvalidKeaCommandError as e:
            errors.append(str(e))

    # The HTTP control-channel URL is operator input that gets connected to,
    # so it is validated here rather than at apply time -- a typo must not
    # survive a save and only surface as a failed apply later. Blank stays
    # valid: it means "read the endpoint out of the Kea config".
    http_url_field = names["http_url_field"]
    candidate_http_url = (
        request.form[http_url_field].strip()
        if http_url_field in request.form
        else str(current_app.config.get(names["http_url_config_key"], "") or "").strip()
    )
    if candidate_http_url:
        try:
            # Stored in the canonical form the apply path will actually use, so
            # the field cannot come back showing something different from what
            # is being connected to.
            candidate_http_url = normalize_http_endpoint(candidate_http_url)
        except ControlChannelError as e:
            errors.append(f"{http_url_field}: {e}")

    candidate_log_file = request.form.get(names["log_file_field"], "").strip() or daemon["log_file"]
    if candidate_log_file != daemon["log_file"]:
        try:
            validate_log_file_path(
                candidate_log_file, current_config_file, daemon["log_file"],
                dhcp_key=daemon["dhcp_root_key"], logger_name=daemon["logger_name"],
            )
        except InvalidLogPathError as e:
            errors.append(str(e))

    if errors:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for(redirect_endpoint, **redirect_kwargs))

    # ── Config-file repoint is a distinct action, not a save: if the operator
    # is pointing EZ-KEA at a different config file, we must NOT also write
    # whatever's currently loaded into memory (from the OLD path) over the top
    # of the NEW path. Just switch the pointer, and require the target to either
    # not exist yet (first-time setup) or already contain parseable Kea JSON.
    candidate_config_file = request.form.get(names["config_file_field"], "").strip() or current_config_file
    if candidate_config_file != current_config_file:
        resolved_target = os.path.abspath(candidate_config_file)
        if os.path.exists(resolved_target):
            try:
                with open(resolved_target, "r") as f:
                    parsed = json.load(f)
                if not isinstance(parsed, dict) or daemon["dhcp_root_key"] not in parsed:
                    raise ValueError(f"missing '{daemon['dhcp_root_key']}' root key")
            except (json.JSONDecodeError, ValueError, OSError) as e:
                flash(
                    f"{names['config_file_field']}: '{candidate_config_file}' already exists but is not a "
                    f"valid Kea DHCPv{version} config ({e}); refusing to repoint to avoid clobbering it.",
                    "danger",
                )
                return redirect(url_for(redirect_endpoint, **redirect_kwargs))

        # Start from the full merged settings (not just this version's
        # fields) so saving one version's settings never silently wipes the
        # other version's persisted settings out of the shared settings file.
        new_settings = dict(load_settings(current_app.config["SETTINGS_FILE"]))
        new_settings[names["cmd_settings_key"]] = candidate_dhcp_cmd
        new_settings["kea_ctrl_cmd"] = candidate_ctrl_cmd
        new_settings[f"{prefix}_config_file"] = resolved_target
        new_settings[f"{prefix}_leases_file"] = request.form.get(names["leases_file_field"], "").strip() or daemon["leases_file"]
        new_settings[f"{prefix}_log_file"] = candidate_log_file
        save_settings(current_app.config["SETTINGS_FILE"], new_settings)
        current_app.config[daemon["cmd_key"]] = new_settings[names["cmd_settings_key"]]
        current_app.config["KEA_CTRL_CMD"] = new_settings["kea_ctrl_cmd"]
        current_app.config["DHCP6_CONFIG_FILE" if version == "6" else "DHCP_CONFIG_FILE"] = new_settings[f"{prefix}_config_file"]
        current_app.config["DHCP6_LEASES_FILE" if version == "6" else "DHCP_LEASES_FILE"] = new_settings[f"{prefix}_leases_file"]
        current_app.config["DHCP6_LOG_FILE" if version == "6" else "DHCP_LOG_FILE"] = new_settings[f"{prefix}_log_file"]

        # First-time setup: create a minimal valid skeleton if nothing exists yet,
        # in whichever control-socket syntax this Kea understands.
        legacy_socket = False
        if not os.path.exists(resolved_target):
            from ..core.kea_version import uses_legacy_control_socket
            legacy_socket = uses_legacy_control_socket(current_app.config["KEA_DHCP4_CMD"])
        if version == "6":
            bootstrap_config6(
                resolved_target, current_app.config["BACKUP_DIR"],
                legacy_control_socket=legacy_socket,
            )
        else:
            bootstrap_config(
                resolved_target, current_app.config["BACKUP_DIR"],
                legacy_control_socket=legacy_socket,
            )

        flash(
            f"Kea config file repointed to '{resolved_target}'. Other settings on this page "
            "were not applied — review and save again against the new file if needed.",
            "info",
        )
        return redirect(url_for(redirect_endpoint, **redirect_kwargs))

    # Runtime EZ-KEA settings (Kea command paths, file paths) — persisted to
    # ez-kea-settings.json. Start from the full merged settings so this
    # version's save never clobbers the other version's persisted fields.
    new_settings = dict(load_settings(current_app.config["SETTINGS_FILE"]))
    # ...but on the FIRST save there is no settings file yet, so load_settings()
    # hands back _DEFAULTS, whose paths are the ./data sandbox. Persisting those
    # for the version we are *not* saving silently demoted it: writing the file
    # makes discover_environment*() take its "operator took control" branch, so
    # a live daemon that auto-discovery had correctly found would never be
    # looked for again, and every later edit went to a sandbox file the running
    # Kea does not read. Seed the other version from what the app actually
    # resolved at startup instead of from the defaults.
    _other = "dhcp6" if prefix == "dhcp" else "dhcp"
    _other_live = {
        f"{_other}_config_file": "DHCP6_CONFIG_FILE" if _other == "dhcp6" else "DHCP_CONFIG_FILE",
        f"{_other}_leases_file": "DHCP6_LEASES_FILE" if _other == "dhcp6" else "DHCP_LEASES_FILE",
        f"{_other}_log_file": "DHCP6_LOG_FILE" if _other == "dhcp6" else "DHCP_LOG_FILE",
    }
    for settings_key, config_key in _other_live.items():
        resolved = current_app.config.get(config_key)
        if resolved:
            new_settings[settings_key] = resolved
    new_settings[names["cmd_settings_key"]] = candidate_dhcp_cmd
    new_settings["kea_ctrl_cmd"] = candidate_ctrl_cmd
    new_settings[f"{prefix}_config_file"] = current_config_file
    new_settings[f"{prefix}_leases_file"] = request.form.get(names["leases_file_field"], "").strip() or daemon["leases_file"]
    new_settings[f"{prefix}_log_file"] = candidate_log_file
    # Docker-deployment settings (see ez_kea/config.py). Blank is a valid,
    # meaningful value here ("same as the host path/no override") so we
    # only fall back to the prior value when the field is altogether
    # absent from this POST.
    new_settings[f"{prefix}_config_file_in_container"] = (
        request.form[names["config_file_in_container_field"]].strip()
        if names["config_file_in_container_field"] in request.form
        else daemon["config_file_in_container"]
    )
    new_settings[f"{prefix}_log_file_in_container"] = (
        request.form[names["log_file_in_container_field"]].strip()
        if names["log_file_in_container_field"] in request.form
        else daemon["log_file_in_container"]
    )
    new_settings["kea_reload_strategy"] = (
        request.form.get("kea-reload-strategy", "").strip()
        or current_app.config.get("KEA_RELOAD_STRATEGY", "keactrl")
    )
    new_settings["kea_docker_container"] = (
        request.form["kea-docker-container"].strip()
        if "kea-docker-container" in request.form
        else current_app.config.get("KEA_DOCKER_CONTAINER", "")
    )
    new_settings[f"{prefix}_http_url"] = candidate_http_url

    # Also update the log file in Kea's own config so it's the source of
    # truth for the running daemon. Use the in-container path when one is
    # configured (Docker deployments) since that's the namespace Kea itself
    # actually reads/writes in — writing the host path verbatim there would
    # silently kill Kea's own logging the moment it's applied.
    log_file_path = new_settings[f"{prefix}_log_file_in_container"] or new_settings[f"{prefix}_log_file"]

    config = load_json(current_config_file, default=_DEFAULT_KEA6_CONFIG if version == "6" else None)
    if daemon["dhcp_root_key"] not in config: config[daemon["dhcp_root_key"]] = {}
    dhcp = config[daemon["dhcp_root_key"]]

    if _dir_exists(log_file_path):
        loggers = dhcp.setdefault("loggers", [])
        found_logger = False
        for logger in loggers:
            if logger.get("name") == daemon["logger_name"]:
                found_logger = True
                # Kea spells this "output_options". A config written by an
                # older EZ-KEA may carry the hyphenated variant, which Kea
                # rejects outright ("got unexpected keyword") — normalise it
                # in place rather than writing a second, differently-spelled
                # key alongside it, which would break the logger either way.
                if "output-options" in logger and "output_options" not in logger:
                    logger["output_options"] = logger.pop("output-options")
                opts = logger.setdefault("output_options", [{"output": log_file_path}])
                if opts:
                    opts[0]["output"] = log_file_path
                else:
                    opts.append({"output": log_file_path})
                break
        if not found_logger:
            loggers.append({
                "name": daemon["logger_name"],
                "severity": "INFO",
                "debuglevel": 0,
                "output_options": [{"output": log_file_path}]
            })
    else:
        container = _docker_container_hint()
        perspective = f"inside container '{container}'" if container else "on the host"
        flash(
            f"Log directory for '{log_file_path}' does not exist ({perspective}) — "
            "Kea's logger configuration was NOT changed, to avoid silently breaking its "
            "logging. Create the directory first (in Docker mode, inside the container "
            "Kea actually runs in), then save again.",
            "warning",
        )

    save_settings(current_app.config["SETTINGS_FILE"], new_settings)
    # Apply immediately to running app
    current_app.config[daemon["cmd_key"]] = new_settings[names["cmd_settings_key"]]
    current_app.config["KEA_CTRL_CMD"] = new_settings["kea_ctrl_cmd"]
    current_app.config["DHCP6_LEASES_FILE" if version == "6" else "DHCP_LEASES_FILE"] = new_settings[f"{prefix}_leases_file"]
    current_app.config["DHCP6_LOG_FILE" if version == "6" else "DHCP_LOG_FILE"] = new_settings[f"{prefix}_log_file"]
    current_app.config["DHCP6_CONFIG_FILE_IN_CONTAINER" if version == "6" else "DHCP_CONFIG_FILE_IN_CONTAINER"] = new_settings[f"{prefix}_config_file_in_container"]
    current_app.config["DHCP6_LOG_FILE_IN_CONTAINER" if version == "6" else "DHCP_LOG_FILE_IN_CONTAINER"] = new_settings[f"{prefix}_log_file_in_container"]
    current_app.config["KEA_RELOAD_STRATEGY"] = new_settings["kea_reload_strategy"]
    current_app.config["KEA_DOCKER_CONTAINER"] = new_settings["kea_docker_container"]
    current_app.config[names["http_url_config_key"]] = new_settings[f"{prefix}_http_url"]

    save_kea_config(config, current_config_file, current_app.config["BACKUP_DIR"])
    return redirect(url_for(redirect_endpoint, **redirect_kwargs))

# ── Advanced: raw Kea configuration editor ──────────────────────────────────
#
# Every structured form in EZ-KEA necessarily knows a fixed set of Kea keys;
# this page is the general fallback for everything else (client-classes,
# option-def/option-space, hooks, custom scope parameters). It edits the exact
# document the daemon reads, so it is scoped to the whole file rather than to
# one scope object -- a partially edited subtree would be ambiguous to merge
# back into a file that may have changed since it was rendered.
#
# It is safe by the same mechanisms every other writer uses: a backup precedes
# the write, the document must parse and carry the right root key before it is
# written at all, and the result is handed to Kea's own syntax checker
# afterwards. Backup/restore remains available if a bad-but-valid config is
# applied.

def _advanced_config_context(version: str) -> Dict[str, Any]:
    daemon = _resolve_daemon_config(version)
    config_file = daemon["config_file"]
    root_key = daemon["dhcp_root_key"]

    text = ""
    try:
        with open(config_file, "r") as handle:
            text = handle.read()
    except FileNotFoundError:
        config = load_json(config_file, default=_DEFAULT_KEA6_CONFIG if version == "6" else None)
        text = json.dumps(config, indent=2)
    except PermissionError:
        # load_json() turns this into the ConfigAccessError the app renders as
        # a friendly 503 (see the error handler in __init__.py); re-reading it
        # here keeps that single explanation rather than inventing a second.
        load_json(config_file, default=_DEFAULT_KEA6_CONFIG if version == "6" else None)

    return {
        "version": version,
        "config_file": config_file,
        "root_key": root_key,
        "config_text": text,
    }


@system_bp.route("/advanced-config")
@login_required
def advanced_config() -> str:
    """Raw editor for the DHCPv4 Kea configuration document."""
    return render_template("advanced_config.html", **_advanced_config_context("4"))


@system_bp.route("/advanced-config/<version>")
@login_required
def advanced_config_version(version: str) -> Union[str, Tuple[str, int]]:
    """Raw editor for the given DHCP version's Kea configuration document."""
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400
    return render_template("advanced_config.html", **_advanced_config_context(version))


def _save_advanced_config_impl(version: str) -> Union[Response, Tuple[str, int]]:
    """Validate and write a hand-edited Kea configuration document, then run
    Kea's own syntax check against the result and report what it said."""
    daemon = _resolve_daemon_config(version)
    config_file = daemon["config_file"]
    root_key = daemon["dhcp_root_key"]
    redirect_endpoint = "main.system.advanced_config" if version == "4" else "main.system.advanced_config_version"
    redirect_kwargs = {} if version == "4" else {"version": version}

    submitted = request.form.get("config-json", "")
    context = _advanced_config_context(version)
    context["config_text"] = submitted

    # Kea's parser accepts //, #, and /* */ comments; accept them here too so
    # a document pasted back out of an ISC-shipped config isn't rejected for
    # syntax Kea itself is happy with. parse_constant rejects the JSON5-ish
    # NaN/Infinity literals json.loads would otherwise accept.
    from ..core.config_manager import _strip_json_comments
    try:
        parsed = json.loads(
            _strip_json_comments(submitted),
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as e:
        position = f" at line {e.lineno}, column {e.colno}" if isinstance(e, json.JSONDecodeError) else ""
        context["errors"] = [f"Not valid JSON{position}: {e.msg if isinstance(e, json.JSONDecodeError) else e}."]
        return render_template("advanced_config.html", **context), 400

    if not isinstance(parsed, dict) or root_key not in parsed:
        context["errors"] = [
            f"The document must be a JSON object containing a \"{root_key}\" key "
            f"(the root of a Kea {root_key.replace('Dhcp', 'DHCP')} configuration)."
        ]
        return render_template("advanced_config.html", **context), 400

    # Kea's own parser is the authority on whether this document is loadable,
    # and this is the page where arbitrary Kea syntax gets typed -- so the save
    # is rolled back when Kea refuses it rather than leaving a file the daemon
    # cannot load. The operator's text stays in the editor either way.
    from ..core.config_validate import InvalidConfigError, save_validated_kea_config
    try:
        warning = save_validated_kea_config(
            parsed, config_file, current_app.config["BACKUP_DIR"],
            config_snapshot(current_app.config), dhcp_key=daemon["dhcp_root_key"],
        )
    except InvalidConfigError as e:
        context["errors"] = [e.message]
        return render_template("advanced_config.html", **context), 400

    if warning:
        flash(f"Configuration saved, but the syntax check could not be run: {warning}", "warning")
    else:
        flash("Configuration saved and accepted by Kea's syntax check. "
              "Apply Changes to make the daemon reload it.", "success")
    return redirect(url_for(redirect_endpoint, **redirect_kwargs))


@system_bp.route("/save-advanced-config", methods=["POST"])
@login_required
@with_config_lock()
def save_advanced_config() -> Union[Response, Tuple[str, int]]:
    """Save the raw DHCPv4 configuration document."""
    return _save_advanced_config_impl("4")


@system_bp.route("/save-advanced-config/<version>", methods=["POST"])
@login_required
@with_config_lock("DHCP6_CONFIG_FILE")
def save_advanced_config_version(version: str) -> Union[Response, Tuple[str, int]]:
    """Save the raw configuration document for the given DHCP version."""
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400
    return _save_advanced_config_impl(version)


@system_bp.route("/save-global-settings", methods=["POST"])
@login_required
@with_config_lock()
def save_global_settings() -> Union[Response, Tuple[str, int]]:
    """Save user-updated DHCPv4 Kea daemon global parameters."""
    return _save_dhcp_settings_impl("4")

@system_bp.route("/save-global-settings/<version>", methods=["POST"])
@login_required
def save_global_settings_version(version: str) -> Union[Response, Tuple[str, int]]:
    """Save user-updated Kea daemon global parameters for the given version (4 or 6)."""
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400
    daemon = _resolve_daemon_config(version)
    with config_lock(daemon["config_file"]):
        return _save_dhcp_settings_impl(version)

@system_bp.route("/save-app-settings", methods=["POST"])
@login_required
@with_config_lock()
def save_app_settings() -> Union[Response, Tuple[str, int]]:
    """Save user-updated EZ-KEA application settings for DHCPv4."""
    return _save_app_settings_impl("4")

@system_bp.route("/save-app-settings/<version>", methods=["POST"])
@login_required
def save_app_settings_version(version: str) -> Union[Response, Tuple[str, int]]:
    """Save user-updated EZ-KEA application settings for the given version (4 or 6)."""
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400
    daemon = _resolve_daemon_config(version)
    with config_lock(daemon["config_file"]):
        return _save_app_settings_impl(version)


@system_bp.route("/api/system/discover")
@login_required
def api_discover() -> Response:
    """Endpoint for UI auto-discovery of Kea settings and modes."""
    from ..core.discovery import discover_environment
    # Force discovery by faking a non-existent settings file, so it sniffs /proc
    # and /etc instead of returning the user's saved overrides.
    fake_config = config_snapshot(current_app.config)
    fake_config["SETTINGS_FILE"] = "/nonexistent_force_discovery"
    env_info = discover_environment(fake_config)
    return jsonify(env_info)