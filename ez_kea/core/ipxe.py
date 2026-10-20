# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/core/ipxe.py

The per-reservation iPXE quick configuration: one checkbox and one script
address on a DHCPv4 reservation, from which EZ-KEA maintains a Kea
client-class the operator never edits by hand.

Why a client-class instead of the reservation's own boot-file-name? The boot
chain has two clients wearing the same MAC. The NIC firmware asks first and
needs a bootable iPXE binary; only once iPXE is running does the machine ask
again, announcing itself in option 77, and only then is a *script* a runnable
answer. A reservation-level boot-file-name is sent on every reply to that
host, so it would hand the firmware a text file. A class whose expression
requires both this MAC and the iPXE user class can tell the two stages apart:

    hexstring(pkt4.mac, ':') == '52:54:00:12:34:56'
      and substring(option[77].hex, 0, 4) == 'iPXE'

Why the front of the class list? Kea assigns matching classes in configuration
order and uses the first class that supplies a given piece of DHCPv4 field
information (see the Kea ARM, "Class Priority"). A general "send every iPXE
client this script" class is exactly the class this per-host one has to
outrank, so the generated class is kept first. It is deliberately *not* added
to the reservation's own client-classes: a class assigned by a host
reservation is a member regardless of its expression, which would hand the
iPXE script to the firmware again.

The class is bound to its reservation by name
(ez-kea-ipxe-<mac hex>-<subnet slug>), so it is found, updated and removed
from that reservation's lifecycle alone. routes/client_classes.py refuses to
edit or delete it while the reservation exists; deleting the reservation (or
the subnet holding it) deletes the class with it.
"""
import re
from typing import Any, Dict, List, Optional

from . import client_class as cc
from .validation import validate_mac_address, validate_option_data

# The generated classes live under client_class.RESERVED_CLASS_PREFIX so the
# ordinary editor refuses the namespace; this is the iPXE slice of it.
MANAGED_PREFIX = cc.RESERVED_CLASS_PREFIX + "ipxe-"

# A boot-file-name is a TFTP filename or a URL. This bounds it to what a client
# can actually fetch -- no spaces, quotes, backslashes or control characters --
# while leaving every URL shape and relative or absolute path alone.
MAX_SCRIPT_LENGTH = 512
_SCRIPT_RE = re.compile(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")

# What a loaded iPXE says in DHCPv4 option 77 (the user class). Verified
# against real iPXE, and what the class editor's own hint recommends.
IPXE_USER_CLASS = "substring(option[77].hex, 0, 4) == 'iPXE'"


def normalize_mac(mac: Any) -> str:
    """The MAC as 12 lowercase hex digits, or "" when it is not a MAC.

    Callers treat "" as "no such host": the reservation routes validate the
    MAC before they get here, but a hand-edited config (or a crafted POST on a
    delete path) must not raise through a page render.
    """
    if not isinstance(mac, str) or not validate_mac_address(mac):
        return ""
    return re.sub(r"[^0-9A-Fa-f]", "", mac).lower()


def mac_colon(mac: Any) -> str:
    """Lowercase colon-separated MAC, the spelling hexstring(pkt4.mac, ':')
    produces, or "" when the input is not a MAC."""
    hexmac = normalize_mac(mac)
    if not hexmac:
        return ""
    return ":".join(hexmac[i:i + 2] for i in range(0, 12, 2))


def subnet_slug(subnet: Any) -> str:
    """A class-name-safe rendering of a subnet ("10.77.0.0/24" ->
    "10-77-0-0-24"). Only part of the generated name; the real subnet string is
    recovered from the reservation that owns the class."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(subnet or "").lower()).strip("-")
    return slug or "subnet"


def managed_class_name(mac: Any, subnet: Any) -> str:
    """The name of the class belonging to (mac, subnet), or "" when the MAC is
    unusable. Deterministic, so the class is found from the reservation without
    storing a separate mapping anywhere."""
    hexmac = normalize_mac(mac)
    if not hexmac:
        return ""
    return MANAGED_PREFIX + hexmac + "-" + subnet_slug(subnet)


def is_managed_name(name: Any) -> bool:
    """True for a name in the generated namespace. Checked by the client-class
    editor (lock), by the reservation lifecycle (find/remove) and by the list
    page (badge). A malformed name still counts: it is not an ordinary class.
    """
    return isinstance(name, str) and name.startswith(MANAGED_PREFIX)


def managed_owner(name: Any) -> Optional[Dict[str, str]]:
    """The (mac, subnet slug) encoded in a generated name, or None.

    The slug is not reversible into a subnet string; callers that need the
    real one use owners(), which reads it from the reservation.
    """
    if not is_managed_name(name):
        return None
    payload = name[len(MANAGED_PREFIX):]
    hexmac, sep, slug = payload.partition("-")
    if not sep or not slug or not re.fullmatch(r"[0-9a-f]{12}", hexmac):
        return None
    # Spelled out rather than through mac_colon(): the payload is 12 bare hex
    # digits, which validate_mac_address() (rightly) does not accept as a MAC.
    mac = ":".join(hexmac[i:i + 2] for i in range(0, 12, 2))
    return {"mac": mac, "slug": slug}


def test_expression(mac: Any) -> str:
    """The class expression: this host, and only once it is running iPXE."""
    return "hexstring(pkt4.mac, ':') == '" + mac_colon(mac) + "' and " + IPXE_USER_CLASS


def build_class(mac: Any, subnet: Any, script: str) -> Dict[str, Any]:
    """The class object for one reservation's iPXE script."""
    return {
        "name": managed_class_name(mac, subnet),
        "test": test_expression(mac),
        "boot-file-name": script,
    }


def validate_script(script: str) -> List[str]:
    """Why a submitted script address cannot be used, or an empty list."""
    if not script:
        return ["An iPXE script address is required when 'Boot with iPXE' is selected."]
    if len(script) > MAX_SCRIPT_LENGTH:
        return ["iPXE script address must be at most " + str(MAX_SCRIPT_LENGTH) + " characters."]
    if not validate_option_data(script) or not _SCRIPT_RE.fullmatch(script):
        return [
            "iPXE script address contains characters that are not allowed. "
            "Use a URL or a path such as http://boot.example.com/node-1.ipxe."
        ]
    return []


def selection_errors(form: Any) -> List[str]:
    """Validate the reservation form's iPXE fields. An unchecked box never
    makes the (possibly stale) script field an error."""
    if form.get("boot-with-ipxe") != "on":
        return []
    return validate_script((form.get("ipxe-script") or "").strip())


def _iter_reservations(config: Dict[str, Any], dhcp_key: str):
    """Yield (subnet string, shared-network name or None, reservation object)
    for every DHCPv4 reservation, standalone and shared-network alike."""
    scope = config.get(dhcp_key)
    if not isinstance(scope, dict):
        return
    for subnet in scope.get("subnet4") or []:
        if not isinstance(subnet, dict):
            continue
        for reservation in subnet.get("reservations") or []:
            if isinstance(reservation, dict):
                yield subnet.get("subnet"), None, reservation
    for network in scope.get("shared-networks") or []:
        if not isinstance(network, dict):
            continue
        for subnet in network.get("subnet4") or []:
            if not isinstance(subnet, dict):
                continue
            for reservation in subnet.get("reservations") or []:
                if isinstance(reservation, dict):
                    yield subnet.get("subnet"), network.get("name"), reservation


def owners(config: Dict[str, Any], dhcp_key: str) -> Dict[str, Dict[str, str]]:
    """Generated class name -> the reservation that owns it.

    Read by the client-class list page: a class in the namespace with no entry
    here is orphaned (its reservation was removed by hand, e.g. through the
    raw configuration editor).
    """
    result: Dict[str, Dict[str, str]] = {}
    for subnet, shared_network_name, reservation in _iter_reservations(config, dhcp_key):
        name = managed_class_name(reservation.get("hw-address"), subnet)
        if not name:
            continue
        result[name] = {
            "subnet": subnet or "",
            "hw-address": reservation.get("hw-address") or "",
            "hostname": reservation.get("hostname") or "",
            "shared-network-name": shared_network_name or "",
        }
    return result


def _strip_host_assignment(config: Dict[str, Any], dhcp_key: str, name: str) -> None:
    """Remove a generated class from every reservation's client-classes.

    The generated class must only ever match by expression (see the module
    docstring). A hand-edited config that also assigns it to the host would
    make it a member regardless of the test, so the write paths clean that up
    rather than leave the firmware receiving an iPXE script.
    """
    for _subnet, _shared, reservation in _iter_reservations(config, dhcp_key):
        classes = reservation.get("client-classes")
        if isinstance(classes, list) and name in classes:
            remaining = [existing for existing in classes if existing != name]
            if remaining:
                reservation["client-classes"] = remaining
            else:
                reservation.pop("client-classes", None)


def apply_script(config: Dict[str, Any], dhcp_key: str, subnet: Any,
                 mac: Any, script: str) -> str:
    """Create or update the class for (mac, subnet) and put it first.

    Returns the class name, or "" when the MAC was not usable. The class list is
    rebuilt in place (an existing class is moved to the front), so its position
    keeps it ahead of any broader iPXE class.
    """
    name = managed_class_name(mac, subnet)
    if not name:
        return ""
    entry = build_class(mac, subnet, script)
    classes = [existing for existing in cc.get_client_classes(config, dhcp_key)
               if existing.get("name") != name]
    cc.set_client_classes(config, dhcp_key, [entry] + classes)
    _strip_host_assignment(config, dhcp_key, name)
    return name


def remove_script(config: Dict[str, Any], dhcp_key: str, subnet: Any, mac: Any) -> bool:
    """Delete the class for (mac, subnet), if present, and any host assignment
    of it. Returns whether a class was removed."""
    name = managed_class_name(mac, subnet)
    if not name:
        return False
    removed = cc.remove_client_class(config, dhcp_key, name)
    _strip_host_assignment(config, dhcp_key, name)
    return removed


def script_for(config: Dict[str, Any], dhcp_key: str, subnet: Any, mac: Any) -> str:
    """The script address already configured for (mac, subnet), or ""."""
    name = managed_class_name(mac, subnet)
    if not name:
        return ""
    entry = cc.find_client_class(config, dhcp_key, name)
    if entry is None:
        return ""
    script = entry.get("boot-file-name")
    return script if isinstance(script, str) else ""


def managed_classes(config: Dict[str, Any], dhcp_key: str) -> List[Dict[str, Any]]:
    """Every generated class in the config, in configuration (priority) order."""
    return [entry for entry in cc.get_client_classes(config, dhcp_key)
            if is_managed_name(entry.get("name"))]


def script_index(config: Dict[str, Any], dhcp_key: str) -> Dict[str, str]:
    """Generated class name -> its boot-file-name, for the search index.

    core/state_index.py builds this once per config parse instead of walking
    the class list for every reservation row.
    """
    index: Dict[str, str] = {}
    for entry in managed_classes(config, dhcp_key):
        script = entry.get("boot-file-name")
        if isinstance(script, str) and script:
            index[entry.get("name")] = script
    return index

