# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from ez_kea import create_app
from ez_kea.__about__ import __version__
from waitress import serve
import os
import signal
import sys

# Ensure Flask instance folder exists - otherwise db.create_all() fails with 'unable to open database file' on fresh clone
# (instance/ is gitignored so clone does not create it)
os.makedirs(os.path.join(os.path.dirname(__file__), "instance"), exist_ok=True)
os.makedirs(os.path.join(os.path.dirname(__file__), "data"), exist_ok=True)

app = create_app()

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "127.0.0.1")

    secret_key_is_default = app.config.get("SECRET_KEY") == "dev"
    if host not in _LOOPBACK_HOSTS and secret_key_is_default:
        print(
            "Refusing to start: SECRET_KEY is still the insecure default ('dev') and "
            f"HOST={host!r} is not loopback. Set a strong, random SECRET_KEY environment "
            "variable before binding EZ-KEA to a non-local address.",
            file=sys.stderr,
        )
        sys.exit(1)

    # In the container this process is PID 1, and the kernel ignores the default
    # action of a signal for PID 1: without an explicit handler, the SIGTERM that
    # "docker stop" (and every compose recreate) sends is simply dropped. The
    # container then sits there until the grace period expires and is killed,
    # which is why it takes 20s to restart and exits 137 -- indistinguishable
    # from a crash. Installing the handler makes the stop immediate and the exit
    # code meaningful.
    def _terminate(signum, _frame):
        print(
            f"EZ-KEA {__version__} shutting down on signal {signum}.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _terminate)

    # Say so. waitress only logs its "Serving on" line at INFO, and it leaves the
    # root logger at WARNING, so a healthy container used to produce no output
    # at all -- impossible to tell from a silent failure in "docker logs".
    print(
        f"EZ-KEA {__version__} serving on http://{host}:{port} "
        f"(mode={app.config.get('EZ-KEA_MODE', 'LIVE')})",
        flush=True,
    )

    # Serve using waitress for local production-like execution
    serve(app, host=host, port=port)
