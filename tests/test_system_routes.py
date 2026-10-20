# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import json
import os
import time
import pytest
from flask import Flask
from unittest.mock import patch, mock_open

# We need to test the endpoints in system_bp
from ez_kea.routes.system import system_bp

@pytest.fixture
def app(tmp_path):
    # Setup a minimalist Flask app for testing the blueprint. Routes are now
    # @login_required, so this also wires up (a test-only, tmp_path-backed)
    # db + login_manager rather than the real ones create_app() would use.
    from ez_kea import db, login_manager

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path}/test.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["DHCP_CONFIG_FILE"] = "/dummy/path/kea-dhcp4.conf"
    app.config["DHCP6_CONFIG_FILE"] = "/dummy/path/kea-dhcp6.conf"
    app.config["BACKUP_DIR"] = "/dummy/path/backups/"
    app.config["KEA_DHCP4_CMD"] = "kea-dhcp4"
    app.config["KEA_DHCP6_CMD"] = "kea-dhcp6"
    app.config["KEA_CTRL_CMD"] = "keactrl"
    app.register_blueprint(system_bp)
    db.init_app(app)
    login_manager.init_app(app)

    # Register a loader on this test's own app. The real one lives on the
    # module-level login_manager in ez_kea/auth.py, so without this the module
    # only works when something else has already imported auth in the same
    # process -- running this file alone failed every @login_required route
    # with "Missing user_loader".
    from ez_kea.models import User

    @login_manager.user_loader
    def _load_user(user_id):
        return db.session.get(User, int(user_id))

    with app.app_context():
        db.create_all()
    yield app

@pytest.fixture
def client(app):
    from conftest import login
    return login(app.test_client(), app)

@pytest.fixture(autouse=True)
def mock_kea_binaries(monkeypatch):
    """
    The `kea-dhcp4`/`keactrl` binaries used throughout these tests are never
    actually installed in the test environment — only `subprocess.run` itself
    is mocked. Since the command-injection guard requires the configured
    command to resolve to a real, executable binary before we even attempt to
    run it, simulate that resolution succeeding for the allowed binary names
    so these tests continue to exercise the subprocess-mocking behavior they
    care about. Basename allowlisting (the actual security boundary) is NOT
    bypassed by this — an unlisted name like '/tmp/evil.sh' is still rejected
    regardless of what `which`/`access` report.
    """
    monkeypatch.setattr(
        "ez_kea.core.security.shutil.which",
        lambda name: f"/usr/sbin/{name}",
    )
    monkeypatch.setattr("ez_kea.core.security.os.access", lambda path, mode: True)
    monkeypatch.setattr("ez_kea.core.security.os.path.isfile", lambda path: True)

@pytest.fixture
def global_settings_app(tmp_path):
    """A fully-wired app (via create_app, matching the "main.system.*"
    endpoint names used internally by redirect/url_for) with real backing
    files, for exercising /global-settings and /save-global-settings."""
    from ez_kea import create_app

    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")

    app = create_app(config_overrides={
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/test.db",
    })
    app.config["TESTING"] = True
    # These tests exercise timer/interface validation logic, not CSRF (which
    # has its own dedicated coverage in test_csrf.py) — disable it here so a
    # raw form POST isn't rejected before reaching the code under test.
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SETTINGS_FILE"] = str(settings_file)
    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text('{"Dhcp4": {"shared-networks": []}}')
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    app.config["BACKUP_DIR"] = str(tmp_path / "backups")
    app.config["DHCP_LEASES_FILE"] = str(tmp_path / "leases.csv")
    app.config["DHCP_LOG_FILE"] = str(tmp_path / "kea.log")

    config6_file = tmp_path / "kea-dhcp6.conf"
    config6_file.write_text('{"Dhcp6": {"shared-networks": []}}')
    app.config["DHCP6_CONFIG_FILE"] = str(config6_file)
    app.config["DHCP6_LEASES_FILE"] = str(tmp_path / "leases6.csv")
    app.config["DHCP6_LOG_FILE"] = str(tmp_path / "kea6.log")
    yield app

@pytest.fixture
def global_settings_client(global_settings_app):
    from conftest import login
    return login(global_settings_app.test_client(), global_settings_app)

@patch('ez_kea.routes.system.copy_file')
def test_backup_config(mock_copy_file, client):
    """Test the /backup-config endpoint successfully calls copy_file."""
    response = client.post("/backup-config", headers={"Referer": "/some/page"})
    
    # Check that copy_file was called with the correct app config vars
    mock_copy_file.assert_called_once_with("/dummy/path/kea-dhcp4.conf", "/dummy/path/backups/")
    # Check that we redirect back to the referrer
    assert response.status_code == 302
    assert response.location == "/some/page"

@patch('ez_kea.routes.system.copy_file')
def test_restore_config(mock_copy_file, client):
    """Test the /restore-config endpoint successfully calls copy_file with restore=True."""
    response = client.post("/restore-config", headers={"Referer": "/some/page"})
    
    # Check that copy_file was called with restore=True
    mock_copy_file.assert_called_once_with("/dummy/path/kea-dhcp4.conf", "/dummy/path/backups/", restore=True)
    # Check that we redirect back to the referrer
    assert response.status_code == 302
    assert response.location == "/some/page"

@patch('ez_kea.routes.system.copy_file')
def test_backup_config_failure(mock_copy_file, client):
    """Test /backup-config handles errors."""
    mock_copy_file.side_effect = Exception("Permission denied")
    response = client.post("/backup-config")
    
    assert response.status_code == 500
    assert b"Permission denied" in response.data

@patch('subprocess.run')
def test_syntax_check_success(mock_subprocess_run, client):
    """Test /test-config endpoint when Kea syntax check passes."""
    # Setup mock to return a successful result (return code 0)
    mock_subprocess_run.return_value.returncode = 0
    
    response = client.post("/test-config")
    
    # Check that subprocess.run was called with the right constructed command
    mock_subprocess_run.assert_called_once()
    called_args = mock_subprocess_run.call_args[0][0]
    assert "kea-dhcp4" in called_args
    assert "-t" in called_args
    assert "/dummy/path/kea-dhcp4.conf" in called_args
    
    assert response.status_code == 200
    assert response.json == {"message": "Syntax check passed!"}

@patch('subprocess.run')
def test_syntax_check_failure(mock_subprocess_run, client):
    """Test /test-config endpoint when Kea syntax check fails."""
    # A failed check is a non-zero exit whose output carries Kea's complaint
    # (see core/config_validate.py -- the exit code is read, not `check=True`).
    mock_subprocess_run.return_value.returncode = 1
    mock_subprocess_run.return_value.stdout = "Some stdout info"
    mock_subprocess_run.return_value.stderr = "Syntax check failed: Parse error at line 10"
    
    response = client.post("/test-config")
    
    assert response.status_code == 500
    assert "Syntax error" in response.json["error"]
    assert "Parse error at line 10" in response.json["error"]
    assert "Some stdout info" in response.json["error"]

@patch('subprocess.run')
def test_apply_config_success(mock_subprocess_run, client):
    """Test /apply-config endpoint successfully reloads."""
    # Setup mock to return a successful result for BOTH test-config and apply-config
    mock_subprocess_run.return_value.returncode = 0
    
    response = client.post("/apply-config")
    
    assert mock_subprocess_run.call_count == 2
    # Verify the reload command was the second one
    called_args = mock_subprocess_run.call_args_list[1][0][0]
    assert "keactrl" in called_args
    assert "reload" in called_args
    
    assert response.status_code == 200
    assert response.json == {"message": "KEA service reloaded successfully!"}

@patch('subprocess.run')
def test_apply_config_syntax_failure(mock_subprocess_run, client):
    """Test /apply-config endpoint fails and aborts if syntax check fails."""
    mock_subprocess_run.return_value.returncode = 1
    mock_subprocess_run.return_value.stdout = ""
    mock_subprocess_run.return_value.stderr = "Syntax check failed!"
    
    response = client.post("/apply-config")
    
    # Should fail in test_config step, so only 1 call is made
    mock_subprocess_run.assert_called_once()
    assert response.status_code == 500
    assert "Syntax error" in response.json["error"]

# ── control-socket reload strategy (Kea 3.x, which ships no keactrl) ────────

def _use_control_socket_strategy(app, tmp_path, dhcp_key="Dhcp4", version="4"):
    """Point the app at a real config file carrying a UNIX control socket and
    select the control-socket reload strategy."""
    config_file = tmp_path / f"kea-dhcp{version}.conf"
    config_file.write_text(json.dumps({dhcp_key: {"control-sockets": [
        {"socket-type": "unix", "socket-name": f"/var/run/kea/kea-dhcp{version}-ctrl.sock"},
    ]}}))
    app.config["DHCP6_CONFIG_FILE" if version == "6" else "DHCP_CONFIG_FILE"] = str(config_file)
    app.config["KEA_RELOAD_STRATEGY"] = "control-socket"


@pytest.mark.parametrize("version,dhcp_key", [("4", "Dhcp4"), ("6", "Dhcp6")])
@patch('subprocess.run')
def test_apply_config_reloads_over_control_socket(mock_subprocess_run, client, app, tmp_path, version, dhcp_key):
    """ISC's Kea 3.x packages ship no keactrl, so the reload must be able to
    go straight down the daemon's own control socket instead."""
    mock_subprocess_run.return_value.returncode = 0
    _use_control_socket_strategy(app, tmp_path, dhcp_key, version)

    with patch('ez_kea.routes.system.send_command', return_value={"result": 0, "text": "ok"}) as send:
        response = client.post(f"/apply-config/{version}")

    assert response.status_code == 200
    assert response.json["message"] == "KEA service reloaded successfully!"
    assert send.call_args[0][1] == "config-reload"
    assert send.call_args[0][0] == f"/var/run/kea/kea-dhcp{version}-ctrl.sock"
    # Only the syntax check shelled out; the reload itself used no binary.
    mock_subprocess_run.assert_called_once()


@patch('subprocess.run')
def test_apply_config_control_socket_reports_daemon_refusal(mock_subprocess_run, client, app, tmp_path):
    """Unlike SIGHUP, this strategy gets a real answer back — a refused reload
    must surface as an error rather than a cheerful success."""
    mock_subprocess_run.return_value.returncode = 0
    _use_control_socket_strategy(app, tmp_path)

    with patch('ez_kea.routes.system.send_command',
               return_value={"result": 1, "text": "configuration rejected"}):
        response = client.post("/apply-config")

    assert response.status_code == 500
    assert "configuration rejected" in response.json["error"]


@patch('subprocess.run')
def test_apply_config_control_socket_unreachable_is_reported(mock_subprocess_run, client, app, tmp_path):
    mock_subprocess_run.return_value.returncode = 0
    _use_control_socket_strategy(app, tmp_path)

    from ez_kea.core.kea_ctrl import ControlChannelError
    with patch('ez_kea.routes.system.send_command',
               side_effect=ControlChannelError("Could not reach control socket")):
        response = client.post("/apply-config")

    assert response.status_code == 500
    assert "control socket" in response.json["error"].lower()


@patch('subprocess.run')
def test_apply_config_control_socket_requires_a_configured_socket(mock_subprocess_run, client, app, tmp_path):
    """Selecting the strategy against a config with no UNIX socket must fail
    loudly at apply time rather than silently doing nothing."""
    mock_subprocess_run.return_value.returncode = 0
    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text(json.dumps({"Dhcp4": {"control-sockets": [
        {"socket-type": "http", "socket-address": "127.0.0.1", "socket-port": 8000},
    ]}}))
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    app.config["KEA_RELOAD_STRATEGY"] = "control-socket"

    response = client.post("/apply-config")

    assert response.status_code == 500
    assert "no UNIX control socket" in response.json["error"]


def test_config_reload_is_allowlisted_but_arbitrary_commands_are_not():
    """The control channel stays an allowlist — adding config-reload must not
    turn it into a general command channel."""
    from ez_kea.core.kea_ctrl import ALLOWED_COMMANDS
    assert "config-reload" in ALLOWED_COMMANDS
    for forbidden in ("config-set", "shutdown", "lease4-wipe", "config-write"):
        assert forbidden not in ALLOWED_COMMANDS

# ── HTTP API reload strategy: config-test first, then config-set ───────────

def _use_http_strategy(app, tmp_path, dhcp_key="Dhcp4", version="4", url=""):
    """Point the app at a real config file exposing an http control socket and
    select the http strategy."""
    config_file = tmp_path / ("kea-dhcp%s.conf" % version)
    config_file.write_text(json.dumps({dhcp_key: {
        "valid-lifetime": 4000,
        "control-sockets": [
            {"socket-type": "http", "socket-address": "127.0.0.1", "socket-port": 8000},
        ],
    }}))
    app.config["DHCP6_CONFIG_FILE" if version == "6" else "DHCP_CONFIG_FILE"] = str(config_file)
    app.config["KEA_RELOAD_STRATEGY"] = "http"
    app.config["KEA_HTTP_URL6" if version == "6" else "KEA_HTTP_URL"] = url
    return config_file


@patch('subprocess.run')
def test_apply_config_http_pushes_the_on_disk_document(mock_run, client, app, tmp_path):
    """The http strategy applies by asking the daemon itself: config-test on the
    document EZ-KEA just wrote, then config-set. No reload, no signal, no
    binary beyond the ordinary -t check."""
    mock_run.return_value.returncode = 0
    config_file = _use_http_strategy(app, tmp_path)

    with patch('ez_kea.routes.system.push_config',
               return_value={"result": 0, "text": "Configuration successful.",
                             "arguments": {"hash": "0123456789abcdef"}}) as push:
        response = client.post("/apply-config")

    assert response.status_code == 200
    assert "config-test, then config-set" in response.json["message"]
    assert "0123456789ab" in response.json["message"]

    endpoint, dhcp_key, document = push.call_args[0]
    assert endpoint == "http://127.0.0.1:8000/"
    assert dhcp_key == "Dhcp4"
    # Exactly the file that was written, wrapped the way Kea expects.
    assert document["Dhcp4"] == json.loads(config_file.read_text())["Dhcp4"]
    # Only the syntax check shelled out.
    mock_run.assert_called_once()


@patch('subprocess.run')
def test_apply_config_http_refused_test_applies_nothing(mock_run, client, app, tmp_path):
    """A refused config-test must stop the apply: the daemon never sees a
    config-set, and the operator is told nothing was applied."""
    from ez_kea.core.kea_ctrl import ConfigRejectedError
    mock_run.return_value.returncode = 0
    _use_http_strategy(app, tmp_path)

    with patch('ez_kea.routes.system.push_config',
               side_effect=ConfigRejectedError("config-test", "unsupported parameter: BOGUS")):
        response = client.post("/apply-config")

    assert response.status_code == 500
    error = response.json["error"]
    assert "config-test" in error
    assert "BOGUS" in error
    assert "previous configuration" in error


@patch('subprocess.run')
def test_apply_config_http_distinguishes_a_config_set_refusal(mock_run, client, app, tmp_path):
    from ez_kea.core.kea_ctrl import ConfigRejectedError
    mock_run.return_value.returncode = 0
    _use_http_strategy(app, tmp_path)

    with patch('ez_kea.routes.system.push_config',
               side_effect=ConfigRejectedError("config-set", "failed to open lease database")):
        response = client.post("/apply-config")

    assert response.status_code == 500
    error = response.json["error"]
    assert "config-set" in error
    assert "lease database" in error


@patch('subprocess.run')
def test_apply_config_http_transport_failure_is_reported(mock_run, client, app, tmp_path):
    from ez_kea.core.kea_ctrl import ControlChannelError
    mock_run.return_value.returncode = 0
    _use_http_strategy(app, tmp_path)

    with patch('ez_kea.routes.system.push_config',
               side_effect=ControlChannelError("Could not reach Kea's HTTP control channel")):
        response = client.post("/apply-config")

    assert response.status_code == 500
    assert "HTTP API" in response.json["error"]


@patch('subprocess.run')
def test_apply_config_http_requires_an_http_socket(mock_run, client, app, tmp_path):
    """Selecting the strategy against a daemon that only has a UNIX socket must
    fail loudly rather than silently doing nothing."""
    mock_run.return_value.returncode = 0
    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text(json.dumps({"Dhcp4": {"control-sockets": [
        {"socket-type": "unix", "socket-name": "/var/run/kea/kea-dhcp4-ctrl.sock"},
    ]}}))
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    app.config["KEA_RELOAD_STRATEGY"] = "http"
    app.config["KEA_HTTP_URL"] = ""

    response = client.post("/apply-config")

    assert response.status_code == 500
    assert "no http or https control socket" in response.json["error"]


@patch('subprocess.run')
def test_apply_config_http_url_override_wins(mock_run, client, app, tmp_path):
    """KEA_HTTP_URL is how a listener the config file does not describe (another
    host, a published container port) is reached."""
    mock_run.return_value.returncode = 0
    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text(json.dumps({"Dhcp4": {"valid-lifetime": 4000}}))
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    app.config["KEA_RELOAD_STRATEGY"] = "http"
    app.config["KEA_HTTP_URL"] = "kea.lan:9000"

    with patch('ez_kea.routes.system.push_config', return_value={"result": 0}) as push:
        response = client.post("/apply-config")

    assert response.status_code == 200
    assert push.call_args[0][0] == "http://kea.lan:9000/"


@patch('subprocess.run')
def test_apply_config_http_rejects_an_unusable_url_override(mock_run, client, app, tmp_path):
    mock_run.return_value.returncode = 0
    _use_http_strategy(app, tmp_path, url="ftp://kea.lan")

    response = client.post("/apply-config")

    assert response.status_code == 500
    assert "misconfigured" in response.json["error"]
    assert "ftp" in response.json["error"]


@patch('subprocess.run')
def test_apply_config_http_passes_credentials_from_the_environment(mock_run, client, app, tmp_path):
    """Kea 3.x refuses an unauthenticated HTTP channel, so the credential EZ-KEA
    sends is what makes the strategy usable at all."""
    mock_run.return_value.returncode = 0
    _use_http_strategy(app, tmp_path)
    app.config["KEA_HTTP_USER"] = "admin"
    app.config["KEA_HTTP_PASSWORD"] = "s3cret"

    with patch('ez_kea.routes.system.push_config', return_value={"result": 0}) as push:
        response = client.post("/apply-config")

    assert response.status_code == 200
    assert push.call_args[1]["auth"] == ("admin", "s3cret")


@patch('subprocess.run')
def test_apply_config_http_rejects_half_a_credential(mock_run, client, app, tmp_path):
    """Half a credential can only produce a 401, so say so before connecting."""
    mock_run.return_value.returncode = 0
    _use_http_strategy(app, tmp_path)
    app.config["KEA_HTTP_USER"] = "admin"
    app.config["KEA_HTTP_PASSWORD"] = ""

    response = client.post("/apply-config")

    assert response.status_code == 500
    assert "KEA_HTTP_USER and KEA_HTTP_PASSWORD" in response.json["error"]


@patch('subprocess.run')
def test_apply_config_http_works_for_dhcp6(mock_run, client, app, tmp_path):
    mock_run.return_value.returncode = 0
    _use_http_strategy(app, tmp_path, dhcp_key="Dhcp6", version="6")

    with patch('ez_kea.routes.system.push_config', return_value={"result": 0}) as push:
        response = client.post("/apply-config/6")

    assert response.status_code == 200
    assert push.call_args[0][1] == "Dhcp6"
    assert "Dhcp6" in push.call_args[0][2]


# ── daemon status over the HTTP channel ────────────────────────────────────

def test_daemon_status_uses_http_when_that_is_the_only_channel(client, app, tmp_path):
    """An HTTP-only daemon is reachable, not "not configured"."""
    _use_http_strategy(app, tmp_path)

    with patch('ez_kea.core.kea_ctrl.send_http_command',
               return_value={"result": 0, "text": ""}) as http_command:
        response = client.get("/api/daemon-status/4")

    assert response.json["status"] == "running"
    assert http_command.call_args[0][1] == "status-get"


def test_daemon_status_reports_not_configured_without_any_channel(client, app, tmp_path):
    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text(json.dumps({"Dhcp4": {"valid-lifetime": 4000}}))
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    app.config["KEA_HTTP_URL"] = ""

    response = client.get("/api/daemon-status/4")

    assert response.json["status"] == "not_configured"



@patch('subprocess.run')
def test_apply_config_reload_failure(mock_subprocess_run, client):
    """Test /apply-config endpoint fails during the reload step."""
    import subprocess
    # First call (test_config) succeeds, second call (reload) fails
    mock_error = subprocess.CalledProcessError(
        returncode=1,
        cmd=["keactrl", "reload"],
        output="",
        stderr="Failed to connect to Kea Control Socket"
    )
    mock_subprocess_run.side_effect = [subprocess.CompletedProcess(args=[], returncode=0), mock_error]
    
    response = client.post("/apply-config")

    assert mock_subprocess_run.call_count == 2
    assert response.status_code == 500
    assert "KEA service reload failed" in response.json["error"]
    assert "Failed to connect" in response.json["error"]

@patch('subprocess.run')
def test_syntax_check_missing_binary(mock_subprocess_run, client):
    """Regression test: /test-config only caught
    subprocess.CalledProcessError, so a missing Kea binary raised an
    uncaught FileNotFoundError -> raw Flask 500 instead of the app's own
    JSON error format."""
    mock_subprocess_run.side_effect = FileNotFoundError("[Errno 2] No such file or directory: 'kea-dhcp4'")

    response = client.post("/test-config")

    assert response.status_code == 500
    assert response.is_json
    assert "binary not found" in response.json["error"]

@patch('subprocess.run')
def test_apply_config_missing_reload_binary(mock_subprocess_run, client):
    """Regression test: /apply-config's reload step
    only caught subprocess.CalledProcessError, so a missing keactrl binary
    raised an uncaught FileNotFoundError."""
    import subprocess
    mock_subprocess_run.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=0),
        FileNotFoundError("[Errno 2] No such file or directory: 'keactrl'"),
    ]

    response = client.post("/apply-config")

    assert mock_subprocess_run.call_count == 2
    assert response.status_code == 500
    assert response.is_json
    assert "control binary not found" in response.json["error"]

def test_save_global_settings_non_numeric_timer_returns_form_error(global_settings_client):
    """Regression test: save_global_settings() used
    a bare int() with no try/except on timer fields, so a non-numeric
    value caused an unhandled 500 instead of a graceful form error."""
    response = global_settings_client.post("/save-global-settings", data={
        "valid-lifetime": "not-a-number",
    })
    assert response.status_code == 400
    assert b"must be a whole number" in response.data

@pytest.mark.parametrize("value", ["-5", "0", "99999999999"])
def test_save_global_settings_timer_bounds_rejected(global_settings_client, value):
    """Regression test: global timers accepted
    negative, zero, and arbitrarily large values with no bounds check."""
    response = global_settings_client.post("/save-global-settings", data={
        "valid-lifetime": value,
    })
    assert response.status_code == 400
    assert b"must be between" in response.data

def test_save_global_settings_valid_timer_persists(global_settings_app):
    from conftest import login
    client = login(global_settings_app.test_client(), global_settings_app)
    response = client.post("/save-global-settings", data={"valid-lifetime": "4000"})
    assert response.status_code == 302

    import json
    with open(global_settings_app.config["DHCP_CONFIG_FILE"]) as f:
        config = json.load(f)
    assert config["Dhcp4"]["valid-lifetime"] == 4000

def test_save_global_settings_dedupes_interfaces(global_settings_app):
    """Regression test: interfaces-config accepted
    duplicate interface names with no de-duplication."""
    from conftest import login
    client = login(global_settings_app.test_client(), global_settings_app)
    response = client.post("/save-global-settings", data={
        "interfaces-config": "eth0, eth1, eth0, eth1, eth2",
    })
    assert response.status_code == 302

    import json
    with open(global_settings_app.config["DHCP_CONFIG_FILE"]) as f:
        config = json.load(f)
    assert config["Dhcp4"]["interfaces-config"]["interfaces"] == ["eth0", "eth1", "eth2"]


# ── DHCPv6 global settings ───────────────────────────────────────────────────

def test_global_settings_v6_get(global_settings_client):
    response = global_settings_client.get("/global-settings/6")
    assert response.status_code == 200

def test_global_settings_invalid_version_rejected(global_settings_client):
    response = global_settings_client.get("/global-settings/7")
    assert response.status_code == 400

def test_save_global_settings_v6_preferred_lifetime_persists(global_settings_app):
    import json
    from conftest import login
    client = login(global_settings_app.test_client(), global_settings_app)
    response = client.post("/save-global-settings/6", data={
        "valid-lifetime": "4000",
        "preferred-lifetime": "3000",
    })
    assert response.status_code == 302

    with open(global_settings_app.config["DHCP6_CONFIG_FILE"]) as f:
        config = json.load(f)
    assert config["Dhcp6"]["preferred-lifetime"] == 3000
    assert config["Dhcp6"]["valid-lifetime"] == 4000

@pytest.mark.parametrize("value", ["-5", "0", "99999999999"])
def test_save_global_settings_v6_preferred_lifetime_bounds_rejected(global_settings_client, value):
    response = global_settings_client.post("/save-global-settings/6", data={
        "preferred-lifetime": value,
    })
    assert response.status_code == 400
    assert b"must be between" in response.data

def test_save_global_settings_v6_server_id_persists(global_settings_app):
    import json
    from conftest import login
    client = login(global_settings_app.test_client(), global_settings_app)
    response = client.post("/save-global-settings/6", data={
        "server-id-type": "LLT",
        "server-id-identifier": "00:03:00:01:aa:bb:cc:dd:ee:ff",
    })
    assert response.status_code == 302

    with open(global_settings_app.config["DHCP6_CONFIG_FILE"]) as f:
        config = json.load(f)
    assert config["Dhcp6"]["server-id"]["type"] == "LLT"
    assert config["Dhcp6"]["server-id"]["identifier"] == "00:03:00:01:aa:bb:cc:dd:ee:ff"

def test_save_global_settings_v6_options_persist(global_settings_app):
    import json
    from conftest import login
    client = login(global_settings_app.test_client(), global_settings_app)
    response = client.post("/save-global-settings/6", data={
        "opt-dns6": "2001:4860:4860::8888",
        "opt-domain-search6": "home.local",
    })
    assert response.status_code == 302

    with open(global_settings_app.config["DHCP6_CONFIG_FILE"]) as f:
        config = json.load(f)
    opts = {o["name"]: o["data"] for o in config["Dhcp6"]["option-data"]}
    assert opts["dns-servers"] == "2001:4860:4860::8888"
    assert opts["domain-search"] == "home.local"

def test_save_app_settings_v6_repoint_requires_dhcp6_key(global_settings_app, tmp_path):
    """Regression test mirroring the v4 repoint-safety check: repointing
    DHCP6_CONFIG_FILE at a file that already exists but
    isn't a valid Dhcp6 config must be refused, not silently overwritten."""
    import json
    bad_target = tmp_path / "not-a-kea6-config.conf"
    bad_target.write_text('{"Dhcp4": {}}')  # wrong root key for a v6 config

    from conftest import login
    client = login(global_settings_app.test_client(), global_settings_app)
    response = client.post("/save-app-settings/6", data={
        "dhcp6-config-file": str(bad_target),
    })
    assert response.status_code == 302
    # Should NOT have overwritten the bad target file.
    with open(bad_target) as f:
        content = json.load(f)
    assert content == {"Dhcp4": {}}

def test_save_app_settings_v4_does_not_clobber_v6_settings(global_settings_app):
    """Regression test for the settings-file merge bug: saving v4 app
    settings must not wipe out previously-saved v6 app settings (and vice
    versa), since both still live in the same ez-kea-settings.json file."""
    import json
    from conftest import login
    client = login(global_settings_app.test_client(), global_settings_app)

    # Save a distinctive v6 command first.
    client.post("/save-app-settings/6", data={"kea-dhcp6-cmd": "/custom/kea-dhcp6"})
    # Now save v4 app settings — must not blank out the v6 command just set.
    client.post("/save-app-settings", data={"kea-reload-strategy": "control-socket"})

    with open(global_settings_app.config["SETTINGS_FILE"]) as f:
        settings = json.load(f)
    assert settings["kea_dhcp6_cmd"] == "/custom/kea-dhcp6"

def test_save_app_settings_v6_does_not_clobber_v4_settings(global_settings_app):
    import json
    from conftest import login
    client = login(global_settings_app.test_client(), global_settings_app)

    client.post("/save-app-settings", data={"kea-dhcp4-cmd": "/custom/kea-dhcp4"})
    dhcp4_cmd_before = global_settings_app.config["KEA_DHCP4_CMD"]
    client.post("/save-app-settings/6", data={"kea-reload-strategy": "control-socket"})

    with open(global_settings_app.config["SETTINGS_FILE"]) as f:
        settings = json.load(f)
    assert settings["kea_dhcp4_cmd"] == dhcp4_cmd_before


# ── RCE via kea_dhcp4_cmd/kea_ctrl_cmd ──────────────────────────────────────

@patch('subprocess.run')
def test_syntax_check_rejects_arbitrary_binary(mock_subprocess_run, app, client):
    """
    Pointing KEA_DHCP4_CMD at an arbitrary script must be rejected before
    subprocess.run is ever called.
    """
    app.config["KEA_DHCP4_CMD"] = "/tmp/evil.sh"

    response = client.post("/test-config")

    mock_subprocess_run.assert_not_called()
    assert response.status_code == 400
    assert "not an allowed executable" in response.json["error"]

@patch('subprocess.run')
def test_apply_config_rejects_arbitrary_ctrl_binary(mock_subprocess_run, app, client):
    """Same PoC, but via KEA_CTRL_CMD on the reload step."""
    app.config["KEA_CTRL_CMD"] = "/tmp/evil.sh"
    # The syntax step must pass for this to reach the reload guard.
    mock_subprocess_run.return_value.returncode = 0

    response = client.post("/apply-config")

    # test_config (kea-dhcp4) succeeds, but the reload step must refuse to run.
    assert mock_subprocess_run.call_count == 1
    assert response.status_code == 400
    assert "not an allowed executable" in response.json["error"]

@patch('subprocess.run')
def test_syntax_check_allows_docker_exec_pattern(mock_subprocess_run, app, client):
    """The documented `docker exec kea-testbed-kea-1 kea-dhcp4` pattern must still work."""
    mock_subprocess_run.return_value.returncode = 0
    app.config["KEA_DHCP4_CMD"] = "docker exec kea-testbed-kea-1 kea-dhcp4"

    response = client.post("/test-config")

    mock_subprocess_run.assert_called_once()
    called_args = mock_subprocess_run.call_args[0][0]
    assert called_args[:4] == ["docker", "exec", "kea-testbed-kea-1", "kea-dhcp4"]
    assert response.status_code == 200

@patch('subprocess.run')
def test_syntax_check_rejects_docker_exec_non_kea_target(mock_subprocess_run, app, client):
    """`docker exec <container> <anything else>` must not be allowed."""
    app.config["KEA_DHCP4_CMD"] = "docker exec kea-testbed-kea-1 /bin/sh"

    response = client.post("/test-config")

    mock_subprocess_run.assert_not_called()
    assert response.status_code == 400


# ── DHCPv6: version-aware test/apply/backup/restore ─────────────────────────
#
# Regression coverage for the confirmed bug where DHCPv6 pages' Test/Apply/
# Backup/Restore buttons silently acted on the v4 daemon: every route below
# now takes an explicit version ("4" or "6"), defaulting to "4" for the
# original unparameterized URLs so nothing that already points at them breaks.

@patch('ez_kea.routes.system.copy_file')
def test_backup_config_v6(mock_copy_file, client):
    response = client.post("/backup-config/6", headers={"Referer": "/some/page"})
    mock_copy_file.assert_called_once_with("/dummy/path/kea-dhcp6.conf", "/dummy/path/backups/")
    assert response.status_code == 302

@patch('ez_kea.routes.system.copy_file')
def test_restore_config_v6(mock_copy_file, client):
    response = client.post("/restore-config/6", headers={"Referer": "/some/page"})
    mock_copy_file.assert_called_once_with("/dummy/path/kea-dhcp6.conf", "/dummy/path/backups/", restore=True)
    assert response.status_code == 302

@patch('subprocess.run')
def test_syntax_check_v6(mock_subprocess_run, client):
    mock_subprocess_run.return_value.returncode = 0

    response = client.post("/test-config/6")

    called_args = mock_subprocess_run.call_args[0][0]
    assert "kea-dhcp6" in called_args
    assert "-t" in called_args
    assert "/dummy/path/kea-dhcp6.conf" in called_args
    assert response.status_code == 200

@patch('subprocess.run')
def test_apply_config_v6(mock_subprocess_run, client):
    mock_subprocess_run.return_value.returncode = 0

    response = client.post("/apply-config/6")

    assert mock_subprocess_run.call_count == 2
    test_call_args = mock_subprocess_run.call_args_list[0][0][0]
    assert "kea-dhcp6" in test_call_args
    assert response.status_code == 200

@pytest.mark.parametrize("route", ["/test-config", "/apply-config", "/backup-config", "/restore-config"])
def test_invalid_dhcp_version_rejected(route, client):
    """A version other than '4'/'6' must be rejected explicitly, not silently
    fall through to acting on the wrong daemon."""
    response = client.post(f"{route}/7")
    assert response.status_code == 400

def test_test_config_bare_route_still_targets_v4(client):
    """The original unparameterized /test-config route must keep acting on
    v4 — existing bookmarks/scripts/tests must not silently start hitting v6."""
    with patch('subprocess.run') as mock_subprocess_run:
        mock_subprocess_run.return_value.returncode = 0
        response = client.post("/test-config")
    called_args = mock_subprocess_run.call_args[0][0]
    assert "kea-dhcp4" in called_args
    assert "/dummy/path/kea-dhcp4.conf" in called_args
    assert response.status_code == 200


# ── save-app-settings: command validation, log path validation, repoint ──

@pytest.fixture
def full_app(tmp_path):
    """
    A more complete fixture for exercising /save-app-settings, which touches
    settings_manager (SETTINGS_FILE), the leases/log paths, and a real config
    file on disk — unlike the minimal `app` fixture used for test/apply-config.

    save_app_settings() redirects via url_for("main.system.app_settings"),
    so system_bp needs to be nested under a "main" blueprint exactly like the
    real app factory does (ez_kea/routes/__init__.py), not registered standalone.
    """
    from flask import Blueprint
    from ez_kea import db, login_manager
    import ez_kea.auth  # noqa: F401  -- registers login_manager.user_loader

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path}/test.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text('{"Dhcp4": {"shared-networks": []}}')
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    app.config["DHCP_LEASES_FILE"] = str(tmp_path / "kea-leases4.csv")
    app.config["DHCP_LOG_FILE"] = str(tmp_path / "kea-dhcp4.log")
    app.config["BACKUP_DIR"] = str(tmp_path / "backups")
    app.config["SETTINGS_FILE"] = str(tmp_path / "ez-kea-settings.json")
    # A command that cannot exist: these tests are about what the form writes,
    # not about the pre-save syntax gate (which would otherwise make the result
    # depend on whichever Kea version happens to be installed on this machine).
    app.config["KEA_DHCP4_CMD"] = "/nonexistent/kea-dhcp4-not-installed"
    app.config["KEA_CTRL_CMD"] = "keactrl"
    main_bp = Blueprint('main', __name__)
    main_bp.register_blueprint(system_bp)
    app.register_blueprint(main_bp)
    db.init_app(app)
    login_manager.init_app(app)
    with app.app_context():
        from ez_kea.models import User  # noqa: F401
        db.create_all()
    return app

@pytest.fixture
def full_client(full_app):
    from conftest import login
    return login(full_app.test_client(), full_app)

def test_save_app_settings_rejects_new_malicious_dhcp4_cmd(full_client):
    """The same rejection, via the save-settings form field itself."""
    response = full_client.post("/save-app-settings", data={"kea-dhcp4-cmd": "/tmp/evil.sh"})
    assert response.status_code == 302  # redirects back with a flashed error, nothing saved

def test_save_app_settings_unrelated_change_survives_unresolvable_existing_command(full_app, full_client, monkeypatch):
    """
    Regression test: an operator's already-configured kea-dhcp4-cmd/kea-ctrl-cmd
    must NOT be re-validated on every save if the operator isn't touching those
    fields. Otherwise, the moment the previously-valid binary stops resolving
    on this host (e.g. dev machine without Kea installed, PATH change), an
    unrelated settings change (like updating the reload strategy) would be
    blocked outright — a real usability regression uncovered while manually
    verifying the 1.1 fix end-to-end.
    """
    # Simulate the currently-configured kea-dhcp4/keactrl NOT resolving on this host.
    monkeypatch.setattr("ez_kea.core.security.shutil.which", lambda name: None)

    response = full_client.post("/save-app-settings", data={"kea-reload-strategy": "control-socket"})
    assert response.status_code == 302

    import json
    with open(full_app.config["SETTINGS_FILE"]) as f:
        settings = json.load(f)
    assert settings["kea_reload_strategy"] == "control-socket"

def test_save_app_settings_does_not_duplicate_logger_output_options(full_app, full_client):
    """
    Regression test: saving global settings (here, just the reload strategy --
    nothing subnet-related) against the skeleton's own default "kea-dhcp4"
    logger must not leave it with both a pre-existing key and a second,
    differently-spelled key for the same setting. An empty file forces
    load_json() to fall back to the skeleton, which already carries the
    "kea-dhcp4" logger this save path has to match by name.

    The spelling asserted here is Kea's own: "output_options" is the key
    "kea-dhcp4 -t" accepts, and "output-options" is rejected outright with
    "got unexpected keyword" -- verified against a real Kea 2.0.2 (see
    tests/test_scope_config.py, which covers the same fact for the skeleton).
    A config carrying both spellings fails to load either way.
    """
    open(full_app.config["DHCP_CONFIG_FILE"], "w").close()

    response = full_client.post("/save-app-settings", data={"kea-reload-strategy": "control-socket"})
    assert response.status_code == 302

    with open(full_app.config["DHCP_CONFIG_FILE"]) as f:
        written = json.load(f)
    loggers = written["Dhcp4"]["loggers"]
    assert len(loggers) == 1
    assert "output_options" in loggers[0]
    assert "output-options" not in loggers[0]

def test_save_app_settings_rejects_new_malicious_log_file(full_client):
    """An out-of-tree log path must be rejected via the save-settings form field."""
    response = full_client.post("/save-app-settings", data={"dhcp-log-file": "/etc/passwd"})
    assert response.status_code == 302  # redirects back with a flashed error, nothing saved

    with full_client.session_transaction() as sess:
        flashes = dict(sess.get("_flashes", []))
    assert any("outside the allowed log directories" in msg for msg in flashes.values())

def test_save_app_settings_repoint_does_not_overwrite_new_target(full_app, full_client, tmp_path):
    """
    Repointing dhcp-config-file to a fresh path in the same request that saves
    other settings must NOT also write the old config's content over that new
    path.
    """
    canary = tmp_path / "victim_file.txt"
    canary.write_text("original victim content — must not be touched")

    response = full_client.post(
        "/save-app-settings",
        data={"dhcp-config-file": str(canary), "kea-docker-container": "should-not-persist"},
    )
    assert response.status_code == 302
    # The repoint is flagged invalid because the target isn't parseable Kea JSON,
    # so the canary's content must be completely untouched.
    assert canary.read_text() == "original victim content — must not be touched"

def test_save_app_settings_repoint_to_nonexistent_path_is_pure_pointer_switch(full_app, full_client, tmp_path):
    """A repoint to a path that doesn't exist yet (first-time setup) must not carry over old content."""
    new_path = tmp_path / "brand_new_kea_dir" / "kea-dhcp4.conf"

    response = full_client.post(
        "/save-app-settings",
        data={"dhcp-config-file": str(new_path), "kea-docker-container": "should-not-persist"},
    )
    assert response.status_code == 302
    assert new_path.exists()  # bootstrap_config wrote a fresh skeleton

    import json
    with open(full_app.config["SETTINGS_FILE"]) as f:
        settings = json.load(f)
    # The unrelated kea-docker-container edit from this same request must NOT
    # have been applied — repoint is a distinct action.
    assert settings.get("kea_docker_container", "") != "should-not-persist"


# --- Homepage dashboard -------------------------------------------------

@pytest.fixture
def dashboard_app(tmp_path):
    """A fully-wired app (create_app, all blueprints registered — the
    dashboard template links into dhcp4/dhcp6/ha routes) with a real
    STATE_INDEX_DB, for exercising the homepage."""
    from ez_kea import create_app

    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")

    app = create_app(config_overrides={
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/test.db",
    })
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SETTINGS_FILE"] = str(settings_file)

    config_file = tmp_path / "kea-dhcp4.conf"
    config_file.write_text('{"Dhcp4": {"shared-networks": [], "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}}')
    app.config["DHCP_CONFIG_FILE"] = str(config_file)
    app.config["BACKUP_DIR"] = str(tmp_path / "backups")
    app.config["DHCP_LEASES_FILE"] = str(tmp_path / "leases.csv")
    app.config["DHCP_LOG_FILE"] = str(tmp_path / "kea.log")

    config6_file = tmp_path / "kea-dhcp6.conf"
    config6_file.write_text('{"Dhcp6": {"shared-networks": []}}')
    app.config["DHCP6_CONFIG_FILE"] = str(config6_file)
    app.config["DHCP6_LEASES_FILE"] = str(tmp_path / "leases6.csv")
    app.config["DHCP6_LOG_FILE"] = str(tmp_path / "kea6.log")

    app.config["STATE_INDEX_DB"] = str(tmp_path / "stateindex.db")
    yield app

@pytest.fixture
def dashboard_client(dashboard_app):
    from conftest import login
    return login(dashboard_app.test_client(), dashboard_app)


def test_index_renders_dashboard_stats(dashboard_client):
    """The homepage renders with lease/reservation/subnet counts for both
    DHCP versions, not the old static link-tile grid.

    The dashboard_app fixture starts from an empty settings.json, so
    discover_environment() classifies it DEMO for both versions (same as any
    fresh sandbox with no real Kea found) — the dashboard must show the
    static "Demo Mode" badge rather than kick off a live daemon check that
    can only ever fail in this environment.
    """
    response = dashboard_client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Active Leases" in body
    assert "IPv4" in body and "IPv6" in body
    assert body.count("Demo Mode") == 2
    assert "daemon-status-4" not in body
    assert "daemon-status-6" not in body


def test_healthz_is_ok_without_login(dashboard_app):
    """The container healthcheck hits /healthz unauthenticated; it must answer
    whether the database and the managed config are usable, not redirect to a
    login page."""
    response = dashboard_app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_healthz_reports_a_config_it_cannot_read(dashboard_app):
    with open(dashboard_app.config["DHCP_CONFIG_FILE"], "w") as handle:
        handle.write("{ not json")

    response = dashboard_app.test_client().get("/healthz")
    assert response.status_code == 503
    assert "config" in response.get_json()["problems"]


def test_index_shows_live_daemon_check_when_not_demo_mode(dashboard_client, dashboard_app):
    """A LIVE-mode install (real Kea config found for that version) must get
    the async daemon-status badge + client-side fetch, not the demo one."""
    dashboard_app.config["EZ-KEA_MODE"] = "LIVE"
    dashboard_app.config["EZ-KEA6_MODE"] = "LIVE"

    response = dashboard_client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "daemon-status-4" in body
    assert "daemon-status-6" in body
    assert "Demo Mode" not in body


def test_index_renders_with_no_state_index_yet(dashboard_client, dashboard_app):
    """A brand-new install has no state-index DB file on disk yet — the
    dashboard must still render (state_index.connect() creates it) rather
    than 500ing on a fresh deployment."""
    assert not os.path.exists(dashboard_app.config["STATE_INDEX_DB"])
    response = dashboard_client.get("/")
    assert response.status_code == 200


def test_api_daemon_status_not_configured_without_control_socket(dashboard_client):
    """No control socket configured (the fixture's config files have none) —
    a normal state for keactrl/SIGHUP-reload installs, distinct from a
    configured-but-unreachable daemon, so it must NOT read as 'unreachable'."""
    response = dashboard_client.get("/api/daemon-status/4")
    assert response.status_code == 200
    assert response.get_json()["status"] == "not_configured"


def test_api_daemon_status_unreachable_with_configured_but_dead_socket(dashboard_client, dashboard_app):
    """A control socket IS configured but nothing is listening — this is the
    real 'daemon down' case and must read as 'unreachable', not blend in with
    the normal not-configured state above."""
    config_file = dashboard_app.config["DHCP_CONFIG_FILE"]
    with open(config_file, "w") as f:
        json.dump({
            "Dhcp4": {
                "shared-networks": [],
                "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}],
                "control-sockets": [{
                    "socket-type": "unix",
                    "socket-name": str(dashboard_app.config["BACKUP_DIR"]) + "/no-such-daemon.sock",
                }],
            }
        }, f)

    response = dashboard_client.get("/api/daemon-status/4")
    assert response.status_code == 200
    assert response.get_json()["status"] == "unreachable"


def test_api_daemon_status_rejects_bad_version(dashboard_client):
    response = dashboard_client.get("/api/daemon-status/9")
    assert response.status_code == 400


# --- Pool utilization -----------------------------------------------------

from ez_kea.core.state_index import connect as state_index_connect
from ez_kea.routes.system import (
    _pool_size, _subnet_pool_capacity, _subnet_utilization, _pool_groups,
    _utilization_pct, _format_pct,
)


@pytest.mark.parametrize("pool_str,expected", [
    ("192.168.1.100 - 192.168.1.199", 100),
    ("192.168.1.100-192.168.1.100", 1),
    ("192.168.1.0/30", 4),  # bare CIDR, e.g. a hand-edited config
    ("2001:db8::100 - 2001:db8::1ff", 256),
    ("not a pool", 0),
    ("", 0),
])
def test_pool_size(pool_str, expected):
    assert _pool_size(pool_str) == expected


def test_subnet_pool_capacity_excludes_pd_pools():
    """Prefix-delegation pools hand out prefixes, not addresses — a
    different unit that must not get summed into the address capacity."""
    subnet = {
        "subnet": "2001:db8::/64",
        "pools": [{"pool": "2001:db8::10 - 2001:db8::19"}],  # 10
        "pd-pools": [{"prefix": "2001:db8:1::", "prefix-len": 48, "delegated-len": 56}],
    }
    assert _subnet_pool_capacity(subnet) == 10


def test_utilization_pct_and_format():
    assert _utilization_pct(0, 0) is None
    assert _format_pct(_utilization_pct(0, 0)) == "No pools configured"

    assert _utilization_pct(50, 200) == 25.0
    assert _format_pct(25.0) == "25.0%"

    # Capped at 100% even if lease count nominally exceeds pool capacity
    # (stale leases outside a shrunk pool, etc.) — a bar can't render >100%.
    assert _utilization_pct(300, 200) == 100.0

    # A nonzero but sub-0.1% figure (routine on a huge DHCPv6 pool) must not
    # collapse to "0.0%", which would read as "no leases at all".
    assert _format_pct(_utilization_pct(1, 10_000_000)) == "<0.1%"


def test_subnet_utilization_keys_lease_counts_by_cidr():
    subnet = {"subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.10 - 10.0.0.19"}]}  # capacity 10
    util = _subnet_utilization(subnet, {"10.0.0.0/24": 5, "10.9.9.0/24": 999})
    assert util == {"subnet": "10.0.0.0/24", "capacity": 10, "used": 5, "pct": 50.0, "pct_text": "50.0%"}


def test_pool_groups_one_entry_per_shared_network_and_standalone_subnet():
    """Matches how pools()/pools6() group the Pools page: each shared
    network is one "Pool" aggregating its subnets, each standalone subnet is
    its own single-subnet "Pool" — not one flat list of every subnet."""
    config = {
        "Dhcp4": {
            "subnet4": [
                {"subnet": "10.9.0.0/24", "pools": [{"pool": "10.9.0.10 - 10.9.0.19"}]},  # standalone, capacity 10
            ],
            "shared-networks": [
                {"name": "campus", "subnet4": [
                    {"subnet": "10.1.0.0/24", "pools": [{"pool": "10.1.0.10 - 10.1.0.29"}]},  # 20
                    {"subnet": "10.2.0.0/24", "pools": [{"pool": "10.2.0.10 - 10.2.0.29"}]},  # 20
                ]},
            ],
        }
    }
    lease_counts = {"10.1.0.0/24": 10, "10.9.0.0/24": 5}
    groups = _pool_groups(config, "Dhcp4", "subnet4", lease_counts)

    assert len(groups) == 2
    campus = next(g for g in groups if g["name"] == "campus")
    assert campus["capacity"] == 40 and campus["used"] == 10 and len(campus["subnets"]) == 2

    standalone = next(g for g in groups if g["name"] is None)
    assert standalone["capacity"] == 10 and standalone["used"] == 5
    assert standalone["subnets"] == [{
        "subnet": "10.9.0.0/24", "capacity": 10, "used": 5, "pct": 50.0, "pct_text": "50.0%",
    }]


def test_pool_groups_unnamed_shared_network_gets_a_fallback_label():
    config = {"Dhcp4": {"shared-networks": [{"subnet4": []}]}}
    groups = _pool_groups(config, "Dhcp4", "subnet4", {})
    assert groups[0]["name"] == "Unnamed shared network"


# -- KEA_HTTP_URL: the http strategy's endpoint is a saved setting --------------

def test_app_settings_page_offers_the_http_strategy(global_settings_client):
    """The new strategy has to be selectable, with its own endpoint field.

    Uses the create_app-backed fixture because it is the one wired with the
    real template folder (the bare Flask app in full_app has none).
    """
    response = global_settings_client.get("/app-settings")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'value="http"' in body
    assert 'name="dhcp-http-url"' in body
    assert "config-test" in body


def test_save_app_settings_persists_the_http_url(full_app, full_client):
    """The http strategy's endpoint is an app setting, not an env-only knob --
    it has to survive a restart like every other field on that page."""
    response = full_client.post("/save-app-settings", data={
        "kea-reload-strategy": "http",
        "dhcp-http-url": "127.0.0.1:8000",
    })
    assert response.status_code == 302

    with open(full_app.config["SETTINGS_FILE"]) as f:
        settings = json.load(f)
    assert settings["kea_reload_strategy"] == "http"
    # Stored canonicalised, so the field round-trips as what will be connected to.
    assert settings["dhcp_http_url"] == "http://127.0.0.1:8000/"
    assert full_app.config["KEA_HTTP_URL"] == "http://127.0.0.1:8000/"


def test_save_app_settings_keeps_the_two_http_urls_apart(full_app, full_client, tmp_path):
    """Each daemon listens on its own address/port, so the override is per
    version -- a single shared value could only ever apply to one of them."""
    v6_config = tmp_path / "kea-dhcp6.conf"
    v6_config.write_text('{"Dhcp6": {"shared-networks": []}}')
    full_app.config["DHCP6_CONFIG_FILE"] = str(v6_config)
    full_app.config["DHCP6_LEASES_FILE"] = str(tmp_path / "kea-leases6.csv")
    full_app.config["DHCP6_LOG_FILE"] = str(tmp_path / "kea-dhcp6.log")
    full_app.config["KEA_DHCP6_CMD"] = "/nonexistent/kea-dhcp6-not-installed"

    full_client.post("/save-app-settings", data={"dhcp-http-url": "http://10.0.0.1:8000"})
    full_client.post("/save-app-settings/6", data={"dhcp6-http-url": "http://10.0.0.2:8001"})

    with open(full_app.config["SETTINGS_FILE"]) as f:
        settings = json.load(f)
    assert settings["dhcp_http_url"] == "http://10.0.0.1:8000/"
    assert settings["dhcp6_http_url"] == "http://10.0.0.2:8001/"
    assert full_app.config["KEA_HTTP_URL"] == "http://10.0.0.1:8000/"
    assert full_app.config["KEA_HTTP_URL6"] == "http://10.0.0.2:8001/"


def test_save_app_settings_rejects_an_unusable_http_url(full_app, full_client):
    response = full_client.post("/save-app-settings", data={
        "kea-reload-strategy": "http",
        "dhcp-http-url": "ftp://kea.lan",
    })
    assert response.status_code == 302  # redirects back with a flashed error, nothing saved

    with full_client.session_transaction() as sess:
        flashes = dict(sess.get("_flashes", []))
    assert any("dhcp-http-url" in msg for msg in flashes.values())
    assert not full_app.config.get("KEA_HTTP_URL")


def test_save_app_settings_blank_http_url_stays_blank(full_app, full_client):
    """Blank is the normal case: read the endpoint out of the Kea config."""
    response = full_client.post("/save-app-settings", data={"kea-reload-strategy": "http"})
    assert response.status_code == 302

    with open(full_app.config["SETTINGS_FILE"]) as f:
        settings = json.load(f)
    assert settings["dhcp_http_url"] == ""



def test_index_renders_pool_group_with_expandable_subnets(dashboard_client, dashboard_app):
    """A shared network with more than one subnet renders as one expandable
    'Pool' entry, not a flat bar per subnet."""
    config_file = dashboard_app.config["DHCP_CONFIG_FILE"]
    with open(config_file, "w") as f:
        json.dump({
            "Dhcp4": {
                "shared-networks": [{"name": "corp-campus", "subnet4": [
                    {"id": 1, "subnet": "10.0.0.0/24", "pools": [{"pool": "10.0.0.10 - 10.0.0.19"}]},
                    {"id": 2, "subnet": "10.0.1.0/24", "pools": [{"pool": "10.0.1.10 - 10.0.1.19"}]},
                ]}],
                "subnet4": [],
            }
        }, f)

    response = dashboard_client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "corp-campus" in body
    assert "10.0.0.0/24" in body and "10.0.1.0/24" in body
    assert "<details" in body  # expandable, since this group has 2 subnets


def test_index_active_lease_counts_flow_into_pool_utilization(dashboard_client, dashboard_app):
    """An active lease recorded against a subnet in the state index shows up
    in that subnet's (and its pool's) utilization percentage."""
    config_file = dashboard_app.config["DHCP_CONFIG_FILE"]
    with open(config_file, "w") as f:
        json.dump({
            "Dhcp4": {
                "shared-networks": [],
                "subnet4": [{
                    "id": 1, "subnet": "10.0.0.0/24",
                    "pools": [{"pool": "10.0.0.10 - 10.0.0.19"}],  # capacity 10
                }],
            }
        }, f)

    conn = state_index_connect(dashboard_app.config["STATE_INDEX_DB"])
    conn.execute(
        "INSERT INTO state_lease4 (id, address, subnet, state, expire) VALUES (1, '10.0.0.10', '10.0.0.0/24', 0, ?)",
        (int(time.time()) + 3600,),
    )
    conn.commit()
    conn.close()

    response = dashboard_client.get("/")
    assert response.status_code == 200
    assert "10.0%" in response.get_data(as_text=True)  # 1 of 10


def test_index_shows_no_pools_configured_state(dashboard_client):
    """The fixture's default config has subnets but no pools at all — the
    dashboard must show the neutral empty state, not a 0/0 crash."""
    response = dashboard_client.get("/")
    assert response.status_code == 200
    assert "No pools configured" in response.get_data(as_text=True)
