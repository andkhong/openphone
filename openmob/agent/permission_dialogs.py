"""Android's runtime permission dialogs, handled by rule with no model call.

"Allow Chess.com to send you notifications? · Allow · Don't allow" used to cost a vision step each (3-6 s), and
the model flip-flopped between Allow and Don't allow. The agent loop now answers these at observation time
(Agent._permission_dialog), before the model sees the screen, by the owner's settings (`permission_prompts` in
the phone's permission settings, openmob/permissions.py):

- notifications: `deny` (default) | `allow`;
- location, camera, microphone, contacts, photos and media, phone, SMS, calendar, nearby devices: `ask` (default:
  the owner is asked on the phone, "Allow Kalshi to use your location?") | `once` | `while_using` | `deny`;
- location accuracy: `approximate` (default: the Approximate card is chosen before granting) | `precise`;
- **never "Allow all the time"**: if that's the only way to grant, the owner is asked;
- a request from an app other than the task's own: ask (S2 of docs/design/phone-gatekeeper.md);
- under Don't ask (`never`): nobody can be asked, so anything that would ask is denied; `once` and `while_using`
  still grant.

On a yes the narrowest grant is tapped: "Only this time", else "While using the app", else "Allow". Each tap goes
through the usual checks (a gatekeeper phone enforces the same settings itself, signed on the phone), as a fast
step recorded with `handled_by: "permission_rule"`.
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
    "precise": re.compile(r"location_accuracy_radio_fine$|accuracy_fine|precise_location", re.I),
    "approximate": re.compile(r"location_accuracy_radio_coarse$|accuracy_coarse|approximate_location", re.I),
}
TEXTS = {"precise": re.compile(r"^precise$", re.I), "approximate": re.compile(r"^approximate$", re.I)}
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
    buttons: dict  # role -> UIElement (grant/deny buttons, and the Precise/Approximate cards)


@dataclass
class Plan:
    answer: str  # allow | deny | ask
    grant: str | None = None  # once | while_using (which grant `allow` taps); None for notifications or deny
    accuracy: str | None = None  # approximate | precise: the location card to choose before granting


def detect(package: str | None, elements: list[UIElement]) -> Dialog | None:
    """A runtime permission dialog on screen, or None."""
    buttons = {}
    message = ""
    for e in elements:
        rid = e.resource_id or ""
        for role, rx in BUTTONS.items():
            if rx.search(rid) and role not in buttons:
                buttons[role] = e
        for role, rx in TEXTS.items():
            if role not in buttons and rx.search((e.text or e.desc or "").strip()):
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


def decide(dialog: Dialog, *, mode: str, prompts: dict, task_app_label: str | None) -> Plan:
    """What to do by the owner's settings. Denying is always safe, so it never needs the owner."""
    never = mode == "never"
    own = not task_app_label or same_app(dialog.app, task_app_label)
    accuracy = (prompts.get("location_accuracy") or "approximate") if dialog.kind == "location" else None
    if dialog.kind == "notifications":
        if prompts.get("notifications", "deny") != "allow":
            return Plan("deny")
        if own:
            return Plan("allow")
        return Plan("deny" if never else "ask")
    setting = prompts.get(dialog.kind, "ask") if own else "ask"  # another app's request (S2) always asks
    if setting == "deny":
        return Plan("deny")
    if setting in ("once", "while_using"):
        if button(dialog, "allow", setting) is not None:
            precise = dialog.buttons.get("precise")
            if accuracy == "approximate" and precise is not None and precise.checked is True \
                    and "approximate" not in dialog.buttons:
                return Plan("deny" if never else "ask", "once", accuracy)  # can't step down from a pre-selected Precise
            return Plan("allow", setting, accuracy)
        # Only a looser grant is offered (e.g. "Allow all the time"): the owner decides.
    return Plan("deny" if never else "ask", "once", accuracy)


def button(dialog: Dialog, answer: str, grant: str | None = None, asked: bool = False) -> UIElement | None:
    """The button for the answer. `once` takes only "Only this time"; `while_using` "While using the app" (or a
    plain "Allow"); after the owner's yes, the narrowest offered. "Allow all the time" only when the owner said yes and it's
    the only way to grant (`asked`)."""
    if answer != "allow":
        order = ("deny", "deny_forever")
    elif dialog.kind == "notifications":
        order = ("allow",)
    elif grant == "once" and not asked:
        order = ("one_time",)
    elif grant == "while_using" and not asked:
        order = ("foreground", "allow", "one_time")
    else:
        order = ("one_time", "foreground", "allow") + (("always",) if asked else ())
    return next((dialog.buttons[r] for r in order if r in dialog.buttons), None)


def accuracy_card(dialog: Dialog, accuracy: str | None) -> UIElement | None:
    """The Precise/Approximate card to tap first, or None (no cards, or already chosen)."""
    card = dialog.buttons.get(accuracy) if accuracy else None
    return None if card is None or card.checked is True else card


def question(dialog: Dialog) -> str:
    return f"Allow {dialog.app or 'this app'} to use your {WORDS.get(dialog.kind, 'data')}?"
