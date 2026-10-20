# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/core/state_index.py

A SQLite-backed index over Kea's lease files and reservation config, so the
Leases and Reservations pages get the same fast search/filter/sort/export the
Logs page already has (see core/log_index.py), without duplicating what Logs
already does.

Why this is a different index shape than the log index
--------------------------------------------------------
The log index tails an unbounded, append-only file incrementally, because a
log is a history and history is the whole point of it. Lease CSVs and the
reservation list in Kea's config are the opposite: they are bounded,
**current-state snapshots** (a lease file, even mid-compaction by Kea's own
LFC process, describes "now," not a log of every past transition; the
reservation list is just whatever's in the config right now). The event
history for a lease -- when it was allocated, renewed, released -- is
already fully searchable in the Logs page; this index is not trying to
recreate that.

So instead of incremental byte-offset tailing, every pass does a full parse
of each source and atomically replaces that source's rows in one
transaction (WAL means a concurrent search never sees a half-replaced
table). That is dramatically simpler than rotation-aware tailing, and cheap
at realistic scale -- a lease CSV or reservation list, even a large one, is
orders of magnitude smaller than a log's cumulative history. A cheap
fingerprint (mtime, size) skips the reparse entirely when a source hasn't
changed since the last pass.

One correctness note: Kea's memfile backend appends a new CSV row on every
lease transaction between LFC compaction runs, so the same address can
appear more than once in the file with different states. The old
get_active_leases()/get_active_leases6() in core/validation.py dedup by
*first* occurrence, which -- between compactions -- can show a stale row for
an address that has since changed state. This index dedups by *last*
occurrence instead, matching what Kea's own compaction converges to.

Reservations are the one source EZ-KEA itself writes (save_kea_config, via
the new/delete/edit reservation routes), so those routes call reindex_now()
right after saving -- otherwise an operator's own edit would look stale on
the very page they just used, up to STATE_INDEX_INTERVAL seconds later.
"""
import csv
import ipaddress
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import ipxe
from .log_index import classify_term, normalize_mac, normalize_mac_prefix
from .validation import V4_BOOT_FIELDS

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
EXPORT_MAX_ROWS = 500_000

# Kea's numeric lease `state` column. "active"/"expired" aren't separate Kea
# states -- both are state 0, distinguished here by comparing `expire` to now,
# because that distinction (still valid vs. past its expiry but not yet
# reclaimed) is exactly what an operator searching leases wants to filter by.
STATE_DECLINED = 1
STATE_RECLAIMED = 2
STATUS_LABELS = ("active", "expired", "declined", "reclaimed")

# Kea's lease6 `lease_type` column.
LEASE6_TYPE_LABELS = {0: "IA_NA", 1: "IA_TA", 2: "IA_PD"}

# Relative time-range presets for the Leases pages' expiry filter, the same
# shape as the Logs page's LOG_TIME_RANGES (routes/system.py) so both search
# experiences work the same way. Windows are forward-looking by default
# (leases mostly expire in the future while active), with an explicit
# backward-looking option for the common "what already expired" question.
LEASE_TIME_RANGES = (
    ("1h",      "Expiring in the next hour",    0, 3600),
    ("24h",     "Expiring in the next 24 hours", 0, 86400),
    ("7d",      "Expiring in the next 7 days",   0, 604800),
    ("expired", "Already expired",               None, 0),
    ("all",     "Any time",                      None, None),
)


def parse_datetime_local(value: str) -> Optional[float]:
    """Epoch seconds for an <input type=datetime-local> value, or None.
    Same parsing as routes/system.py's _parse_datetime_local -- lifted here
    since both the Logs and Leases pages need it."""
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


_LEASE_RANGE_OFFSETS = {value: (start, end) for value, _label, start, end in LEASE_TIME_RANGES}


def resolve_lease_time_range(
    range_value: str, start_text: str, end_text: str
) -> Tuple[Optional[float], Optional[float], str]:
    """Turn the Leases page's range selection into absolute (start, end)
    epoch bounds, the same way routes/system.py's _log_search_params resolves
    LOG_TIME_RANGES -- a preset wins unless "custom" is selected, in which
    case the calendar start/end fields are used as typed."""
    start_text = (start_text or "").strip()
    end_text = (end_text or "").strip()
    range_value = (range_value or "").strip() or ("custom" if (start_text or end_text) else "all")

    if range_value == "custom":
        return parse_datetime_local(start_text), parse_datetime_local(end_text), range_value

    offsets = _LEASE_RANGE_OFFSETS.get(range_value)
    if offsets is None:
        return None, None, "all"
    start_offset, end_offset = offsets
    now = time.time()
    start = (now + start_offset) if start_offset is not None else None
    end = (now + end_offset) if end_offset is not None else None
    return start, end, range_value


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_lease4 (
    id             INTEGER PRIMARY KEY,
    address        TEXT NOT NULL,
    mac_address    TEXT,
    client_id      TEXT,
    hostname       TEXT,
    subnet_id      INTEGER,
    subnet         TEXT,
    valid_lifetime INTEGER,
    expire         INTEGER,
    state          INTEGER,
    pool_id        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lease4_expire   ON state_lease4(expire);
CREATE INDEX IF NOT EXISTS idx_lease4_subnet   ON state_lease4(subnet);
CREATE INDEX IF NOT EXISTS idx_lease4_state    ON state_lease4(state);
CREATE INDEX IF NOT EXISTS idx_lease4_hostname ON state_lease4(hostname);

CREATE TABLE IF NOT EXISTS state_lease6 (
    id             INTEGER PRIMARY KEY,
    address        TEXT NOT NULL,
    prefix_len     INTEGER,
    duid           TEXT,
    hostname       TEXT,
    subnet_id      INTEGER,
    subnet         TEXT,
    pref_lifetime  INTEGER,
    valid_lifetime INTEGER,
    expire         INTEGER,
    lease_type     INTEGER,
    iaid           INTEGER,
    state          INTEGER,
    pool_id        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lease6_expire   ON state_lease6(expire);
CREATE INDEX IF NOT EXISTS idx_lease6_subnet   ON state_lease6(subnet);
CREATE INDEX IF NOT EXISTS idx_lease6_state    ON state_lease6(state);
CREATE INDEX IF NOT EXISTS idx_lease6_type     ON state_lease6(lease_type);
CREATE INDEX IF NOT EXISTS idx_lease6_hostname ON state_lease6(hostname);

CREATE TABLE IF NOT EXISTS state_reservation4 (
    id                   INTEGER PRIMARY KEY,
    mac_address          TEXT,
    ip_address           TEXT,
    hostname             TEXT,
    subnet               TEXT,
    shared_network_name  TEXT,
    -- per-host metadata, so the list can show that a host carries something
    -- beyond address/hostname (see _reservation_flags)
    options_count        INTEGER,
    boot_config          TEXT,
    classes              TEXT,
    -- the per-host iPXE script a reservation's generated client-class carries
    -- (see core/ipxe.py); NULL when the host does not boot via iPXE
    ipxe_script          TEXT
);
CREATE INDEX IF NOT EXISTS idx_res4_subnet ON state_reservation4(subnet);

CREATE TABLE IF NOT EXISTS state_reservation6 (
    id                   INTEGER PRIMARY KEY,
    duid                 TEXT,
    ip_address           TEXT,
    prefix               TEXT,
    hostname             TEXT,
    subnet               TEXT,
    shared_network_name  TEXT
);
CREATE INDEX IF NOT EXISTS idx_res6_subnet ON state_reservation6(subnet);

CREATE TABLE IF NOT EXISTS state_terms (
    table_name TEXT NOT NULL,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    row_id     INTEGER NOT NULL,
    sort_key   BLOB
);
CREATE INDEX IF NOT EXISTS idx_state_terms_lookup ON state_terms(table_name, kind, value, row_id);
CREATE INDEX IF NOT EXISTS idx_state_terms_sort   ON state_terms(table_name, kind, sort_key, row_id);
CREATE INDEX IF NOT EXISTS idx_state_terms_row    ON state_terms(table_name, row_id);

CREATE TABLE IF NOT EXISTS state_index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the state-index database."""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    _migrate_schema(conn)
    conn.commit()
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database already exists on disk.

    The index is deliberately disposable (see module docstring) so this is
    the only migration path -- no version table, just "does the column
    exist yet." Idempotent and cheap on the common (already-migrated) case:
    PRAGMA table_info is a metadata read, not a table scan, and the
    DROP/CREATE INDEX pair below only runs the one time a column is
    actually added, not on every connect().
    """
    migrated = False
    # Every column added after a table shipped, as (table, column, type). The
    # index is disposable, so "does the column exist yet" is the whole
    # migration story -- no version table.
    added_columns = (
        ("state_lease4", "subnet", "TEXT"),
        ("state_lease6", "subnet", "TEXT"),
        ("state_reservation4", "options_count", "INTEGER"),
        ("state_reservation4", "boot_config", "TEXT"),
        ("state_reservation4", "classes", "TEXT"),
        ("state_reservation4", "ipxe_script", "TEXT"),
    )
    for table, column, column_type in added_columns:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            migrated = True
    if migrated:
        # idx_lease4_subnet/idx_lease6_subnet pre-existed pointing at
        # subnet_id under this same name; CREATE INDEX IF NOT EXISTS would
        # see the name taken and silently keep indexing the wrong column.
        conn.execute("DROP INDEX IF EXISTS idx_lease4_subnet")
        conn.execute("DROP INDEX IF EXISTS idx_lease6_subnet")
        conn.execute("CREATE INDEX idx_lease4_subnet ON state_lease4(subnet)")
        conn.execute("CREATE INDEX idx_lease6_subnet ON state_lease6(subnet)")


def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM state_index_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state_index_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _ip_sort_key(addr: "ipaddress._BaseAddress") -> bytes:
    """Same fixed-width ordering key as log_index._ip_sort_key, duplicated
    rather than imported since it's a private helper of that module."""
    if addr.version == 4:
        return b"\x00" * 10 + b"\xff\xff" + addr.packed
    return addr.packed


def _fingerprint(path: str) -> str:
    """A cheap "has this source changed" signal. A stable sentinel for a
    missing file (rather than None) means a source that's consistently
    absent settles into "nothing to do" instead of reingesting every pass
    forever."""
    try:
        stat = os.stat(path)
    except OSError:
        return "missing"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _subnet_id_map(config: Dict[str, Any], dhcp_key: str, subnet_key: str) -> Dict[int, str]:
    """Kea's numeric subnet id -> the actual subnet CIDR, standalone and
    shared-network subnets alike. The lease CSVs only ever record the id;
    this is what makes it possible to search/display the CIDR instead."""
    mapping: Dict[int, str] = {}
    root = config.get(dhcp_key, {})
    for subnet in root.get(subnet_key, []):
        if "id" in subnet and "subnet" in subnet:
            mapping[subnet["id"]] = subnet["subnet"]
    for network in root.get("shared-networks", []):
        for subnet in network.get(subnet_key, []):
            if "id" in subnet and "subnet" in subnet:
                mapping[subnet["id"]] = subnet["subnet"]
    return mapping


def parse_lease4(path: str, subnet_map: Optional[Dict[int, str]] = None) -> List[Dict[str, Any]]:
    """Every row currently in the lease4 CSV, one per address, keeping the
    *last* occurrence (see module docstring for why that's the correct
    choice between LFC compaction passes).

    `subnet_map` resolves Kea's internal numeric subnet_id to the actual
    subnet CIDR (e.g. 10.10.10.0/24) an operator can search and recognise --
    subnet_id on its own is meaningless outside Kea's own config and isn't
    displayed anywhere, so it's not something a search filter should expose.
    """
    subnet_map = subnet_map or {}
    by_address: "Dict[str, Dict[str, Any]]" = {}
    try:
        with open(path, "r", newline="") as handle:
            for row in csv.DictReader(handle):
                address = row.get("address")
                if not address:
                    continue
                try:
                    expire = int(row.get("expire") or 0)
                except (TypeError, ValueError):
                    continue
                try:
                    state = int(row.get("state") or 0)
                except (TypeError, ValueError):
                    state = 0
                try:
                    subnet_id = int(row.get("subnet_id") or 0) or None
                except (TypeError, ValueError):
                    subnet_id = None
                try:
                    valid_lifetime = int(row.get("valid_lifetime") or 0)
                except (TypeError, ValueError):
                    valid_lifetime = None
                by_address[address] = {
                    "address": address,
                    "mac_address": (row.get("hwaddr") or "").lower() or None,
                    "client_id": row.get("client_id") or None,
                    "hostname": row.get("hostname") or None,
                    "subnet_id": subnet_id,
                    "subnet": subnet_map.get(subnet_id),
                    "valid_lifetime": valid_lifetime,
                    "expire": expire,
                    "state": state,
                    "pool_id": row.get("pool_id") or None,
                }
    except FileNotFoundError:
        return []
    return list(by_address.values())


def parse_lease6(path: str, subnet_map: Optional[Dict[int, str]] = None) -> List[Dict[str, Any]]:
    """Every row currently in the lease6 CSV, one per address, last
    occurrence wins. See parse_lease4 for why subnet_id is resolved to the
    actual subnet CIDR here rather than left as Kea's internal number."""
    subnet_map = subnet_map or {}
    by_address: "Dict[str, Dict[str, Any]]" = {}
    try:
        with open(path, "r", newline="") as handle:
            for row in csv.DictReader(handle):
                address = row.get("address")
                if not address:
                    continue
                try:
                    expire = int(row.get("expire") or 0)
                except (TypeError, ValueError):
                    continue
                try:
                    state = int(row.get("state") or 0)
                except (TypeError, ValueError):
                    state = 0
                try:
                    subnet_id = int(row.get("subnet_id") or 0) or None
                except (TypeError, ValueError):
                    subnet_id = None
                try:
                    lease_type = int(row.get("lease_type") or 0)
                except (TypeError, ValueError):
                    lease_type = 0
                try:
                    iaid = int(row.get("iaid") or 0) or None
                except (TypeError, ValueError):
                    iaid = None
                try:
                    prefix_len = int(row.get("prefix_len") or 0) or None
                except (TypeError, ValueError):
                    prefix_len = None
                by_address[address] = {
                    "address": address,
                    "prefix_len": prefix_len if lease_type == 2 else None,
                    "duid": (row.get("duid") or "").lower() or None,
                    "hostname": row.get("hostname") or None,
                    "subnet_id": subnet_id,
                    "subnet": subnet_map.get(subnet_id),
                    "pref_lifetime": row.get("pref_lifetime") or None,
                    "valid_lifetime": row.get("valid_lifetime") or None,
                    "expire": expire,
                    "lease_type": lease_type,
                    "iaid": iaid,
                    "state": state,
                    "pool_id": row.get("pool_id") or None,
                }
    except FileNotFoundError:
        return []
    return list(by_address.values())


def _reservation_flags(reservation: Dict[str, Any], subnet: Optional[str] = None,
                       ipxe_scripts: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Per-host metadata for the reservation list: whether the host carries
    options, a boot source, or class assignments beyond its address.

    Computed here (not in the route) so the fingerprint-gated ingest always
    refreshes it when the config changes, which is what makes the badges
    trustworthy without a separate query per page render.
    """
    if not isinstance(reservation, dict):
        return {"options_count": 0, "boot_config": None, "classes": None, "ipxe_script": None}

    options = reservation.get("option-data")
    options_count = len([o for o in options if isinstance(o, dict)]) if isinstance(options, list) else 0

    boot_props = {name: reservation[name] for name in V4_BOOT_FIELDS if reservation.get(name)}
    classes = reservation.get("client-classes")
    classes = [str(c) for c in classes] if isinstance(classes, list) else []

    # The reservation itself does not reference its generated class (see
    # core/ipxe.py), so the association is recomputed from the same (MAC,
    # subnet) pair the class name is built from.
    ipxe_script = None
    if ipxe_scripts:
        ipxe_script = ipxe_scripts.get(
            ipxe.managed_class_name(reservation.get("hw-address"), subnet))

    return {
        "options_count": options_count,
        "boot_config": json.dumps(boot_props) if boot_props else None,
        "classes": json.dumps(classes) if classes else None,
        "ipxe_script": ipxe_script,
    }


def parse_reservation4(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every MAC reservation in the config, standalone and shared-network
    subnets alike -- the same walk mac_reservations() does in routes/dhcp4.py."""
    rows: List[Dict[str, Any]] = []
    dhcp4 = config.get("Dhcp4", {})
    # Built once per parse: generated class name -> the script it carries.
    ipxe_scripts = ipxe.script_index(config, "Dhcp4")
    for subnet in dhcp4.get("subnet4", []):
        for reservation in subnet.get("reservations", []):
            rows.append({
                "mac_address": (reservation.get("hw-address") or "").lower() or None,
                "ip_address": reservation.get("ip-address"),
                "hostname": reservation.get("hostname"),
                "subnet": subnet.get("subnet"),
                "shared_network_name": None,
                **_reservation_flags(reservation, subnet.get("subnet"), ipxe_scripts),
            })
    for network in dhcp4.get("shared-networks", []):
        name = network.get("name")
        for subnet in network.get("subnet4", []):
            for reservation in subnet.get("reservations", []):
                rows.append({
                    "mac_address": (reservation.get("hw-address") or "").lower() or None,
                    "ip_address": reservation.get("ip-address"),
                    "hostname": reservation.get("hostname"),
                    "subnet": subnet.get("subnet"),
                    "shared_network_name": name,
                    **_reservation_flags(reservation, subnet.get("subnet"), ipxe_scripts),
                })
    return rows


def parse_reservation6(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every DUID reservation in the config -- mirrors reservations6() in
    routes/dhcp6.py."""
    rows: List[Dict[str, Any]] = []
    dhcp6 = config.get("Dhcp6", {})
    for subnet in dhcp6.get("subnet6", []):
        for reservation in subnet.get("reservations", []):
            ip_addresses = reservation.get("ip-addresses") or []
            prefixes = reservation.get("prefixes") or []
            rows.append({
                "duid": (reservation.get("duid") or "").lower() or None,
                "ip_address": ip_addresses[0] if ip_addresses else None,
                "prefix": prefixes[0] if prefixes else None,
                "hostname": reservation.get("hostname"),
                "subnet": subnet.get("subnet"),
                "shared_network_name": None,
            })
    for network in dhcp6.get("shared-networks", []):
        name = network.get("name")
        for subnet in network.get("subnet6", []):
            for reservation in subnet.get("reservations", []):
                ip_addresses = reservation.get("ip-addresses") or []
                prefixes = reservation.get("prefixes") or []
                rows.append({
                    "duid": (reservation.get("duid") or "").lower() or None,
                    "ip_address": ip_addresses[0] if ip_addresses else None,
                    "prefix": prefixes[0] if prefixes else None,
                    "hostname": reservation.get("hostname"),
                    "subnet": subnet.get("subnet"),
                    "shared_network_name": name,
                })
    return rows


# ---------------------------------------------------------------------------
# Ingest -- full parse, atomic replace, per source
# ---------------------------------------------------------------------------

# Which term kinds to extract from which column, per table -- drives both
# ingest (what gets written to state_terms) and search (what a plain search
# box's auto-classified term is allowed to match against).
_TERM_COLUMNS = {
    "lease4":       (("mac", "mac_address"), ("ip", "address")),
    "lease6":       (("duid", "duid"), ("ip", "address")),
    "reservation4": (("mac", "mac_address"), ("ip", "ip_address")),
    "reservation6": (("duid", "duid"), ("ip", "ip_address")),
}

_TABLE_NAMES = {
    "lease4": "state_lease4", "lease6": "state_lease6",
    "reservation4": "state_reservation4", "reservation6": "state_reservation6",
}


def _replace_rows(conn: sqlite3.Connection, kind: str, rows: List[Dict[str, Any]]) -> int:
    """Atomically swap a table's contents for `rows`. Caller owns the
    transaction. Returns the row count written."""
    table = _TABLE_NAMES[kind]
    conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM state_terms WHERE table_name = ?", (kind,))
    if not rows:
        return 0

    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    insert_sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"

    terms: List[Tuple[str, str, str, int, Optional[bytes]]] = []
    for row in rows:
        cursor = conn.execute(insert_sql, [row[c] for c in columns])
        row_id = cursor.lastrowid
        for term_kind, column in _TERM_COLUMNS[kind]:
            value = row.get(column)
            if not value:
                continue
            sort_key = None
            if term_kind == "ip":
                try:
                    addr = ipaddress.ip_address(value.split("/")[0])
                except ValueError:
                    continue
                value = str(addr)
                sort_key = _ip_sort_key(addr)
            elif term_kind == "mac":
                value = value.lower()
            terms.append((kind, term_kind, value, row_id, sort_key))

    if terms:
        conn.executemany(
            "INSERT INTO state_terms (table_name, kind, value, row_id, sort_key) VALUES (?, ?, ?, ?, ?)",
            terms,
        )
    return len(rows)


def _ingest_source(
    conn: sqlite3.Connection, kind: str, fingerprint_key: str,
    current_fingerprint: str, parse: "Any",
) -> bool:
    """Reparse-and-replace one source if its fingerprint changed since the
    last pass. Returns whether it was reingested."""
    if current_fingerprint == _get_meta(conn, fingerprint_key):
        return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = parse()
        _replace_rows(conn, kind, rows)
        _set_meta(conn, fingerprint_key, current_fingerprint)
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def ingest_all(config: Dict[str, Any], conn: sqlite3.Connection, kinds: Optional[Sequence[str]] = None) -> Dict[str, bool]:
    """Reparse-and-replace whichever of the four sources changed since the
    last pass (or all of them, if `kinds` narrows the set -- used by the
    post-save reindex nudge to touch only the reservation table that just
    changed)."""
    from .config_manager import load_json, _DEFAULT_KEA6_CONFIG

    kinds = set(kinds) if kinds else {"lease4", "lease6", "reservation4", "reservation6"}
    result: Dict[str, bool] = {}

    if "lease4" in kinds:
        path = config.get("DHCP_LEASES_FILE", "") or ""
        config_path = config.get("DHCP_CONFIG_FILE", "") or ""
        # Folded into one fingerprint: a subnet's CIDR can change (or a
        # subnet can be added/removed) without the lease file itself
        # changing, and that must still trigger a reingest since lease rows
        # carry the resolved CIDR, not just Kea's internal subnet_id.
        fingerprint = f"{_fingerprint(path)}|{_fingerprint(config_path)}"
        result["lease4"] = _ingest_source(
            conn, "lease4", "lease4_fingerprint", fingerprint,
            lambda: parse_lease4(path, _subnet_id_map(load_json(config_path), "Dhcp4", "subnet4")),
        )
    if "lease6" in kinds:
        path = config.get("DHCP6_LEASES_FILE", "") or ""
        config_path = config.get("DHCP6_CONFIG_FILE", "") or ""
        fingerprint = f"{_fingerprint(path)}|{_fingerprint(config_path)}"
        result["lease6"] = _ingest_source(
            conn, "lease6", "lease6_fingerprint", fingerprint,
            lambda: parse_lease6(path, _subnet_id_map(load_json(config_path, default=_DEFAULT_KEA6_CONFIG), "Dhcp6", "subnet6")),
        )
    if "reservation4" in kinds:
        path = config.get("DHCP_CONFIG_FILE", "") or ""
        result["reservation4"] = _ingest_source(
            conn, "reservation4", "reservation4_fingerprint", _fingerprint(path),
            lambda: parse_reservation4(load_json(path)),
        )
    if "reservation6" in kinds:
        path = config.get("DHCP6_CONFIG_FILE", "") or ""
        result["reservation6"] = _ingest_source(
            conn, "reservation6", "reservation6_fingerprint", _fingerprint(path),
            lambda: parse_reservation6(load_json(path, default=_DEFAULT_KEA6_CONFIG)),
        )

    _set_meta(conn, "last_ingest", str(time.time()))
    conn.commit()
    return result


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _term_join(kind: str, alias: str, term_kind: str, value: str) -> Tuple[str, List[Any]]:
    return (
        f"JOIN state_terms {alias} ON {alias}.row_id = t.id AND {alias}.table_name = ?",
        [kind],
    ), (f"{alias}.kind = ? AND {alias}.value = ?", [term_kind, value])


def _apply_address_filter(kind: str, joins: List[str], where: List[str], params: List[Any],
                           mac: Optional[str] = None, ip: Optional[str] = None,
                           duid: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Shared MAC/IP/DUID filter handling. Returns an early-out empty result
    if a filter value is uninterpretable, else None."""
    n = len(joins)
    if mac:
        normalized = normalize_mac(mac)
        alias = f"tm{n}"
        joins.append(f"JOIN state_terms {alias} ON {alias}.row_id = t.id AND {alias}.table_name = ?")
        params.append(kind)
        if normalized:
            where.append(f"{alias}.kind = 'mac' AND {alias}.value = ?")
            params.append(normalized)
        else:
            prefix = normalize_mac_prefix(mac)
            if prefix is None:
                return {"rows": [], "total": 0}
            where.append(f"{alias}.kind = 'mac' AND {alias}.value LIKE ? ESCAPE '\\'")
            params.append(_like_escape(prefix) + "%")
        n += 1
    if ip:
        try:
            addr = ipaddress.ip_address(ip.strip())
        except ValueError:
            return {"rows": [], "total": 0}
        alias = f"tm{n}"
        joins.append(f"JOIN state_terms {alias} ON {alias}.row_id = t.id AND {alias}.table_name = ?")
        params.append(kind)
        where.append(f"{alias}.kind = 'ip' AND {alias}.value = ?")
        params.append(str(addr))
        n += 1
    if duid:
        alias = f"tm{n}"
        joins.append(f"JOIN state_terms {alias} ON {alias}.row_id = t.id AND {alias}.table_name = ?")
        params.append(kind)
        where.append(f"{alias}.kind = 'duid' AND {alias}.value = ?")
        params.append(duid.strip().lower())
    return None


def _run_search(conn: sqlite3.Connection, table: str, columns: Sequence[str],
                 joins: List[str], where: List[str], params: List[Any],
                 sort_column: str, sort_dir: str, limit: int, offset: int) -> Dict[str, Any]:
    limit = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    offset = max(0, int(offset or 0))
    direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"

    join_sql = " ".join(joins)
    where_sql = (" WHERE " + " AND ".join(f"({clause})" for clause in where)) if where else ""
    col_sql = ", ".join(f"t.{c}" for c in columns)

    rows = conn.execute(
        f"SELECT t.id, {col_sql} FROM {table} t {join_sql}{where_sql} "
        f"ORDER BY t.{sort_column} {direction}, t.id {direction} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM (SELECT t.id FROM {table} t {join_sql}{where_sql})",
        params,
    ).fetchone()

    return {
        "rows": [dict(row) for row in rows],
        "total": int(total_row["n"]) if total_row else 0,
        "limit": limit,
        "offset": offset,
    }


_LEASE4_SORT_COLUMNS = {"address": "address", "hostname": "hostname", "expire": "expire", "subnet": "subnet"}
_LEASE6_SORT_COLUMNS = {"address": "address", "hostname": "hostname", "expire": "expire", "subnet": "subnet"}
_RES4_SORT_COLUMNS = {"ip_address": "ip_address", "mac_address": "mac_address", "hostname": "hostname", "subnet": "subnet"}
_RES6_SORT_COLUMNS = {"ip_address": "ip_address", "duid": "duid", "hostname": "hostname", "subnet": "subnet"}


def status_label(state: int, expire: Optional[int], now: Optional[float] = None) -> str:
    """The human-facing status for a lease row -- the inverse of
    _status_clause's filter logic, used to annotate search results."""
    if state == STATE_DECLINED:
        return "declined"
    if state == STATE_RECLAIMED:
        return "reclaimed"
    now = int(now if now is not None else time.time())
    return "active" if (expire and expire > now) else "expired"


def _status_clause(status: Optional[str], now: Optional[float] = None) -> Tuple[List[str], List[Any]]:
    if not status or status not in STATUS_LABELS:
        return [], []
    now = int(now if now is not None else time.time())
    if status == "declined":
        return ["t.state = ?"], [STATE_DECLINED]
    if status == "reclaimed":
        return ["t.state = ?"], [STATE_RECLAIMED]
    if status == "active":
        return ["t.state = 0 AND t.expire > ?"], [now]
    return ["t.state = 0 AND t.expire <= ?"], [now]  # "expired"


def _expire_range_clause(start: Optional[float], end: Optional[float]) -> Tuple[List[str], List[Any]]:
    """Calendar-style time window on a lease's expiry, the same shape as the
    Logs page's time range filter (see routes/system.py's _log_search_params
    and LOG_TIME_RANGES) -- preset-to-absolute conversion happens in the
    route layer, this just applies the resulting bounds."""
    where: List[str] = []
    params: List[Any] = []
    if start is not None:
        where.append("t.expire >= ?")
        params.append(int(start))
    if end is not None:
        where.append("t.expire <= ?")
        params.append(int(end))
    return where, params


def search_lease4(conn: sqlite3.Connection, *, q: Optional[str] = None, mac: Optional[str] = None,
                   ip: Optional[str] = None, status: Optional[str] = None, subnet: Optional[str] = None,
                   start: Optional[float] = None, end: Optional[float] = None,
                   sort: str = "expire", direction: str = "desc",
                   limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> Dict[str, Any]:
    joins: List[str] = []
    where: List[str] = []
    params: List[Any] = []

    if q and not mac and not ip:
        kind, term = classify_term(q)
        if kind == "mac":
            mac = term
        elif kind == "ip":
            ip = term
        else:
            where.append("t.hostname LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(q.strip()) + "%")

    early_out = _apply_address_filter("lease4", joins, where, params, mac=mac, ip=ip)
    if early_out is not None:
        early_out.update(limit=limit, offset=offset)
        return early_out

    status_where, status_params = _status_clause(status)
    where.extend(status_where)
    params.extend(status_params)
    range_where, range_params = _expire_range_clause(start, end)
    where.extend(range_where)
    params.extend(range_params)
    if subnet:
        where.append("t.subnet = ?")
        params.append(subnet)

    sort_column = _LEASE4_SORT_COLUMNS.get(sort, "expire")
    return _run_search(
        conn, "state_lease4",
        ("address", "mac_address", "client_id", "hostname", "subnet", "valid_lifetime", "expire", "state"),
        joins, where, params, sort_column, direction, limit, offset,
    )


def search_lease6(conn: sqlite3.Connection, *, q: Optional[str] = None, duid: Optional[str] = None,
                   ip: Optional[str] = None, status: Optional[str] = None, subnet: Optional[str] = None,
                   start: Optional[float] = None, end: Optional[float] = None,
                   lease_type: Optional[int] = None, sort: str = "expire", direction: str = "desc",
                   limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> Dict[str, Any]:
    joins: List[str] = []
    where: List[str] = []
    params: List[Any] = []

    if q and not duid and not ip:
        kind, term = classify_term(q)
        if kind == "mac":
            # A MAC typed against a v6 page most likely means "the DUID
            # embeds this link-layer address" -- but DUID matching here is
            # exact-value, not decode-and-compare, so fall through to a
            # substring match on hostname instead of silently matching
            # nothing.
            where.append("t.hostname LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(q.strip()) + "%")
        elif kind == "ip":
            ip = term
        else:
            duid_normalized = q.strip().lower()
            where.append("(t.hostname LIKE ? ESCAPE '\\' OR t.duid = ?)")
            params.extend(["%" + _like_escape(q.strip()) + "%", duid_normalized])

    early_out = _apply_address_filter("lease6", joins, where, params, duid=duid, ip=ip)
    if early_out is not None:
        early_out.update(limit=limit, offset=offset)
        return early_out

    status_where, status_params = _status_clause(status)
    where.extend(status_where)
    params.extend(status_params)
    range_where, range_params = _expire_range_clause(start, end)
    where.extend(range_where)
    params.extend(range_params)
    if subnet:
        where.append("t.subnet = ?")
        params.append(subnet)
    if lease_type is not None:
        where.append("t.lease_type = ?")
        params.append(int(lease_type))

    sort_column = _LEASE6_SORT_COLUMNS.get(sort, "expire")
    return _run_search(
        conn, "state_lease6",
        ("address", "prefix_len", "duid", "hostname", "subnet", "pref_lifetime",
         "valid_lifetime", "expire", "lease_type", "state"),
        joins, where, params, sort_column, direction, limit, offset,
    )


def search_reservation4(conn: sqlite3.Connection, *, q: Optional[str] = None, mac: Optional[str] = None,
                         ip: Optional[str] = None, subnet: Optional[str] = None,
                         shared_network_name: Optional[str] = None, sort: str = "ip_address",
                         direction: str = "asc", limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> Dict[str, Any]:
    joins: List[str] = []
    where: List[str] = []
    params: List[Any] = []

    if q and not mac and not ip:
        kind, term = classify_term(q)
        if kind == "mac":
            mac = term
        elif kind == "ip":
            ip = term
        else:
            where.append("t.hostname LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(q.strip()) + "%")

    early_out = _apply_address_filter("reservation4", joins, where, params, mac=mac, ip=ip)
    if early_out is not None:
        early_out.update(limit=limit, offset=offset)
        return early_out

    if subnet:
        where.append("t.subnet = ?")
        params.append(subnet)
    if shared_network_name:
        where.append("t.shared_network_name = ?")
        params.append(shared_network_name)

    sort_column = _RES4_SORT_COLUMNS.get(sort, "ip_address")
    return _run_search(
        conn, "state_reservation4",
        ("mac_address", "ip_address", "hostname", "subnet", "shared_network_name",
         "options_count", "boot_config", "classes", "ipxe_script"),
        joins, where, params, sort_column, direction, limit, offset,
    )


def search_reservation6(conn: sqlite3.Connection, *, q: Optional[str] = None, duid: Optional[str] = None,
                         ip: Optional[str] = None, subnet: Optional[str] = None,
                         shared_network_name: Optional[str] = None, sort: str = "ip_address",
                         direction: str = "asc", limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> Dict[str, Any]:
    joins: List[str] = []
    where: List[str] = []
    params: List[Any] = []

    if q and not duid and not ip:
        kind, term = classify_term(q)
        if kind == "ip":
            ip = term
        elif kind != "mac":
            duid_normalized = q.strip().lower()
            where.append("(t.hostname LIKE ? ESCAPE '\\' OR t.duid = ?)")
            params.extend(["%" + _like_escape(q.strip()) + "%", duid_normalized])
        else:
            where.append("t.hostname LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(q.strip()) + "%")

    early_out = _apply_address_filter("reservation6", joins, where, params, duid=duid, ip=ip)
    if early_out is not None:
        early_out.update(limit=limit, offset=offset)
        return early_out

    if subnet:
        where.append("t.subnet = ?")
        params.append(subnet)
    if shared_network_name:
        where.append("t.shared_network_name = ?")
        params.append(shared_network_name)

    sort_column = _RES6_SORT_COLUMNS.get(sort, "ip_address")
    return _run_search(
        conn, "state_reservation6",
        ("duid", "ip_address", "prefix", "hostname", "subnet", "shared_network_name"),
        joins, where, params, sort_column, direction, limit, offset,
    )


_SEARCH_FUNCS = {
    "lease4": search_lease4, "lease6": search_lease6,
    "reservation4": search_reservation4, "reservation6": search_reservation6,
}


def iter_search(conn: sqlite3.Connection, kind: str, *, page_size: int = MAX_PAGE_SIZE, **filters: Any) -> Iterable[Dict[str, Any]]:
    """Yield every match, page by page -- the CSV export's data source."""
    search = _SEARCH_FUNCS[kind]
    filters.pop("limit", None)
    offset = int(filters.pop("offset", 0) or 0)
    yielded = 0
    while True:
        result = search(conn, limit=page_size, offset=offset, **filters)
        rows = result["rows"]
        if not rows:
            return
        for row in rows:
            yield row
            yielded += 1
            if yielded >= EXPORT_MAX_ROWS:
                return
        if len(rows) < page_size:
            return
        offset += page_size


def index_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    stats: Dict[str, Any] = {}
    for kind, table in _TABLE_NAMES.items():
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        stats[kind] = int(row["n"] or 0)
    last_ingest = _get_meta(conn, "last_ingest")
    stats["last_ingest"] = float(last_ingest) if last_ingest else None
    return stats


def active_lease_counts_by_subnet(conn: sqlite3.Connection, kind: str, now: Optional[float] = None) -> Dict[str, int]:
    """Active lease counts grouped by subnet CIDR, for the homepage's
    per-subnet pool utilization bars. `kind` is "lease4" or "lease6"; lease6
    excludes IA_PD (prefix delegation) rows -- those consume delegated
    prefixes, not individual addresses, the same distinction
    routes/system.py's _pool_capacity draws when it skips pd-pools."""
    table = _TABLE_NAMES[kind]
    now = int(now if now is not None else time.time())
    where = "state = 0 AND expire > ?"
    params: List[Any] = [now]
    if kind == "lease6":
        where += " AND lease_type != 2"
    rows = conn.execute(f"SELECT subnet, COUNT(*) AS n FROM {table} WHERE {where} GROUP BY subnet", params).fetchall()
    return {row["subnet"]: row["n"] for row in rows if row["subnet"]}


# ---------------------------------------------------------------------------
# Wiring into the app
# ---------------------------------------------------------------------------

def run_once(config: Dict[str, Any], kinds: Optional[Sequence[str]] = None) -> Dict[str, bool]:
    """One ingest pass. Used by the background thread, the manual refresh
    action, the post-save reindex nudge, and the tests."""
    conn = connect(config["STATE_INDEX_DB"])
    try:
        return ingest_all(config, conn, kinds=kinds)
    finally:
        conn.close()


def reindex_now(app: Any, kinds: Optional[Sequence[str]] = None) -> Dict[str, bool]:
    """Synchronous, immediate reindex of (by default) everything, or just
    `kinds` -- used right after a reservation save so the page an operator
    just edited doesn't show stale data until the next poll."""
    return run_once(dict(app.config), kinds=kinds)


_indexer_threads: "dict[int, threading.Thread]" = {}
_indexer_lock = threading.Lock()


def start_background_state_indexer(app: Any) -> None:
    """Start the daemon thread that keeps the lease/reservation index
    current. Mirrors log_index.start_background_indexer."""
    if not app.config.get("STATE_INDEX_ENABLED", True):
        return
    if app.config.get("TESTING"):
        return
    with _indexer_lock:
        existing = _indexer_threads.get(id(app))
        if existing is not None and existing.is_alive():
            return

        interval = max(5, int(app.config.get("STATE_INDEX_INTERVAL", 20)))

        def loop() -> None:
            while True:
                try:
                    run_once(dict(app.config))
                except Exception:  # pragma: no cover - defensive
                    app.logger.exception("state index pass failed")
                time.sleep(interval)

        thread = threading.Thread(target=loop, name="ez-kea-state-indexer", daemon=True)
        _indexer_threads[id(app)] = thread
        thread.start()
