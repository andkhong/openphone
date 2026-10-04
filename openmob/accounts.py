"""Accounts, device keys, and one-time pairing codes.

The security rule lives here: a pairing code can only be redeemed by a request authenticated
as the same account that created it, and each paired phone gets its own revocable key. So a QR
code is useless to anyone not signed into the owner's account, and one phone can be revoked
without touching the others.

Storage is a single JSON file guarded by a lock — fine for one host. Only hashes of passwords,
session tokens and device keys are stored, never the secrets themselves.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PBKDF2_ROUNDS = 200_000
SESSION_TTL = 30 * 24 * 3600  # dashboard login lasts 30 days
PAIRING_TTL = 10 * 60  # a pairing code is valid for 10 minutes, once
DEFAULT_STORE = Path.home() / ".config" / "openmob" / "accounts.json"


class AuthError(RuntimeError):
    """Login failed, or a pairing code was wrong, expired, used, or for another account."""


@dataclass
class Device:
    id: str
    account_id: str
    model: str
    created: float
    last_seen: float
    revoked: bool


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS).hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AccountStore:
    def __init__(self, path: Path | str = DEFAULT_STORE):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"accounts": {}, "devices": {}, "sessions": {}, "pairings": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)

    # --- accounts --------------------------------------------------------

    def create_account(self, email: str, password: str) -> str:
        email = normalize_email(email)
        if "@" not in email or len(password) < 8:
            raise AuthError("Enter a valid email and a password of at least 8 characters.")
        with self._lock:
            if any(a["email"] == email for a in self._data["accounts"].values()):
                raise AuthError("An account with that email already exists.")
            account_id = secrets.token_hex(8)
            salt = secrets.token_hex(16)
            self._data["accounts"][account_id] = {
                "email": email,
                "salt": salt,
                "pw_hash": _hash_password(password, salt),
                "created": time.time(),
            }
            self._save()
            return account_id

    def verify_login(self, email: str, password: str) -> str:
        email = normalize_email(email)
        with self._lock:
            for account_id, account in self._data["accounts"].items():
                if account["email"] == email:
                    expected = account["pw_hash"]
                    if hmac.compare_digest(expected, _hash_password(password, account["salt"])):
                        return account_id
                    break
        raise AuthError("Wrong email or password.")

    def account_email(self, account_id: str) -> str | None:
        with self._lock:
            account = self._data["accounts"].get(account_id)
            return account["email"] if account else None

    # --- sessions (dashboard + app login) --------------------------------

    def create_session(self, account_id: str) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._data["sessions"][_hash_token(token)] = {
                "account_id": account_id,
                "expires": time.time() + SESSION_TTL,
            }
            self._save()
        return token

    def account_for_session(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            session = self._data["sessions"].get(_hash_token(token))
            if not session or session["expires"] < time.time():
                return None
            return session["account_id"]

    def end_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            if self._data["sessions"].pop(_hash_token(token), None):
                self._save()

    # --- pairing ---------------------------------------------------------

    def create_pairing_code(self, account_id: str) -> tuple[str, float]:
        """A short, single-use code bound to this account. Returns (code, expiry)."""
        code = secrets.token_urlsafe(9)
        expires = time.time() + PAIRING_TTL
        with self._lock:
            # One active code per account at a time: making a new one voids the old.
            self._data["pairings"] = {
                c: p for c, p in self._data["pairings"].items() if p["account_id"] != account_id
            }
            self._data["pairings"][code] = {"account_id": account_id, "expires": expires, "used": False}
            self._save()
        return code, expires

    def redeem_pairing_code(self, code: str, account_id: str, device_id: str, model: str) -> str:
        """Bind a phone to the account and return its own device key.

        Enforces the rule: the request must be authenticated as the SAME account that made the
        code. A different account (or none) is refused even with a valid code.
        """
        with self._lock:
            pairing = self._data["pairings"].get(code)
            if not pairing or pairing["expires"] < time.time() or pairing["used"]:
                raise AuthError("This pairing code is invalid or has expired. Generate a new one.")
            if pairing["account_id"] != account_id:
                raise AuthError("This pairing code belongs to a different account.")
            pairing["used"] = True

            key = secrets.token_urlsafe(24)
            existing = self._data["devices"].get(device_id)
            self._data["devices"][device_id] = {
                "account_id": account_id,
                "model": model or (existing or {}).get("model", "Android phone"),
                "key_hash": _hash_token(key),
                "created": (existing or {}).get("created", time.time()),
                "last_seen": time.time(),
                "revoked": False,
            }
            self._save()
            return key

    # --- device auth -----------------------------------------------------

    def verify_device(self, device_id: str, key: str) -> str:
        """Return the owning account for a phone's key, or raise. Updates last-seen."""
        with self._lock:
            device = self._data["devices"].get(device_id)
            if not device or device["revoked"]:
                raise AuthError("This phone is not paired. Pair it again from your dashboard.")
            if not hmac.compare_digest(device["key_hash"], _hash_token(key)):
                raise AuthError("This phone's key is wrong. Pair it again from your dashboard.")
            device["last_seen"] = time.time()
            self._save()
            return device["account_id"]

    def list_devices(self, account_id: str) -> list[Device]:
        with self._lock:
            return [
                Device(id=device_id, **{k: d[k] for k in ("account_id", "model", "created", "last_seen", "revoked")})
                for device_id, d in self._data["devices"].items()
                if d["account_id"] == account_id and not d["revoked"]
            ]

    def revoke_device(self, account_id: str, device_id: str) -> bool:
        """Mark a phone as unpaired. Only the owning account may do this."""
        with self._lock:
            device = self._data["devices"].get(device_id)
            if not device or device["account_id"] != account_id:
                return False
            device["revoked"] = True
            self._save()
            return True
