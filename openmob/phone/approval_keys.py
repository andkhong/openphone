"""Approvals bound to the phone's screen lock, and to the action they approve.

When paired (or on first connect, for phones paired before this existed), the OpenMob app makes an
Android Keystore key pair that can only sign after the owner passes the screen lock (strong
biometric or device credential), and registers the public key with the server.

Every approval_request carries the action it asks about (`action`: the tool, the app, the target
element's text, description, resource id and bounds, and any typed text, URL or key) and its
`digest` (action_digest below). The app recomputes the digest from the fields it shows, and an
approval carries a signature over `<approval id>|<task id>|<digest>|true|<unix time>`, made right
after the prompt. "Always allow this" adds `|always`, so the grant itself carries the screen lock.
A permission change (`mode_change`) and a plan are signed the same way; their "action" describes the
change or the plan. The server checks the signature against the digest of the action it recorded,
and the agent checks that the action it is about to run still has that digest, so a yes for one
action can't authorize another. (The older format without the digest is no longer accepted.)

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
ACTION_TAG = "openmob-action-1"
# What an action digest covers, in this order (docs/phone-protocol.md, "Approval signing").
ACTION_FIELDS = ("tool", "package", "text", "desc", "resource_id", "bounds", "input")


def action_fields(action: dict) -> dict[str, str]:
    """The action as the strings that are hashed (missing fields are empty; bounds as "l,t,r,b")."""
    out = {}
    for name in ACTION_FIELDS:
        value = action.get(name)
        if name == "bounds" and isinstance(value, (list, tuple)):
            value = ",".join(str(int(v)) for v in value)
        out[name] = "" if value is None else str(value)
    return out


def action_digest(action: dict) -> str:
    """SHA-256 (hex) of the action: the tag, then each field as `|<UTF-8 byte length>:<value>`, so no
    value can run into the next. The app computes the same (ApprovalKeys.digest)."""
    import hashlib

    data = ACTION_TAG.encode()
    for value in action_fields(action).values():
        raw = value.encode()
        data += f"|{len(raw)}:".encode() + raw
    return hashlib.sha256(data).hexdigest()


def approval_message(approval_id: str, task_id: str, approved: bool, timestamp: int, extra: str | None = None,
                     digest: str | None = None) -> bytes:
    """What the phone signs. With `digest` (current apps): `id|task|digest|true|ts[|always]`; without
    it, the older `id|task|true|ts[|always]` (kept only so tests can show it is refused)."""
    head = f"{approval_id}|{task_id}|{digest}" if digest else f"{approval_id}|{task_id}"
    text = f"{head}|{'true' if approved else 'false'}|{int(timestamp)}"
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


TRUST_TAG = "openmob-trust-1"


def trust_message(target_device_id: str, timestamp: int) -> bytes:
    """What a trusted phone signs (with its screen lock) to trust another phone for approvals:
    `openmob-trust-1|trust|<target device id>|<unix time>`.

    Domain-separated from approvals so neither can be replayed as the other: an approval message
    starts with the server's approval id (32 hex characters, never this tag) and has a 64-hex digest
    in its third field, while this one starts with the tag and has the literal `trust` second. The
    target may not contain `|` (verify_trust refuses one), so it can't forge more fields."""
    return f"{TRUST_TAG}|trust|{target_device_id}|{int(timestamp)}".encode()


def verify_trust(public_key: str, target_device_id: str, timestamp, signature: str | None,
                 now: float | None = None) -> bool:
    """True only for a fresh, valid trust signature (trust_message) by this phone's approval key."""
    if not target_device_id or "|" in target_device_id:
        return False
    return _verify(public_key, lambda ts: trust_message(target_device_id, ts), timestamp, signature, now)


def verify_approval(public_key: str, approval_id: str, task_id: str, approved: bool,
                    timestamp, signature: str | None, now: float | None = None, extra: str | None = None,
                    digest: str | None = None) -> bool:
    """True only for a fresh, valid signature by this phone's approval key over this action's digest
    (`id|task|digest|true|ts[|always]`). The old digest-less format is refused, and so is anything that
    could be read as a trust message (trust_message)."""
    if not digest or not approval_id or "|" in str(approval_id) or str(approval_id).startswith(TRUST_TAG):
        return False
    return _verify(public_key, lambda ts: approval_message(approval_id, task_id, approved, ts, extra, digest),
                   timestamp, signature, now)


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
        key.verify(base64.b64decode(signature, validate=True), message(int(timestamp)), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
