# SPDX-FileCopyrightText: 2026 Kaleb Fenley
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
ez_kea/license.py

RSA-signed license validation for EZ-KEA.

EZ-KEA is licensed under PolyForm Noncommercial 1.0.0: free for personal,
hobby, and other noncommercial use, with commercial use requiring a paid
license. That boundary is a *license term*, not something this module tries to
enforce in code — see the note below.

What this module does:
  - Verifies commercial license keys against an embedded RSA public key, so a
    paying customer's installation can display who it's licensed to and when
    the license expires.
  - Computes the unlicensed reminder ("nag") shown in the UI.

What this module deliberately does NOT do: block anything. There is no feature
gate, no lease ceiling, and no write lock. Any such check would run on the
user's own machine against source they can edit, so it would stop nobody who
meant to bypass it while risking locking a legitimate admin out of their own
DHCP configuration. Compliance for commercial use rests on the license terms.

License key format: EZK1.<base64url-payload>.<base64url-signature>

The payload is a JSON blob signed with a 2048-bit RSA private key held by
the vendor. The public key embedded here is EZ-KEA specific — it was
generated fresh for this project and is not shared with any other product.
"""
import base64
import json
from datetime import date

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature

# ── EZ-KEA RSA-2048 public key ──────────────────────────────────────────────
# Generated exclusively for this project.  The matching private key is kept
# offline by the vendor and is NEVER distributed with the application.
_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAyBElRJJJ38CPasr0FH6P
xdNGUkCMxjNCqCDyY2uqlBHGVn5iSehFnvVN7HAmfgsfV5F21R9EFjQlKljbyl/h
73jiz53VQJhB6NMj65a7j+wpxR9zH1KUDvHFAntnNPYXoNGmeCxe36BQvk9q/lDv
E6CQMm1ewYdtnE6YKoVWow2e1uwm+BgOtMI4LkVd15EQRxQXJb6ZGcoNUR3zjj4p
WBoDNi/ogYsfK7CMpeCmHJSE6NM8SuIdy2136mr6Q2uO9kt86qU4pr6wmFlKEbQz
7FLsA7p85XCUxRVpdKcutSJU2SZy7uG0jQst+ZwPh+AlYaZDvuKfsXw+GlZA/WUK
0QIDAQAB
-----END PUBLIC KEY-----"""

_public_key = serialization.load_pem_public_key(_PUBLIC_KEY_PEM)

LICENSE_PREFIX = "EZK1."

# Above this many active leases, an unlicensed install gets a visible banner
# above the page content. This is a heuristic for "this install is probably not
# somebody's homelab", used only to decide how loudly to mention licensing.
# Nothing is gated on it, so its exact value costs nobody anything.
NAG_LEASE_THRESHOLD = 100


def parse_license(key_str: str) -> dict:
    """Parse and cryptographically verify a license key string.

    Args:
        key_str: The raw license key string.

    Returns:
        A dict with:
          valid (bool), error (str|None), license_id, licensee, email,
          issued, expires, days_remaining, features.
    """
    key_str = (key_str or "").strip()
    if not key_str:
        return {"valid": False, "error": "No license key provided"}

    if not key_str.startswith(LICENSE_PREFIX):
        return {"valid": False, "error": "Invalid license format"}

    body = key_str[len(LICENSE_PREFIX):]
    parts = body.split(".")
    if len(parts) != 2:
        return {"valid": False, "error": "Malformed license key"}

    payload_b64, sig_b64 = parts
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "==")
        sig_bytes     = base64.urlsafe_b64decode(sig_b64 + "==")
    except Exception:
        return {"valid": False, "error": "Could not decode license key"}

    try:
        _public_key.verify(sig_bytes, payload_bytes, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return {"valid": False, "error": "License signature is invalid"}
    except Exception as e:
        return {"valid": False, "error": f"Signature verification error: {e}"}

    try:
        payload = json.loads(payload_bytes)
    except Exception:
        return {"valid": False, "error": "License payload is corrupt"}

    expires = date.fromisoformat(payload.get("expires", "1970-01-01"))
    today   = date.today()
    days_remaining = (expires - today).days

    return {
        "valid":          days_remaining > 0,
        "error":          None if days_remaining > 0 else "License has expired",
        "license_id":     payload.get("id", ""),
        "licensee":       payload.get("licensee", ""),
        "email":          payload.get("email", ""),
        "issued":         payload.get("issued", ""),
        "expires":        payload.get("expires", ""),
        "days_remaining": days_remaining,
        "features":       payload.get("features", []),
    }


def get_license() -> dict:
    """Load the stored license key from the DB and parse it.

    Safe to call from anywhere; returns an invalid license dict if the DB
    is not yet initialised or no key has been entered.
    """
    try:
        from .models import SystemSetting
        row = SystemSetting.query.get("license_key")
        key_str = row.value if row else ""
        return parse_license(key_str)
    except Exception:
        return {"valid": False, "error": "License not configured"}


def is_licensed() -> bool:
    """Return True if a valid, unexpired commercial license is stored."""
    return get_license().get("valid", False)


def license_status(active_lease_count: int = 0) -> dict:
    """Build the licensing reminder shown in the UI.

    Args:
        active_lease_count: Number of currently active DHCP leases. Only used
            to decide whether to show a banner.

    Returns:
        dict with keys:
          licensed (bool): a valid commercial license is installed.
          notice (str):    always "" — kept so callers and templates that read
                           it keep working without a coordinated change.
          banner (str):    prominent banner text, "" when licensed or when the
                           install is small enough not to warrant one.
    """
    if is_licensed():
        return {"licensed": True, "notice": "", "banner": ""}

    banner = ""
    if active_lease_count > NAG_LEASE_THRESHOLD:
        banner = (
            f"This installation is serving {active_lease_count} active leases. "
            "EZ-KEA is free for noncommercial use; commercial use requires a "
            "license — $500 per year for a single deployment."
        )

    return {"licensed": False, "notice": "", "banner": banner}
