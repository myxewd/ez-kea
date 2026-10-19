# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import ipaddress
from ipaddress import IPv4Network
import re
import warnings
import time
import csv
from typing import Any, Dict, List, Set, Tuple, Union

def classify_network_address(address: str) -> int:
    """
    Classify a network address string.
    Returns:
        1: Host IP with netmask
        2: Valid network or bare IP
        3: Invalid network address
    """
    try:
        network = ipaddress.ip_network(address, strict=False)
        # If address contains a host bit (e.g. 192.168.1.5/24), it's a host IP with prefix
        if '/' in address and ipaddress.ip_interface(address).ip != network.network_address:
            return 1  # Host IP with netmask — not a clean network address
        return 2  # Valid network
    except (ValueError, ipaddress.AddressValueError):
        try:
            ipaddress.ip_address(address)
            return 2  # Valid bare IP
        except:
            return 3  # Invalid

def sanitize_hostname(hostname: str) -> str:
    """Sanitize hostname by keeping only alphanumeric chars, hyphens, and dots."""
    # Keep only allowed chars: alphanumeric, hyphens, dots
    allowed_chars = re.compile(r'[^a-zA-Z0-9\-.]')
    return allowed_chars.sub('', hostname)

_CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')

def validate_option_data(value: str) -> bool:
    """
    Reject DHCP option-data (and option-name) values containing control
    characters or angle brackets. This is DHCP option data serialized into a
    config file real DHCP clients parse, not free-form HTML — control chars
    can break the Kea config/log format and angle brackets have no legitimate
    use here.
    """
    if not isinstance(value, str) or not value:
        return False
    if _CONTROL_CHAR_RE.search(value):
        return False
    if '<' in value or '>' in value:
        return False
    return True

# ── Kea scope-level "boot" parameters ────────────────────────────────────────
#
# These are top-level keys on a Kea scope object (Dhcp4, a shared-network, a
# subnet4, a reservation) that are carried in the fixed BOOTP header fields of
# the packet rather than in the DHCP options area:
#
#   next-server     -> siaddr   (the host to fetch the boot file from)
#   server-hostname -> sname
#   boot-file-name  -> file
#
# They are NOT options and cannot be expressed through option-data: the BOOTP
# header predates the options field, and no option code maps onto it. That is
# why Kea exposes them as ordinary scope keys alongside valid-lifetime and
# option-data, rather than as option names.
#
# next-server and server-hostname share their meaning with options 66 and 67,
# but are not the same thing as those options -- 66/67 are written into the
# options field, these write the BOOTP header. Kea exposes both; clients
# differ in which they read, so a real config frequently sets both.
#
# Only "next-server" has a type Kea constrains (an IPv4 address -- Kea rejects
# a hostname there). The other two are free-form strings that may contain a
# URL, so they are validated only against control characters, like option-data.
V4_BOOT_FIELDS = ("next-server", "server-hostname", "boot-file-name")
# DHCPv6 has no BOOTP header at all, so none of the v4 names apply there.
V6_BOOT_FIELDS: tuple = ()


def boot_fields_for(dhcp_key: str) -> tuple:
    """The Kea fixed-header scope parameters valid for a config root key
    ("Dhcp4" / "Dhcp6"). Anything not listed is an option."""
    return V4_BOOT_FIELDS if dhcp_key == "Dhcp4" else V6_BOOT_FIELDS


def validate_ip_or_hostname(value: str) -> bool:
    """
    True when a value is usable as a server address: a literal IPv4 address,
    or a plausible hostname/FQDN.

    Deliberately permissive about hostnames -- Kea itself is the authority on
    whether a given boot server is reachable, and this only rejects obvious
    typos and control-character smuggling, not anything Kea would accept.
    """
    if not validate_option_data(value):
        return False
    if validate_ipv4_address(value):
        return True
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", value))


def validate_boot_field(name: str, value: str) -> bool:
    """Type-check one Kea fixed-header scope parameter. Returns False for a
    value that would make the resulting config fail Kea's own syntax check."""
    if name == "next-server":
        return validate_ipv4_address(value)
    return validate_option_data(value)


def parse_scope_params(form: Any, valid_fields: tuple) -> "tuple[Dict[str, str], List[str]]":
    """
    Read `valid_fields` out of a submitted form for one Kea scope object.

    Returns (params, errors) where params maps field name -> cleaned value for
    every field that was present and non-blank. A field present but blank is
    omitted from params: callers read that as "remove the key", which is how
    an operator clears a value they previously set.

    A field absent from the form entirely is not touched at all, so a form
    that never rendered a given field can never clear it.
    """
    params: Dict[str, str] = {}
    errors: List[str] = []
    for name in valid_fields:
        raw = form.get(name)
        if raw is None:
            continue
        value = raw.strip()
        if not value:
            continue
        if not validate_boot_field(name, value):
            if name == "next-server":
                errors.append(f"{name} must be a single IPv4 address (e.g. 192.168.1.10).")
            else:
                errors.append(f"{name} contains characters that are not allowed.")
            continue
        params[name] = value
    return params, errors


def apply_scope_params(scope: Dict[str, Any], params: Dict[str, str], valid_fields: tuple) -> None:
    """
    Write `params` onto a Kea scope object, deleting any listed field that was
    intentionally left blank. Keys outside `valid_fields` are left exactly as
    they were.
    """
    for name in valid_fields:
        if name in params:
            scope[name] = params[name]
        else:
            scope.pop(name, None)


def update_option_data(scope: Dict[str, Any], updates: Dict[str, str],
                      managed: Any = None, deletions: Any = None,
                      row_order: Any = None) -> None:
    """
    Merge named options into a scope's option-data in place.

    Replaces the old "rebuild option-data from the one field this form knows
    about" pattern, which silently deleted every other option on the scope
    (hand-written root-path, vendor suboptions, ...) the first time someone
    saved an unrelated edit.

    `updates` maps option name -> data. `deletions` names options the operator
    explicitly asked to remove -- this has to be said out loud, because "the
    name is absent from the submission" cannot mean "delete": the table only
    renders options it already knows about, so an option the form never showed
    would then be silently dropped.

    `managed` names options this form owns and that must be dropped when the
    form no longer supplies them (used by the global-settings presets, whose
    blank field does mean "remove"). Pass None to only ever add/update.

    `row_order` is the option tables' row order, as
    [(config index, name, removed), ...] sorted by index. Supplying it switches
    to a positional merge: the submitted rows are written back in the order the
    operator saw them, so an option that was removed and then put back (the ✕
    clicked twice) returns to its own place rather than jumping to the end --
    which is what rebuilding the list from a name-keyed submission would do,
    since by then the option is no longer in the config to be found.
    """
    existing = scope.get("option-data")
    if not isinstance(existing, list):
        existing = []
    managed_set = set(managed) if managed is not None else set()
    deleted_set = set(deletions or ())

    if row_order is not None:
        # Positional merge, for the option tables. Each submitted row knows the
        # index it was rendered at, so this rebuilds the list in the order the
        # operator saw -- new rows first (they were rendered after the existing
        # ones), then anything the config holds that the form never showed.
        by_name = {name: entry for name, entry in
                   ((e.get("name"), e) for e in existing if isinstance(e, dict))}

        result: List[Any] = []
        placed: set = set()
        for _index, name, removed in sorted(row_order, key=lambda r: r[0]):
            if removed:
                # Deleting wins over keeping: a name can only appear as both if
                # a stale form submitted one row as removed and another present.
                continue
            if not name:
                continue                       # an untouched blank seed row
            if name in placed:
                continue                       # legacy duplicate: first wins
            placed.add(name)
            entry = dict(by_name[name]) if name in by_name else {}
            entry["name"] = name
            entry["data"] = updates.get(name, entry.get("data", ""))
            result.append(entry)

        # Options the config holds that the form never rendered at all. A name
        # that *did* appear (present or removed) has already been decided above,
        # which is what makes a removal stick. The exceptions: entries that are
        # not option dicts (put back untouched), and a form-owned preset the
        # form chose not to supply (dropped).
        rendered_names = {name for _i, name, _r in row_order if name}
        for entry in existing:
            if not isinstance(entry, dict):
                result.append(entry)
                continue
            name = entry.get("name")
            if name in placed or name in rendered_names or name in deleted_set:
                continue
            if name in managed_set and name not in updates:
                continue
            result.append(entry)

        if result:
            scope["option-data"] = result
        else:
            scope.pop("option-data", None)
        return

    merged: List[Dict[str, Any]] = []
    seen = set()
    for entry in existing:
        if not isinstance(entry, dict):
            merged.append(entry)
            continue
        name = entry.get("name")
        if name in deleted_set:
            # Explicitly removed by the operator.
            continue
        if name in updates:
            # Newly supplied or edited: take the submitted value, and drop any
            # legacy duplicate of the same name.
            if name in seen:
                continue
            seen.add(name)
            entry = dict(entry)
            entry["data"] = updates[name]
        elif name in managed_set:
            # A form-owned option whose field was left blank: remove it.
            continue
        merged.append(entry)

    for name, data in updates.items():
        if name in seen or name in deleted_set:
            continue
        # Reuse the existing entry if there is one (any extra keys Kea
        # accepts, e.g. always-send, are already carried over above); append
        # only when this option isn't on the scope yet.
        if not any(isinstance(e, dict) and e.get("name") == name for e in merged):
            merged.append({"name": name, "data": data})

    if merged:
        scope["option-data"] = merged
    else:
        scope.pop("option-data", None)


def _parse_option_rows(form: Any, prefix: str, max_rows: int) -> Tuple[Dict[str, str], set, list]:
    """
    Read one option table off a submitted form.

    Every option table (global, client class, per-reservation) submits the same
    three fields per row: `<prefix>-name-<i>`, `<prefix>-data-<i>` and
    `<prefix>-deleted-<i>`. The delete field is what makes removal expressible:
    a row whose name was simply cleared cannot mean "delete", because the table
    only renders options it already knows about.

    Returns (updates, row_order, errors), where row_order is
    [(form index, name, removed), ...] in form order -- the positional merge in
    update_option_data() needs the order, not just the set of names.
    """
    updates: Dict[str, str] = {}
    row_order: List[Tuple[int, str, bool]] = []
    errors: list = []
    for index in range(max_rows):
        name = (form.get(f"{prefix}-name-{index}") or "").strip()
        data = (form.get(f"{prefix}-data-{index}") or "").strip()
        removed = bool(form.get(f"{prefix}-deleted-{index}"))
        if removed:
            if name:
                row_order.append((index, name, True))
            continue
        if not name and not data:
            continue
        if not name:
            errors.append("An option row is missing its name.")
            continue
        if not data:
            errors.append(f"Option '{name}' is missing its data.")
            continue
        if not validate_option_data(name) or not validate_option_data(data):
            errors.append(f"Option '{name}' contains characters that are not allowed.")
            continue
        if name in updates:
            errors.append(f"Option '{name}' is listed more than once.")
            continue
        updates[name] = data
        row_order.append((index, name, False))
    for index, name, removed in row_order:
        if removed:
            updates.pop(name, None)
    return updates, row_order, errors


def validate_mac_address(mac_address: str) -> bool:
    """Check if the provided string is a valid MAC address format."""
    pattern = r"^([0-9A-Fa-f]{2}([:-])){5}[0-9A-Fa-f]{2}$"
    try:
        warnings.filterwarnings("ignore", category=FutureWarning)
        return bool(re.match(pattern, mac_address))
    except ModuleNotFoundError:
        return bool(re.match(pattern, mac_address))

_DUID_OCTET_RE = re.compile(r'^[0-9A-Fa-f]{2}$')
_DUID_HEX_RE = re.compile(r'^[0-9A-Fa-f]+$')

def validate_duid(duid: str) -> bool:
    """
    Check if the provided string is a plausible Kea DUID: colon- or
    hyphen-separated hex octets (e.g. "00:03:00:01:aa:bb:cc:dd:ee:ff"), or
    one continuous hex string. RFC 8415 defines specific DUID-LLT/EN/LL/UUID
    layouts, but Kea also accepts an arbitrary "flexible" DUID, so this
    validates the general octet-string shape (an even number of hex digits,
    long enough to hold at least the 2-byte type field) rather than one
    specific DUID type.
    """
    if not isinstance(duid, str) or not duid:
        return False
    if _CONTROL_CHAR_RE.search(duid) or '<' in duid or '>' in duid:
        return False

    has_colon = ':' in duid
    has_hyphen = '-' in duid
    if has_colon and has_hyphen:
        return False  # Don't accept a mix of separators.

    if has_colon or has_hyphen:
        octets = duid.split(':' if has_colon else '-')
        if any(not _DUID_OCTET_RE.match(o) for o in octets):
            return False
        octet_count = len(octets)
    else:
        if len(duid) % 2 != 0 or not _DUID_HEX_RE.match(duid):
            return False
        octet_count = len(duid) // 2

    return 2 <= octet_count <= 130

def validate_ipv4_address(ip_address: str) -> bool:
    """Check if the provided string is a syntactically valid IPv4 address."""
    try:
        ipaddress.IPv4Address(ip_address)
        return True
    except (ValueError, TypeError):
        return False

def validate_ip_range(subnet: str, range_start: str, range_end: str) -> bool:
    """Validate that start and end IPs are within the subnet and start < end."""
    try:
        network = ipaddress.ip_network(subnet, strict=False)
        start_ip = ipaddress.ip_address(range_start)
        end_ip = ipaddress.ip_address(range_end)
        if start_ip not in network or end_ip not in network:
            return False
        return start_ip < end_ip
    except (ValueError, ipaddress.AddressValueError):
        return False

def has_overlap(new_subnet: str, data: Dict[str, Any], prefix: str = "Dhcp4") -> Union[str, bool]:
    """
    Check if a new subnet overlaps with any existing subnets in the config.
    Checks both standalone top-level subnets (Dhcp4.subnet4[]/Dhcp6.subnet6[])
    and subnets nested inside shared-networks.
    Returns the overlapping subnet string if it exists, otherwise False.
    """
    try:
        new_network = ipaddress.ip_network(new_subnet)
    except ValueError:
         return False

    subnet_key = "subnet4" if prefix == "Dhcp4" else "subnet6"

    if prefix in data:
        # Standalone subnets declared directly under Dhcp4/Dhcp6
        for existing_subnet in data[prefix].get(subnet_key, []):
            try:
                existing_network = ipaddress.ip_network(existing_subnet["subnet"])
                if existing_network.overlaps(new_network):
                    return existing_network.exploded
            except ValueError:
                continue

        # Subnets nested inside shared-networks
        for shared_network in data[prefix].get("shared-networks", []):
            for existing_subnet in shared_network.get(subnet_key, []):
                try:
                    existing_network = ipaddress.ip_network(existing_subnet["subnet"])
                    if existing_network.overlaps(new_network):
                        return existing_network.exploded
                except ValueError:
                     continue
    return False

def get_active_leases(leases_file: str) -> List[Dict[str, Any]]:
    """Return a list of active DHCP leases parsed from the Kea leases CSV."""
    active_leases = []
    seen_ips = set()
    current_time = int(time.time())
    
    try:
        with open(leases_file, "r") as file:
            reader = csv.DictReader(file)
            for row in reader:
                try:
                    expiration_time = int(row.get("expire", 0))
                except (ValueError, TypeError):
                    # Malformed/corrupted expire field (e.g. from a truncated
                    # disk-full write) — skip this row instead of crashing
                    # the whole leases list.
                    continue
                address = row.get("address")
                if expiration_time > current_time and address not in seen_ips:
                    seen_ips.add(address)
                    active_leases.append({
                        "ip_address": address,
                        "mac_address": row.get("hwaddr"),
                        # Hostnames here come straight from whatever the DHCP client
                        # sent in its hostname option — sanitize the same way a
                        # human-typed hostname is sanitized.
                        "hostname": sanitize_hostname(row.get("hostname") or ""),
                        "expire": expiration_time
                    })
    except FileNotFoundError:
        pass
    return active_leases

# Kea's lease6 memfile CSV "lease_type" column: 0=IA_NA, 1=IA_TA, 2=IA_PD.
_LEASE6_TYPE_LABELS = {"0": "IA_NA", "1": "IA_TA", "2": "IA_PD"}

def get_active_leases6(leases_file: str) -> List[Dict[str, Any]]:
    """
    Return a list of active DHCPv6 leases parsed from Kea's lease6 CSV.

    Kea's lease6 schema differs from lease4's (duid instead of hwaddr,
    subnet_id/pref_lifetime/lease_type/iaid/prefix_len columns that lease4
    doesn't have) so this is a separate parser rather than a version branch
    inside get_active_leases(). lease_type distinguishes IA_NA/IA_TA (a plain
    host address, in `address`) from IA_PD (a delegated prefix, whose first
    address is in `address` with its length in `prefix_len`).
    """
    active_leases = []
    seen_addresses = set()
    current_time = int(time.time())

    try:
        with open(leases_file, "r") as file:
            reader = csv.DictReader(file)
            for row in reader:
                try:
                    expiration_time = int(row.get("expire", 0))
                except (ValueError, TypeError):
                    # Malformed/corrupted expire field — skip this row
                    # instead of crashing the whole leases list.
                    continue
                address = row.get("address")
                if expiration_time > current_time and address not in seen_addresses:
                    seen_addresses.add(address)
                    lease_type_label = _LEASE6_TYPE_LABELS.get(row.get("lease_type", "0"), "IA_NA")
                    active_leases.append({
                        "address": address,
                        "prefix_len": row.get("prefix_len") if lease_type_label == "IA_PD" else None,
                        "duid": row.get("duid"),
                        "hostname": sanitize_hostname(row.get("hostname") or ""),
                        "expire": expiration_time,
                        "lease_type": lease_type_label,
                    })
    except FileNotFoundError:
        pass
    return active_leases

def _iter_subnet4_lists(config_data: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    """Return every subnet4 list in the config: the standalone top-level list
    (Dhcp4.subnet4[]) plus each shared-network's own subnet4[] list."""
    dhcp4 = config_data.get("Dhcp4", {})
    lists = []
    if "subnet4" in dhcp4:
        lists.append(dhcp4["subnet4"])
    for shared_network in dhcp4.get("shared-networks", []):
        if "subnet4" in shared_network:
            lists.append(shared_network["subnet4"])
    return lists

def return_available_ips(config_data: Dict[str, Any], leases_file: str) -> Dict[str, List[str]]:
    """Calculate and return available IP addresses per subnet.
    Covers both standalone subnets (Dhcp4.subnet4[]) and subnets nested
    inside shared-networks."""
    subnet_data: Dict[str, List[str]] = {}
    reserved_ips = set()
    active_leases_ips = set(lease['ip_address'] for lease in get_active_leases(leases_file))

    if "Dhcp4" not in config_data:
         return subnet_data

    subnet4_lists = _iter_subnet4_lists(config_data)

    for subnet4_list in subnet4_lists:
        for subnet in subnet4_list:
            if "reservations" in subnet:
                for reservation in subnet["reservations"]:
                    if "ip-address" in reservation:
                        reserved_ips.add(reservation["ip-address"])

    for subnet4_list in subnet4_lists:
        for subnet in subnet4_list:
            router_address = None
            if "option-data" in subnet:
                 for option in subnet["option-data"]:
                      if option.get("name") == "routers":
                           router_address = option.get("data")
                           break

            available_ips = []
            try:
                network = ipaddress.ip_network(subnet["subnet"])
                for ip in network.hosts():
                    ip_str = str(ip)
                    if (ip_str not in reserved_ips
                        and ip_str not in active_leases_ips
                        and ip_str != router_address):
                        available_ips.append(ip_str)
                subnet_data[subnet["subnet"]] = available_ips
            except ValueError:
                 continue
    return subnet_data

def human_readable_time(seconds: int) -> str:
    """Convert an integer of seconds into a human readable description."""
    periods = [
        ("week", 60 * 60 * 24 * 7),
        ("day", 60 * 60 * 24),
        ("hour", 60 * 60),
        ("minute", 60),
    ]
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, remainder = divmod(seconds, period_seconds)
            return f"{period_value} {period_name}{'s' if period_value > 1 else ''}"
    return f"{seconds} seconds"

def unix_to_human_readable(timestamp: Union[int, str, float]) -> str:
    """Convert a UNIX timestamp to a localized human-readable string."""
    try:
        timestamp = int(timestamp)
    except ValueError:
        return "Invalid Timestamp"
    import datetime
    return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %I:%M:%S %p")
