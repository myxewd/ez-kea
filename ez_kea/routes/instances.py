# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/routes/instances.py

The Endpoints page: register Kea management endpoints, pick the one the rest of
the UI acts on, and pull a remote daemon's live configuration into its mirror.

Selecting is a POST that stores the choice in the session and bounces back to
the page the operator was on, so the navbar dropdown behaves like a mode
switch rather than a navigation.
"""
from typing import Union

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template, request,
    session, url_for,
)
from flask_login import login_required
from werkzeug.wrappers import Response

from ..core import instances
from ..core.kea_ctrl import ControlChannelError, normalize_http_endpoint

instances_bp = Blueprint("instances", __name__)


@instances_bp.route("/instances")
@login_required
def instances_page() -> str:
    """List the registered endpoints, and offer the add form."""
    registered = instances.all_instances()
    return render_template(
        "instances.html",
        instances=registered,
        describe=instances.describe,
        active_id=session.get(instances.SESSION_KEY),
        has_defaults=bool(current_app.config.get("DHCP_CONFIG_FILE")),
        default_config_file=current_app.config.get("DHCP_CONFIG_FILE", ""),
        default_http_url=current_app.config.get("KEA_HTTP_URL", ""),
    )


def _validate_url(field: str, value: str, errors: list) -> str:
    """Blank is fine; anything else has to be a usable control-channel URL."""
    if not (value or "").strip():
        return ""
    try:
        return normalize_http_endpoint(value)
    except ControlChannelError as e:
        errors.append("%s: %s" % (field, e))
        return ""


@instances_bp.route("/instances/add", methods=["POST"])
@login_required
def add_instance() -> Response:
    """Register a new endpoint. Credentials are stored, never rendered back."""
    mode = request.form.get("mode", instances.MODE_LOCAL)
    if mode not in (instances.MODE_LOCAL, instances.MODE_HTTP):
        mode = instances.MODE_LOCAL

    errors = []
    name = (request.form.get("name") or "").strip()
    if not name:
        errors.append("A name is required, so the dropdown can tell the endpoints apart.")

    dhcp4_config_file = (request.form.get("dhcp4_config_file") or "").strip()
    if mode == instances.MODE_LOCAL and not dhcp4_config_file:
        errors.append("A local endpoint needs the DHCPv4 config file the daemon reads.")

    http_url = _validate_url("v4 endpoint", request.form.get("http_url", ""), errors)
    http_url6 = _validate_url("v6 endpoint", request.form.get("http_url6", ""), errors)
    if mode == instances.MODE_HTTP and not http_url and not http_url6:
        errors.append("An HTTP endpoint needs at least one control URL (v4 or v6).")

    if errors:
        for message in errors:
            flash(message, "danger")
        return redirect(url_for("main.instances.instances_page"))

    instance = instances.create_instance(
        name,
        mode,
        dhcp4_config_file=dhcp4_config_file,
        dhcp6_config_file=(request.form.get("dhcp6_config_file") or "").strip(),
        http_url=http_url,
        http_url6=http_url6,
        http_user=request.form.get("http_user", ""),
        http_password=request.form.get("http_password", ""),
    )

    if instance.mode == instances.MODE_HTTP:
        warnings = instances.pull_config(instance)
        if warnings:
            for warning in warnings:
                flash(warning, "warning")
        else:
            flash("Endpoint added and its live configuration pulled.", "success")
    else:
        flash("Endpoint added.", "success")

    # Selecting the endpoint that was just added is what the operator meant.
    session[instances.SESSION_KEY] = instance.id
    return redirect(url_for("main.instances.instances_page"))


@instances_bp.route("/instances/select", methods=["POST"])
@login_required
def select_instance() -> Response:
    """Switch the whole UI to an endpoint (or back to this installation)."""
    raw_id = request.form.get("instance_id", "")
    if raw_id in ("", "default", "0", None):
        session.pop(instances.SESSION_KEY, None)
        flash("Switched back to this installation's own Kea configuration.", "info")
        return redirect(request.referrer or url_for("main.system.index"))

    instance = instances.get_instance(raw_id)
    if instance is None:
        session.pop(instances.SESSION_KEY, None)
        flash("That endpoint no longer exists.", "danger")
        return redirect(url_for("main.instances.instances_page"))

    session[instances.SESSION_KEY] = instance.id

    # An http endpoint's mirror is only as fresh as its last pull, so pull now:
    # the operator expects to be looking at that daemon, not at yesterday's copy.
    # Leases come along too, so the dashboard's active-lease count is real.
    if instance.mode == instances.MODE_HTTP:
        for warning in instances.pull_config(instance):
            flash(warning, "warning")
        for version in ("4", "6"):
            for warning in instances.pull_leases(instance, version):
                flash(warning, "warning")

    flash("Now managing '%s'." % instance.name, "success")
    return redirect(request.referrer or url_for("main.system.index"))


@instances_bp.route("/instances/<int:instance_id>/edit", methods=["POST"])
@login_required
def edit_instance(instance_id: int) -> Response:
    """
    Update an endpoint from its own settings panel.

    This is the page that replaces "Global Settings" while a remote endpoint is
    selected: the file paths and binary paths on that page describe *this*
    installation's Kea, so offering them for a daemon on another host would be
    an invitation to point EZ-KEA at the wrong file.
    """
    instance = instances.get_instance(instance_id)
    if instance is None:
        flash("That endpoint no longer exists.", "danger")
        return redirect(url_for("main.instances.instances_page"))

    errors = []
    http_url = _validate_url("v4 endpoint", request.form.get("http_url", ""), errors)
    http_url6 = _validate_url("v6 endpoint", request.form.get("http_url6", ""), errors)
    if not http_url and not http_url6:
        errors.append("An HTTP endpoint needs at least one control URL (v4 or v6).")

    if errors:
        for message in errors:
            flash(message, "danger")
        return redirect(request.referrer or url_for("main.instances.instances_page"))

    instances.update_instance(
        instance.id,
        name=request.form.get("name", ""),
        http_url=http_url,
        http_url6=http_url6,
        http_user=request.form.get("http_user", ""),
        http_password=request.form.get("http_password", ""),
    )
    flash("Endpoint '%s' updated." % instance.name, "success")
    return redirect(request.referrer or url_for("main.instances.instances_page"))


@instances_bp.route("/instances/<int:instance_id>/pull", methods=["POST"])
@login_required
def pull_instance(instance_id: int) -> Response:
    """Re-fetch an http endpoint's live configuration into its mirror."""
    instance = instances.get_instance(instance_id)
    if instance is None:
        flash("That endpoint no longer exists.", "danger")
        return redirect(url_for("main.instances.instances_page"))

    messages = instances.pull_config(
        instance, discard_local_edits=request.form.get("discard") == "on")
    if messages:
        for message in messages:
            flash(message, "warning")
    else:
        flash("Pulled the live configuration from '%s'." % instance.name, "success")
    return redirect(url_for("main.instances.instances_page"))


@instances_bp.route("/instances/<int:instance_id>/delete", methods=["POST"])
@login_required
def delete_instance(instance_id: int) -> Response:
    """Forget an endpoint (its mirror directory is left on disk on purpose)."""
    instance = instances.get_instance(instance_id)
    if instance is None:
        flash("That endpoint no longer exists.", "danger")
        return redirect(url_for("main.instances.instances_page"))

    if session.get(instances.SESSION_KEY) == instance.id:
        session.pop(instances.SESSION_KEY, None)
    instances.delete_instance(instance.id)
    flash("Removed endpoint '%s'." % instance.name, "info")
    return redirect(url_for("main.instances.instances_page"))
