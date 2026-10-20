# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/core/kea_ctrl.py

Talks to a running kea-dhcp4/kea-dhcp6 daemon over its own control channel
using Kea's JSON command-channel protocol. Used for live status checks
(e.g. "ha-heartbeat") that a config-file edit can't answer -- those need a
real response from the running daemon, not the file on disk -- and, over HTTP,
for pushing a whole configuration (config-test then config-set).

This is the direct-to-daemon control channel, NOT the Kea Control Agent
(kea-ctrl-agent), which ISC deprecated in Kea 3.0 and removed in 3.2. Nothing
here ever went through the agent, so that removal doesn't affect us. What did
change is the config key: Kea 3.0 renamed the singular `control-socket`
object to a `control-sockets` list so a daemon can expose UNIX plus HTTP/HTTPS
at once. find_unix_socket_path() and find_http_endpoint() read both spellings.

Only ever sends commands from an allowlist, so there is no path for user input
to choose an arbitrary Kea command to run. The parameterless commands sent over
the UNIX socket cannot carry any argument at all; the only command that carries
a payload is config-test/config-set, whose argument is the config document
EZ-KEA itself just wrote to disk -- never a field from a request.
"""
import base64
import json
import os
import socket
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

# config-reload makes the daemon re-read its own config file from disk. It is
# how you reload a Kea 3.x installed from ISC's packages, which ship no
# keactrl at all -- see the "control-socket" reload strategy in routes/system.py.
ALLOWED_COMMANDS = {"ha-heartbeat", "status-get", "config-reload"}

# The HTTP/HTTPS channel speaks the same protocol but is also the only transport
# that can carry a configuration document, which is what the "http" apply
# strategy in routes/system.py uses. config-test and config-set are here and
# deliberately NOT in ALLOWED_COMMANDS above: they take an `arguments` payload,
# and the UNIX helper is kept strictly parameterless so it can never be turned
# into a way to push a document down a channel that never expected one.
ALLOWED_HTTP_COMMANDS = {
    "ha-heartbeat", "status-get", "config-get", "config-test", "config-set",
    # Pulling a remote endpoint's lease table into its local mirror; both take
    # no arguments, so they cannot be steered by request input.
    "lease4-get-all", "lease6-get-all",
}

# Kea's own HTTP/HTTPS control socket (since 2.7.2) reuses the listener defaults
# the standalone Control Agent had, and the migration section of the ARM says to
# carry `http-host`/`http-port` over as `socket-address`/`socket-port`.
# The default address is per-family: the ARM documents 127.0.0.1 for DHCPv4 and
# ::1 for DHCPv6.
KEA_DEFAULT_HTTP_ADDRESS = {"Dhcp4": "127.0.0.1", "Dhcp6": "::1"}
KEA_DEFAULT_HTTP_PORT = 8000

# Kea abandons a control connection after ten seconds (not configurable), so a
# config command gets exactly that budget -- waiting longer would only be
# describing a request the daemon has already given up on.
HTTP_CONFIG_TIMEOUT_SECONDS = 10.0

# Where Kea resolves a bare `socket-name` (one with no directory component).
# ISC's own packaged kea-dhcp4.conf ships exactly that -- "kea4-ctrl-socket" --
# and Kea 3.x will not accept a socket path outside this directory at all
# ("invalid path specified: '/run/kea', supported path is '/var/run/kea'"), so
# it is the one correct place to look rather than a guess.
KEA_DEFAULT_SOCKET_DIR = "/var/run/kea"


class ControlChannelError(Exception):
    """Raised when the control socket can't be reached or returns unusable data."""


def find_unix_socket_path(config: Dict[str, Any], dhcp_key: str) -> str:
    """
    Return the UNIX control-socket path configured for `dhcp_key` ("Dhcp4" or
    "Dhcp6"), or "" if the daemon has no UNIX socket.

    Accepts both the pre-3.0 singular `control-socket` object and the Kea 3.0+
    `control-sockets` list, since EZ-KEA edits configs it did not necessarily
    write and Kea itself rewrites the singular form to the list form on any
    `config-write`. A daemon exposing only HTTP/HTTPS yields "" -- we speak the
    UNIX channel only, and reporting "no socket" is more honest than handing an
    HTTP listener to a socket connect().
    """
    daemon = config.get(dhcp_key)
    if not isinstance(daemon, dict):
        return ""

    entries = daemon.get("control-sockets", daemon.get("control-socket"))
    if isinstance(entries, dict):  # singular form, or a list-key holding one object
        entries = [entries]
    if not isinstance(entries, list):
        return ""

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # socket-type is mandatory in practice, but treat a missing one as
        # "unix": that is the only type that existed when it was optional.
        if entry.get("socket-type", "unix") != "unix":
            continue
        name = entry.get("socket-name")
        if isinstance(name, str) and name:
            # A bare name is resolved by Kea against its own runtime directory,
            # not against EZ-KEA's working directory. Passing it through
            # verbatim made socket.connect() look for "kea4-ctrl-socket" in
            # whatever cwd EZ-KEA happened to start in, so the control-socket
            # reload strategy could never work against a stock packaged Kea --
            # the one platform where it is the *only* strategy available,
            # keactrl having been dropped.
            if os.path.dirname(name):
                return name
            return os.path.join(KEA_DEFAULT_SOCKET_DIR, name)

    return ""


def send_command(socket_path: str, command: str, timeout: float = 5.0) -> Dict[str, Any]:
    """
    Send a single JSON command to a Kea daemon's UNIX control socket and
    return the parsed JSON response dict.

    Raises ControlChannelError if `command` isn't allowlisted, no socket path
    is configured, the socket can't be reached in time, or the response isn't
    valid JSON.
    """
    if command not in ALLOWED_COMMANDS:
        raise ControlChannelError(f"'{command}' is not an allowed control-channel command")
    if not socket_path:
        raise ControlChannelError(
            "No UNIX control socket is configured for this daemon "
            "(checked both 'control-socket' and 'control-sockets')"
        )

    payload = json.dumps({"command": command}).encode("utf-8")

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path)
            sock.sendall(payload)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass  # some platforms disallow shutdown() on AF_UNIX; recv loop below still works

            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except (OSError, socket.timeout) as e:
        raise ControlChannelError(f"Could not reach control socket '{socket_path}': {e}")

    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not raw:
        raise ControlChannelError("Empty response from control socket")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ControlChannelError(f"Malformed response from control socket: {e}")

    if not isinstance(parsed, dict):
        raise ControlChannelError("Unexpected response shape from control socket")

    return parsed

# ── HTTP/HTTPS control channel ──────────────────────────────────────────────
#
# Kea 2.7.2 gave the daemons themselves an HTTP/HTTPS control socket; the
# standalone Control Agent that used to provide one was removed in 3.1.8. A
# daemon opts in with an entry in its control-sockets list:
#
#   "control-sockets": [
#       {"socket-type": "http", "socket-address": "127.0.0.1", "socket-port": 8000}
#   ]
#
# The command protocol is the same JSON as the UNIX channel. Three differences
# matter here:
#
#   * responses are still wrapped in a one-element list, kept for compatibility
#     with the removed Control Agent, so they are unwrapped below;
#   * HTTP is the only transport that can carry a whole configuration document,
#     which is exactly what config-test/config-set need;
#   * an HTTP endpoint can be remote, so it is operator-configurable rather
#     than only discoverable from the config file.
#
# HTTPS is supported as a transport, verified against the system trust store.
# Client certificates -- which the daemon's cert-required parameter defaults to
# demanding -- are not, so an HTTPS listener has to set cert-required false.
#
# Basic authentication IS supported, and on Kea 3.x it is effectively required:
# the daemon refuses to start with an "Unsecured HTTP control channel" when the
# channel does not authenticate its clients, and refuses a clear-text password
# in its own config just as firmly, so the compliant setup is basic auth with
# user-file/password-file. Credentials therefore come from the environment
# (KEA_HTTP_USER/KEA_HTTP_PASSWORD), never from a file EZ-KEA writes.


def normalize_http_endpoint(raw: str) -> str:
    """
    Validate and canonicalize an operator-supplied control-channel URL.

    Accepts "127.0.0.1:8000", "http://kea.lan:8000/", "[::1]:8000" and the
    like; returns a form that always carries an explicit scheme and a trailing
    slash, so the same value can be compared against what the config file says.

    Raises ControlChannelError for anything that is not a plain http/https URL
    with a host -- including a URL carrying credentials, which would end up in
    the settings file in clear text.
    """
    value = (raw or "").strip()
    if not value:
        raise ControlChannelError("No HTTP endpoint given")
    if "://" not in value:
        value = "http://" + value

    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ControlChannelError(
            f"'{parts.scheme}' is not a usable scheme: the Kea HTTP control channel "
            "speaks plain http or https only"
        )
    if not parts.hostname:
        raise ControlChannelError("the URL has no host")
    if parts.username or parts.password:
        raise ControlChannelError("credentials in the URL are not supported (they would be stored in clear text)")

    raw_port = parts.netloc.rsplit(":", 1)[-1] if ":" in parts.netloc else ""
    try:
        port = parts.port
    except ValueError:
        raise ControlChannelError(f"'{raw_port}' is not a valid port")

    host = parts.hostname
    if ":" in host:  # bare IPv6 literal, must be bracketed in a URL
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parts.scheme, netloc, "/", "", ""))


def _http_endpoint_from_entry(entry: Dict[str, Any], dhcp_key: str) -> str:
    """Build the base URL for one http/https control-sockets entry."""
    scheme = entry.get("socket-type")
    address = entry.get("socket-address")
    port = entry.get("socket-port")

    host = address.strip() if isinstance(address, str) and address.strip() else KEA_DEFAULT_HTTP_ADDRESS.get(dhcp_key, "127.0.0.1")
    # A config may spell an IPv6 literal either way; strip any brackets the
    # operator wrote so they are added back exactly once below.
    host = host.strip("[]")
    # A wildcard bind is not a connectable address; the daemon is on this host
    # or the operator would have set KEA_HTTP_URL explicitly.
    if host in ("0.0.0.0", "*"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"

    if ":" in host:  # bare IPv6 literal, must be bracketed to form a URL
        host = f"[{host}]"
    if port in (None, ""):
        port_number = KEA_DEFAULT_HTTP_PORT
    else:
        try:
            port_number = int(port)
        except (TypeError, ValueError):
            raise ControlChannelError(f"control socket has a non-numeric socket-port ({port!r})")
    if not 0 < port_number < 65536:
        raise ControlChannelError(f"control socket has an out-of-range socket-port ({port_number})")

    return normalize_http_endpoint(f"{scheme}://{host}:{port_number}")


def find_http_endpoint(config: Dict[str, Any], dhcp_key: str) -> str:
    """
    Return the base URL of the daemon's own HTTP/HTTPS control socket for
    dhcp_key ("Dhcp4" or "Dhcp6"), or "" if it exposes none.

    Mirrors find_unix_socket_path(): accepts both the pre-3.0 singular
    control-socket object and the 3.0+ control-sockets list, and ignores
    unix entries. A daemon with only a UNIX socket yields "".
    """
    daemon = config.get(dhcp_key)
    if not isinstance(daemon, dict):
        return ""

    entries = daemon.get("control-sockets", daemon.get("control-socket"))
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return ""

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("socket-type") not in ("http", "https"):
            continue
        return _http_endpoint_from_entry(entry, dhcp_key)

    return ""


def _unwrap_http_response(parsed: Any, endpoint: str) -> Dict[str, Any]:
    """
    Normalize a control-channel HTTP response to the single dict Kea means.

    Kea wraps every HTTP response body in a one-element JSON list for backward
    compatibility with the removed Control Agent (an auth failure is the one
    exception, arriving as a bare object). A multi-element list only happens
    when talking to a Control Agent fronting several services; in that case any
    failing element wins, so a refusal is never reported as a success.
    """
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        responses = [item for item in parsed if isinstance(item, dict)]
        if not responses:
            raise ControlChannelError(f"Empty response from '{endpoint}'")
        failures = [item for item in responses if item.get("result") not in (0, None)]
        return failures[0] if failures else responses[0]
    raise ControlChannelError("Unexpected response shape from the HTTP control channel")


def send_http_command(
    endpoint: str,
    command: str,
    arguments: Optional[Dict[str, Any]] = None,
    timeout: float = 5.0,
    auth: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """
    POST one JSON command to a Kea HTTP/HTTPS control socket and return the
    parsed response dict.

    auth is an optional (user, password) pair sent as HTTP Basic credentials.
    Kea 3.x refuses to start with an unauthenticated HTTP control channel at
    all ("Unsecured HTTP control channel"), and refuses clear-text passwords in
    its own config, so the compliant setup is basic auth with user-file /
    password-file -- which is exactly what this header satisfies.

    Raises ControlChannelError if the command is not allowlisted, the URL is
    unusable, the endpoint cannot be reached, or the body is not the JSON
    response Kea defines.
    """
    if command not in ALLOWED_HTTP_COMMANDS:
        raise ControlChannelError(f"'{command}' is not an allowed HTTP control-channel command")

    url = normalize_http_endpoint(endpoint)
    payload: Dict[str, Any] = {"command": command}
    if arguments is not None:
        payload["arguments"] = arguments

    headers = {"Content-Type": "application/json"}
    if auth:
        user, password = auth
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token

    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        # Kea answers 4xx/5xx with a JSON body carrying the reason; surface that
        # rather than "HTTP Error 400: Bad Request".
        detail = ""
        try:
            detail = _unwrap_http_response(json.loads(e.read().decode("utf-8", "replace")), url).get("text", "")
        except (ValueError, OSError, ControlChannelError):
            detail = ""
        hint = ""
        if e.code in (401, 403):
            # Kea 3.x requires authentication on an HTTP control channel, so a
            # 401 here is the expected first symptom of a missing credential
            # rather than something wrong with the endpoint.
            hint = " -- the daemon is demanding credentials; set KEA_HTTP_USER and KEA_HTTP_PASSWORD" if not auth else ""
        raise ControlChannelError(
            f"Kea's HTTP control channel at '{url}' answered HTTP {e.code}"
            + (f": {detail}" if detail else "")
            + hint
        )
    except URLError as e:
        raise ControlChannelError(f"Could not reach Kea's HTTP control channel at '{url}': {e.reason}")
    except (OSError, socket.timeout) as e:
        raise ControlChannelError(f"Could not reach Kea's HTTP control channel at '{url}': {e}")

    raw = raw.strip()
    if not raw:
        raise ControlChannelError(f"Empty response from Kea's HTTP control channel at '{url}'")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ControlChannelError(f"Malformed response from Kea's HTTP control channel at '{url}': {e}")

    return _unwrap_http_response(parsed, url)


def config_document_for_push(config: Dict[str, Any], dhcp_key: str) -> Dict[str, Any]:
    """
    The document to send to config-test/config-set: the daemon's own section,
    plus the optional top-level Logging block that Kea expects alongside it.

    Logging is included whenever the file has it because Kea reverts to its
    default logging when a config-set does not carry one -- an apply would
    otherwise quietly undo the operator's logger configuration.
    """
    section = config.get(dhcp_key)
    if not isinstance(section, dict):
        raise ControlChannelError(f"No '{dhcp_key}' section in the config file; nothing to push")

    document: Dict[str, Any] = {}
    logging_section = config.get("Logging")
    if isinstance(logging_section, dict):
        document["Logging"] = logging_section
    document[dhcp_key] = section
    return document


class ConfigRejectedError(ControlChannelError):
    """
    Kea answered, and its answer was no.

    Carries which command was refused ("config-test" or "config-set") and Kea's
    own explanation, because the two mean different things to an operator: a
    refused config-test changed nothing at all, while a refused config-set had
    to be rolled back by the daemon itself.
    """

    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(f"Kea refused {phase}: {detail}")
        self.phase = phase
        self.detail = detail


def push_config(
    endpoint: str,
    dhcp_key: str,
    document: Dict[str, Any],
    timeout: float = HTTP_CONFIG_TIMEOUT_SECONDS,
    auth: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """
    Apply a configuration over the HTTP channel: config-test first, config-set
    only if Kea accepted the test.

    The whole point of the two steps is that a document Kea will not load never
    reaches the running daemon -- config-test is Kea's own parser (not a
    reimplementation of it) evaluating this exact document. It is not a full
    guarantee: the ARM notes a document can pass config-test and still fail
    config-set, which is why a refusal at either step is reported as such and
    never silently ignored.

    config-set keeps the new configuration in memory only, which is the right
    behaviour here: the durable copy is the file EZ-KEA already wrote and
    validated, so a restart of the daemon lands on the same configuration.

    Returns the config-set response (Kea 2.4+ includes a SHA-256 digest of the
    configuration it now holds). Raises ConfigRejectedError on a refusal and
    ControlChannelError on a transport problem.
    """
    test_response = send_http_command(endpoint, "config-test", document, timeout=timeout, auth=auth)
    if test_response.get("result") != 0:
        raise ConfigRejectedError("config-test", str(test_response.get("text") or "no explanation given"))

    set_response = send_http_command(endpoint, "config-set", document, timeout=timeout, auth=auth)
    if set_response.get("result") != 0:
        raise ConfigRejectedError("config-set", str(set_response.get("text") or "no explanation given"))

    return set_response


def send_command_any(
    config: Dict[str, Any],
    dhcp_key: str,
    command: str,
    timeout: float = 5.0,
    http_endpoint: str = "",
    http_auth: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """
    Send a parameterless status command over whichever control channel the
    daemon actually exposes: the UNIX socket when there is one, else its
    HTTP/HTTPS listener.

    http_endpoint lets a caller pass an operator-configured URL (KEA_HTTP_URL),
    which is the only way to reach a daemon whose listener lives somewhere the
    config file does not describe.
    """
    socket_path = find_unix_socket_path(config, dhcp_key)
    if socket_path:
        return send_command(socket_path, command, timeout=timeout)

    endpoint = http_endpoint or find_http_endpoint(config, dhcp_key)
    if endpoint:
        return send_http_command(endpoint, command, timeout=timeout, auth=http_auth)

    raise ControlChannelError(
        f"No UNIX or HTTP control channel is configured for {dhcp_key}"
    )

