# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/core/config_validate.py

Writing a Kea config only if Kea itself accepts it.

Every structured form in EZ-KEA validates the little it can (a subnet CIDR, an
IPv4 next-server, a duplicate class name), but Kea's own grammar is far larger
than anything worth reimplementing -- class expressions above all. So the
authority on "is this a loadable config" is `kea-dhcp4 -t`, and this module
makes saves go through it:

  1. back up the current file and write the candidate (the ordinary
     save_kea_config path -- no backup, no write),
  2. run the daemon's own syntax check against the file that is now on disk,
  3. if Kea rejects it, put the backup back, so the file is never left holding
     a config the daemon cannot load.

The rollback is deliberately chapter-and-verse rather than clever: the same
bytes that a successful save would have produced are tested, so what was
checked is exactly what would have been kept.
"""
from typing import Any, Callable, Dict, NamedTuple, Optional

from .config_manager import BackupError, _write_kea_config

# The daemon command, its config key, and the key holding the in-container path
# (blank on non-Docker installs) -- see core/discovery.py and config.py.
DAEMON_COMMAND_KEYS = {
    "Dhcp4": ("KEA_DHCP4_CMD", "DHCP_CONFIG_FILE_IN_CONTAINER"),
    "Dhcp6": ("KEA_DHCP6_CMD", "DHCP6_CONFIG_FILE_IN_CONTAINER"),
}

# A syntax check on a small file; generous for a cold binary, short enough that
# a hung check does not hold the config lock forever.
SYNTAX_CHECK_TIMEOUT_SECONDS = 30


class InvalidConfigError(Exception):
    """Kea refused the candidate config. The file has already been rolled back."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SyntaxCheckResult(NamedTuple):
    """Outcome of one `kea-dhcpN -t` run."""

    ok: bool
    output: str          # Kea's own words, empty on success
    ran: bool            # False when the check could not be run at all
    # ran=False has two very different causes: the configured command was
    # refused (the operator's own input) or the environment cannot run it
    # (missing binary, timeout). invalid_command separates them, because only
    # the first deserves a 400.
    invalid_command: bool = False


def test_config_file(config_file: str, app_config: Dict[str, Any],
                     dhcp_key: str = "Dhcp4") -> SyntaxCheckResult:
    """
    Run the daemon's own syntax check (`-t`) against `config_file`.

    Returns a SyntaxCheckResult rather than raising, so a caller can tell
    "Kea said no" (ok=False, ran=True) apart from "the check could not be run"
    (ok=False, ran=False) -- the first blocks a save, the second is an
    environment problem the operator should be told about but which must not
    make saving impossible.

    `app_config` is an ordinary mapping (the Flask config, or a plain dict of
    the same keys); the daemon command is validated through the same allowlist
    every other invocation uses, and the path handed to Kea is the in-container
    one when a Docker deployment configured it.
    """
    import os
    import subprocess

    from .security import InvalidKeaCommandError, validate_kea_command

    # A remote endpoint's file is a local mirror of another host's config: the
    # daemon's own config-test is the authority there, and running this host's
    # -t against it can fail for reasons that have nothing to do with the edit
    # (hook libraries, TLS/auth files that only exist on the daemon's host).
    try:
        from .instances import local_syntax_check_applicable
        if not local_syntax_check_applicable():
            return SyntaxCheckResult(
                False,
                "Skipped: this is a remote endpoint, where the daemon's own "
                "config-test validates the configuration at apply time.",
                False,
            )
    except Exception:  # pragma: no cover - never let this decide a save
        pass

    cmd_key, in_container_key = DAEMON_COMMAND_KEYS.get(dhcp_key, DAEMON_COMMAND_KEYS["Dhcp4"])
    binary = app_config.get(cmd_key, f"kea-dhcp{dhcp_key[-1]}")
    try:
        command = validate_kea_command(binary, f"kea-{'dhcp6' if dhcp_key == 'Dhcp6' else 'dhcp4'}-cmd")
    except InvalidKeaCommandError as e:
        return SyntaxCheckResult(False, str(e), False, True)

    in_container = str(app_config.get(in_container_key, "") or "").strip()
    path_for_kea = in_container or config_file
    argv = list(command) + ["-t", path_for_kea]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=SYNTAX_CHECK_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as e:
        return SyntaxCheckResult(False, f"Kea binary not found: {e}", False)
    except subprocess.TimeoutExpired:
        return SyntaxCheckResult(
            False, f"Kea syntax check timed out after {SYNTAX_CHECK_TIMEOUT_SECONDS}s", False)
    except OSError as e:
        return SyntaxCheckResult(False, f"Could not run the Kea syntax check: {e}", False)

    # str()/int() are defensive: an unexpected object here must still produce a
    # readable message rather than blowing up jsonify downstream.
    output = (str(completed.stdout or "") + "\n" + str(completed.stderr or "")).strip()
    try:
        returncode = int(completed.returncode)
    except (TypeError, ValueError):
        return SyntaxCheckResult(False, output or "the syntax check returned no exit code", False)
    if returncode != 0:
        return SyntaxCheckResult(False, output or f"kea-dhcp4 -t exited {returncode}", True)
    return SyntaxCheckResult(True, output, True)


def save_validated_kea_config(data: Dict[str, Any], file_path: str, backup_dir: str,
                              app_config: Dict[str, Any], dhcp_key: str = "Dhcp4",
                              keep_backups: int = 100,
                              validator: Optional[Callable[[str, Dict[str, Any], str], SyntaxCheckResult]] = None,
                              ) -> Optional[str]:
    """
    Save `data` as the Kea config **only if Kea accepts it**.

    Writes through save_kea_config (so the pre-write backup still happens and a
    failed backup still aborts), runs the daemon's syntax check, and restores
    from that backup when Kea refuses -- leaving the file exactly as it was.

    Returns a warning message when the write succeeded but the check could not
    be run (no Kea binary on this host, say); returns None on a clean success.

    Raises InvalidConfigError when Kea rejected the candidate (file rolled
    back), and BackupError when the backup/write itself failed.
    """
    import os

    from .config_manager import copy_file

    had_file = os.path.isfile(file_path)
    _write_kea_config(data, file_path, backup_dir, keep_backups)

    check = (validator or test_config_file)(file_path, app_config, dhcp_key)
    if check.ok:
        return None

    if not check.ran:
        # The check is unavailable, not failing. Refusing every save would make
        # EZ-KEA unusable on a host where the binary is not on PATH, and the
        # write is exactly what would have happened before this existed.
        return check.output

    # Roll back. copy_file(restore=True) finds the newest backup belonging to
    # *this* config file; save_kea_config just took it, so it is the one.
    rolled_back = False
    if had_file:
        try:
            rolled_back = bool(copy_file(file_path, backup_dir, restore=True))
        except Exception:
            rolled_back = False

    detail = check.output.strip() or "no output"
    if rolled_back:
        raise InvalidConfigError(
            "Kea rejected this configuration, so it was not saved (the previous "
            f"file was restored):\n\n{detail}"
        )
    raise InvalidConfigError(
        "Kea rejected this configuration and it could not be rolled back "
        f"automatically -- the file on disk is the rejected one. Restore a backup "
        f"from the Backups page. Kea said:\n\n{detail}"
    )