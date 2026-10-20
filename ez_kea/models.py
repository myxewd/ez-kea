# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/models.py

SQLAlchemy models for EZ-KEA user authentication, licensing, and settings.
"""
from datetime import datetime
from flask_login import UserMixin
from . import db


class User(UserMixin, db.Model):
    """Application user account."""
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(64), unique=True, nullable=False)
    name          = db.Column(db.String(128), default="")
    password_hash = db.Column(db.String(256), nullable=False)
    email         = db.Column(db.String(128), default="")
    is_admin      = db.Column(db.Boolean, default=False)

    # TOTP two-factor authentication — opt-in per user via Profile.
    # totp_enabled stays False until a real code is verified, so a
    # half-finished enrollment can never lock anyone out.
    totp_secret   = db.Column(db.String(64), default="")
    totp_enabled  = db.Column(db.Boolean, default=False)
    # Admin-forced enrollment: next login redirects through 2FA setup
    # before reaching the dashboard. Cleared once enrollment completes.
    totp_required = db.Column(db.Boolean, default=False)

    # One-time-use 2FA backup codes — JSON list of Werkzeug-hashed codes.
    # Generated when the break-glass account completes TOTP enrollment.
    recovery_codes = db.Column(db.Text, default="[]")

    # Self-service "forgot password" reset token (hashed, not plaintext).
    reset_token_hash    = db.Column(db.String(255), nullable=True)
    reset_token_expires = db.Column(db.DateTime, nullable=True)

    # Force a credential change at next login (set when an admin assigns
    # a password, or for the seeded default account).
    must_change_password = db.Column(db.Boolean, default=False)
    # Only set on the seeded default account — forces moving off "admin".
    must_change_username = db.Column(db.Boolean, default=False)

    # Marks the original seeded admin account as permanently undeletable
    # and un-de-adminable — a permanent recovery login.
    is_break_glass = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login  = db.Column(db.DateTime, nullable=True)


class SystemSetting(db.Model):
    """Key-value store for application settings (e.g., license key)."""
    __tablename__ = "system_settings"

    key   = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, default="")

class KeaInstance(db.Model):
    """
    One Kea management endpoint the operator can point EZ-KEA at.

    Two kinds, because a Kea host can be reached two ways:

    * "local" -- EZ-KEA runs on the Kea host and edits the daemon's own config
      files (the pre-existing single-instance model, now one row among many).
    * "http" -- the daemon exposes its own HTTP/HTTPS control socket, so
      EZ-KEA pulls the live configuration with config-get into a local mirror
      file, edits that mirror, and pushes it back with config-test/config-set.
      The mirror is a cache of what the daemon reported, so a page render never
      has to wait for the network.

    The password column is plaintext, exactly like the SMTP password in
    SystemSetting: the database is the trust boundary here, and the column is
    never rendered back to the browser.
    """
    __tablename__ = "kea_instances"

    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(64), nullable=False)
    mode             = db.Column(db.String(8), nullable=False, default="local")

    # "local" mode: the files the daemon itself reads.
    dhcp4_config_file = db.Column(db.String(512), default="")
    dhcp6_config_file = db.Column(db.String(512), default="")

    # "http" mode: the daemon's control channels (v6 optional) and credentials.
    http_url      = db.Column(db.String(255), default="")
    http_url6     = db.Column(db.String(255), default="")
    http_user     = db.Column(db.String(128), default="")
    http_password = db.Column(db.String(255), default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

