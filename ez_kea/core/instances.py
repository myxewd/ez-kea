# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/core/instances.py

The registry of Kea management endpoints (models.KeaInstance) and the
per-request switch that makes every existing page act on the selected one.

How the switch works: every route in EZ-KEA reads its Kea paths, commands and
control endpoint out of current_app.config. Rather than thread an "instance"
argument through a hundred call sites, the selected instance contributes a
small override mapping that create_app()'s ScopedConfig consults *during that
request only* (see ez_kea/__init__.py). Selecting an endpoint therefore
re-points the dashboard, subnets, leases, the raw editor and Apply, all of
which keep working unchanged -- and two operators looking at two different
Kea servers at the same time cannot see each other's data, because the
override lives on flask.g, not on the app.

Two kinds of instance:

* "local" -- EZ-KEA runs on the Kea host and edits the daemon's own files.
* "http"  -- the daemon exposes its own HTTP/HTTPS control socket. The live
  configuration is pulled with config-get into a local mirror file, which is
  what every page then reads (so a render never waits on the network), and
  Apply pushes it back with config-test/config-set.

Deleting an instance leaves its mirror directory alone: it holds a copy of
somebody's Kea configuration, and silently deleting that is not this module's
call to make.
"""
import csv
import json
import os
import time
from typing import Any, Dict, List, Optional

from flask import current_app, g, has_app_context, session

from .config_manager import (
    _DEFAULT_KEA6_CONFIG, _DEFAULT_KEA_CONFIG, copy_file, save_json,
)

# Session key holding the id of the selected instance. Deliberately in the
# session rather than in a URL parameter: every link in the app would otherwise
# have to carry it, and a forgotten one would silently act on the wrong daemon.
SESSION_KEY = "kea_instance_id"

MODE_LOCAL = "local"
MODE_HTTP = "http"


def _model():
    """Import the model lazily so this module can be imported before db.init_app()."""
    from ..models import KeaInstance
    return KeaInstance


def all_instances() -> List[Any]:
    """Every registered instance, ordered by name."""
    KeaInstance = _model()
    return KeaInstance.query.order_by(KeaInstance.name).all()


def get_instance(instance_id: Any) -> Optional[Any]:
    """Look up one instance by id, or None for a missing/invalid id."""
    if instance_id in (None, ""):
        return None
    try:
        int_id = int(instance_id)
    except (TypeError, ValueError):
        return None
    KeaInstance = _model()
    from .. import db
    return db.session.get(KeaInstance, int_id)


def instance_dir(instance) -> str:
    """Directory holding this instance's local mirror(s)."""
    base = current_app.config.get("INSTANCE_DIR", "./data/instances")
    return os.path.join(base, "instance-%d" % int(instance.id))


def mirror_paths(instance) -> Dict[str, str]:
    """
    The local files backing one http instance.

    "4"/"6" are the mirrors the pages read and edit. "base4"/"base6" hold the
    document the last successful pull returned: that is what makes it possible
    to tell an EZ-KEA edit from a change made on the daemon (see
    core/config_merge.py), and its presence is also the proof that the mirror
    is a copy of a real daemon rather than a seeded skeleton. "pull_state"
    records which endpoint URL each baseline came from and when, so a mirror
    re-pointed at another daemon is not mistaken for a current one.
    """
    directory = instance_dir(instance)
    return {
        "4": os.path.join(directory, "kea-dhcp4.conf"),
        "6": os.path.join(directory, "kea-dhcp6.conf"),
        "base4": os.path.join(directory, "kea-dhcp4.pulled.conf"),
        "base6": os.path.join(directory, "kea-dhcp6.pulled.conf"),
        "pull_state": os.path.join(directory, "kea-pull-state.json"),
        # Lease tables pulled with lease4-get-all / lease6-get-all, in the same
        # memfile CSV shape the local daemon writes -- so the existing Leases
        # page (and its search index) reads them without knowing where they
        # came from.
        "leases4": os.path.join(directory, "kea-leases4.csv"),
        "leases6": os.path.join(directory, "kea-leases6.csv"),
        # Per-endpoint search index: sharing one SQLite file would let a remote
        # endpoint's leases and this installation's leases answer each other's
        # searches.
        "state_index": os.path.join(directory, "stateindex.db"),
    }


def ensure_mirrors(instance) -> Dict[str, str]:
    """
    Write a valid skeleton mirror for any that is missing.

    A freshly added http instance has never been pulled, and every page expects
    a readable config file, so seeding a skeleton keeps the UI working (empty,
    but honest) until the first pull fills it.
    """
    paths = mirror_paths(instance)
    if not os.path.isfile(paths["4"]):
        save_json(dict(_DEFAULT_KEA_CONFIG), paths["4"])
    if not os.path.isfile(paths["6"]):
        save_json(dict(_DEFAULT_KEA6_CONFIG), paths["6"])
    return paths


def document_from_arguments(arguments: Any, version: str) -> Optional[Dict[str, Any]]:
    """
    The document to mirror from one config-get response's arguments.

    Kea answers with the daemon's section plus some metadata (notably the
    configuration hash, which must not be pushed back to config-test/config-set
    as if it were a setting), and with the top-level Logging block when one is
    configured. Only those real document sections are kept.
    """
    key = "Dhcp6" if str(version) == "6" else "Dhcp4"
    if not isinstance(arguments, dict):
        return None
    section = arguments.get(key)
    if not isinstance(section, dict):
        return None
    payload: Dict[str, Any] = {key: section}
    if isinstance(arguments.get("Logging"), dict):
        payload["Logging"] = arguments["Logging"]
    return payload


def _pull_state_file(instance) -> str:
    return mirror_paths(instance)["pull_state"]


def _read_pull_state(instance) -> Dict[str, Any]:
    """The per-version records of the last successful pull, or {} if unreadable."""
    try:
        with open(_pull_state_file(instance), "r") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pull_state(instance) -> Dict[str, Any]:
    """Public read of the pull bookkeeping (which URL each baseline came from)."""
    return _read_pull_state(instance)


def _write_pull_state(instance, version: str, url: str, config_hash: Any) -> None:
    state = _read_pull_state(instance)
    state[str(version)] = {
        "url": url or "",
        "pulled_at": time.time(),
        "hash": str(config_hash or ""),
    }
    save_json(state, _pull_state_file(instance))


def _document_from_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def merge_base(instance, version: str) -> Optional[Dict[str, Any]]:
    """
    The pulled snapshot Apply must merge against, or None when there is none.

    None means EZ-KEA cannot tell an edit of its own from a change made on the
    daemon -- because the endpoint was never pulled successfully, the copy
    predates this bookkeeping, or the endpoint URL has changed since the pull.
    Apply refuses rather than pushing a document it cannot reason about.
    """
    if instance is None or instance.mode != MODE_HTTP:
        return None
    url = instance.http_url if str(version) == "4" else instance.http_url6
    state = _read_pull_state(instance).get(str(version))
    if not isinstance(state, dict) or state.get("url") != (url or ""):
        return None
    path = mirror_paths(instance)["base4" if str(version) == "4" else "base6"]
    return _document_from_file(path)


def _backup_mirror(path: str) -> None:
    """Best-effort backup of a mirror file before it is replaced."""
    try:
        if os.path.isfile(path):
            copy_file(path, current_app.config["BACKUP_DIR"])
    except Exception:
        pass  # a mirror is a copy, not the daemon's own file; never block on this


def record_pull(instance, version: str, url: str, document: Dict[str, Any],
                config_hash: Any = "") -> None:
    """
    Remember a successful pull as this endpoint's merge baseline.

    Called only after config-get succeeded, so a mirror that is still the seeded
    skeleton never gets a baseline and can never be applied.
    """
    save_json(document, mirror_paths(instance)["base4" if str(version) == "4" else "base6"])
    _write_pull_state(instance, version, url, config_hash)


def record_applied(instance, version: str, document: Dict[str, Any],
                   config_hash: Any = "") -> None:
    """
    Record that the daemon now runs `document`.

    After a successful Apply there is no unapplied delta left, so the mirror and
    the merge baseline both become the configuration the daemon reports -- same
    bytes, so the next Apply diffs against the truth rather than against a
    memory of it.
    """
    paths = mirror_paths(instance)
    target = paths[str(version)]
    _backup_mirror(target)
    save_json(document, target)
    save_json(document, paths["base4" if str(version) == "4" else "base6"])
    url = instance.http_url if str(version) == "4" else instance.http_url6
    _write_pull_state(instance, version, url, config_hash)


def overrides_for(instance) -> Optional[Dict[str, Any]]:
    """
    The current_app.config values a selected instance imposes on the request.

    Only keys that genuinely differ per endpoint are overridden; commands,
    backup directory and lease/log paths stay whatever this installation has
    configured, so an instance is a *target*, not a second copy of the settings
    page.
    """
    if instance is None:
        return None

    if instance.mode == MODE_HTTP:
        paths = mirror_paths(instance)
        return {
            "DHCP_CONFIG_FILE": paths["4"],
            "DHCP6_CONFIG_FILE": paths["6"],
            # A pulled config is applied back over the channel it came from.
            "KEA_RELOAD_STRATEGY": "http",
            "KEA_HTTP_URL": instance.http_url or "",
            "KEA_HTTP_URL6": instance.http_url6 or "",
            "KEA_HTTP_USER": instance.http_user or "",
            "KEA_HTTP_PASSWORD": instance.http_password or "",
            # Leases are pulled into the mirror, and the search index is
            # per-endpoint so the two tables never mix.
            "DHCP_LEASES_FILE": paths["leases4"],
            "DHCP6_LEASES_FILE": paths["leases6"],
            "STATE_INDEX_DB": paths["state_index"],
            # Something is selected, so the UI is not in sandbox mode.
            "EZ-KEA_MODE": "LIVE",
            "EZ-KEA6_MODE": "LIVE",
        }

    overrides: Dict[str, Any] = {
        "DHCP_CONFIG_FILE": instance.dhcp4_config_file,
        "EZ-KEA_MODE": "LIVE",
    }
    # A local instance may only know about v4; leave v6 pointing where this
    # installation already found it rather than inventing a path.
    if instance.dhcp6_config_file:
        overrides["DHCP6_CONFIG_FILE"] = instance.dhcp6_config_file
        overrides["EZ-KEA6_MODE"] = "LIVE"
    return overrides


def active_overrides() -> Optional[Dict[str, Any]]:
    """The overrides for the current request, or None outside a request."""
    if not has_app_context():
        return None
    return getattr(g, "instance_overrides", None)


def selected_instance() -> Optional[Any]:
    """The instance selected in this session, if it still exists."""
    return get_instance(session.get(SESSION_KEY))


def apply_selected_instance() -> None:
    """
    Resolve the session's selection onto flask.g for the current request.

    Called from a before_request hook, so templates, the context processor and
    every view see one consistent answer. A selection whose instance has been
    deleted falls back to the installation default instead of erroring.
    """
    instance = None
    try:
        instance = selected_instance()
    except Exception:
        # A missing table (first run, before db.create_all()) or a session that
        # cannot be read must not take the whole application down.
        instance = None

    if instance is None:
        session.pop(SESSION_KEY, None)
        # A standalone deployment has no local Kea to fall back to, so with
        # nothing selected every page would describe an empty sandbox file.
        # Select the first registered endpoint instead of showing nonsense.
        if not (current_app.config.get("LOCAL_KEA4_PRESENT")
                or current_app.config.get("LOCAL_KEA6_PRESENT")):
            try:
                candidates = all_instances()
            except Exception:
                candidates = []
            if candidates:
                instance = candidates[0]
                session[SESSION_KEY] = instance.id

    g.kea_instance = instance
    g.instance_overrides = overrides_for(instance)


def create_instance(name: str, mode: str, *, dhcp4_config_file: str = "",
                    dhcp6_config_file: str = "", http_url: str = "",
                    http_url6: str = "", http_user: str = "",
                    http_password: str = ""):
    """Persist a new instance (and seed its mirrors when it is an http one)."""
    from .. import db

    KeaInstance = _model()
    instance = KeaInstance(
        name=(name or "").strip()[:64] or "Unnamed",
        mode=MODE_HTTP if mode == MODE_HTTP else MODE_LOCAL,
        dhcp4_config_file=(dhcp4_config_file or "").strip(),
        dhcp6_config_file=(dhcp6_config_file or "").strip(),
        http_url=(http_url or "").strip(),
        http_url6=(http_url6 or "").strip(),
        http_user=(http_user or "").strip(),
        http_password=http_password or "",
    )
    db.session.add(instance)
    db.session.commit()

    if instance.mode == MODE_HTTP:
        ensure_mirrors(instance)
    return instance


def delete_instance(instance_id: Any) -> bool:
    """Remove an instance row. Its mirror directory is intentionally kept."""
    from .. import db

    instance = get_instance(instance_id)
    if instance is None:
        return False
    db.session.delete(instance)
    db.session.commit()
    return True


def capabilities_for(instance) -> Dict[str, bool]:
    """
    What the UI can honestly offer for an endpoint.

    A remote daemon's logs and lease CSV live on its own host, and an endpoint
    with no IPv6 control channel has no IPv6 configuration to show. Rather than
    render pages that would silently read *this* installation's files and look
    plausible, the navbar and the guards below use this to hide or refuse them.
    """
    if instance is None:
        present4 = bool(current_app.config.get("LOCAL_KEA4_PRESENT"))
        present6 = bool(current_app.config.get("LOCAL_KEA6_PRESENT"))
        return {
            "ipv4": present4,
            "ipv6": present6,
            "logs": present4 or present6,
            "leases": present4,
            "leases6": present6,
            # The file/binary settings only mean anything for a local daemon.
            "file_settings": present4 or present6,
            "remote": False,
        }

    if instance.mode == MODE_HTTP:
        return {
            "ipv4": bool(instance.http_url),
            "ipv6": bool(instance.http_url6),
            # Logs stay on the daemon's host; leases are pulled into the mirror
            # with lease4-get-all / lease6-get-all, so those pages work.
            "logs": False,
            "leases": bool(instance.http_url),
            "leases6": bool(instance.http_url6),
            "file_settings": False,
            "remote": True,
        }

    return {
        "ipv4": bool(instance.dhcp4_config_file),
        "ipv6": bool(instance.dhcp6_config_file),
        "logs": True,
        "leases": True,
        "leases6": bool(instance.dhcp6_config_file),
        "file_settings": True,
        "remote": False,
    }


def feature_block_reason(endpoint: Optional[str], view_args: Optional[Dict[str, Any]]) -> str:
    """
    Why the page being requested cannot be served for the active endpoint, or "".

    Checked in a before_request hook so a bookmarked URL or a stale tab gets the
    same treatment as a hidden nav link -- hiding a link is presentation, not a
    guarantee.
    """
    if not endpoint:
        return ""

    caps = capabilities_for(active_instance())

    if (view_args or {}).get("version") == "6" and not caps["ipv6"]:
        return ("This Kea endpoint has no DHCPv6 control channel, so there is no "
                "DHCPv6 configuration to show.")

    if endpoint.startswith("main.dhcp6.") and not caps["ipv6"]:
        return ("This Kea endpoint has no DHCPv6 control channel, so there is no "
                "DHCPv6 configuration to show.")

    if endpoint in ("main.system.logs", "main.system.logs_export", "main.system.logs_reindex"):
        if not caps["logs"]:
            return ("Log search reads log files on this host. A remote HTTP endpoint's logs "
                    "are not pulled, so the page is unavailable while it is selected.")

    if endpoint in ("main.dhcp4.leases", "main.dhcp4.leases_export") and not caps["leases"]:
        return ("Leases are read from a local CSV file. A remote HTTP endpoint's lease "
                "database is not pulled, so the page is unavailable while it is selected.")

    if endpoint in ("main.dhcp6.leases6", "main.dhcp6.leases6_export") and not caps["leases6"]:
        return "This Kea endpoint has no DHCPv6 leases to show."

    return ""


def local_syntax_check_applicable() -> bool:
    """
    Whether running this host's kea-dhcpN -t means anything for the selection.

    For a remote HTTP endpoint it does not: the file is a mirror of another
    host's daemon config, its hooks and authentication files may not even exist
    here, and the authority is the daemon's own config-test at apply time. So
    the local check is skipped rather than allowed to block edits with an error
    about a path on a machine this process cannot see.
    """
    instance = active_instance()
    return not (instance is not None and instance.mode == MODE_HTTP)


def config_snapshot(app_config: Any) -> Dict[str, Any]:
    """
    A plain dict of the app config **with the selected instance's overrides**.

    dict(current_app.config) is not enough: dict() on a dict subclass copies the
    underlying table straight through and never calls __getitem__, so the
    overrides would vanish. Every call site that snapshots the config inside a
    request (lease ingest, syntax checks, client-class validation) has to use
    this instead, or it silently works on the installation default while the UI
    shows an endpoint.
    """
    snapshot = dict(app_config)
    overrides = active_overrides()
    if overrides:
        snapshot.update(overrides)
    return snapshot


def active_instance() -> Optional[Any]:
    """The instance this request is scoped to (None = the installation default)."""
    if not has_app_context():
        return None
    return getattr(g, "kea_instance", None)


def update_instance(instance_id: Any, *, name: str, http_url: str, http_url6: str,
                    http_user: str, http_password: str) -> bool:
    """
    Update an endpoint's name/endpoints/credentials.

    A blank password means "keep the stored one": the form never renders the
    current value back, so treating blank as "clear it" would silently drop the
    credential on any edit that did not retype it.
    """
    instance = get_instance(instance_id)
    if instance is None:
        return False

    from .. import db

    if (name or "").strip():
        instance.name = name.strip()[:64]
    instance.http_url = (http_url or "").strip()
    instance.http_url6 = (http_url6 or "").strip()
    instance.http_user = (http_user or "").strip()
    if http_password:
        instance.http_password = http_password
    db.session.commit()
    return True


# Kea's memfile CSV columns, in the order the local daemon writes them. The
# pulled table is written in exactly this shape so every existing reader
# (validation.get_active_leases, state_index.parse_lease4/lease6) is unchanged.
_LEASE4_COLUMNS = (
    "address", "hwaddr", "client_id", "valid_lifetime", "expire", "subnet_id",
    "fqdn_fwd", "fqdn_rev", "hostname", "state", "user_context", "pool_id",
)
_LEASE6_COLUMNS = (
    "address", "duid", "valid_lifetime", "expire", "subnet_id", "pref_lifetime",
    "lease_type", "iaid", "prefix_len", "fqdn_fwd", "fqdn_rev", "hostname",
    "hwaddr", "state", "user_context", "pool_id",
)


def _csv_value(value: Any) -> str:
    """Memfile CSV values are plain strings; booleans are written 0/1."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _lease_row(lease: Dict[str, Any], version: str) -> Dict[str, str]:
    """Convert one lease4-get-all/lease6-get-all entry to memfile columns."""
    cltt = lease.get("cltt") or 0
    lifetime = lease.get("valid-lifetime") or lease.get("valid_lifetime") or 0
    try:
        expire = int(cltt) + int(lifetime)
    except (TypeError, ValueError):
        expire = 0

    row = {
        "address": _csv_value(lease.get("ip-address")),
        "valid_lifetime": _csv_value(lifetime),
        "expire": _csv_value(expire),
        "subnet_id": _csv_value(lease.get("subnet-id")),
        "fqdn_fwd": _csv_value(lease.get("fqdn-fwd")),
        "fqdn_rev": _csv_value(lease.get("fqdn-rev")),
        "hostname": _csv_value(lease.get("hostname")),
        "state": _csv_value(lease.get("state")),
        "user_context": _csv_value(lease.get("user-context")),
        "pool_id": _csv_value(lease.get("pool-id")),
    }
    if version == "4":
        row["hwaddr"] = _csv_value(lease.get("hw-address"))
        row["client_id"] = _csv_value(lease.get("client-id"))
    else:
        row["duid"] = _csv_value(lease.get("duid"))
        row["pref_lifetime"] = _csv_value(lease.get("pref-lifetime"))
        # Kea calls the v6 lease type "type"; the CSV calls it "lease_type".
        row["lease_type"] = _csv_value(lease.get("type"))
        row["iaid"] = _csv_value(lease.get("iaid"))
        row["prefix_len"] = _csv_value(lease.get("prefix-len"))
        row["hwaddr"] = _csv_value(lease.get("hw-address"))
    return row


def pull_leases(instance, version: str) -> List[str]:
    """
    Refresh one version's lease mirror from the daemon (lease4/6-get-all).

    Returns warnings; an empty list means the mirror now holds the daemon's
    current table. Kea answers result=3 ("0 leases found") for an empty table,
    which is a success, not a failure.
    """
    from .kea_ctrl import HTTP_CONFIG_TIMEOUT_SECONDS, ControlChannelError, send_http_command

    if instance.mode != MODE_HTTP:
        return []

    url = instance.http_url if version == "4" else instance.http_url6
    if not url:
        return []

    command = "lease4-get-all" if version == "4" else "lease6-get-all"
    auth = (instance.http_user, instance.http_password) if instance.http_user and instance.http_password else None

    try:
        response = send_http_command(url, command, timeout=HTTP_CONFIG_TIMEOUT_SECONDS, auth=auth)
    except ControlChannelError as e:
        return ["Could not pull leases from %s: %s" % (url, e)]

    result = response.get("result")
    arguments = response.get("arguments") or {}
    if result not in (0, 3):
        text = response.get("text") or "no explanation given"
        if result == 2 and "not supported" in str(text).lower():
            # Kea 3.x moved the lease4/6-* commands into a hook library that
            # the daemon has to load; without it there is simply no command to
            # call, and that is a deployment fact worth naming.
            text += (" -- the daemon is not loading the lease-commands hook. Add "
                     "hooks-libraries: [{\"library\": \".../libdhcp_lease_cmds.so\"}] "
                     "(package isc-kea-hooks) to its config and reload.")
        return ["Kea at %s refused %s: %s" % (url, command, text)]

    leases = arguments.get("leases") or []
    if not isinstance(leases, list):
        return ["Kea at %s returned an unexpected lease table." % url]

    paths = mirror_paths(instance)
    target = paths["leases4"] if version == "4" else paths["leases6"]
    columns = _LEASE4_COLUMNS if version == "4" else _LEASE6_COLUMNS

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.isfile(target):
            copy_file(target, current_app.config["BACKUP_DIR"])
        with open(target, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
            writer.writeheader()
            for lease in leases:
                if isinstance(lease, dict):
                    writer.writerow(_lease_row(lease, version))
    except Exception as e:  # disk/permission -- report, don't break the page
        return ["Could not write the local lease mirror %s: %s" % (target, e)]

    return []


def ensure_leases(version: str, max_age: float = 15.0) -> List[str]:
    """
    Make sure the lease mirror is fresh enough to render, pulling if it is not.

    A short age window keeps the page responsive (lease4-get-all returns the
    whole table in one response) while still showing what the daemon holds:
    selecting the endpoint, or opening the page after 15 quiet seconds, pulls
    again.
    """
    instance = active_instance()
    if instance is None or instance.mode != MODE_HTTP:
        return []
    if not (instance.http_url if version == "4" else instance.http_url6):
        return []

    paths = mirror_paths(instance)
    target = paths["leases4"] if version == "4" else paths["leases6"]
    try:
        age = time.time() - os.path.getmtime(target)
    except OSError:
        age = None
    if age is not None and age < max_age:
        return []
    return pull_leases(instance, version)


def describe(instance) -> str:
    """One-line summary of where an instance points, for the UI."""
    if instance.mode == MODE_HTTP:
        parts = [instance.http_url or "no v4 endpoint"]
        if instance.http_url6:
            parts.append(instance.http_url6)
        return " · ".join(parts)
    parts = [instance.dhcp4_config_file or "no v4 config file"]
    if instance.dhcp6_config_file:
        parts.append(instance.dhcp6_config_file)
    return " · ".join(parts)


def pull_config(instance) -> List[str]:
    """
    Refresh an http instance's mirrors from the daemon's own configuration.

    Uses config-get, which returns the configuration the daemon is running
    right now -- not the file on its disk, which may differ if someone applied
    a config-set. Returns a list of warnings (empty means everything pulled).

    A successful pull also stores the document as the endpoint's merge baseline
    (see merge_base), which is what lets Apply replay only EZ-KEA's own edits
    onto a daemon that changed underneath the mirror.
    """
    from .kea_ctrl import HTTP_CONFIG_TIMEOUT_SECONDS, ControlChannelError, send_http_command

    if instance.mode != MODE_HTTP:
        return ["'%s' is a local instance: its config file is the daemon's own, "
                "so there is nothing to pull." % instance.name]

    paths = ensure_mirrors(instance)
    auth = (instance.http_user, instance.http_password) if instance.http_user and instance.http_password else None
    warnings: List[str] = []

    for version, url, key in (("4", instance.http_url, "Dhcp4"), ("6", instance.http_url6, "Dhcp6")):
        if not url:
            continue
        try:
            response = send_http_command(url, "config-get", timeout=HTTP_CONFIG_TIMEOUT_SECONDS, auth=auth)
        except ControlChannelError as e:
            warnings.append("Could not pull %s from %s: %s" % (key, url, e))
            continue

        if response.get("result") != 0:
            warnings.append("Kea at %s refused config-get: %s"
                            % (url, response.get("text") or "no explanation given"))
            continue

        arguments = response.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        # The daemon's section plus the top-level Logging block (part of what
        # the running configuration is; dropping it would describe a daemon
        # with no logging configured), and none of the response metadata.
        payload = document_from_arguments(arguments, version)
        if payload is None:
            warnings.append("The configuration returned by %s has no %s section." % (url, key))
            continue

        target = paths[version]
        try:
            if os.path.isfile(target):
                copy_file(target, current_app.config["BACKUP_DIR"])
            save_json(payload, target)
            record_pull(instance, version, url, payload,
                        config_hash=arguments.get("hash", ""))
        except Exception as e:  # permission, disk, whatever -- report, don't crash the UI
            warnings.append("Could not write the local mirror %s: %s" % (target, e))

    return warnings
