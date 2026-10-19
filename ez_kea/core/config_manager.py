# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import copy
import json
import os
import re
import shutil
import datetime
import fcntl
import hashlib
import threading
from contextlib import contextmanager
from functools import wraps
from typing import Any, Dict, Optional, Tuple


class ConfigAccessError(Exception):
    """
    Raised when a Kea config file exists but EZ-KEA cannot read or write it.

    Deliberately NOT folded into the "return the skeleton" fallback used for a
    missing or corrupt file. A permission error means a real config is sitting
    there that we simply cannot see -- handing the caller an empty skeleton
    would render the UI as though the server had no subnets at all, and the
    operator's first edit would then overwrite their real config with that
    skeleton. Failing loudly is the only safe option.
    """


class BackupError(Exception):
    """
    Raised when the pre-write backup of a Kea config could not be taken.

    Callers must treat this as fatal to the write. EZ-KEA's headline promise is
    that it backs up before it writes; silently writing anyway when the backup
    failed would break exactly the guarantee an operator is relying on.
    """


# Minimal valid Kea DHCPv4 skeleton written on first run.
#
# `control-sockets` (list) is the Kea 3.0+ spelling of what used to be a single
# `control-socket` object. It is the default here because Kea 2.6 is EOL; for a
# daemon we positively identify as pre-3.0, bootstrap_config() downgrades this
# via to_legacy_control_socket().
_DEFAULT_KEA_CONFIG = {
    "Dhcp4": {
        "interfaces-config": {
            "interfaces": []
        },
        "control-sockets": [
            {
                "socket-type": "unix",
                "socket-name": "/var/run/kea/kea-dhcp4-ctrl.sock"
            }
        ],
        "lease-database": {
            "type": "memfile",
            "lfc-interval": 3600,
            "name": "/var/lib/kea/kea-leases4.csv"
        },
        "host-reservation-identifiers": ["hw-address"],
        "valid-lifetime": 4000,
        "option-data": [],
        "shared-networks": [],
        "subnet4": [],
        "loggers": [
            {
                "name": "kea-dhcp4",
                "output_options": [
                    {
                        "output": "/var/log/kea/kea-dhcp4.log",
                        "maxver": 8,
                        "maxsize": 204800,
                        "flush": True
                    }
                ],
                "severity": "INFO",
                "debuglevel": 0
            }
        ]
    }
}

# Minimal valid Kea DHCPv6 skeleton written on first run. Kept as its own
# constant (rather than deriving it from _DEFAULT_KEA_CONFIG) since v6's
# control-socket/lease-database/logger paths and the extra preferred-lifetime
# field are genuinely different, not just a Dhcp4->Dhcp6 rename.
_DEFAULT_KEA6_CONFIG = {
    "Dhcp6": {
        "interfaces-config": {
            "interfaces": []
        },
        "control-sockets": [
            {
                "socket-type": "unix",
                "socket-name": "/var/run/kea/kea-dhcp6-ctrl.sock"
            }
        ],
        "lease-database": {
            "type": "memfile",
            "lfc-interval": 3600,
            "name": "/var/lib/kea/kea-leases6.csv"
        },
        "host-reservation-identifiers": ["duid"],
        "preferred-lifetime": 3000,
        "valid-lifetime": 4000,
        "option-data": [],
        "shared-networks": [],
        "subnet6": [],
        "loggers": [
            {
                "name": "kea-dhcp6",
                "output_options": [
                    {
                        "output": "/var/log/kea/kea-dhcp6.log",
                        "maxver": 8,
                        "maxsize": 204800,
                        "flush": True
                    }
                ],
                "severity": "INFO",
                "debuglevel": 0
            }
        ]
    }
}


from typing import Any, Callable, Dict, Optional, TypeVar, Union

# Kea's config parser accepts C++/shell-style comments that are not valid
# JSON: "//" and "#" line comments, and "/* */" block comments -- exactly
# what ISC's own shipped example configs are full of. This strips them while
# tracking string-literal state, so a value that itself contains "//" (e.g.
# a URL in boot-file-name) is never touched.
def _strip_json_comments(text: str) -> str:
    result = []
    in_string = False
    escape = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_string:
            result.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            result.append(c)
            i += 1
            continue
        if c in "#" or (c == "/" and i + 1 < n and text[i + 1] == "/"):
            end = text.find("\n", i)
            if end == -1:
                break
            result.append("\n")
            i = end + 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        result.append(c)
        i += 1
    return "".join(result)


def load_json(file_path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Load and return JSON data from the specified file.

    Uses shared file locks to ensure safety. Returns a deep copy of `default`
    (the v4 Kea config skeleton unless a caller passes its own, e.g. the v6
    skeleton) if the file does not exist or is empty. Kea's own "//", "#" and
    "/* */" comments are stripped before parsing, since ISC's shipped configs
    are full of them and are not valid strict JSON otherwise.

    Args:
        file_path (str): The path to the JSON file to load.
        default (Optional[Dict[str, Any]]): Skeleton to fall back to. Defaults
            to the v4 skeleton for backwards compatibility with existing callers.

    Returns:
        Dict[str, Any]: The loaded JSON dictionary or a deep copy of the
            default skeleton.

    Raises:
        ConfigAccessError: the file exists, has content, and still fails to
            parse as JSON after stripping comments. Silently handing back an
            empty skeleton here would make the UI look like the server has no
            subnets at all, and the operator's next edit would overwrite
            their real config with that skeleton -- the same reasoning
            PermissionError below already follows.
    """
    fallback = default if default is not None else _DEFAULT_KEA_CONFIG
    try:
        with open(file_path, "r") as file:
            fcntl.flock(file, fcntl.LOCK_SH)
            content = file.read()
            fcntl.flock(file, fcntl.LOCK_UN)
    except FileNotFoundError:
        return copy.deepcopy(fallback)
    except PermissionError as e:
        # A real config we're not allowed to read -- never fall back to the
        # skeleton here (see ConfigAccessError). Standard Kea packages install
        # /etc/kea 0750 _kea:_kea, so this is the normal outcome until the
        # EZ-KEA service account is added to Kea's group.
        raise ConfigAccessError(
            f"EZ-KEA cannot read '{file_path}': {e.strerror}. The account "
            "EZ-KEA runs as needs read and write access to this file — on a "
            "packaged Kea install, add that account to Kea's group "
            "(usually '_kea' or 'kea') and make the file group-writable."
        ) from e

    if not content.strip():
        return copy.deepcopy(fallback)

    try:
        return dict(json.loads(_strip_json_comments(content)))
    except json.JSONDecodeError as e:
        raise ConfigAccessError(
            f"EZ-KEA cannot parse '{file_path}': {e.msg} at line {e.lineno}, "
            f"column {e.colno} (after stripping Kea's //, #, and /* */ "
            "comments). The file exists and is not empty, so EZ-KEA is "
            "refusing to treat it as blank -- fix the syntax error (or "
            "restore a backup) before EZ-KEA can safely read or write this "
            "config."
        ) from e


def save_json(data: Dict[str, Any], file_path: str) -> None:
    """
    Save dict data to the specified file as formatted JSON.
    
    Creates necessary parent directories. Uses an exclusive file lock
    during the write process to ensure thread/process safety.

    Args:
        data (Dict[str, Any]): The dictionary data to write.
        file_path (str): The destination file path.
    """
    # Ensure parent directory exists
    parent = os.path.dirname(os.path.abspath(file_path))
    os.makedirs(parent, exist_ok=True)
    with open(file_path, "w") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        json.dump(data, file, indent=2)
        fcntl.flock(file, fcntl.LOCK_UN)



# ── Process-wide, per-file locking for load→mutate→save cycles ──────────────
#
# fcntl.flock() (used inside load_json/save_json above) only guards the
# individual read or write syscall — it does nothing to stop two concurrent
# requests from both loading the same config, mutating their own in-memory
# copies, and then racing to save, silently discarding one of them.
# Waitress's default worker model is threaded, so a plain threading.Lock
# per resolved file path is enough to serialize the entire cycle within this process.

_file_locks: Dict[str, threading.Lock] = {}
_file_locks_guard = threading.Lock()

_F = TypeVar("_F", bound=Callable[..., Any])


def _get_file_lock(file_path: str) -> threading.Lock:
    """Return the (creating if necessary) lock associated with a config file's resolved path."""
    resolved = os.path.realpath(file_path)
    with _file_locks_guard:
        lock = _file_locks.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _file_locks[resolved] = lock
        return lock


@contextmanager
def config_lock(file_path: str):
    """Context manager holding the process-wide lock for `file_path` for its duration."""
    lock = _get_file_lock(file_path)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def with_config_lock(config_key: str = "DHCP_CONFIG_FILE", default: Optional[str] = None) -> Callable[[_F], _F]:
    """
    Route decorator: acquire the process-wide lock for `current_app.config[config_key]`
    (falling back to `default` if the key is absent) for the entire duration of the
    decorated view function, so its load→mutate→save cycle can't interleave with
    another request's.
    """
    def decorator(fn: _F) -> _F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from flask import current_app
            path = current_app.config.get(config_key, default) if default is not None else current_app.config[config_key]
            with config_lock(path):
                return fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


def extract_log_file_from_config(config_file: str, default_fallback: str, dhcp_key: str = "Dhcp4", logger_name: str = "kea-dhcp4") -> str:
    """
    Attempts to read the Kea configuration file and extract the log file path
    from <dhcp_key> -> loggers -> output_options. Falls back to default_fallback.
    """
    try:
        config = load_json(config_file, default=_DEFAULT_KEA6_CONFIG if dhcp_key == "Dhcp6" else None)
        loggers = config.get(dhcp_key, {}).get("loggers", [])
        for logger in loggers:
            if logger.get("name") == logger_name:
                # Kea spells this "output_options"; the hyphenated variant is a
                # historical EZ-KEA typo that some installs still carry in their
                # config file, so read either rather than silently ignoring a
                # logger that is really configured.
                outputs = logger.get("output_options", logger.get("output-options", []))
                for opt in outputs:
                    output_path = opt.get("output", "")
                    if output_path and output_path not in ("stdout", "stderr"):
                        return output_path
    except Exception:
        # Best-effort: this is used to pre-fill a display value, never to
        # decide whether the config is safe to write, so a ConfigAccessError
        # here should degrade to the fallback rather than break the page.
        pass
    return default_fallback


def extract_log_file_from_config6(config_file: str, default_fallback: str) -> str:
    """extract_log_file_from_config(), scoped to the DHCPv6 config/logger."""
    return extract_log_file_from_config(config_file, default_fallback, dhcp_key="Dhcp6", logger_name="kea-dhcp6")


def to_legacy_control_socket(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rewrite a skeleton's Kea 3.0+ `control-sockets` list back to the pre-3.0
    singular `control-socket` object, for a daemon old enough to require it.

    Only the first socket entry survives, which is lossless for our own
    skeletons (they define exactly one UNIX socket) and is the only thing a
    pre-3.0 Kea could have expressed anyway. Returns a new dict; the input is
    untouched.
    """
    rewritten: Dict[str, Any] = {}
    for dhcp_key, daemon in config.items():
        if not isinstance(daemon, dict):
            rewritten[dhcp_key] = daemon
            continue
        rewritten_daemon: Dict[str, Any] = {}
        for key, value in daemon.items():
            # Rebuild key-by-key so each entry stays where it was in the file
            # rather than being popped to the end.
            if key == "control-sockets" and isinstance(value, list) and value:
                rewritten_daemon["control-socket"] = value[0]
            else:
                rewritten_daemon[key] = value
        rewritten[dhcp_key] = rewritten_daemon
    return rewritten


def bootstrap_config(
    config_file: str,
    backup_dir: str,
    default_config: Optional[Dict[str, Any]] = None,
    legacy_control_socket: bool = False,
) -> None:
    """
    Called at app startup. Creates the data directory and writes a minimal
    valid Kea config skeleton (the v4 skeleton unless `default_config` is
    given, e.g. the v6 skeleton) if the config file does not already exist.

    Set `legacy_control_socket` when the target daemon is older than Kea 3.0,
    so the skeleton uses the singular `control-socket` object it understands.
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(config_file)), exist_ok=True)
    except PermissionError:
        pass # In LIVE mode with system paths (/etc) we might not have permission, ignore it.

    try:
        os.makedirs(backup_dir, exist_ok=True)
    except PermissionError:
        pass

    if not os.path.exists(config_file):
        skeleton = dict(default_config if default_config is not None else _DEFAULT_KEA_CONFIG)
        if legacy_control_socket:
            skeleton = to_legacy_control_socket(skeleton)
        try:
            save_json(skeleton, config_file)
        except PermissionError:
            pass # We can't write the default config, so we'll run read-only and fail on save


def bootstrap_config6(config_file: str, backup_dir: str, legacy_control_socket: bool = False) -> None:
    """bootstrap_config(), scoped to the DHCPv6 default skeleton."""
    bootstrap_config(
        config_file, backup_dir,
        default_config=_DEFAULT_KEA6_CONFIG,
        legacy_control_socket=legacy_control_socket,
    )


def prune_backups(config_file: str, backup_dir: str, keep: int) -> None:
    """
    Delete all but the `keep` most recent backups of `config_file`.

    Every config write now takes a backup (see save_kea_config), so without a
    cap a busy install would grow one file per edit forever. Scoped by the same
    identity fingerprint used elsewhere, so pruning one config's history never
    touches another's. `keep` <= 0 disables pruning entirely.
    """
    if keep <= 0:
        return

    prefix = f"{os.path.basename(config_file)}.{_config_identity(config_file)}.bak."
    try:
        stamped = sorted(
            (f for f in os.listdir(backup_dir) if f.startswith(prefix)),
            reverse=True,  # names end in YYYYmmddHHMMSS, so lexical == chronological
        )
    except OSError:
        return

    for stale in stamped[keep:]:
        try:
            os.remove(os.path.join(backup_dir, stale))
        except OSError:
            pass  # a backup we couldn't delete is not worth failing a config write over


def _write_kea_config(
    data: Dict[str, Any], file_path: str, backup_dir: str, keep_backups: int = 100
) -> None:
    """Back up and write, no questions asked. The guts of save_kea_config.

    Split out so that save_validated_kea_config can reuse the backup-and-write
    step without recursing back into the validating wrapper.
    """
    if os.path.isfile(file_path):
        try:
            copy_file(file_path, backup_dir)
        except Exception as e:
            raise BackupError(
                f"Refusing to write '{file_path}': its pre-write backup into "
                f"'{backup_dir}' failed ({e}). Fix the backup directory's path "
                "or permissions and try again — EZ-KEA will not modify a Kea "
                "config it cannot back up first."
            ) from e
        prune_backups(file_path, backup_dir, keep_backups)

    save_json(data, file_path)


def _validation_context(file_path: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    The app config to validate against, and which daemon owns this file.

    Returns (None, "Dhcp4") when there is no app context or the file is not one
    of the two Kea configs this app manages -- in that case the write is the
    plain backup-and-write it always was.
    """
    try:
        from flask import current_app, has_app_context
    except Exception:  # pragma: no cover - flask is a hard dependency
        return None, "Dhcp4"
    if not has_app_context():
        return None, "Dhcp4"
    config = current_app.config
    target = os.path.realpath(file_path)
    for key, dhcp_key in (("DHCP_CONFIG_FILE", "Dhcp4"), ("DHCP6_CONFIG_FILE", "Dhcp6")):
        configured = config.get(key)
        if configured and os.path.realpath(str(configured)) == target:
            return dict(config), dhcp_key
    return None, "Dhcp4"


def save_kea_config(
    data: Dict[str, Any], file_path: str, backup_dir: str, keep_backups: int = 100
) -> Optional[str]:
    """
    Write a Kea config file: back it up, then write it only if Kea accepts it.

    This is the only function that should ever write a *Kea* config. Plain
    save_json() does not back anything up, and using it directly for a config
    EZ-KEA manages is what let "backs up your Kea configs before it writes"
    quietly become untrue for every edit path except the explicit Backup button.

    A failed backup aborts the write (BackupError) rather than proceeding
    without one -- an operator relying on that guarantee during a bad edit is
    exactly who gets hurt if we write anyway.

    Inside a request, the candidate is also handed to the daemon's own syntax
    check and rolled back if Kea refuses it (see core/config_validate.py). That
    check lives here rather than in each route so that a route added later
    cannot quietly reintroduce "saved, but the daemon will not load it" -- which
    is exactly what happened to reservations and subnets.

    Returns a warning string when the check could not be run (no Kea binary on
    this host); None on success. Raises InvalidConfigError if Kea refused it.
    """
    app_config, dhcp_key = _validation_context(file_path)
    if app_config is not None:
        from .config_validate import save_validated_kea_config

        return save_validated_kea_config(
            data, file_path, backup_dir, app_config,
            dhcp_key=dhcp_key, keep_backups=keep_backups,
        )
    _write_kea_config(data, file_path, backup_dir, keep_backups)
    return None


def _config_identity(config_file: str) -> str:
    """
    Stable short identifier for a config file's full resolved path.

    Encoded into backup filenames so that restore can tell backups of *this*
    config_file apart from backups of some other config_file that happens to
    share the same basename in the same backup_dir (`dhcp_config_file` is
    operator-settable, so basename alone is not a safe way to scope a restore).
    """
    return hashlib.sha256(os.path.realpath(config_file).encode()).hexdigest()[:16]


def copy_file(config_file: str, backup_dir: str, restore: bool = False) -> Union[str, bool]:
    """
    Backup or restore the Kea DHCP configuration file.

    When `restore` is False, copies the `config_file` to `backup_dir` with a timestamp.
    When `restore` is True, finds the most recent backup in `backup_dir` that was
    made *for this same config_file* and copies it over `config_file`.

    Args:
        config_file (str): Path to the active Kea configuration file.
        backup_dir (str): Directory where backups are stored.
        restore (bool): Whether to perform a restore operation from a backup.

    Returns:
        Union[str, bool]: If not restoring, the path to the backup file created.
                          If restoring, True on success, False if no backup found.
    """
    filename = os.path.basename(config_file)
    identity = _config_identity(config_file)

    if restore:
        most_recent_backup = None
        largest_timestamp = None
        if not os.path.exists(backup_dir):
            print("Backup directory does not exist.")
            return False

        # Only consider backups that were made for *this* config_file (matched by
        # the identity fingerprint), never merely the newest ".bak." file in the
        # directory regardless of which config it belongs to.
        prefix = f"{filename}.{identity}.bak."
        for backup_file in os.listdir(backup_dir):
            if backup_file.startswith(prefix):
                try:
                    timestamp = int(backup_file[len(prefix):])
                except ValueError:
                    continue
                if largest_timestamp is None or timestamp > largest_timestamp:
                    most_recent_backup = os.path.join(backup_dir, backup_file)
                    largest_timestamp = timestamp

        if most_recent_backup:
            try:
                # copyfile(), NOT copy2(): copy2 also copies the permission
                # bits, and chmod() requires *ownership* of the destination.
                # Every packaged Kea install leaves /etc/kea/kea-dhcp4.conf
                # owned by _kea while EZ-KEA runs as its own account, so copy2
                # raises EPERM *after* already writing the contents -- the
                # restore silently succeeds while reporting a 500. We only ever
                # want the file's contents replaced; its ownership and mode
                # belong to whoever set up the Kea install.
                shutil.copyfile(most_recent_backup, config_file)
                print(f"Successfully restored configuration from {most_recent_backup}")
                return True
            except Exception as e:
                print(f"Error copying file: {e}")
                raise
        else:
            print("No backup file found matching the current configuration to restore.")
            return False
    else:
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        backup_name = os.path.join(backup_dir, f"{filename}.{identity}.bak.{timestamp}")
        try:
            shutil.copy2(config_file, backup_name)
            print(f"Successfully backed up configuration to {backup_name}")
            return backup_name
        except Exception as e:
            print(f"Error copying file: {e}")
            raise
