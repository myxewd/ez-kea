# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import json
import os
import socket
import threading

import pytest

from ez_kea.core.kea_ctrl import send_command, find_unix_socket_path, ControlChannelError


def _run_fake_kea_socket(path, response_payload):
    """Accept exactly one connection on a UNIX socket, read the request,
    and reply with response_payload (a dict, JSON-encoded)."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def _serve():
        conn, _ = srv.accept()
        with conn:
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            conn.sendall(json.dumps(response_payload).encode("utf-8"))

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return srv, thread


def test_send_command_rejects_unknown_command():
    with pytest.raises(ControlChannelError, match="not an allowed"):
        send_command("/tmp/whatever.sock", "config-set")


def test_send_command_rejects_empty_socket_path():
    with pytest.raises(ControlChannelError, match="No UNIX control socket"):
        send_command("", "ha-heartbeat")


def test_send_command_missing_socket_raises(tmp_path):
    missing = str(tmp_path / "does-not-exist.sock")
    with pytest.raises(ControlChannelError, match="Could not reach"):
        send_command(missing, "ha-heartbeat", timeout=1.0)


def test_send_command_success(tmp_path):
    sock_path = str(tmp_path / "kea-dhcp4-ctrl.sock")
    expected = {"result": 0, "text": "HA peer status returned.", "arguments": {"state": "hot-standby"}}
    srv, thread = _run_fake_kea_socket(sock_path, expected)
    try:
        response = send_command(sock_path, "ha-heartbeat", timeout=2.0)
        assert response == expected
    finally:
        srv.close()
        thread.join(timeout=2)


# ── find_unix_socket_path(): pre-3.0 vs Kea 3.0+ control-socket spelling ────

@pytest.mark.parametrize("dhcp_key", ["Dhcp4", "Dhcp6"])
def test_finds_socket_in_legacy_singular_form(dhcp_key):
    """Pre-3.0 configs use a single `control-socket` object."""
    config = {dhcp_key: {"control-socket": {
        "socket-type": "unix", "socket-name": "/var/run/kea/ctrl.sock",
    }}}
    assert find_unix_socket_path(config, dhcp_key) == "/var/run/kea/ctrl.sock"


@pytest.mark.parametrize("dhcp_key", ["Dhcp4", "Dhcp6"])
def test_finds_socket_in_kea3_list_form(dhcp_key):
    """Kea 3.0 renamed the key to `control-sockets` and made it a list."""
    config = {dhcp_key: {"control-sockets": [{
        "socket-type": "unix", "socket-name": "/var/run/kea/ctrl.sock",
    }]}}
    assert find_unix_socket_path(config, dhcp_key) == "/var/run/kea/ctrl.sock"


def test_finds_unix_socket_alongside_http_listener():
    """The Kea 3.0 direct-API layout: a UNIX socket and an HTTP listener
    side by side. We must pick the UNIX one regardless of ordering."""
    config = {"Dhcp4": {"control-sockets": [
        {"socket-type": "http", "socket-address": "127.0.0.1", "socket-port": 8000},
        {"socket-type": "unix", "socket-name": "/var/run/kea/ctrl.sock"},
    ]}}
    assert find_unix_socket_path(config, "Dhcp4") == "/var/run/kea/ctrl.sock"


def test_http_only_daemon_reports_no_unix_socket():
    """A daemon exposing only HTTP has no channel we can speak; returning ""
    surfaces a clear error instead of connect()ing to a nonexistent path."""
    config = {"Dhcp4": {"control-sockets": [
        {"socket-type": "http", "socket-address": "127.0.0.1", "socket-port": 8000},
    ]}}
    assert find_unix_socket_path(config, "Dhcp4") == ""


def test_socket_type_defaults_to_unix_when_omitted():
    config = {"Dhcp4": {"control-sockets": [{"socket-name": "/var/run/kea/ctrl.sock"}]}}
    assert find_unix_socket_path(config, "Dhcp4") == "/var/run/kea/ctrl.sock"


@pytest.mark.parametrize("config", [
    {},
    {"Dhcp4": {}},
    {"Dhcp4": None},
    {"Dhcp4": {"control-sockets": []}},
    {"Dhcp4": {"control-sockets": "not-a-list"}},
    {"Dhcp4": {"control-sockets": [None, "junk"]}},
    {"Dhcp4": {"control-socket": {"socket-type": "unix"}}},      # no socket-name
    {"Dhcp4": {"control-socket": {"socket-type": "unix", "socket-name": ""}}},
])
def test_missing_or_malformed_socket_config_returns_empty(config):
    """A hand-edited or partial config must not crash the HA status endpoint."""
    assert find_unix_socket_path(config, "Dhcp4") == ""


def test_send_command_malformed_json_raises(tmp_path):
    sock_path = str(tmp_path / "kea-dhcp4-ctrl.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)

    def _serve():
        conn, _ = srv.accept()
        with conn:
            while conn.recv(65536):
                pass
            conn.sendall(b"not json")

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(ControlChannelError, match="Malformed response"):
            send_command(sock_path, "ha-heartbeat", timeout=2.0)
    finally:
        srv.close()
        thread.join(timeout=2)

# -- HTTP/HTTPS control channel -------------------------------------------------
#
# These exercise the transport against a real loopback HTTP server rather than
# a mocked urlopen, so the request body Kea would receive (JSON, POST,
# Content-Type) is what is actually asserted.

import contextlib
import json as _json
import threading as _threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ez_kea.core.kea_ctrl import (
    ALLOWED_HTTP_COMMANDS, ConfigRejectedError, config_document_for_push,
    find_http_endpoint, normalize_http_endpoint, push_config, send_command_any,
    send_http_command,
)


@contextlib.contextmanager
def fake_kea_http(*responses):
    """
    Serve the given responses on a loopback port, one per request, and record
    the decoded request bodies.

    A response is either a bare payload (sent with HTTP 200) or a
    (status, payload) tuple. Yields (endpoint, requests).
    """
    queue = list(responses)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            length = int(self.headers.get("Content-Length", 0) or 0)
            requests.append({
                "path": self.path,
                "content_type": self.headers.get("Content-Type"),
                "authorization": self.headers.get("Authorization"),
                "body": _json.loads(self.rfile.read(length) or b"{}"),
            })
            if queue and isinstance(queue[0], tuple):
                status, payload = queue.pop(0)
            elif queue:
                status, payload = 200, queue.pop(0)
            else:
                status, payload = 500, {"result": 1, "text": "no more canned responses"}
            data = _json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # keep pytest output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d/" % server.server_address[1], requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- endpoint discovery --------------------------------------------------------

def test_finds_http_endpoint_in_kea3_list():
    config = {"Dhcp4": {"control-sockets": [
        {"socket-type": "unix", "socket-name": "/var/run/kea/ctrl.sock"},
        {"socket-type": "http", "socket-address": "192.0.2.10", "socket-port": 8004},
    ]}}
    assert find_http_endpoint(config, "Dhcp4") == "http://192.0.2.10:8004/"


def test_finds_https_endpoint():
    config = {"Dhcp4": {"control-sockets": [
        {"socket-type": "https", "socket-address": "kea.example.org", "socket-port": 8443,
         "cert-file": "/etc/kea/agent.crt", "key-file": "/etc/kea/agent.key"},
    ]}}
    assert find_http_endpoint(config, "Dhcp4") == "https://kea.example.org:8443/"


@pytest.mark.parametrize("dhcp_key,expected", [
    ("Dhcp4", "http://127.0.0.1:8000/"),
    ("Dhcp6", "http://[::1]:8000/"),
])
def test_http_endpoint_defaults_address_and_port_per_family(dhcp_key, expected):
    """The ARM documents 127.0.0.1:8000 for DHCPv4 and [::1]:8000 for DHCPv6."""
    config = {dhcp_key: {"control-sockets": [{"socket-type": "http"}]}}
    assert find_http_endpoint(config, dhcp_key) == expected


def test_http_endpoint_accepts_legacy_singular_control_socket():
    config = {"Dhcp4": {"control-socket": {
        "socket-type": "http", "socket-address": "127.0.0.1", "socket-port": 9000,
    }}}
    assert find_http_endpoint(config, "Dhcp4") == "http://127.0.0.1:9000/"


def test_http_endpoint_wildcard_bind_becomes_loopback():
    """0.0.0.0 is a bind address, not something to connect to."""
    config = {"Dhcp4": {"control-sockets": [
        {"socket-type": "http", "socket-address": "0.0.0.0", "socket-port": 8000},
    ]}}
    assert find_http_endpoint(config, "Dhcp4") == "http://127.0.0.1:8000/"


@pytest.mark.parametrize("config", [
    {},
    {"Dhcp4": {}},
    {"Dhcp4": {"control-sockets": []}},
    {"Dhcp4": {"control-sockets": [{"socket-type": "unix", "socket-name": "/run/kea/x.sock"}]}},
    {"Dhcp4": {"control-sockets": "not-a-list"}},
    {"Dhcp4": {"control-sockets": [None, "junk"]}},
])
def test_no_http_endpoint_when_daemon_has_none(config):
    assert find_http_endpoint(config, "Dhcp4") == ""


def test_http_endpoint_rejects_nonsense_port():
    from ez_kea.core.kea_ctrl import ControlChannelError
    config = {"Dhcp4": {"control-sockets": [
        {"socket-type": "http", "socket-address": "127.0.0.1", "socket-port": "eighty"},
    ]}}
    with pytest.raises(ControlChannelError, match="socket-port"):
        find_http_endpoint(config, "Dhcp4")


@pytest.mark.parametrize("raw,expected", [
    ("127.0.0.1:8000", "http://127.0.0.1:8000/"),
    ("http://kea.lan:8000", "http://kea.lan:8000/"),
    ("https://kea.lan/", "https://kea.lan/"),
    ("http://[::1]:8000/", "http://[::1]:8000/"),
])
def test_normalize_http_endpoint(raw, expected):
    assert normalize_http_endpoint(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "ftp://kea.lan:8000", "http://user:pw@kea.lan:8000", "http://kea.lan:99999",
])
def test_normalize_http_endpoint_rejects_unusable_input(raw):
    from ez_kea.core.kea_ctrl import ControlChannelError
    with pytest.raises(ControlChannelError):
        normalize_http_endpoint(raw)


# -- one command over HTTP -----------------------------------------------------

def test_send_http_command_posts_json_and_unwraps_the_list_wrapper():
    """Kea still wraps HTTP responses in a one-element list (a Control Agent
    compatibility quirk); callers must see the dict inside it."""
    with fake_kea_http([{"result": 0, "text": "Configuration successful."}]) as (endpoint, requests):
        response = send_http_command(endpoint, "status-get")

    assert response == {"result": 0, "text": "Configuration successful."}
    assert requests[0]["content_type"].startswith("application/json")
    assert requests[0]["body"] == {"command": "status-get"}
    assert requests[0]["path"] == "/"


def test_send_http_command_rejects_non_allowlisted_command():
    from ez_kea.core.kea_ctrl import ControlChannelError
    for forbidden in ("shutdown", "lease4-wipe", "config-write", "list-commands"):
        assert forbidden not in ALLOWED_HTTP_COMMANDS
    # config-get is allowlisted on purpose: pulling a selected endpoint's live
    # configuration into its local mirror is the multi-endpoint read path.
    assert "config-get" in ALLOWED_HTTP_COMMANDS
    with pytest.raises(ControlChannelError, match="not an allowed"):
        send_http_command("http://127.0.0.1:1/", "shutdown")


def test_send_http_command_surfaces_kea_text_on_http_error():
    """An auth/forbidden answer is still JSON; the operator gets Kea's words,
    not 'HTTP Error 401'."""
    from ez_kea.core.kea_ctrl import ControlChannelError
    with fake_kea_http((401, {"result": 403, "text": "Forbidden"})) as (endpoint, _):
        with pytest.raises(ControlChannelError, match="Forbidden"):
            send_http_command(endpoint, "status-get")


def test_send_http_command_malformed_body():
    from ez_kea.core.kea_ctrl import ControlChannelError

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            self.send_response(200)
            self.send_header("Content-Length", "7")
            self.end_headers()
            self.wfile.write(b"not jsn")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = "http://127.0.0.1:%d/" % server.server_address[1]
        with pytest.raises(ControlChannelError, match="Malformed response"):
            send_http_command(endpoint, "status-get")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_send_http_command_prefers_a_failure_in_a_multi_service_response():
    """Only a Control Agent fronting several services answers with more than
    one element; a refusal must never be reported as a success."""
    with fake_kea_http([
        {"result": 0, "text": "ok"},
        {"result": 1, "text": "dhcp6 refused"},
    ]) as (endpoint, _):
        response = send_http_command(endpoint, "status-get")
    assert response["result"] == 1
    assert response["text"] == "dhcp6 refused"


def test_send_http_command_unreachable_endpoint():
    from ez_kea.core.kea_ctrl import ControlChannelError
    # Port 1 on loopback: nothing listens there.
    with pytest.raises(ControlChannelError, match="Could not reach"):
        send_http_command("http://127.0.0.1:1/", "status-get", timeout=1.0)


# -- config-test then config-set -----------------------------------------------

DOCUMENT = {"Dhcp4": {"valid-lifetime": 4000, "subnet4": []}}


def test_push_config_tests_before_it_sets():
    with fake_kea_http(
        [{"result": 0, "text": "Configuration seems sane."}],
        [{"result": 0, "text": "Configuration successful."}],
    ) as (endpoint, requests):
        response = push_config(endpoint, "Dhcp4", DOCUMENT)

    assert [r["body"]["command"] for r in requests] == ["config-test", "config-set"]
    assert requests[0]["body"]["arguments"] == DOCUMENT
    assert requests[1]["body"]["arguments"] == DOCUMENT
    assert response["text"] == "Configuration successful."


def test_push_config_does_not_set_when_the_test_is_refused():
    """The whole point: a document Kea will not load never reaches the daemon."""
    with fake_kea_http([{"result": 1, "text": "unsupported parameter: BOGUS"}]) as (endpoint, requests):
        with pytest.raises(ConfigRejectedError) as excinfo:
            push_config(endpoint, "Dhcp4", DOCUMENT)

    assert excinfo.value.phase == "config-test"
    assert "BOGUS" in excinfo.value.detail
    assert [r["body"]["command"] for r in requests] == ["config-test"]


def test_push_config_reports_a_config_set_refusal_separately():
    with fake_kea_http(
        [{"result": 0, "text": "Configuration seems sane."}],
        [{"result": 1, "text": "failed to open lease database"}],
    ) as (endpoint, _):
        with pytest.raises(ConfigRejectedError) as excinfo:
            push_config(endpoint, "Dhcp4", DOCUMENT)

    assert excinfo.value.phase == "config-set"
    assert "lease database" in excinfo.value.detail


def test_config_document_carries_the_logger_block():
    """Dropping Logging makes Kea revert to its default logging on config-set."""
    config = {
        "Logging": {"loggers": [{"name": "kea-dhcp4"}]},
        "Dhcp4": {"valid-lifetime": 4000},
    }
    document = config_document_for_push(config, "Dhcp4")
    assert document["Logging"] == config["Logging"]
    assert document["Dhcp4"] == config["Dhcp4"]


def test_config_document_omits_logging_when_the_file_has_none():
    document = config_document_for_push({"Dhcp4": {"valid-lifetime": 1}}, "Dhcp4")
    assert document == {"Dhcp4": {"valid-lifetime": 1}}


def test_config_document_requires_the_daemon_section():
    from ez_kea.core.kea_ctrl import ControlChannelError
    with pytest.raises(ControlChannelError, match="nothing to push"):
        config_document_for_push({"Dhcp6": {}}, "Dhcp4")


# -- send_command_any(): status over whichever channel exists ------------------

def test_send_command_any_prefers_the_unix_socket(tmp_path):
    sock_path = str(tmp_path / "kea-dhcp4-ctrl.sock")
    srv, thread = _run_fake_kea_socket(sock_path, {"result": 0, "text": "ok"})
    try:
        config = {"Dhcp4": {"control-sockets": [
            {"socket-type": "unix", "socket-name": sock_path},
            {"socket-type": "http", "socket-address": "127.0.0.1", "socket-port": 8000},
        ]}}
        with fake_kea_http({"result": 0, "text": "http should not be used"}) as (endpoint, requests):
            response = send_command_any(config, "Dhcp4", "status-get", timeout=2.0, http_endpoint=endpoint)
        assert response["text"] == "ok"
        assert requests == []
    finally:
        srv.close()
        thread.join(timeout=2)


def test_send_command_any_falls_back_to_http():
    with fake_kea_http([{"result": 0, "text": "running"}]) as (endpoint, requests):
        config = {"Dhcp4": {"control-sockets": [
            {"socket-type": "http", "socket-address": "127.0.0.1", "socket-port": 8000},
        ]}}
        response = send_command_any(config, "Dhcp4", "status-get", http_endpoint=endpoint)

    assert response["text"] == "running"
    assert requests[0]["body"] == {"command": "status-get"}


def test_send_command_any_without_any_channel_raises():
    from ez_kea.core.kea_ctrl import ControlChannelError
    with pytest.raises(ControlChannelError, match="No UNIX or HTTP control"):
        send_command_any({"Dhcp4": {}}, "Dhcp4", "status-get")

# -- basic authentication (Kea 3.x refuses an unauthenticated channel) ---------

def test_send_http_command_sends_basic_credentials():
    import base64 as _base64
    with fake_kea_http({"result": 0}) as (endpoint, requests):
        send_http_command(endpoint, "status-get", auth=("admin", "s3cret"))

    expected = "Basic " + _base64.b64encode(b"admin:s3cret").decode()
    assert requests[0]["authorization"] == expected


def test_send_http_command_sends_no_authorization_when_unauthenticated():
    with fake_kea_http({"result": 0}) as (endpoint, requests):
        send_http_command(endpoint, "status-get")
    assert requests[0]["authorization"] is None


def test_a_401_says_credentials_are_the_likely_cause():
    """Kea answers 401 with {"result": 401, "text": "Unauthorized"} when basic
    auth is configured and the request carries none -- the message must point
    at the environment variables rather than at the URL."""
    from ez_kea.core.kea_ctrl import ControlChannelError
    with fake_kea_http((401, {"result": 401, "text": "Unauthorized"})) as (endpoint, _):
        with pytest.raises(ControlChannelError) as excinfo:
            send_http_command(endpoint, "status-get")
    assert "KEA_HTTP_USER" in str(excinfo.value)


def test_push_config_carries_credentials_on_both_commands():
    with fake_kea_http({"result": 0}, {"result": 0}) as (endpoint, requests):
        push_config(endpoint, "Dhcp4", DOCUMENT, auth=("admin", "s3cret"))

    assert [r["authorization"] is not None for r in requests] == [True, True]


