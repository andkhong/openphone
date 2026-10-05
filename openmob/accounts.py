"""Accounts, device keys, and one-time pairing codes.

The security rule lives here: a pairing code can only be redeemed by a request authenticated
as the same account that created it, and each paired phone gets its own revocable key. So a QR
code is useless to anyone not signed into the owner's account, and one phone can be revoked
without touching the others.

Developer API keys live here too: `om_live_<id>_<secret>`, shown once, stored as a hash, each
with its own scopes. An admin can switch off one key or a whole account's API access.

Storage is a single JSON file guarded by a lock — fine for one host. Only hashes of passwords,
session tokens, device keys and API keys are stored, never the secrets themselves. (Webhook
signing secrets are the exception: signing needs the secret itself.)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PBKDF2_ROUNDS = 200_000
SESSION_TTL = 30 * 24 * 3600  # dashboard login lasts 30 days
PAIRING_TTL = 10 * 60  # a pairing code is valid for 10 minutes, once
LOGIN_LINK_TTL = 10 * 60  # a sign-in link from `openmob account link` works once, for 10 minutes
KEY_PREFIX = "om_live_"  # easy to grep for in leaked code and logs
# What an API key may do. screen:read (see the phone's screen) is sensitive, so it's never a default.
SCOPES = ("devices:read", "tasks:run", "tasks:read", "screen:read", "history:read", "memory:read", "memory:write")
DEFAULT_SCOPES = ("devices:read", "tasks:run", "tasks:read")
LAST_USED_EVERY = 60  # seconds between saves of a key's last-used time
# OPENMOB_DATA_DIR points at persistent storage when hosted (e.g. a Fly volume at /data).
DATA_DIR = Path(os.environ.get("OPENMOB_DATA_DIR") or Path.home() / ".config" / "openmob")
DEFAULT_STORE = DATA_DIR / "accounts.json"


class AuthError(RuntimeError):
    """Login failed, or a pairing code was wrong, expired, used, or for another account."""


class AccessDisabled(AuthError):
    """A valid API key whose key or account has been switched off by an admin."""


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
        self._mtime = 0.0
        self._data = self._load()

    def _load(self) -> dict:
        try:
            self._mtime = self.path.stat().st_mtime
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        for section in ("accounts", "devices", "sessions", "pairings", "logins", "api_keys"):
            data.setdefault(section, {})
        return data

    def _refresh(self) -> None:
        """Pick up changes made by another process (e.g. `openmob account add` while the server runs)."""
        try:
            if self.path.stat().st_mtime != self._mtime:
                self._data = self._load()
        except OSError:
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.chmod(0o600)  # password hashes and key hashes: owner-only
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime

    # --- accounts --------------------------------------------------------

    def create_account(self, email: str, password: str) -> str:
        email = normalize_email(email)
        if "@" not in email or len(password) < 8:
            raise AuthError("Enter a valid email and a password of at least 8 characters.")
        with self._lock:
            self._refresh()
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
            self._refresh()
            for account_id, account in self._data["accounts"].items():
                if account["email"] == email:
                    expected = account["pw_hash"]
                    if hmac.compare_digest(expected, _hash_password(password, account["salt"])):
                        return account_id
                    break
        raise AuthError("Wrong email or password.")

    def account_id_for(self, email: str) -> str | None:
        email = normalize_email(email)
        with self._lock:
            self._refresh()
            return next((i for i, a in self._data["accounts"].items() if a["email"] == email), None)

    def list_accounts(self) -> list[str]:
        with self._lock:
            self._refresh()
            return sorted(a["email"] for a in self._data["accounts"].values())

    # --- one-time sign-in links (for the server operator; there's no web sign-up page yet) -------

    def create_login_link(self, account_id: str) -> str:
        code = secrets.token_urlsafe(18)
        with self._lock:
            self._refresh()
            self._data["logins"][_hash_token(code)] = {"account_id": account_id, "expires": time.time() + LOGIN_LINK_TTL}
            self._save()
        return code

    def redeem_login_link(self, code: str) -> str:
        """Single use: returns the account and forgets the code."""
        with self._lock:
            self._refresh()
            login = self._data["logins"].pop(_hash_token(code or ""), None)
            self._save()
        if not login or login["expires"] < time.time():
            raise AuthError("This sign-in link is invalid or expired. Ask for a new one.")
        return login["account_id"]

    def account_email(self, account_id: str) -> str | None:
        with self._lock:
            self._refresh()
            account = self._data["accounts"].get(account_id)
            return account["email"] if account else None

    # --- sessions (dashboard + app login) --------------------------------

    def create_session(self, account_id: str) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._refresh()
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
            self._refresh()
            session = self._data["sessions"].get(_hash_token(token))
            if not session or session["expires"] < time.time():
                return None
            return session["account_id"]

    def end_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._refresh()
            if self._data["sessions"].pop(_hash_token(token), None):
                self._save()

    # --- pairing ---------------------------------------------------------

    def create_pairing_code(self, account_id: str) -> tuple[str, float]:
        """A short, single-use code bound to this account. Returns (code, expiry)."""
        code = secrets.token_urlsafe(9)
        expires = time.time() + PAIRING_TTL
        with self._lock:
            self._refresh()
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
            self._refresh()
            pairing = self._data["pairings"].get(code)
            if not pairing or pairing["expires"] < time.time() or pairing["used"]:
                raise AuthError("This pairing code is invalid or has expired. Generate a new one.")
            if pairing["account_id"] != account_id:
                raise AuthError("This pairing code belongs to a different account.")
            existing = self._data["devices"].get(device_id)
            # A phone registered to someone else stays theirs until they revoke it.
            if existing and not existing["revoked"] and existing["account_id"] != account_id:
                raise AuthError("This phone is registered to another account. Its owner must remove it first.")
            if existing and existing["account_id"] != account_id:
                existing = None  # previously revoked by another account: start a fresh record
            pairing["used"] = True

            key = secrets.token_urlsafe(24)
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
            self._refresh()
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
            self._refresh()
            return [
                Device(id=device_id, **{k: d[k] for k in ("account_id", "model", "created", "last_seen", "revoked")})
                for device_id, d in self._data["devices"].items()
                if d["account_id"] == account_id and not d["revoked"]
            ]

    def revoke_device(self, account_id: str, device_id: str) -> bool:
        """Mark a phone as unpaired. Only the owning account may do this."""
        with self._lock:
            self._refresh()
            device = self._data["devices"].get(device_id)
            if not device or device["account_id"] != account_id:
                return False
            device["revoked"] = True
            self._save()
            return True

    # --- admin (granted only from the server's command line) --------------

    def set_admin(self, email: str, on: bool) -> bool:
        """Grant or remove the admin role. False if there's no such account."""
        email = normalize_email(email)
        with self._lock:
            self._refresh()
            account = next((a for a in self._data["accounts"].values() if a["email"] == email), None)
            if account is None:
                return False
            account["is_admin"] = bool(on)
            self._save()
            return True

    def is_admin(self, account_id: str | None) -> bool:
        if not account_id:
            return False
        with self._lock:
            self._refresh()
            return bool(self._data["accounts"].get(account_id, {}).get("is_admin", False))

    def admin_list_accounts(self) -> list[dict]:
        """Every account, without any secret (no salt or password hash)."""
        with self._lock:
            self._refresh()
            counts: dict[str, int] = {}
            for d in self._data["devices"].values():
                if not d["revoked"]:
                    counts[d["account_id"]] = counts.get(d["account_id"], 0) + 1
            accounts = [
                {"id": account_id, "email": a["email"], "created": a.get("created"),
                 "is_admin": bool(a.get("is_admin", False)), "device_count": counts.get(account_id, 0),
                 "api_disabled": bool(a.get("api_disabled", False))}
                for account_id, a in self._data["accounts"].items()
            ]
        return sorted(accounts, key=lambda a: a["email"])

    def admin_list_devices(self) -> list[dict]:
        """Every paired phone (revoked ones included), without key hashes."""
        with self._lock:
            self._refresh()
            accounts = self._data["accounts"]
            return [
                {"id": device_id, "account_id": d["account_id"],
                 "email": accounts.get(d["account_id"], {}).get("email"),
                 **{k: d[k] for k in ("model", "created", "last_seen", "revoked")}}
                for device_id, d in self._data["devices"].items()
            ]

    def admin_revoke_device(self, device_id: str) -> bool:
        """Unpair a phone regardless of owner. False if unknown."""
        with self._lock:
            self._refresh()
            device = self._data["devices"].get(device_id)
            if not device:
                return False
            device["revoked"] = True
            self._save()
            return True

    # --- developer API keys --------------------------------------------------

    def create_api_key(self, account_id: str, name: str, scopes=DEFAULT_SCOPES) -> tuple[str, str]:
        """Returns (key id, key). The key is shown once; only its hash is kept."""
        scopes = sorted(set(scopes))
        unknown = [sc for sc in scopes if sc not in SCOPES]
        if unknown or not scopes:
            raise AuthError(f"Unknown or missing scopes: {', '.join(unknown) or 'none'}. Choose from: {', '.join(SCOPES)}")
        name = " ".join((name or "").split())[:60] or "API key"
        key_id = secrets.token_hex(4)
        key = f"{KEY_PREFIX}{key_id}_{secrets.token_urlsafe(24)}"
        with self._lock:
            self._refresh()
            if account_id not in self._data["accounts"]:
                raise AuthError("Unknown account.")
            self._data["api_keys"][key_id] = {
                "account_id": account_id, "name": name, "scopes": scopes, "hash": _hash_token(key),
                "created": time.time(), "last_used": None, "revoked": False, "disabled": False,
            }
            self._save()
        return key_id, key

    def verify_api_key(self, key: str | None) -> dict:
        """The key's {id, account_id, scopes, name}, or raise AuthError (AccessDisabled when switched off)."""
        parts = (key or "").split("_", 3)
        if len(parts) != 4 or f"{parts[0]}_{parts[1]}_" != KEY_PREFIX:
            raise AuthError("Invalid API key.")
        key_id = parts[2]
        with self._lock:
            self._refresh()
            record = self._data["api_keys"].get(key_id)
            # Always compare a hash, even for unknown ids, so timing doesn't reveal which ids exist.
            expected = record["hash"] if record else _hash_token(secrets.token_hex(8))
            if not hmac.compare_digest(expected, _hash_token(key)) or not record or record["revoked"]:
                raise AuthError("Invalid API key.")
            account = self._data["accounts"].get(record["account_id"])
            if account is None:
                raise AuthError("Invalid API key.")
            if record.get("disabled") or account.get("api_disabled"):
                raise AccessDisabled("API access for this key has been disabled. Contact support.")
            now = time.time()
            if not record["last_used"] or now - record["last_used"] > LAST_USED_EVERY:
                record["last_used"] = now
                self._save()
            return {"id": key_id, "account_id": record["account_id"], "scopes": list(record["scopes"]), "name": record["name"]}

    @staticmethod
    def _public_key(key_id: str, k: dict) -> dict:
        return {"id": key_id, "prefix": f"{KEY_PREFIX}{key_id}", "name": k["name"], "scopes": list(k["scopes"]),
                "created": k["created"], "last_used": k["last_used"], "disabled": bool(k.get("disabled"))}

    def list_api_keys(self, account_id: str) -> list[dict]:
        """This account's live keys (never the hash)."""
        with self._lock:
            self._refresh()
            keys = [self._public_key(i, k) for i, k in self._data["api_keys"].items()
                    if k["account_id"] == account_id and not k["revoked"]]
        return sorted(keys, key=lambda k: k["created"], reverse=True)

    def api_key_count(self, account_id: str) -> int:
        return len(self.list_api_keys(account_id))

    def revoke_api_key(self, account_id: str, key_id: str) -> bool:
        """Only the owning account may revoke its key. False if unknown or not theirs."""
        with self._lock:
            self._refresh()
            k = self._data["api_keys"].get(key_id)
            if not k or k["account_id"] != account_id or k["revoked"]:
                return False
            k["revoked"] = True
            self._save()
            return True

    def rotate_api_key(self, account_id: str, key_id: str) -> tuple[str, str] | None:
        """Replace a key with a new one (same name and scopes); the old one stops working now."""
        with self._lock:
            self._refresh()
            k = self._data["api_keys"].get(key_id)
            if not k or k["account_id"] != account_id or k["revoked"]:
                return None
            new = self.create_api_key(account_id, k["name"], k["scopes"])
            self._data["api_keys"][key_id]["revoked"] = True
            self._save()
            return new

    # --- per-account developer settings ------------------------------------------

    def settings(self, account_id: str) -> dict:
        with self._lock:
            self._refresh()
            a = self._data["accounts"].get(account_id, {})
            hook = a.get("webhook") or {}
            return {"allow_sensitive_apps": bool(a.get("allow_sensitive_apps", False)),
                    "webhook_url": hook.get("url"), "api_disabled": bool(a.get("api_disabled", False))}

    def set_allow_sensitive_apps(self, account_id: str, on: bool) -> None:
        with self._lock:
            self._refresh()
            self._data["accounts"][account_id]["allow_sensitive_apps"] = bool(on)
            self._save()

    def set_webhook(self, account_id: str, url: str | None) -> str | None:
        """Set (returns the new signing secret, shown once) or clear (url=None) the task webhook."""
        with self._lock:
            self._refresh()
            account = self._data["accounts"][account_id]
            if not url:
                account.pop("webhook", None)
                self._save()
                return None
            secret = "whsec_" + secrets.token_urlsafe(24)
            account["webhook"] = {"url": url, "secret": secret}
            self._save()
            return secret

    def webhook(self, account_id: str) -> tuple[str, str] | None:
        """(url, signing secret) for delivering task events, or None."""
        with self._lock:
            self._refresh()
            hook = self._data["accounts"].get(account_id, {}).get("webhook")
            return (hook["url"], hook["secret"]) if hook else None

    # --- admin: API kill switches -----------------------------------------------

    def admin_list_api_keys(self) -> list[dict]:
        with self._lock:
            self._refresh()
            accounts = self._data["accounts"]
            keys = [{**self._public_key(i, k), "account_id": k["account_id"],
                     "email": accounts.get(k["account_id"], {}).get("email")}
                    for i, k in self._data["api_keys"].items() if not k["revoked"]]
        return sorted(keys, key=lambda k: k["created"], reverse=True)

    def admin_set_key_disabled(self, key_id: str, on: bool) -> str | None:
        """Switch one key off or on. Returns its account, or None if unknown."""
        with self._lock:
            self._refresh()
            k = self._data["api_keys"].get(key_id)
            if not k or k["revoked"]:
                return None
            k["disabled"] = bool(on)
            self._save()
            return k["account_id"]

    def admin_set_api_disabled(self, account_id: str, on: bool) -> bool:
        """Switch off (or back on) every API key of one account."""
        with self._lock:
            self._refresh()
            account = self._data["accounts"].get(account_id)
            if account is None:
                return False
            account["api_disabled"] = bool(on)
            self._save()
            return True
