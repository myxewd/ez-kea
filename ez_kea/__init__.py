# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import os
from typing import Any
from flask import Flask
from flask_wtf import CSRFProtect
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from .config import Config
from .__about__ import __version__

csrf = CSRFProtect()
db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access EZ-KEA."
login_manager.login_message_category = "error"


def create_app(config_class: Any = Config, config_overrides: dict | None = None) -> Flask:
    """
    Application factory. Creates and configures the Flask app, initialises
    extensions, seeds the default admin account, and registers blueprints.

    config_overrides lets callers (tests, in particular) replace settings
    like SQLALCHEMY_DATABASE_URI *before* the eager startup side effects
    below (db.create_all/_seed_admin, settings/discovery, config bootstrap)
    run — a plain post-hoc `app.config[...] = ...` after create_app() returns
    is too late for those, since they execute inside this function.
    """
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    app.config.from_object(config_class)
    if config_overrides:
        app.config.update(config_overrides)

    # Warning when SECRET_KEY is still set to the default value.
    if app.config.get("SECRET_KEY") == "dev":
        warning = (
            "SECURITY WARNING: SECRET_KEY is set to the insecure default 'dev'. "
            "Set a strong, random SECRET_KEY environment variable before exposing "
            "this application beyond localhost."
        )
        app.logger.warning(warning)
        print(f"\n{'!'*70}\n{warning}\n{'!'*70}\n", flush=True)

    csrf.init_app(app)
    db.init_app(app)
    login_manager.init_app(app)

    with app.app_context():
        from .models import User, SystemSetting  # noqa: F401
        db.create_all()
        _seed_admin()

    # Apply persisted Kea runtime settings
    from .core.settings_manager import apply_settings_to_app
    apply_settings_to_app(app)

    # Auto-discover Kea environment
    from .core.discovery import discover_environment, discover_environment6
    env_info = discover_environment(app.config)
    app.config["EZ-KEA_MODE"] = env_info["mode"]
    if "dhcp_config_file" in env_info:
        app.config["DHCP_CONFIG_FILE"] = env_info["dhcp_config_file"]
        app.config["KEA_DHCP4_CMD"]    = env_info["kea_dhcp4_cmd"]
        app.config["KEA_CTRL_CMD"]     = env_info["kea_ctrl_cmd"]
        base_dir = os.path.dirname(env_info["dhcp_config_file"])
        if env_info["mode"] == "DEMO":
            app.config["DHCP_LEASES_FILE"] = os.path.join(base_dir, "kea-leases4.csv")
            app.config["DHCP_LOG_FILE"]    = os.path.join(base_dir, "kea-dhcp4.log")
        else:
            app.config["DHCP_LEASES_FILE"] = "/var/lib/kea/kea-leases4.csv"
            app.config["DHCP_LOG_FILE"]    = "/var/log/kea/kea-dhcp4.log"

    env6_info = discover_environment6(app.config)
    app.config["EZ-KEA6_MODE"] = env6_info["mode"]
    if "dhcp6_config_file" in env6_info:
        app.config["DHCP6_CONFIG_FILE"] = env6_info["dhcp6_config_file"]
        app.config["KEA_DHCP6_CMD"]     = env6_info["kea_dhcp6_cmd"]
        base_dir6 = os.path.dirname(env6_info["dhcp6_config_file"])
        if env6_info["mode"] == "DEMO":
            app.config["DHCP6_LEASES_FILE"] = os.path.join(base_dir6, "kea-leases6.csv")
            app.config["DHCP6_LOG_FILE"]    = os.path.join(base_dir6, "kea-dhcp6.log")
        else:
            app.config["DHCP6_LEASES_FILE"] = "/var/lib/kea/kea-leases6.csv"
            app.config["DHCP6_LOG_FILE"]    = "/var/log/kea/kea-dhcp6.log"

    from .core.config_manager import bootstrap_config, bootstrap_config6
    # Only probe the Kea version when a skeleton is actually about to be
    # written -- the probe execs kea-dhcp4, and paying that on every startup to
    # answer a question about a file that already exists would be wasteful.
    # One probe covers both daemons: v4 and v6 ship from the same Kea install.
    legacy_socket = False
    if not (os.path.exists(app.config["DHCP_CONFIG_FILE"])
            and os.path.exists(app.config["DHCP6_CONFIG_FILE"])):
        from .core.kea_version import uses_legacy_control_socket
        legacy_socket = uses_legacy_control_socket(app.config["KEA_DHCP4_CMD"])

    bootstrap_config(
        app.config["DHCP_CONFIG_FILE"], app.config["BACKUP_DIR"],
        legacy_control_socket=legacy_socket,
    )
    bootstrap_config6(
        app.config["DHCP6_CONFIG_FILE"], app.config["BACKUP_DIR"],
        legacy_control_socket=legacy_socket,
    )

    # Keep the searchable log history current. This runs on a background
    # daemon thread and never on a request, so a first-run backfill of months
    # of rotated logs doesn't hold up page loads — /logs just serves whatever
    # is indexed so far while the rest fills in behind it.
    from .core.log_index import start_background_indexer
    start_background_indexer(app)

    # Same idea, for the Leases/Reservations pages' search index.
    from .core.state_index import start_background_state_indexer
    start_background_state_indexer(app)

    # Register blueprints
    from .routes import main_bp
    app.register_blueprint(main_bp)

    from .auth import auth_bp
    app.register_blueprint(auth_bp)

    # A Kea config that exists but cannot be read or backed up is an operator
    # problem with a specific fix, not a crash. Without these, both surfaced as
    # a bare HTTP 500 with the real reason visible only in the server log --
    # which is precisely what a fresh install against a packaged Kea hits,
    # since those ship /etc/kea 0750 _kea:_kea.
    from .core.config_manager import BackupError, ConfigAccessError
    from .core.config_validate import InvalidConfigError

    @app.errorhandler(InvalidConfigError)
    def _handle_invalid_config_error(e: InvalidConfigError):
        """A route that does not catch this itself still gets a page naming the
        reason, instead of a bare 500."""
        from flask import jsonify as _jsonify, render_template, request as _request
        if _request.path.startswith(("/apply-changes", "/test-config", "/backup-config", "/restore-config")):
            return _jsonify({"error": str(e)}), 400
        return render_template("error.html", message=str(e)), 400

    @app.errorhandler(ConfigAccessError)
    def _handle_config_access_error(e: ConfigAccessError):
        from flask import jsonify as _jsonify, render_template, request as _request
        if _request.path.startswith(("/apply-changes", "/test-config", "/backup-config", "/restore-config")):
            return _jsonify({"error": str(e)}), 503
        return render_template("error.html", message=str(e)), 503

    @app.errorhandler(BackupError)
    def _handle_backup_error(e: BackupError):
        from flask import jsonify as _jsonify, render_template, request as _request
        if _request.path.startswith(("/apply-changes", "/test-config", "/backup-config", "/restore-config")):
            return _jsonify({"error": str(e)}), 503
        return render_template("error.html", message=str(e)), 503

    @app.url_defaults
    def add_static_cache_buster(endpoint: str, values: dict) -> None:
        """
        Append a content-derived ?v= to every static URL.

        Deployments are expected to serve /static/ with a long-lived,
        immutable Cache-Control (see demo/nginx/demo.ezkea.com.conf), which is
        what keeps origin bandwidth down. Without a version in the URL, though,
        a CSS or JS change stays invisible to anyone holding a cached copy
        until it expires — potentially weeks. Keying the URL on the file's
        mtime means edited assets get a new URL, and therefore a new cache
        entry, the moment they change.
        """
        if endpoint != "static" or "filename" not in values:
            return
        filename = values["filename"]
        if not isinstance(filename, str):
            return
        try:
            path = os.path.join(app.static_folder, filename)
            values["v"] = str(int(os.path.getmtime(path)))
        except (OSError, TypeError):
            # Missing or unreadable file: emit the plain URL rather than break
            # rendering over a cache optimisation.
            pass

    @app.context_processor
    def inject_globals():
        from .license import license_status
        from .core.validation import get_active_leases
        try:
            active = len(get_active_leases(app.config.get("DHCP_LEASES_FILE", "")))
        except Exception:
            # The lease count only decides how loudly the unlicensed notice is
            # shown, so an unreadable leases file just means "no banner".
            active = 0
        return dict(
            ez_kea_mode=app.config.get("EZ-KEA_MODE", "LIVE"),
            ez_kea_version=__version__,
            license_state=license_status(active),
            # Only truthy on the public demo (see Config.PUBLIC_DEMO), where
            # the login page pre-fills these. Absent everywhere else, so no
            # ordinary install can leak a credential hint into its login page.
            public_demo=(
                {
                    "username": app.config.get("PUBLIC_DEMO_USERNAME", ""),
                    "password": app.config.get("PUBLIC_DEMO_PASSWORD", ""),
                }
                if app.config.get("PUBLIC_DEMO")
                else None
            ),
        )

    return app


def _seed_admin() -> None:
    """Create the default break-glass admin account if no users exist yet."""
    from .models import User
    if User.query.count() == 0:
        from werkzeug.security import generate_password_hash
        admin = User(
            username="admin",
            password_hash=generate_password_hash("changeme"),
            is_admin=True,
            is_break_glass=True,
            must_change_password=True,
            must_change_username=True,
        )
        db.session.add(admin)
        db.session.commit()
