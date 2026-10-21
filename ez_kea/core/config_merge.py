# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/core/config_merge.py

Replay EZ-KEA's own edits onto a configuration somebody else may have changed.

A remote endpoint's configuration is kept as a local mirror: pulled with
config-get, edited by the ordinary pages, pushed back with config-set. But
config-set replaces the daemon's *entire* running configuration, and the mirror
is only as fresh as the last pull. Pushing it wholesale therefore silently
reverts anything that changed on the daemon in between -- the operator adding a
hooks-libraries entry is the case that prompted this module: Apply unloaded the
lease-commands hook that had just been enabled, and the Leases page started
failing with "'lease4-get-all' command not supported".

So Apply does not push the mirror. It computes what EZ-KEA itself changed --
diff(pulled, mirror) -- and replays exactly that delta onto the document the
daemon reports *now*: a three-way merge whose `base` is the copy taken by the
last successful pull. Keys EZ-KEA did not touch keep whatever the daemon says,
including the daemon's subsequent changes.

A setting changed on both sides, differently, is a conflict: it is reported by
path and nothing is pushed, so the operator's unapplied edit stays in the
mirror and the daemon keeps running what it had.
"""
import copy
from typing import Any, Dict, List, Tuple

# Sentinel for "this key/element does not exist on this side". A real config can
# contain null, so None cannot mean absence.
_MISSING = object()

# Fields that identify an element inside a list Kea treats as a set: subnets by
# id, shared networks and client classes by name, reservations by their
# identifier, options by code/name. The first field the element actually
# carries is used, so one list can mix (a reservation list may key one entry by
# hw-address and another by duid).
#
# "pool" is deliberately NOT here: a pool's string *is* its identity and is
# also what an edit changes, so keying by it would turn "the operator narrowed
# this range" into "one range was removed, another added" on each side and
# merge both ranges into the subnet. A pool list is therefore all-or-nothing:
# changed on one side it is taken, changed on both sides it is a conflict.
_KEY_FIELDS = ("id", "name", "subnet", "client-id", "hw-address", "duid", "ip-address", "code")

# The fields that make up a key for option-data and similar space-qualified
# elements: "dns-servers" in the dhcp4 space is not the same option as
# "dns-servers" in dhcp6.
_SCOPED_KEY_FIELDS = ("name", "code")


def merge_documents(base: Dict[str, Any], local: Dict[str, Any],
                    remote: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Three-way merge of Kea configuration documents.

    `base` is the document as the last successful pull saw it, `local` is the
    mirror after EZ-KEA's edits, and `remote` is what the daemon reports now.
    Returns (merged, conflicts): `merged` is base+(local-base) applied to
    `remote`, and `conflicts` lists the dotted paths both sides changed
    differently (empty means the merge is clean).
    """
    conflicts: List[str] = []
    merged = _merge(base, local, remote, "", conflicts)
    # Preserve discovery order while dropping duplicates.
    conflicts = list(dict.fromkeys(conflicts))
    if merged is _MISSING:
        return {}, conflicts
    return merged, conflicts


def _same(a: Any, b: Any) -> bool:
    """Equality that treats two absences as equal and absence as distinct from None."""
    if a is _MISSING or b is _MISSING:
        return a is b
    return a == b


def _copy(value: Any) -> Any:
    return _MISSING if value is _MISSING else copy.deepcopy(value)


def _merge(base: Any, local: Any, remote: Any, path: str, conflicts: List[str]) -> Any:
    """Merge one node. Returns _MISSING when the node is absent afterwards."""
    # Both sides agree (including "both deleted"): nothing to decide.
    if _same(local, remote):
        return _copy(remote)
    # EZ-KEA did not change it: the daemon's value (or deletion) wins.
    if _same(base, local):
        return _copy(remote)
    # The daemon did not change it: EZ-KEA's value (or deletion) wins.
    if _same(base, remote):
        return _copy(local)

    # Both sides changed it, differently.
    if base is _MISSING or local is _MISSING or remote is _MISSING:
        # Added on both sides with different content, or deleted on one side
        # and edited on the other. Neither is safe to guess.
        conflicts.append(path or "the whole document")
        return _copy(local)

    if isinstance(base, dict) and isinstance(local, dict) and isinstance(remote, dict):
        return _merge_dicts(base, local, remote, path, conflicts)
    if isinstance(base, list) and isinstance(local, list) and isinstance(remote, list):
        return _merge_lists(base, local, remote, path, conflicts)

    conflicts.append(path or "the whole document")
    return _copy(local)


def _merge_dicts(base: Dict[str, Any], local: Dict[str, Any], remote: Dict[str, Any],
                 path: str, conflicts: List[str]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    # The daemon's key order first, then anything only EZ-KEA added, so the
    # pushed document reads like the daemon's own.
    keys = list(remote.keys())
    keys.extend(key for key in local.keys() if key not in remote)
    for key in keys:
        value = _merge(
            base.get(key, _MISSING),
            local.get(key, _MISSING),
            remote.get(key, _MISSING),
            _join(path, key),
            conflicts,
        )
        if value is not _MISSING:
            merged[key] = value
    return merged


def _element_key(element: Any) -> Any:
    """A stable identity for one list element, or None when it has none."""
    if not isinstance(element, dict):
        return None
    for field in _KEY_FIELDS:
        value = element.get(field)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            if field in _SCOPED_KEY_FIELDS:
                return (field, element.get("space"), value)
            return (field, value)
    return None


def _key_map(elements: List[Any]) -> Tuple[bool, Dict[Any, Any]]:
    """
    {key: element} for a list, or (False, {}) when it cannot be keyed.

    A list is only merged element-wise when *every* element has a unique
    identity; anything else (a list of bare strings, duplicate ids) falls back
    to an all-or-nothing comparison, because guessing which duplicate is which
    would corrupt the configuration rather than merge it.
    """
    mapping: Dict[Any, Any] = {}
    for element in elements:
        key = _element_key(element)
        if key is None or key in mapping:
            return False, {}
        mapping[key] = element
    return True, mapping


def _merge_lists(base: List[Any], local: List[Any], remote: List[Any],
                 path: str, conflicts: List[str]) -> List[Any]:
    keyed = True
    maps = []
    for elements in (base, local, remote):
        ok, mapping = _key_map(elements)
        keyed = keyed and ok
        maps.append(mapping)
    if not keyed:
        conflicts.append(path or "the whole document")
        return _copy(local)

    base_map, local_map, remote_map = maps
    merged: List[Any] = []
    # Start from the daemon's list: its order survives, and elements only it has
    # stay exactly where it put them.
    for element in remote:
        key = _element_key(element)
        value = _merge(
            base_map.get(key, _MISSING),
            local_map.get(key, _MISSING),
            element,
            _element_path(path, key),
            conflicts,
        )
        if value is not _MISSING:
            merged.append(value)
    # Then the elements EZ-KEA added, which the daemon does not know yet.
    for element in local:
        key = _element_key(element)
        if key in remote_map:
            continue
        value = _merge(
            base_map.get(key, _MISSING),
            element,
            _MISSING,
            _element_path(path, key),
            conflicts,
        )
        if value is not _MISSING:
            merged.append(value)
    return merged


def _join(path: str, key: str) -> str:
    return "%s.%s" % (path, key) if path else str(key)


def _element_path(path: str, key: Any) -> str:
    """A readable path for one list element, e.g. Dhcp4.subnet4[id=3]."""
    label = "%s=%s" % (key[0], key[-1])
    return "%s[%s]" % (path, label) if path else "[%s]" % label
