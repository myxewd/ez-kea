# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
tests/conftest.py

Shared test helpers for logging a Flask test client in as a real,
DB-backed user. Routes are gated with @login_required (see ez_kea/auth.py),
so route-level tests need a session that flask_login recognizes as
authenticated. We create a real User row and stash its id directly in the
test client's session the same way flask_login's login_user() does,
bypassing the password/2FA flow itself, which these route-level tests
aren't concerned with.
"""
import os

# Keep create_app() from starting the log-index background thread during tests.
# Config reads this at import time and several fixtures set TESTING only *after*
# create_app() returns, too late for the guard inside start_background_indexer.
# Left to its default, every app a test builds would spawn a thread writing to
# the developer's real ./data directory on a timer. The index itself is tested
# directly in test_log_index.py.
os.environ.setdefault("LOG_INDEX_ENABLED", "0")
# Same reasoning, for the lease/reservation search index (core/state_index.py).
os.environ.setdefault("STATE_INDEX_ENABLED", "0")

# The suite must not behave differently depending on whether a Kea binary
# happens to be installed on the machine running it. Save paths run the daemon's
# own `-t` against the candidate config (core/config_validate.py), and a local
# Kea 2.x rejects the `control-sockets` list spelling that most fixtures here
# write by hand -- while a machine without Kea skips the check entirely. Pin the
# command to a path that cannot exist (a test that wants the check to run
# supplies its own validator or its own command).
os.environ["KEA_DHCP4_CMD"] = "/nonexistent/kea-dhcp4-not-installed"
os.environ["KEA_DHCP6_CMD"] = "/nonexistent/kea-dhcp6-not-installed"

from werkzeug.security import generate_password_hash  # noqa: E402

TEST_PASSWORD_HASH = generate_password_hash("test-password-not-used-1234567")


def create_test_user(app, username="testadmin", is_admin=True):
    """Create (or fetch) a real DB-backed user within app's own database binding.
    Returns the user's id."""
    from ez_kea import db
    from ez_kea.models import User

    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if user is None:
            user = User(
                username=username,
                password_hash=TEST_PASSWORD_HASH,
                is_admin=is_admin,
            )
            db.session.add(user)
            db.session.commit()
        return user.id


def login(client, app, username="testadmin", is_admin=True):
    """Log a test client in as a real user, bypassing the login form itself."""
    uid = create_test_user(app, username=username, is_admin=is_admin)
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    return client
