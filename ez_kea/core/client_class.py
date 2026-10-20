# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/core/client_class.py

Editing Kea's DHCPv4/DHCPv6 `client-classes` list.

Client classification is a core Kea feature, not a boot-server trick: a class
is a named condition over the incoming packet (vendor class, user class,
client architecture, MAC OUI, ...) that can then carry its own parameters --
options, lease lifetimes, and the DHCPv4 fixed-header fields (next-server /
server-hostname / boot-file-name, see core/validation.py). Anything Kea can
match on can be expressed here, which is why this module contains no
knowledge of any particular client type.

Two things the UI deliberately does not infer:

  * whether an expression is valid -- Kea's expression language is larger than
    anything worth reimplementing, so the operator's text is written through
    verbatim and Kea's own parser judges it at "Test Config"/"Apply Changes";
  * which class a given client matches -- only Kea sees the real packets. The
    list page is the source of truth for what is configured, not for what will
    happen.

The one piece of structure enforced here is uniqueness: Kea refuses to load a
config with two client-classes sharing a name, so a collision is caught before
the write rather than surfacing as a daemon that will not start.
"""
from typing import Any, Dict, List, Optional, Tuple

from .validation import V4_BOOT_FIELDS, V6_BOOT_FIELDS, validate_option_data, _parse_option_rows

# Kea's own limit is not documented; this is a sanity bound on a hand-crafted
# POST, generous enough for any real class name.
MAX_CLASS_NAME = 128
# Upper bound on option rows accepted from one class form (see
# routes/system.py's identical reasoning for global options).
MAX_CLASS_OPTION_ROWS = 100
# Names beginning with this prefix belong to classes EZ-KEA maintains itself
# (currently the per-reservation iPXE classes in core/ipxe.py). They are not
# ordinary classes: the editor refuses the namespace so an operator cannot
# create, rename into, or edit one out from under its reservation.
RESERVED_CLASS_PREFIX = "ez-kea-"
# Option rows the form always renders so a new one can be typed in without
# JavaScript.
CLASS_OPTION_BLANK_ROWS = 2


def dhcp_key_for(version: str) -> str:
    """Config root key for a version string ("4"/"6")."""
    return "Dhcp6" if version == "6" else "Dhcp4"


def boot_fields_for_version(version: str) -> tuple:
    """Fixed-header fields a class may carry: DHCPv4 only (DHCPv6 has no
    BOOTP header -- see core/validation.py)."""
    return V4_BOOT_FIELDS if version == "4" else V6_BOOT_FIELDS


def get_client_classes(config: Dict[str, Any], dhcp_key: str) -> List[Dict[str, Any]]:
    """The scope's client-classes list, or an empty list when none exist.

    Also tolerates a config where the key exists but is not a list (hand-edited
    into something Kea would reject) rather than raising while rendering a
    page -- the syntax check is where that gets reported.
    """
    classes = config.get(dhcp_key, {}).get("client-classes")
    if not isinstance(classes, list):
        return []
    return [c for c in classes if isinstance(c, dict)]


def find_client_class(config: Dict[str, Any], dhcp_key: str, name: str) -> Optional[Dict[str, Any]]:
    """Locate one class by name (exact match, the way Kea compares them)."""
    for entry in get_client_classes(config, dhcp_key):
        if entry.get("name") == name:
            return entry
    return None


def set_client_classes(config: Dict[str, Any], dhcp_key: str, classes: List[Dict[str, Any]]) -> None:
    """Write the list back onto the scope, dropping the key entirely when it
    is empty -- an empty array is valid Kea, but an absent key is what a
    config that never had classes looks like, and it keeps diffs honest."""
    scope = config.setdefault(dhcp_key, {})
    if classes:
        scope["client-classes"] = classes
    else:
        scope.pop("client-classes", None)


def class_summary(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The handful of facts the list page needs about one class."""
    options = entry.get("option-data")
    options = options if isinstance(options, list) else []
    boot = {name: entry.get(name) for name in V4_BOOT_FIELDS if entry.get(name)}
    return {
        "name": entry.get("name", ""),
        "test": entry.get("test", ""),
        "boot": boot,
        "option_count": len(options),
        "options": options,
    }


def _validate_name(name: str, existing: Dict[str, Any], original: str, dhcp_key: str, errors: List[str]) -> bool:
    if not name:
        errors.append("Class name is required.")
        return False
    if len(name) > MAX_CLASS_NAME:
        errors.append(f"Class name must be at most {MAX_CLASS_NAME} characters.")
        return False
    # Kea's class names end up in expressions as bare words and in log lines;
    # control characters and angle brackets have no place there (same rule the
    # option fields already follow).
    if not validate_option_data(name):
        errors.append("Class name contains characters that are not allowed.")
        return False
    # Reserved for classes the app maintains from another page (see
    # core/ipxe.py); editing one here would silently break its binding.
    if name.startswith(RESERVED_CLASS_PREFIX):
        errors.append(
            f"Class names beginning with '{RESERVED_CLASS_PREFIX}' are reserved for "
            "classes EZ-KEA generates and maintains from another page."
        )
        return False
    if name != original and name in existing:
        errors.append(f"A client class named '{name}' already exists.")
        return False
    return True


def parse_class_form(form: Any, version: str, *, original: str = "",
                     existing_names: Optional[set] = None,
                     extra_options: Optional[Dict[str, str]] = None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """
    Parse a submitted client-class form into a class object plus a list of
    errors. Returns (None, errors) when anything is wrong, so callers never
    write a half-validated class.

    `original` is the name being edited (so renaming past its own name is not
    reported as a collision), and `existing_names` is every other class name on
    the scope. `extra_options` lets a caller merge in option rows it already
    parsed (the free-form table is parsed by the caller because it needs the
    ConfigAccessError-free request object, not this module).
    """
    errors: List[str] = []
    existing = existing_names or set()
    boot_fields = boot_fields_for_version(version)

    name = (form.get("class-name") or "").strip()
    if not _validate_name(name, existing, original, dhcp_key_for(version), errors):
        return None, errors

    entry: Dict[str, Any] = {"name": name}

    test = (form.get("class-test") or "").strip()
    if test:
        if not validate_option_data(test):
            errors.append("Class expression contains characters that are not allowed.")
        else:
            entry["test"] = test
    elif not original:
        # Kea accepts a test-less class (it exists to be assigned to clients by
        # name), but writing one by accident is far more likely than wanting
        # one, so a new class without a test is refused with an explanation.
        errors.append(
            "Class expression is required. A class with no expression can only be "
            "assigned to clients by name, which is rarely what is wanted."
        )

    for field in boot_fields:
        raw = form.get(field)
        if raw is None:
            continue
        value = raw.strip()
        if not value:
            continue
        if field == "next-server":
            from .validation import validate_ipv4_address
            if not validate_ipv4_address(value):
                errors.append("Next Server must be a single IPv4 address (e.g. 192.168.1.10).")
                continue
        elif not validate_option_data(value):
            errors.append(f"{field} contains characters that are not allowed.")
            continue
        entry[field] = value

    # Option rows: named pairs only, merged by name so editing a class does not
    # rewrite options this form knows nothing about. Removal is explicit
    # (validation._parse_option_rows explains why a cleared name cannot mean it).
    parsed_options, _row_order, option_errors = _parse_option_rows(
        form, "class-option", MAX_CLASS_OPTION_ROWS)
    # A class's options are rebuilt from the form (and the class is replaced
    # wholesale on save), so only the resulting dict matters here.
    deletions = {name for _i, name, removed in _row_order if removed}
    options: Dict[str, str] = dict(extra_options or {})
    options.update(parsed_options)
    errors.extend(option_errors)

    if options:
        entry["option-data"] = [{"name": n, "data": d} for n, d in options.items()]

    if deletions:
        # Carried on the parsed entry so the caller can apply them without a
        # second walk of the form. Underscore-prefixed: not a Kea field.
        entry["_removed-options"] = sorted(deletions)

    return (entry if not errors else None), errors


def upsert_client_class(config: Dict[str, Any], dhcp_key: str, entry: Dict[str, Any],
                        original: str = "") -> None:
    """
    Insert a new class at the end of the list, or replace the class named
    `original` in place (keeping its position, so the list does not reshuffle
    on every edit).
    """
    classes = get_client_classes(config, dhcp_key)
    if original:
        for index, existing in enumerate(classes):
            if existing.get("name") == original:
                classes[index] = entry
                break
        else:
            classes.append(entry)
    else:
        classes.append(entry)
    set_client_classes(config, dhcp_key, classes)


def remove_client_class(config: Dict[str, Any], dhcp_key: str, name: str) -> bool:
    """Delete a class by name. Returns True when something was removed."""
    classes = get_client_classes(config, dhcp_key)
    remaining = [c for c in classes if c.get("name") != name]
    removed = len(remaining) != len(classes)
    if removed:
        set_client_classes(config, dhcp_key, remaining)
    return removed
