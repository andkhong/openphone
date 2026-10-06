"""Android's runtime permission dialogs, handled by rule with no model call.

"Allow Chess.com to send you notifications? · Allow · Don't allow" used to cost a vision step each (3-6 s), and
the model flip-flopped between Allow and Don't allow. The agent loop now answers these at observation time
(Agent._permission_dialog), before the model sees the screen, by the owner's policy:

- notifications: the phone's setting (`notifications_permission`, default deny);
- camera, microphone, location, contacts, photos and media, phone, SMS, calendar, nearby devices, and anything
  else: ask the owner on the phone ("Allow Chess.com to use your camera?"). On a yes the narrowest grant is
  tapped: "Only this time", else "While using the app", else "Allow"; never "Allow all the time".
- a request from an app other than the task's own: ask (S2 of docs/design/phone-gatekeeper.md);
- under `never` (Don't ask): notifications follow the setting; everything else is denied.

The tap goes through the usual checks (on a gatekeeper phone, the phone's own gate), as a fast step recorded with
`handled_by: "permission_rule"`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..phone.adb import UIElement

PACKAGES = re.compile(r"^(com\.google\.android|com\.android|com\.samsung\.android)\.permissioncontroller$"
                      r"|^com\.(google\.)?android\.packageinstaller$")
BUTTONS = {
    "one_time": re.compile(r"permission_allow_one_time_button$"),
    "foreground": re.compile(r"permission_allow_foreground_only_button$"),
    "allow": re.compile(r"permission_allow_button$"),
    "always": re.compile(r"permission_allow_always_button$"),
    "deny": re.compile(r"permission_deny_button$"),
    "deny_forever": re.compile(r"permission_deny_and_dont_ask_again_button$"),
}
MESSAGE = re.compile(r"permission_message$")
# The dialog's wording -> the permission it asks for (AOSP and One UI phrasing, English).
KINDS = [
    ("notifications", re.compile(r"notification", re.I)),
    ("camera", re.compile(r"take pictures|record video|camera", re.I)),
    ("microphone", re.compile(r"record audio|microphone", re.I)),
    ("location", re.compile(r"location", re.I)),
    ("contacts", re.compile(r"contacts", re.I)),
    ("nearby_devices", re.compile(r"nearby devices", re.I)),
    ("photos", re.compile(r"photos|videos|media|music and audio|files", re.I)),
    ("phone", re.compile(r"phone calls|call logs", re.I)),
    ("sms", re.compile(r"\bSMS\b|text messages", re.I)),
    ("calendar", re.compile(r"calendar", re.I)),
]
WORDS = {"notifications": "notifications", "camera": "camera", "microphone": "microphone", "location": "location",
         "contacts": "contacts", "nearby_devices": "nearby devices", "photos": "photos and media",
         "phone": "phone calls", "sms": "text messages", "calendar": "calendar", "other": "data"}
APP = re.compile(r"^Allow\s+(?P<app>.+?)\s+to\s", re.I | re.S)


@dataclass
class Dialog:
    app: str  # the requesting app's label, as the dialog names it
    kind: str  # notifications | camera | … | other
    message: str
    buttons: dict  # role -> UIElement


def detect(package: str | None, elements: list[UIElement]) -> Dialog | None:
    """A runtime permission dialog on screen, or None."""
    buttons = {}
    message = ""
    for e in elements:
        rid = e.resource_id or ""
        for role, rx in BUTTONS.items():
            if rx.search(rid) and role not in buttons:
                buttons[role] = e
        if MESSAGE.search(rid) and (e.text or e.desc):
            message = e.text or e.desc
    if not (package and PACKAGES.search(package)) and not (buttons and message):
        return None
    if not buttons or not ("deny" in buttons or "deny_forever" in buttons):
        return None
    kind = next((k for k, rx in KINDS if rx.search(message)), "other")
    m = APP.search(message)
    return Dialog(app=(m.group("app").strip() if m else ""), kind=kind, message=message, buttons=buttons)


def same_app(a: str, b: str | None) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())  # noqa: E731
    return bool(a) and bool(b) and norm(a) == norm(b)


def decide(dialog: Dialog, *, mode: str, notifications: str, task_app_label: str | None) -> str:
    """allow | deny | ask. Denying is always safe, so it never needs the owner."""
    own = not task_app_label or same_app(dialog.app, task_app_label)
    if dialog.kind == "notifications":
        if notifications != "allow":
            return "deny"
        if own:
            return "allow"
        return "deny" if mode == "never" else "ask"  # another app's request (S2)
    if mode == "never":
        return "deny"  # nobody can be asked
    return "ask"


def button(dialog: Dialog, answer: str) -> UIElement | None:
    """The narrowest button for the answer: a yes prefers "Only this time", then "While using the app"."""
    order = ("one_time", "foreground", "allow") if answer == "allow" else ("deny", "deny_forever")
    return next((dialog.buttons[r] for r in order if r in dialog.buttons), None)


def question(dialog: Dialog) -> str:
    return f"Allow {dialog.app or 'this app'} to use your {WORDS.get(dialog.kind, 'data')}?"
