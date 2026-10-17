"""Who is calling. Every front door turns its credential into a Principal here and hands it to
V1Service, which decides everything else from it.

Today the only credential is an API key (Authorization: Bearer om_live_…). OAuth access tokens
(om_at_…, audience-bound to /mcp) will be one more branch in `resolve`; nothing downstream changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..accounts import AccessDisabled, AuthError
from .errors import Forbidden, Unauthorized

API_KEY_PREFIX = "om_live_"
MISSING = "Send your API key as: Authorization: Bearer om_live_…"


@dataclass(frozen=True)
class Principal:
    id: str  # what quotas, rate limits and the audit log are keyed by: the API key id
    account_id: str  # whose phones, tasks and notes this caller may reach
    scopes: frozenset[str]
    name: str  # shown to the owner, e.g. on the phone's "who is using it" line
    kind: str = "api_key"  # later: "oauth"
    confirm_approve: bool = False  # the owner let this caller approve risky actions itself
    max_mode: str = "auto"  # the loosest permission mode its tasks may ask for (the phone's ceiling applies too)

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def bearer_token(authorization: str | None) -> str | None:
    """The token from an `Authorization: Bearer …` header, or None."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def resolve(store, token: str | None) -> Principal:
    """The Principal behind a bearer token, or Unauthorized (401) / Forbidden (403, switched off)."""
    if not token:
        raise Unauthorized(MISSING)
    try:
        key = store.verify_api_key(token)
    except AccessDisabled as exc:
        raise Forbidden(str(exc)) from None
    except AuthError:
        raise Unauthorized("Invalid API key.") from None
    return Principal(id=key["id"], account_id=key["account_id"], scopes=frozenset(key["scopes"]),
                     name=key["name"], confirm_approve=bool(key.get("confirm_approve")),
                     max_mode=key.get("max_mode") or "auto")
