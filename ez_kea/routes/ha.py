# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/routes/ha.py

Configure ISC-Kea's High Availability hook (libdhcp_ha.so) for the DHCPv4
and DHCPv6 daemons, and check live HA status via each daemon's control
socket. See ez_kea/core/ha_manager.py and ez_kea/core/kea_ctrl.py for the
underlying config-editing and control-channel logic.
"""
from typing import Union
from flask_login import login_required
from flask import Blueprint, render_template, request, redirect, url_for, current_app, flash, jsonify
from werkzeug.wrappers import Response

from ..core.config_manager import load_json, save_kea_config, with_config_lock, _DEFAULT_KEA6_CONFIG
from ..core.ha_manager import (
    find_ha_hook, get_ha_params, set_ha_config, remove_ha_config, parse_ha_form,
    DEFAULT_HA_LIBRARY_PATH,
)
from ..core.kea_ctrl import send_command_any, ControlChannelError

ha_bp = Blueprint('ha', __name__)


@ha_bp.route("/high-availability")
@login_required
def high_availability() -> str:
    """Render the High Availability configuration page for DHCPv4 and DHCPv6."""
    config4 = load_json(current_app.config["DHCP_CONFIG_FILE"])
    config6 = load_json(current_app.config["DHCP6_CONFIG_FILE"], default=_DEFAULT_KEA6_CONFIG)
    hook4 = find_ha_hook(config4, "Dhcp4")
    hook6 = find_ha_hook(config6, "Dhcp6")
    return render_template(
        "high_availability.html",
        ha4=get_ha_params(config4, "Dhcp4"),
        ha4_library=hook4.get("library") if hook4 else DEFAULT_HA_LIBRARY_PATH,
        ha6=get_ha_params(config6, "Dhcp6"),
        ha6_library=hook6.get("library") if hook6 else DEFAULT_HA_LIBRARY_PATH,
        default_library=DEFAULT_HA_LIBRARY_PATH,
    )


def _save_ha(dhcp_key: str, config_file: str) -> Response:
    default = _DEFAULT_KEA6_CONFIG if dhcp_key == "Dhcp6" else None
    if request.form.get("ha-enabled") != "on":
        config = load_json(config_file, default=default)
        remove_ha_config(config, dhcp_key)
        save_kea_config(config, config_file, current_app.config["BACKUP_DIR"])
        flash(f"High Availability disabled for {dhcp_key}.", "info")
        return redirect(url_for("main.ha.high_availability"))

    result, errors = parse_ha_form(request.form)
    if errors:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for("main.ha.high_availability"))

    library_path, ha_params = result
    config = load_json(config_file, default=default)
    set_ha_config(config, library_path, ha_params, dhcp_key)
    save_kea_config(config, config_file, current_app.config["BACKUP_DIR"])
    flash(f"High Availability configuration saved for {dhcp_key}.", "success")
    return redirect(url_for("main.ha.high_availability"))


@ha_bp.route("/save-ha-config", methods=["POST"])
@login_required
@with_config_lock()
def save_ha_config() -> Response:
    """Save the DHCPv4 High Availability hook configuration."""
    return _save_ha("Dhcp4", current_app.config["DHCP_CONFIG_FILE"])


@ha_bp.route("/save-ha-config6", methods=["POST"])
@login_required
@with_config_lock("DHCP6_CONFIG_FILE")
def save_ha_config6() -> Response:
    """Save the DHCPv6 High Availability hook configuration."""
    return _save_ha("Dhcp6", current_app.config["DHCP6_CONFIG_FILE"])


def _ha_status_response(config_file: str, dhcp_key: str) -> Response:
    config = load_json(config_file, default=_DEFAULT_KEA6_CONFIG if dhcp_key == "Dhcp6" else None)
    # Whichever control channel the daemon exposes: a heartbeat is a plain
    # parameterless command, so HTTP serves it exactly as the UNIX socket does.
    # Per version: the two daemons cannot share an address/port pair.
    http_url_key = "KEA_HTTP_URL6" if dhcp_key == "Dhcp6" else "KEA_HTTP_URL"
    http_endpoint = str(current_app.config.get(http_url_key, "") or "").strip()
    http_user = str(current_app.config.get("KEA_HTTP_USER", "") or "")
    http_password = str(current_app.config.get("KEA_HTTP_PASSWORD", "") or "")
    http_auth = (http_user, http_password) if http_user and http_password else None
    try:
        response = send_command_any(
            config, dhcp_key, "ha-heartbeat",
            http_endpoint=http_endpoint, http_auth=http_auth,
        )
    except ControlChannelError as e:
        return jsonify({"error": str(e)}), 502

    if response.get("result") != 0:
        return jsonify({"error": response.get("text", "Command failed.")}), 502

    return jsonify({"message": response.get("text", "OK"), "state": response.get("arguments", {})})


@ha_bp.route("/api/ha-status")
@login_required
def api_ha_status() -> Response:
    """Live DHCPv4 HA status via the running daemon's control socket (ha-heartbeat)."""
    return _ha_status_response(current_app.config["DHCP_CONFIG_FILE"], "Dhcp4")


@ha_bp.route("/api/ha-status6")
@login_required
def api_ha_status6() -> Response:
    """Live DHCPv6 HA status via the running daemon's control socket (ha-heartbeat)."""
    return _ha_status_response(current_app.config["DHCP6_CONFIG_FILE"], "Dhcp6")
