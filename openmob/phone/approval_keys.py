"""Approvals bound to the phone's screen lock.

When paired (or on first connect, for phones paired before this existed), the OpenMob app makes an
Android Keystore key pair that can only sign after the owner passes the screen lock (strong
biometric or device credential), and registers the public key with the server. An approval then
carries a signature over `<approval id>|<task id>|true|<unix time>`, made right after the prompt.
"Always allow this" adds `|always` to what is signed, so the grant itself carries the screen lock.
A permission change (`mode_change`) is signed the same way; its "task id" names the change.

So a stolen device key (or any other client on the phone's socket) can't approve: it can't
produce the signature. Denials are never signed: anyone may say no.

Keys are EC P-256: the public key as base64 X.509 SubjectPublicKeyInfo (Java's PublicKey.encoded),
the signature as base64 DER ECDSA with SHA-256 (Java's "SHA256withECDSA").
"""

from __future__ import annotations

import base64
import binascii
import time

MAX_AGE = 300  # seconds an approval signature stays good (and how far ahead a phone's clock may be)
MAX_KEY = 1024  # characters of base64 public key we'll store


def approval_message(approval_id: str, task_id: str, approved: bool, timestamp: int, extra: str | None = None) -> bytes:
    text = f"{approval_id}|{task_id}|{'true' if approved else 'false'}|{int(timestamp)}"
    return (f"{text}|{extra}" if extra else text).encode()


def valid_public_key(public_key: str | None) -> bool:
    """A usable EC P-256 public key, as the app sends it."""
    if not public_key or len(public_key) > MAX_KEY:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        key = load_der_public_key(base64.b64decode(public_key, validate=True))
        return isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == "secp256r1"
    except (ValueError, TypeError, binascii.Error, ImportError):
        return False


def verify_approval(public_key: str, approval_id: str, task_id: str, approved: bool,
                    timestamp, signature: str | None, now: float | None = None, extra: str | None = None) -> bool:
    """True only for a fresh, valid signature by this phone's approval key."""
    if not signature or not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        return False
    now = time.time() if now is None else now
    if abs(now - timestamp) > MAX_AGE:
        return False  # stale (or a clock far off): an old signed approval can't be replayed later
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        key = load_der_public_key(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(signature, validate=True),
                   approval_message(approval_id, task_id, approved, int(timestamp), extra), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
