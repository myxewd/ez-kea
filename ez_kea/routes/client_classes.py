# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/routes/client_classes.py

The client-class editor for both DHCP daemons. Client classification is a
standard Kea configuration surface (like shared networks or prefix
delegation), so this page knows about Kea's class shape and nothing about what
any class is used for. See ez_kea/core/client_class.py for the parsing rules.
"""
from typing import Any, Dict, Tuple, Union

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required
from werkzeug.wrappers import Response

from ..core import client_class as cc
from ..core import ipxe
from ..core.config_manager import _DEFAULT_KEA6_CONFIG, load_json, save_kea_config, with_config_lock
from ..core.config_validate import InvalidConfigError, save_validated_kea_config
from ..core.instances import config_snapshot

client_classes_bp = Blueprint('client_classes', __name__)


def _daemon(version: str) -> Dict[str, str]:
    """Config file, root key and lock key for a version string."""
    if version == "6":
        return {
            "config_key": "DHCP6_CONFIG_FILE",
            "root_key": "Dhcp6",
            "label": "DHCPv6",
            "default": _DEFAULT_KEA6_CONFIG,
        }
    return {
        "config_key": "DHCP_CONFIG_FILE",
        "root_key": "Dhcp4",
        "label": "DHCPv4",
        "default": None,
    }


def _context(version: str) -> Dict[str, Any]:
    daemon = _daemon(version)
    config = load_json(current_app.config[daemon["config_key"]], default=daemon["default"])
    entries = cc.get_client_classes(config, daemon["root_key"])
    # A generated class is shown as managed and linked to the reservation that
    # owns it, instead of offering the ordinary edit/delete actions. The owner
    # is read from the config (not decoded from the name) so the link carries
    # the real subnet string; a managed name with no owner is orphaned.
    owners = ipxe.owners(config, daemon["root_key"]) if version == "4" else {}
    classes = []
    for entry in entries:
        summary = cc.class_summary(entry)
        name = summary.get("name", "")
        summary["managed"] = ipxe.is_managed_name(name)
        summary["owner"] = owners.get(name) if summary["managed"] else None
        classes.append(summary)
    return {
        "version": version,
        "dhcp_label": daemon["label"],
        "classes": classes,
        "class_count": len(entries),
        "max_option_rows": cc.MAX_CLASS_OPTION_ROWS,
        "blank_option_rows": cc.CLASS_OPTION_BLANK_ROWS,
        "boot_fields": cc.boot_fields_for_version(version),
    }


def _form_context(version: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Context for the new/edit form, including the option rows to render."""
    boot_values = {name: entry.get(name, "") for name in cc.boot_fields_for_version(version)}
    options = entry.get("option-data")
    options = options if isinstance(options, list) else []
    # A data of None means "this row was marked for removal" (see the save
    # path's error re-render), so the template can show it struck through
    # rather than silently resurrecting it.
    rows = [
        (o.get("name", ""), None if o.get("data") is None else o.get("data", ""), True)
        for o in options
        if isinstance(o, dict)
    ]
    if not rows:
        # A fresh form starts usable without clicking "Add Option"; these are
        # not saved rows, so the ✕ just removes them (see the macro).
        rows = [("", "", False)] * cc.CLASS_OPTION_BLANK_ROWS
    return {
        "version": version,
        "dhcp_label": _daemon(version)["label"],
        "class_entry": entry,
        "boot_fields": cc.boot_fields_for_version(version),
        "boot_values": boot_values,
        # Live rows only; a class with none gets blank ones (see above).
        "option_rows": rows,
    }


def _managed_owner(config: Dict[str, Any], daemon: Dict[str, Any], name: str) -> Dict[str, str]:
    """The reservation that owns a generated class, or {} when it is orphaned."""
    return ipxe.owners(config, daemon["root_key"]).get(name) or {}


def _managed_message(name: str, owner: Dict[str, str], action: str) -> str:
    """One sentence explaining why a managed class cannot be changed here,
    naming the reservation to change instead."""
    if owner:
        label = owner.get("hostname") or owner.get("hw-address") or "a host"
        return (
            f"Client class '{name}' is generated from the reservation for {label} "
            f"in {owner.get('subnet')} and cannot be {action} directly. "
            "Change the reservation's iPXE settings instead."
        )
    return (
        f"Client class '{name}' is managed by EZ-KEA and has no reservation left "
        f"to change it from, so it cannot be {action} here."
    )


def _managed_redirect(version: str, name: str, config: Dict[str, Any],
                      daemon: Dict[str, Any]) -> Response:
    """Send the operator to the reservation that owns a managed class."""
    owner = _managed_owner(config, daemon, name)
    if version == "4" and owner:
        flash(_managed_message(name, owner, "edited"), "warning")
        return redirect(url_for(
            "main.dhcp4.edit_reservation",
            **{"hw-address": owner["hw-address"], "subnet": owner["subnet"]},
        ))
    flash(_managed_message(name, owner, "edited"), "warning")
    return redirect(url_for("main.client_classes.client_classes_version", version=version))


@client_classes_bp.route("/client-classes")
@login_required
def client_classes() -> str:
    """List the DHCPv4 client classes."""
    return render_template("client_classes.html", **_context("4"))


@client_classes_bp.route("/client-classes/<version>")
@login_required
def client_classes_version(version: str) -> Union[str, Tuple[str, int]]:
    """List the client classes for the given DHCP version."""
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400
    return render_template("client_classes.html", **_context(version))


@client_classes_bp.route("/client-classes/new")
@login_required
def new_client_class() -> str:
    """Render the empty DHCPv4 class form."""
    return render_template("new_client_class.html", editing=False, errors=[],
                           **_form_context("4", {}))


@client_classes_bp.route("/client-classes/<version>/new")
@login_required
def new_client_class_version(version: str) -> Union[str, Tuple[str, int]]:
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400
    return render_template("new_client_class.html", editing=False, errors=[],
                           **_form_context(version, {}))


@client_classes_bp.route("/client-classes/edit")
@login_required
def edit_client_class() -> Response:
    """Redirect the versionless edit link onto the DHCPv4 form."""
    return redirect(url_for("main.client_classes.edit_client_class_version",
                            version="4", name=request.args.get("name", "")))


@client_classes_bp.route("/client-classes/<version>/edit")
@login_required
def edit_client_class_version(version: str) -> Union[str, Response, Tuple[str, int]]:
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400

    name = request.args.get("name", "")
    daemon = _daemon(version)
    config = load_json(current_app.config[daemon["config_key"]], default=daemon["default"])
    entry = cc.find_client_class(config, daemon["root_key"], name)
    if entry is None:
        flash(f"Client class '{name}' was not found.", "danger")
        return redirect(url_for("main.client_classes.client_classes_version", version=version))
    if ipxe.is_managed_name(name):
        # The class is generated from a reservation's iPXE settings; editing it
        # here would silently diverge from the reservation.
        return _managed_redirect(version, name, config, daemon)

    return render_template("new_client_class.html", editing=True, errors=[],
                           original_name=name, **_form_context(version, entry))


@client_classes_bp.route("/save-client-class", methods=["POST"])
@login_required
@with_config_lock()
def save_client_class() -> Union[Response, Tuple[str, int]]:
    """Create or update a DHCPv4 client class."""
    return _save("4")


@client_classes_bp.route("/save-client-class/<version>", methods=["POST"])
@login_required
@with_config_lock("DHCP6_CONFIG_FILE")
def save_client_class_version(version: str) -> Union[Response, Tuple[str, int]]:
    """Create or update a client class for the given DHCP version."""
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400
    return _save(version)


def _save(version: str) -> Union[Response, Tuple[str, int]]:
    daemon = _daemon(version)
    config_file = current_app.config[daemon["config_key"]]
    root_key = daemon["root_key"]

    original = request.form.get("original-name", "").strip()
    config = load_json(config_file, default=daemon["default"])
    existing_names = {c.get("name") for c in cc.get_client_classes(config, root_key)}

    # What the operator typed, so neither a validation error nor Kea rejecting
    # the config loses it. Built before either check runs.
    submitted: Dict[str, Any] = {
        "name": (request.form.get("class-name") or "").strip(),
        "test": (request.form.get("class-test") or "").strip(),
    }
    for field in cc.boot_fields_for_version(version):
        submitted[field] = (request.form.get(field) or "").strip()
    submitted_rows = []
    for i in range(cc.MAX_CLASS_OPTION_ROWS):
        row_name = (request.form.get(f"class-option-name-{i}") or "").strip()
        row_data = (request.form.get(f"class-option-data-{i}") or "").strip()
        if request.form.get(f"class-option-deleted-{i}"):
            if row_name:
                submitted_rows.append({"name": row_name, "data": None})
            continue
        if row_name or row_data:
            submitted_rows.append({"name": row_name, "data": row_data})
    submitted["option-data"] = submitted_rows

    # Renaming out of the managed namespace, or into it, is refused for the
    # same reason as an edit: the name is the binding to the reservation.
    if ipxe.is_managed_name(original):
        return render_template(
            "new_client_class.html", editing=True,
            errors=[_managed_message(original, _managed_owner(config, daemon, original), "edited")],
            original_name=original, **_form_context(version, submitted),
        ), 400

    entry, errors = cc.parse_class_form(
        request.form, version,
        original=original,
        existing_names=existing_names - {original} if original else existing_names,
    )

    if errors:
        return render_template(
            "new_client_class.html", editing=bool(original), errors=errors,
            original_name=original, **_form_context(version, submitted),
        ), 400

    # Renaming is a distinct operation from editing: Kea identifies a class by
    # name, so a rename is remove-then-insert. Doing it in place keeps the
    # class's position in the list.
    # Options the operator removed are named explicitly; everything else in the
    # class's option-data is preserved as-is.
    removed = set(entry.pop("_removed-options", []))
    if removed and "option-data" in entry:
        entry["option-data"] = [o for o in entry["option-data"] if o.get("name") not in removed]
        if not entry["option-data"]:
            entry.pop("option-data")

    if original and original != entry["name"]:
        cc.remove_client_class(config, root_key, original)
    cc.upsert_client_class(config, root_key, entry, original=entry["name"])

    # Kea's own parser judges the expression; a config it refuses is rolled
    # back rather than left on disk for the daemon to choke on.
    try:
        warning = save_validated_kea_config(
            config, config_file, current_app.config["BACKUP_DIR"],
            config_snapshot(current_app.config), dhcp_key=daemon["root_key"],
        )
    except InvalidConfigError as e:
        return render_template(
            "new_client_class.html", editing=bool(original),
            errors=[e.message], original_name=original,
            **_form_context(version, submitted),
        ), 400

    if warning:
        flash(f"Client class '{entry['name']}' saved, but the Kea syntax check "
              f"could not be run: {warning}", "warning")
    else:
        flash(f"Client class '{entry['name']}' saved and accepted by Kea's syntax check. "
              f"Apply Changes to make the daemon use it.", "success")
    return redirect(url_for("main.client_classes.client_classes_version", version=version))


@client_classes_bp.route("/delete-client-class", methods=["POST"])
@login_required
@with_config_lock()
def delete_client_class() -> Response:
    """Delete a DHCPv4 client class."""
    return _delete("4")


@client_classes_bp.route("/delete-client-class/<version>", methods=["POST"])
@login_required
@with_config_lock("DHCP6_CONFIG_FILE")
def delete_client_class_version(version: str) -> Union[Response, Tuple[str, int]]:
    """Delete a client class for the given DHCP version."""
    if version not in ("4", "6"):
        return "Unknown DHCP version — must be '4' or '6'", 400
    return _delete(version)


def _delete(version: str) -> Response:
    daemon = _daemon(version)
    name = (request.form.get("name") or "").strip()
    config_file = current_app.config[daemon["config_key"]]
    config = load_json(config_file, default=daemon["default"])

    if ipxe.is_managed_name(name):
        owner = _managed_owner(config, daemon, name)
        if owner:
            flash(_managed_message(name, owner, "deleted"), "danger")
        elif cc.remove_client_class(config, daemon["root_key"], name):
            # Orphaned: the reservation it was generated for is gone (usually a
            # hand edit of the raw config), so deleting the leftover is cleanup
            # rather than overriding a live binding.
            save_kea_config(config, config_file, current_app.config["BACKUP_DIR"])
            flash(f"Orphaned client class '{name}' deleted. "
                  "Apply Changes to make the daemon use it.", "info")
        else:
            flash(f"Client class '{name}' was not found.", "danger")
        return redirect(url_for("main.client_classes.client_classes_version", version=version))

    if name and cc.remove_client_class(config, daemon["root_key"], name):
        save_kea_config(config, config_file, current_app.config["BACKUP_DIR"])
        flash(f"Client class '{name}' deleted. Apply Changes to make the daemon use it.", "info")
    else:
        flash(f"Client class '{name}' was not found.", "danger")

    return redirect(url_for("main.client_classes.client_classes_version", version=version))
