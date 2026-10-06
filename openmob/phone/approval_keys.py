"""Approvals bound to the phone's screen lock.

When paired (or on first connect, for phones paired before this existed), the OpenMob app makes an
Android Keystore key pair that can only sign after the owner passes the screen lock (strong
biometric or device credential), and registers the public key with the server. An approval then
carries a signature over `<approval id>|<task id>|true|<unix time>`, made right after the prompt.

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


def approval_message(approval_id: str, task_id: str, approved: bool, timestamp: int) -> bytes:
    return f"{approval_id}|{task_id}|{'true' if approved else 'false'}|{int(timestamp)}".encode()


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


def trust_message(target_device_id: str, timestamp: int) -> bytes:
    """What a trusted phone signs (with its screen lock) to trust another phone for approvals."""
    return f"trust|{target_device_id}|{int(timestamp)}".encode()


def verify_trust(public_key: str, target_device_id: str, timestamp, signature: str | None,
                 now: float | None = None) -> bool:
    return _verify(public_key, lambda ts: trust_message(target_device_id, ts), timestamp, signature, now)


def verify_approval(public_key: str, approval_id: str, task_id: str, approved: bool,
                    timestamp, signature: str | None, now: float | None = None) -> bool:
    """True only for a fresh, valid signature by this phone's approval key."""
    return _verify(public_key, lambda ts: approval_message(approval_id, task_id, approved, ts), timestamp, signature, now)


def _verify(public_key: str, message, timestamp, signature: str | None, now: float | None = None) -> bool:
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
                   message(int(timestamp)), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
