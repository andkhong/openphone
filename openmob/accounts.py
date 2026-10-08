"""Accounts, device keys, and one-time pairing codes.

Pairing: the signed-in dashboard (or `openmob phone add`, with a `devices:add` API key) mints a
pairing code. The code itself is the credential: single use, valid for 2 minutes, bound to the
account that made it, 128 bits of secret, and stored only as a hash. The phone redeems it (after
its owner confirms with the screen lock) for its own revocable device key, and never holds a
dashboard session. Wrong guesses void a code after a few tries, and the server rate-limits
redemption per client address and per account. One phone can be revoked without touching the others.

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
PAIRING_TTL = 2 * 60  # a pairing code is valid for 2 minutes, once
PAIRING_ID_LEN = 8  # a code is <8 hex id><secret>: the id finds the record, the secret is checked as a hash
PAIRING_MAX_FAILURES = 5  # wrong secrets for one code before it's voided
PAIRING_MINTS_PER_HOUR = 30  # codes one account may make in an hour
LOGIN_LINK_TTL = 10 * 60  # a sign-in link from `openmob account link` works once, for 10 minutes
INVITE_TTL = 7 * 24 * 3600  # an invite code from `openmob account invite` works once, for 7 days
KEY_PREFIX = "om_live_"  # easy to grep for in leaked code and logs
# What an API key may do. screen:read (see the phone's screen) is sensitive, so it's never a default.
# devices:add (enroll phones with `openmob phone add`) is sensitive too, so it's never a default either.
SCOPES = ("devices:read", "tasks:run", "tasks:read", "screen:read", "history:read",
          "devices:add")
DEFAULT_SCOPES = ("devices:read", "tasks:run", "tasks:read")
KEY_MODES = ("ask", "plan", "auto", "trusted", "never")  # an API key's max_mode (permissions.MODES)
LAST_USED_EVERY = 60  # seconds between saves of a key's last-used time
WEBHOOK_SECRET_OVERLAP = 24 * 3600  # a replaced webhook secret still signs deliveries this long
# OPENMOB_DATA_DIR points at persistent storage when hosted (e.g. a Fly volume at /data).
DATA_DIR = Path(os.environ.get("OPENMOB_DATA_DIR") or Path.home() / ".config" / "openmob")
DEFAULT_STORE = DATA_DIR / "accounts.json"


class AuthError(RuntimeError):
    """Login failed, or a pairing code was wrong, expired, used, or for another account."""


class AccessDisabled(AuthError):
    """A valid API key whose key or account has been switched off by an admin."""


class InviteInvalid(AuthError):
    """The invite code is missing, unknown, already used or expired."""


class AccountExists(AuthError):
    """Sign-up with an email that already has an account (the server never tells the client)."""


class PairingRefused(AuthError):
    """A pairing code was wrong, expired, used or voided. `account_id` is the account the code's id
    belongs to, when the id matched one (so the server can rate-limit guesses per account)."""

    def __init__(self, message: str, account_id: str | None = None):
        super().__init__(message)
        self.account_id = account_id


class PairingLimit(AuthError):
    """Too many pairing codes made by one account in the last hour."""


@dataclass
class Device:
    id: str
    account_id: str
    model: str
    created: float
    last_seen: float
    revoked: bool
    paired_at: float = 0.0  # the latest pairing (a re-pair moves it; `created` stays the first)
    trusted: bool = True  # may answer approvals for the owner's other phones (see trust_device)


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
        for section in ("accounts", "devices", "sessions", "pairings", "logins", "api_keys", "invites"):
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
                _hash_password(password, secrets.token_hex(16))  # same work as a new account: no timing tell
                raise AccountExists("An account with that email already exists.")
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

    # --- invite codes: sign-up needs one ------------------------------------

    def create_invite(self, ttl: float = INVITE_TTL) -> str:
        """A one-time invite code. Only its hash is stored, like sign-in links."""
        code = secrets.token_urlsafe(12)
        with self._lock:
            self._refresh()
            now = time.time()
            self._data["invites"] = {h: i for h, i in self._data["invites"].items() if i["expires"] > now}
            self._data["invites"][_hash_token(code)] = {"created": now, "expires": now + ttl}
            self._save()
        return code

    def create_account_with_invite(self, email: str, password: str, invite: str) -> str:
        """Sign-up: the invite is checked first and used up whether or not the email already has an
        account, so a valid code can't be replayed to test which emails exist."""
        if "@" not in normalize_email(email) or len(password) < 8:
            raise AuthError("Enter a valid email and a password of at least 8 characters.")
        with self._lock:
            self._refresh()
            found = self._data["invites"].pop(_hash_token((invite or "").strip()), None)
            if not found or found["expires"] < time.time():
                if found:
                    self._save()  # forget the expired code
                raise InviteInvalid("This invite code is invalid, already used or expired. Ask for a new one.")
            self._save()
            return self.create_account(email, password)

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

    def create_pairing_code(self, account_id: str, purpose: str = "dashboard") -> tuple[str, float]:
        """A single-use code bound to this account, valid for PAIRING_TTL. Returns (code, expiry).
        Only the code's id and a hash of its secret are stored. `purpose`: "dashboard" or "cli"."""
        code_id = secrets.token_hex(PAIRING_ID_LEN // 2)
        secret = secrets.token_urlsafe(16)  # 128 bits
        now = time.time()
        expires = now + PAIRING_TTL
        with self._lock:
            self._refresh()
            account = self._data["accounts"].get(account_id)
            if account is None:
                raise AuthError("Unknown account.")
            mints = [t for t in account.get("pairing_mints", []) if now - t < 3600]
            if len(mints) >= PAIRING_MINTS_PER_HOUR:
                raise PairingLimit("Too many pairing codes in the last hour. Try again later.")
            account["pairing_mints"] = mints + [now]
            # One live code per account: a new one voids the old. Spent and stale records go too (a
            # redeemed one is kept for a day, so `openmob phone add` can see which phone took it).
            self._data["pairings"] = {
                i: p for i, p in self._data["pairings"].items()
                if "secret_hash" in p and (p["account_id"] != account_id or p.get("device_id"))
                and now - p["expires"] < 86400
            }
            self._data["pairings"][code_id] = {
                "account_id": account_id, "secret_hash": _hash_token(secret), "expires": expires,
                "used": False, "failures": 0, "purpose": purpose, "device_id": None,
            }
            self._save()
        return code_id + secret, expires

    def _live_pairing(self, code: str) -> tuple[str, dict]:
        """(code id, record) for a valid, unused, unexpired code; the lock is held by the caller. A
        wrong secret for a known id counts against that code, voided after PAIRING_MAX_FAILURES."""
        code = (code or "").strip()
        code_id, secret = code[:PAIRING_ID_LEN], code[PAIRING_ID_LEN:]
        pairing = self._data["pairings"].get(code_id) if secret else None
        if pairing is not None and "secret_hash" not in pairing:
            pairing = None  # a record from before codes were hashed
        # Always compare a hash, even for unknown ids, so timing doesn't reveal which ids exist.
        expected = pairing["secret_hash"] if pairing else _hash_token(secrets.token_hex(8))
        good = hmac.compare_digest(expected, _hash_token(secret))
        invalid = "This pairing code is invalid or has expired. Make a new one on your dashboard."
        if pairing is None:
            raise PairingRefused(invalid)
        if not good:
            pairing["failures"] = pairing.get("failures", 0) + 1
            if pairing["failures"] >= PAIRING_MAX_FAILURES:
                pairing["used"] = True  # voided: too many wrong guesses
            self._save()
            raise PairingRefused(invalid, pairing["account_id"])
        if pairing["used"] or pairing["expires"] < time.time():
            raise PairingRefused(invalid, pairing["account_id"])
        return code_id, pairing

    def check_pairing_code(self, code: str) -> dict:
        """What a phone shows before its owner confirms: {account_id, email, expires}. Doesn't use the code."""
        with self._lock:
            self._refresh()
            _, pairing = self._live_pairing(code)
            account = self._data["accounts"].get(pairing["account_id"], {})
            return {"account_id": pairing["account_id"], "email": account.get("email"), "expires": pairing["expires"]}

    def redeem_pairing_code(self, code: str, account_id: str | None, device_id: str, model: str,
                            approval_key: str | None = None) -> str:
        """Bind a phone to the code's account and return its own device key.

        The code is the credential. `account_id`, when given (the older signed-in route), must be
        the code's own account: a different account is refused even with a valid code.
        """
        device_id = (device_id or "").strip()
        if not device_id or len(device_id) > 128:
            raise AuthError("A device id is required.")
        with self._lock:
            self._refresh()
            _, pairing = self._live_pairing(code)
            owner = pairing["account_id"]
            if account_id is not None and owner != account_id:
                raise PairingRefused("This pairing code belongs to a different account.", owner)
            existing = self._data["devices"].get(device_id)
            # A phone registered to someone else stays theirs until they revoke it.
            if existing and not existing["revoked"] and existing["account_id"] != owner:
                raise AuthError("This phone is registered to another account. Its owner must remove it first.")
            if existing and existing["account_id"] != owner:
                existing = None  # previously revoked by another account: start a fresh record
            pairing["used"] = True
            pairing["device_id"] = device_id
            # A new pairing may approve only its own actions until the owner trusts it, since a leaked
            # code or devices:add key mustn't yield a phone that approves purchases on the others.
            # The account's first phone has nothing to compare against, so it's trusted. A re-pair
            # starts over: device ids aren't secret, so trust can't ride along with one.
            first = not any(i != device_id and d["account_id"] == owner and not d["revoked"]
                            for i, d in self._data["devices"].items())

            key = secrets.token_urlsafe(32)
            now = time.time()
            self._data["devices"][device_id] = {
                "account_id": owner,
                "model": " ".join((model or "").split())[:80] or (existing or {}).get("model", "Android phone"),
                "key_hash": _hash_token(key),
                "created": (existing or {}).get("created", now),
                "paired_at": now,
                "last_seen": now,
                "revoked": False,
                # The phone's screen-lock-bound public key: its approvals must be signed with it.
                # Pairing again replaces it (or clears it, for an app too old to send one).
                "approval_key": approval_key or None,
                "trusted": first,
            }
            self._save()
            return key

    def pairing_status(self, account_id: str, code_id: str) -> dict | None:
        """For `openmob phone add`: {status: pending|paired|expired, device_id}. None if not this account's."""
        with self._lock:
            self._refresh()
            p = self._data["pairings"].get((code_id or "")[:PAIRING_ID_LEN])
            if not p or p["account_id"] != account_id:
                return None
            if p.get("device_id"):
                return {"status": "paired", "device_id": p["device_id"]}
            if p["used"] or p["expires"] < time.time():
                return {"status": "expired", "device_id": None}
            return {"status": "pending", "device_id": None}

    def trusted_for_approvals(self, device_id: str) -> bool:
        """May this phone answer approvals for its owner's other phones? Pairings from before trust
        existed count as trusted (they were paired with a signed-in session)."""
        with self._lock:
            self._refresh()
            d = self._data["devices"].get(device_id or "")
            return bool(d and not d["revoked"] and d.get("trusted", True))

    def trust_device(self, account_id: str, device_id: str) -> bool:
        """The owner trusts one of their phones for approvals. False if not theirs (or revoked)."""
        with self._lock:
            self._refresh()
            d = self._data["devices"].get(device_id or "")
            if not d or d["revoked"] or d["account_id"] != account_id:
                return False
            if not d.get("trusted", True):
                d["trusted"] = True
                d["trusted_at"] = time.time()
                self._save()
            return True

    def is_paired(self, account_id: str, device_id: str) -> bool:
        """This phone is paired (and not revoked) to this account."""
        with self._lock:
            self._refresh()
            d = self._data["devices"].get(device_id or "")
            return bool(d and not d["revoked"] and d["account_id"] == account_id)

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

    @staticmethod
    def key_fingerprint(key: str) -> str:
        """What a phone's key is stored as; a socket remembers it to notice a re-pair."""
        return _hash_token(key or "")

    def device_auth(self, device_id: str) -> dict | None:
        """A phone's pairing as it stands now: {account_id, revoked, key_hash, approval_key}."""
        with self._lock:
            self._refresh()
            d = self._data["devices"].get(device_id)
            if d is None:
                return None
            return {"account_id": d["account_id"], "revoked": bool(d["revoked"]), "key_hash": d["key_hash"],
                    "approval_key": d.get("approval_key")}

    def register_approval_key(self, device_id: str, key_hash: str, public_key: str) -> bool:
        """First connect of an already-paired phone whose app now has an approval key: keep it, once.
        Never replaces a registered key (only pairing again does) and only for the current device key."""
        with self._lock:
            self._refresh()
            d = self._data["devices"].get(device_id)
            if not d or d["revoked"] or d.get("approval_key") or d["key_hash"] != key_hash or not public_key:
                return False
            d["approval_key"] = public_key
            self._save()
            return True

    def device_permissions(self, device_id: str) -> dict | None:
        """The phone's permission settings (openmob/permissions.py), or None. Pairing again resets them."""
        with self._lock:
            self._refresh()
            d = self._data["devices"].get(device_id)
            return dict(d["permissions"]) if d and not d["revoked"] and isinstance(d.get("permissions"), dict) else None

    def set_device_permissions(self, device_id: str, settings: dict) -> bool:
        with self._lock:
            self._refresh()
            d = self._data["devices"].get(device_id)
            if not d or d["revoked"]:
                return False
            d["permissions"] = settings
            self._save()
            return True

    def list_devices(self, account_id: str) -> list[Device]:
        """Every phone paired to this account, online or not (revoked ones are left out)."""
        with self._lock:
            self._refresh()
            return [
                Device(id=device_id, paired_at=d.get("paired_at") or d["created"], trusted=d.get("trusted", True),
                       **{k: d[k] for k in ("account_id", "model", "created", "last_seen", "revoked")})
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

    def unpair_by_key(self, device_id: str, key: str) -> str:
        """The phone unpairs itself, proving it with its own device key. Returns its account, or raises."""
        with self._lock:
            account_id = self.verify_device(device_id, key)
            self._data["devices"][device_id]["revoked"] = True
            self._save()
            return account_id

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

    def create_api_key(self, account_id: str, name: str, scopes=DEFAULT_SCOPES,
                       confirm_approve: bool = False, max_mode: str = "auto") -> tuple[str, str]:
        """Returns (key id, key). The key is shown once; only its hash is kept.
        `confirm_approve`: the key may approve risky actions itself (else only the owner can).
        `max_mode`: the loosest permission mode its tasks may ask for (the phone's ceiling still applies)."""
        if max_mode not in KEY_MODES:
            raise AuthError(f"max_mode must be one of: {', '.join(KEY_MODES)}")
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
                "confirm_approve": bool(confirm_approve), "max_mode": max_mode,
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
            return {"id": key_id, "account_id": record["account_id"], "scopes": list(record["scopes"]),
                    "name": record["name"], "confirm_approve": bool(record.get("confirm_approve")),
                    "max_mode": record.get("max_mode") or "auto"}

    @staticmethod
    def _public_key(key_id: str, k: dict) -> dict:
        return {"id": key_id, "prefix": f"{KEY_PREFIX}{key_id}", "name": k["name"], "scopes": list(k["scopes"]),
                "created": k["created"], "last_used": k["last_used"], "disabled": bool(k.get("disabled")),
                "confirm_approve": bool(k.get("confirm_approve")), "max_mode": k.get("max_mode") or "auto"}

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
        """Give a key a new secret; the old secret stops working now. It stays the same key (same
        id, name, scopes and settings), so its limits, tasks and running work carry on unchanged."""
        with self._lock:
            self._refresh()
            k = self._data["api_keys"].get(key_id)
            if not k or k["account_id"] != account_id or k["revoked"]:
                return None
            key = f"{KEY_PREFIX}{key_id}_{secrets.token_urlsafe(24)}"
            k["hash"] = _hash_token(key)
            k["rotated"] = time.time()
            self._save()
            return key_id, key

    def set_key_confirm_approve(self, account_id: str, key_id: str, on: bool) -> bool:
        """Let one of the owner's keys approve risky actions itself (or take that back)."""
        with self._lock:
            self._refresh()
            k = self._data["api_keys"].get(key_id)
            if not k or k["account_id"] != account_id or k["revoked"]:
                return False
            k["confirm_approve"] = bool(on)
            self._save()
            return True

    def set_key_max_mode(self, account_id: str, key_id: str, mode: str) -> bool:
        """The loosest permission mode this key's tasks may ask for."""
        if mode not in KEY_MODES:
            raise AuthError(f"max_mode must be one of: {', '.join(KEY_MODES)}")
        with self._lock:
            self._refresh()
            k = self._data["api_keys"].get(key_id)
            if not k or k["account_id"] != account_id or k["revoked"]:
                return False
            k["max_mode"] = mode
            self._save()
            return True

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
            old = account.get("webhook") or {}
            account["webhook"] = {"url": url, "secret": secret}
            if old.get("secret"):  # the previous secret keeps verifying for a day, so receivers can switch over
                account["webhook"].update(previous_secret=old["secret"], previous_until=time.time() + WEBHOOK_SECRET_OVERLAP)
            self._save()
            return secret

    def webhook(self, account_id: str) -> tuple[str, str] | None:
        """(url, signing secret) for delivering task events, or None."""
        with self._lock:
            self._refresh()
            hook = self._data["accounts"].get(account_id, {}).get("webhook")
            return (hook["url"], hook["secret"]) if hook else None

    def webhook_secrets(self, account_id: str) -> list[str]:
        """Secrets to sign with now: the current one, plus the previous one during its 24 h overlap."""
        with self._lock:
            self._refresh()
            hook = self._data["accounts"].get(account_id, {}).get("webhook")
            if not hook:
                return []
            out = [hook["secret"]]
            if hook.get("previous_secret") and hook.get("previous_until", 0) > time.time():
                out.append(hook["previous_secret"])
            return out

    # --- admin: API kill switches -----------------------------------------------

    def admin_list_api_keys(self) -> list[dict]:
        with self._lock:
            self._refresh()
            accounts = self._data["accounts"]
            keys = [{**self._public_key(i, k), "account_id": k["account_id"],
                     "email": accounts.get(k["account_id"], {}).get("email")}
                    for i, k in self._data["api_keys"].items() if not k["revoked"]]
        return sorted(keys, key=lambda k: k["created"], reverse=True)

    def api_key_principal(self, key_id: str) -> dict | None:
        """A key's current standing, read fresh (no secret needed): for re-checking long-running work
        such as orchestrations. {id, account_id, scopes, name, confirm_approve, revoked, disabled,
        api_disabled}, or None if the key or its account doesn't exist."""
        with self._lock:
            self._refresh()
            k = self._data["api_keys"].get(key_id)
            account = self._data["accounts"].get(k["account_id"]) if k else None
            if not k or account is None:
                return None
            return {"id": key_id, "account_id": k["account_id"], "scopes": list(k["scopes"]), "name": k["name"],
                    "confirm_approve": bool(k.get("confirm_approve")), "max_mode": k.get("max_mode") or "auto",
                    "revoked": bool(k["revoked"]),
                    "disabled": bool(k.get("disabled")), "api_disabled": bool(account.get("api_disabled"))}

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
