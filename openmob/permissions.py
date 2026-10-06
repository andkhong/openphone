"""Permission modes: when the agent stops to ask the phone's owner before acting.

Five modes, tightest first (docs/safety.md#permission-modes):

    ask      Ask every step: the owner approves every changing action (tap, type + submit, launch…).
    plan     Plan first: one approval for a short numbered plan, then asks only off-plan or for money.
    auto     The default: asks at the commit point of risky actions (Send, Place order, Delete…) the
             request didn't ask for; a requested one runs once, in the task's app; money asks once.
    trusted  Like auto, and risky actions in owner-chosen apps or categories run without asking.
    never    No prompts, for a limited time. Blocked apps stay blocked. Purchases are refused unless the
             owner also turned on "Also allow purchases" when enabling it (never_money, below).

A mode only decides whether to ask. Approving always takes the owner's screen lock on a phone.

Per phone we keep {default_mode, ceiling, trusted, never_until, never_money}. `ceiling` is the loosest
mode that phone allows (ask … trusted); `never` is on while never_until is in the future. `never_money`
("Also allow purchases") lets money actions run under `never`; it can only be turned on together with
`never` (the patch's `allow_money`), and it ends with that window. Tightening is free from anywhere;
loosening past the ceiling, adding trusted entries, turning on `never` and allowing purchases need a
screen-lock-signed approval on that phone (DeviceManager.request_permission_change).
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MODES = ("ask", "plan", "auto", "trusted", "never")  # tightest first
RANK = {m: i for i, m in enumerate(MODES)}
CEILINGS = MODES[:-1]  # `never` is never a standing ceiling: it's switched on for a while
LABELS = {"ask": "Ask every step", "plan": "Plan first", "auto": "Ask for risky", "trusted": "Trusted apps", "never": "Don't ask"}
DESCRIPTIONS = {
    "ask": "Approve every tap, typed text and app launch on your phone.",
    "plan": "Approve a short plan once; asks again only off-plan or to pay.",
    "auto": "Asks at the final step of sends, deletes and payments you didn't ask for.",
    "trusted": "Like Auto, and apps or categories you trust act without asking.",
    "never": "No prompts for a while. Blocked apps stay blocked; purchases are refused unless you also allow them.",
}
DEFAULT_MODE = "auto"
KINDS = ("send", "delete", "post", "pay", "fill")
NEVER_DURATIONS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
DAY = 86400
# "Always allow" from a prompt: how long the trusted entry it creates lasts.
ALWAYS_DAYS = {"send": 30, "post": 30, "delete": 30, "pay": 1, "fill": 30}
PAY_MAX_DAYS = 7
MAX_ENTRY_DAYS = 90
MAX_ENTRIES = 50

# Which risky words make which kind of action (money words are config.money_keywords: kind "pay").
KIND_WORDS = {
    "send": ("send", "reply", "share to", "forward to"),
    "delete": ("delete", "remove", "uninstall", "erase", "clear", "discard", "trash"),
    "post": ("post", "publish", "tweet", "upload", "comment"),
}

DEFAULT_MONEY_KEYWORDS = ["buy", "pay", "order", "checkout", "subscribe", "purchase", "transfer", "book",
                          "donate", "place order", "payment", "top up", "recharge"]
# Steps on the way to a commit point, not the commit itself: never asked about on their own.
DEFAULT_SKIP_PHRASES = [
    "add to cart", "add to basket", "add to bag", "proceed to checkout", "go to checkout", "continue to checkout",
    "view cart", "view basket", "view order", "order history", "order details", "track order", "your orders",
    "my orders", "order status", "buy again", "see all", "view all", "learn more", "more options",
]
# App categories for trusted entries (package substrings). `pay` is never granted by category.
DEFAULT_APP_CATEGORIES = {
    "messaging": ["messaging", "mms", "whatsapp", "telegram", "signal", "messenger", "orca", "discord", "slack",
                  "viber", "line", "kakao", "wechat", "tencent.mm", "skype", "teams"],
    "email": ["gm", "email", "mail", "outlook", "protonmail", "fastmail"],
    "social": ["twitter", "instagram", "facebook.katana", "reddit", "linkedin", "mastodon", "threads", "tiktok",
               "zhiliaoapp", "snapchat", "bluesky", "tumblr", "pinterest"],
    "notes": ["keep", "notes", "notion", "evernote", "obsidian", "todo", "tasks"],
    "photos": ["photos", "gallery"],
}
SETTINGS_PACKAGE = re.compile(r"(^|\.)settings($|\.)|^com\.android\.settings|securitycenter|permissioncontroller",
                              re.IGNORECASE)
# Settings tiers (a page is known by its title, the text at the top of the screen, and by the target):
# refused in every mode: security-critical pages and the way into them.
DEFAULT_SETTINGS_REFUSE = [
    "screen lock", "lock screen", "security", "passwords", "passkeys", "fingerprint", "face unlock", "biometric",
    "accounts", "users", "multiple users", "add account", "remove account", "notification access",
    "special app access", "device admin", "device administrators", "default apps", "wireless debugging",
    "developer options", "usb debugging", "on-screen keyboard", "manage keyboards", "virtual keyboard",
    "keyboard list", "date & time", "date and time", "set time", "time zone", "install unknown apps",
    "unknown sources", "accessibility", "factory reset", "reset options", "erase all data", "display over other apps",
    "all files access", "usage access", "modify system settings", "device & app notifications",
    "trusted credentials", "encryption & credentials", "smart lock", "find my device", "private dns", "vpn",
]
# never asked: everyday pages and switches.
DEFAULT_SETTINGS_EVERYDAY = [
    "wi-fi", "wifi", "internet", "network & internet", "hotspot", "airplane mode", "bluetooth",
    "connected devices", "display", "dark theme", "dark mode", "brightness", "adaptive brightness", "font size",
    "screen timeout", "sound", "volume", "vibration", "do not disturb", "ringtone", "battery", "battery saver",
    "location", "notifications", "app notifications", "flashlight", "auto-rotate", "night light",
]
# asked even on an everyday page: pairing a new Bluetooth device.
DEFAULT_SETTINGS_ASK = ["pair new device", "pair a new device", "pair new", "pair"]
APP_INFO_MARKERS = ("app info", "force stop")
APP_INFO_ALLOWED = ("force stop", "clear cache")
APP_INFO_ASK = ("uninstall", "disable", "clear storage", "clear data", "uninstall updates")
# A tap that changes a setting (rather than opening a page): a switch, or one of these words.
SETTINGS_CHANGE_WORDS = re.compile(
    r"\b(turn on|turn off|enable|disable|allow|deny|pair|connect|disconnect|forget|reset|add|remove|delete|"
    r"uninstall|clear|ok|apply|save|confirm|activate|deactivate|grant|revoke|install|update|restart)\b", re.I)

# Autofill and saved-credential suggestions: asked about (refused under never).
DEFAULT_CREDENTIAL_TERMS = [
    "autofill", "auto-fill", "saved password", "saved passwords", "use password", "use saved password",
    "suggested password", "suggest strong password", "use passkey", "passkey", "password manager",
    "fill password", "use saved card", "saved card", "use saved payment", "sign in as", "continue as",
    "use this account", "choose an account",
]
BROWSER_PACKAGE = re.compile(r"chrome|browser|firefox|opera|brave|edge|sbrowser|duckduckgo|vivaldi|kiwi|webview",
                             re.IGNORECASE)
# After an approved money tap, the one follow-up commit tap these words name is covered (same app and
# task, within FOLLOWUP_SECONDS, until an app switch). Words that start a new flow always ask again.
DEFAULT_TERMINAL_WORDS = ["place order", "place your order", "confirm purchase", "complete purchase", "pay",
                          "pay now", "confirm", "confirm order", "confirm payment"]
DEFAULT_START_FLOW_WORDS = ["buy now", "buy", "subscribe", "book", "top up", "recharge", "donate"]
FOLLOWUP_SECONDS = 180
# Android runtime permission dialogs (agent/permission_dialogs.py; the phone's gatekeeper enforces the same,
# rules.json "permission_dialog"). Values tightest first. Never "Allow all the time", whatever the setting.
PERMISSION_TYPES = ("location", "camera", "microphone", "contacts", "photos", "phone", "sms", "calendar",
                    "nearby_devices", "physical_activity", "body_sensors")
# Owner decision: the task's own app gets "Only this time" with no prompt (the narrowest grant that lets the task
# go on), notifications allowed, location approximate.
PERMISSION_DEFAULTS = {"notifications": "allow", "location_accuracy": "approximate",
                       **{k: "once" for k in PERMISSION_TYPES}}
PERMISSION_QUESTIONS = {
    "once": "Let agents allow {what} \"only this time\" in permission dialogs without asking?",
    "while_using": "Let agents allow {what} \"while using the app\" in permission dialogs without asking?",
    "ask": "Ask you again before agents allow {what}?",
    "allow": "Let agents allow {what} in permission dialogs without asking?",
    "precise": "Let agents give apps your precise location without asking?",
}


def permission_values(kind: str) -> tuple:
    """A permission type's settings, tightest first."""
    if kind == "notifications":
        return ("deny", "allow")
    if kind == "location_accuracy":
        return ("approximate", "precise")
    return ("deny", "ask", "once", "while_using")


class PermissionsInvalid(ValueError):
    """A permissions change that isn't valid (bad mode, bad entry)."""


class ModeNotAllowed(Exception):
    """A mode or loosening this caller or phone may not have (REST/MCP: 403 mode_not_allowed)."""


class PhoneUnavailable(Exception):
    """A loosening needs the phone's approval, but the phone isn't connected (409 phone_offline)."""


def rank(mode: str) -> int:
    return RANK[mode]


def tightest(*modes: str | None) -> str:
    return min((m for m in modes if m), key=rank)


def looser(a: str, b: str) -> bool:
    """`a` asks less than `b`."""
    return RANK[a] > RANK[b]


def default_settings() -> dict:
    return {"default_mode": DEFAULT_MODE, "ceiling": DEFAULT_MODE, "trusted": [], "never_until": None,
            "never_money": False, "permission_prompts": dict(PERMISSION_DEFAULTS), "task_approval": True,
            "everyday": False}


def normalize(settings: dict | None, now: float | None = None) -> dict:
    """A clean copy: unknown values replaced by defaults, expired trusted entries dropped, and an
    expired `never` switched off (a default of `never` reverts to auto)."""
    now = time.time() if now is None else now
    s = default_settings()
    if isinstance(settings, dict):
        if settings.get("ceiling") in CEILINGS:
            s["ceiling"] = settings["ceiling"]
        if settings.get("default_mode") in MODES:
            s["default_mode"] = settings["default_mode"]
        until = settings.get("never_until")
        s["never_until"] = float(until) if isinstance(until, (int, float)) and until > now else None
        # Purchases without asking last only as long as the never window they were allowed with.
        s["never_money"] = bool(s["never_until"]) and settings.get("never_money") is True
        # Android's runtime permission dialogs (agent/permission_dialogs.py): the owner's defaults per type.
        if isinstance(settings.get("everyday"), bool):
            s["everyday"] = settings["everyday"]
        if isinstance(settings.get("task_approval"), bool):
            s["task_approval"] = settings["task_approval"]
        prompts = settings.get("permission_prompts") if isinstance(settings.get("permission_prompts"), dict) else {}
        s["permission_prompts"] = {k: (prompts.get(k) if prompts.get(k) in permission_values(k) else v)
                                   for k, v in PERMISSION_DEFAULTS.items()}
        s["trusted"] = [e for e in (settings.get("trusted") or [])
                        if isinstance(e, dict) and (e.get("expires_at") or 0) > now]
    if s["default_mode"] == "never" and not s["never_until"]:
        s["default_mode"] = DEFAULT_MODE
    elif s["default_mode"] != "never" and looser(s["default_mode"], s["ceiling"]):
        s["default_mode"] = s["ceiling"]
    return s


def effective_ceiling(settings: dict, now: float | None = None) -> str:
    now = time.time() if now is None else now
    until = settings.get("never_until")
    return "never" if until and until > now else settings.get("ceiling", DEFAULT_MODE)


def public(settings: dict, now: float | None = None) -> dict:
    """The settings as the dashboard and the API show them."""
    s = normalize(settings, now)
    return {"default_mode": s["default_mode"], "ceiling": effective_ceiling(s, now), "standing_ceiling": s["ceiling"],
            "never_until": s["never_until"], "never_money": s["never_money"], "trusted": s["trusted"],
            "permission_prompts": s["permission_prompts"], "task_approval": s["task_approval"],
            "everyday": s["everyday"],
            "modes": list(MODES)}


def validate_entry(entry: dict, categories: dict, now: float | None = None) -> dict:
    """One trusted entry as stored: {app | category, kinds, expires_at, source}."""
    now = time.time() if now is None else now
    if not isinstance(entry, dict):
        raise PermissionsInvalid("Each trusted entry is an object.")
    app = (entry.get("app") or "").strip() or None
    category = (entry.get("category") or "").strip().lower() or None
    if bool(app) == bool(category):
        raise PermissionsInvalid("A trusted entry names exactly one of app (a package) or category.")
    if app and not re.fullmatch(r"[A-Za-z0-9_.]{1,200}", app):
        raise PermissionsInvalid(f"Not a package name: {app!r}.")
    if category and category not in categories:
        raise PermissionsInvalid(f"Unknown category {category!r}. Choose one of: {', '.join(sorted(categories))}.")
    kinds = sorted({str(k) for k in (entry.get("kinds") or [])})
    if not kinds or any(k not in KINDS for k in kinds):
        raise PermissionsInvalid(f"kinds must be some of: {', '.join(KINDS)}.")
    if ("pay" in kinds or "fill" in kinds) and category:
        raise PermissionsInvalid("pay and fill can only be trusted for one named app, never for a category.")
    expires = entry.get("expires_at")
    default_days = ALWAYS_DAYS["pay"] if "pay" in kinds else ALWAYS_DAYS["send"]
    if expires is None:
        expires = now + default_days * DAY
    if not isinstance(expires, (int, float)) or expires <= now:
        raise PermissionsInvalid("expires_at must be a future unix time.")
    expires = min(float(expires), now + (PAY_MAX_DAYS if "pay" in kinds else MAX_ENTRY_DAYS) * DAY)
    out = {"kinds": kinds, "expires_at": expires, "source": str(entry.get("source") or "settings")[:20]}
    if entry.get("target"):  # a share grant: only to this target
        out["target"] = " ".join(str(entry["target"]).split()).lower()[:80]
    out["app" if app else "category"] = app or category
    return out


def _covered(new: dict, old_entries: list[dict]) -> bool:
    """`new` grants nothing an existing entry doesn't already grant."""
    target = ("app", new["app"]) if "app" in new else ("category", new["category"])
    return any(o.get(target[0]) == target[1] and set(new["kinds"]) <= set(o["kinds"])
               and o.get("target") in (None, new.get("target"))
               and new["expires_at"] <= o["expires_at"] + 60 for o in old_entries)


def apply_patch(current: dict, patch: dict, categories: dict, now: float | None = None) -> tuple[dict, list[str]]:
    """The settings after `patch`, and what in it loosens them (empty: a pure tightening, which needs
    no approval). patch: {default_mode?, ceiling?, trusted? (the whole new list), never? ("1h" | "24h" |
    "7d" | "off"), allow_money? (bool)}. `allow_money: true` ("Also allow purchases") goes only with a
    `never` duration and always needs the phone's approval; `never` without it (or `allow_money: false`)
    turns purchases off. Raises PermissionsInvalid for invalid values."""
    now = time.time() if now is None else now
    cur = normalize(current, now)
    new = {**cur, "trusted": list(cur["trusted"])}
    loosens: list[str] = []
    if "ceiling" in patch and patch["ceiling"] is not None:
        ceiling = patch["ceiling"]
        if ceiling not in CEILINGS:
            raise PermissionsInvalid(f"ceiling must be one of: {', '.join(CEILINGS)} (turn on never with never).")
        if looser(ceiling, cur["ceiling"]):
            loosens.append(f"ceiling:{ceiling}")
        new["ceiling"] = ceiling
    allow_money = patch.get("allow_money")
    if allow_money is not None and not isinstance(allow_money, bool):
        raise PermissionsInvalid("allow_money is true or false.")
    if allow_money and patch.get("never") not in NEVER_DURATIONS:
        raise PermissionsInvalid("allow_money goes with turning on never (never: 1h, 24h or 7d), approved on "
                                 "the phone.")
    if "never" in patch and patch["never"] is not None:
        never = patch["never"]
        if never == "off":
            new["never_until"] = None
            new["never_money"] = False
        elif never in NEVER_DURATIONS:
            until = now + NEVER_DURATIONS[never]
            if not cur["never_until"] or until > cur["never_until"] + 60:
                loosens.append(f"never:{never}")
            new["never_until"] = until
            new["never_money"] = bool(allow_money)  # off unless asked for again, explicitly
            if allow_money:  # always the phone's approval, with its own wording (describe_loosening)
                loosens.append(f"money:{never}")
        else:
            raise PermissionsInvalid(f"never must be one of: {', '.join(NEVER_DURATIONS)} or off.")
    if allow_money is False:
        new["never_money"] = False
    if patch.get("everyday") is not None:
        if not isinstance(patch["everyday"], bool):
            raise PermissionsInvalid("everyday is true or false.")
        if patch["everyday"] and not cur["everyday"]:
            loosens.append("everyday:on")  # stop confirming everyday actions: the phone approves (fingerprint)
        new["everyday"] = patch["everyday"]
    if patch.get("task_approval") is not None:
        if not isinstance(patch["task_approval"], bool):
            raise PermissionsInvalid("task_approval is true or false.")
        if patch["task_approval"] and not cur["task_approval"]:
            loosens.append("task_approval:on")  # one approval covering a task's steps: the phone approves
        new["task_approval"] = patch["task_approval"]
    if patch.get("permission_prompts") is not None:
        prompts = patch["permission_prompts"]
        if not isinstance(prompts, dict):
            raise PermissionsInvalid("permission_prompts is an object: {type: setting}.")
        new["permission_prompts"] = dict(cur["permission_prompts"])
        for kind, value in prompts.items():
            if kind not in PERMISSION_DEFAULTS:
                raise PermissionsInvalid(f"Unknown permission type {kind!r}. One of: {', '.join(PERMISSION_DEFAULTS)}.")
            values = permission_values(kind)
            if value not in values:
                raise PermissionsInvalid(f"{kind} is one of: {', '.join(values)}.")
            if values.index(value) > values.index(cur["permission_prompts"][kind]):
                loosens.append(f"permission:{kind}:{value}")  # loosening: the phone's screen lock
            new["permission_prompts"][kind] = value
    if "trusted" in patch and patch["trusted"] is not None:
        if not isinstance(patch["trusted"], list) or len(patch["trusted"]) > MAX_ENTRIES:
            raise PermissionsInvalid(f"trusted is a list of at most {MAX_ENTRIES} entries.")
        entries = [validate_entry(e, categories, now) for e in patch["trusted"]]
        for e in entries:
            if not _covered(e, cur["trusted"]):
                loosens.append(f"trusted:{e.get('app') or e.get('category')}:{','.join(e['kinds'])}")
        new["trusted"] = entries
    if "default_mode" in patch and patch["default_mode"] is not None:
        mode = patch["default_mode"]
        if mode not in MODES:
            raise PermissionsInvalid(f"default_mode must be one of: {', '.join(MODES)}.")
        if mode == "never":
            if not new["never_until"]:
                raise PermissionsInvalid("Turn on never first (never: 1h, 24h or 7d), with your phone's approval.")
        elif looser(mode, new["ceiling"]):
            loosens.append(f"ceiling:{mode}")
            new["ceiling"] = mode
        new["default_mode"] = mode
    if new["default_mode"] == "never" and not new["never_until"]:
        new["default_mode"] = DEFAULT_MODE
    return normalize(new, now), loosens


def raise_for_mode(current: dict, mode: str, never: str | None = None) -> dict:
    """The smallest patch that allows `mode` on a phone (for "Approve on your phone" from the picker)."""
    if mode == "never":
        return {"never": never or "1h"}
    return {"ceiling": mode} if mode in CEILINGS and looser(mode, current.get("ceiling", DEFAULT_MODE)) else {}


def describe_loosening(loosens: list[str]) -> str:
    """The phone's question for a loosening, in plain words."""
    parts = []
    money = any(item.startswith("money:") for item in loosens)
    for item in loosens:
        kind, _, rest = item.partition(":")
        if kind in ("never", "money"):
            span = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days"}[rest]
            parts.append(f"Allow agents to act without asking on this phone including purchases for {span}?" if money
                         else f"Allow agents to act without asking on this phone for {span}?")
        elif kind == "ceiling":
            parts.append(f"Allow the {LABELS[rest]} mode on this phone?")
        elif kind == "everyday":
            parts.append("Stop confirming everyday actions (messages, shares, posts, deletes)? Purchases and "
                         "dangerous actions still need your fingerprint.")
        elif kind == "task_approval":
            parts.append("Let one approval of a task cover the risky steps it needs (purchases still ask)?")
        elif kind == "permission":
            which, _, value = rest.partition(":")
            parts.append(PERMISSION_QUESTIONS.get(value, "Change {what} prompts?").format(what=which.replace("_", " ")))
        elif kind == "trusted":
            target, _, kinds = rest.rpartition(":")
            parts.append(f"Let agents {'/'.join(kinds.split(','))} in {target} without asking?")
    return " ".join(dict.fromkeys(parts)) or "Change how agents ask on this phone?"


def always_entry(package: str, kind: str, target: str | None = None, now: float | None = None) -> dict:
    """The trusted entry an "Always allow" answer creates: this app, this kind (and for a share, this
    target), for ALWAYS_DAYS."""
    now = time.time() if now is None else now
    entry = {"app": package, "kinds": [kind], "expires_at": now + ALWAYS_DAYS[kind] * DAY, "source": "prompt"}
    if target:
        entry["target"] = " ".join(target.split()).lower()[:80]
    return entry


def add_entry(settings: dict, entry: dict, now: float | None = None) -> dict:
    s = normalize(settings, now)
    if not _covered(entry, s["trusted"]):
        s["trusted"] = (s["trusted"] + [entry])[-MAX_ENTRIES:]
    return s


def category_of(package: str | None, categories: dict) -> set[str]:
    if not package:
        return set()
    parts = set(re.split(r"[._]", package.lower()))
    low = package.lower()
    return {c for c, needles in categories.items()
            if any(n in parts or ("." in n and n in low) or (len(n) > 4 and n in low) for n in needles)}


def trusted_for(settings: dict, package: str | None, kind: str | None, categories: dict, use_categories: bool,
                target: str | None = None, now: float | None = None) -> bool:
    """A live trusted entry lets `kind` happen in `package` without asking. App entries count in
    auto and trusted; category entries only in trusted; pay and fill only ever by an app entry. A
    share (`target` set) needs an entry for that target; an entry with a target covers nothing else."""
    if not package or not kind:
        return False
    target = " ".join(target.split()).lower()[:80] if target else None
    cats = category_of(package, categories) if use_categories and kind not in ("pay", "fill") else set()
    for e in normalize(settings, now)["trusted"]:
        if kind not in e["kinds"] or e.get("target") != target:
            continue
        if e.get("app") == package or (e.get("category") in cats and kind not in ("pay", "fill")):
            return True
    return False


# --- storage --------------------------------------------------------------------------------


class PermissionStore:
    """Per-phone settings, by session id ("app-<device id>" or an adb serial). In memory, or a JSON
    file when given a path. Hosted servers use AccountPermissions, so a re-pair starts clean."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if self.path and self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self._data = {}

    def get(self, session_id: str) -> dict:
        with self._lock:
            return normalize(self._data.get(session_id))

    def set(self, session_id: str, settings: dict) -> dict:
        settings = normalize(settings)
        with self._lock:
            self._data[session_id] = settings
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, indent=2))
                tmp.replace(self.path)
        return settings


class AccountPermissions:
    """Settings kept on the phone's pairing record in the AccountStore (pairing again resets them)."""

    def __init__(self, store, fallback: PermissionStore | None = None):
        self.store = store
        self.fallback = fallback or PermissionStore()  # adb phones on a hosted server (none in practice)

    def get(self, session_id: str) -> dict:
        if session_id.startswith("app-"):
            return normalize(self.store.device_permissions(session_id.removeprefix("app-")))
        return self.fallback.get(session_id)

    def set(self, session_id: str, settings: dict) -> dict:
        settings = normalize(settings)
        if session_id.startswith("app-"):
            self.store.set_device_permissions(session_id.removeprefix("app-"), settings)
            return settings
        return self.fallback.set(session_id, settings)


# --- one task's policy ------------------------------------------------------------------------


@dataclass
class TaskPermissions:
    """The mode a task asked for, checked against the phone's live settings at every decision, so
    turning `never` off (or its expiry) takes effect mid-task."""

    requested: str = DEFAULT_MODE
    settings: Callable[[], dict] = field(default=default_settings)

    def mode(self) -> str:
        return tightest(self.requested, effective_ceiling(normalize(self.settings())))

    def current_settings(self) -> dict:
        return normalize(self.settings())
