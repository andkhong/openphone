"""Android's runtime permission dialogs, handled by rule with no model call.

"Allow Chess.com to send you notifications? · Allow · Don't allow" used to cost a vision step each (3-6 s), and
the model flip-flopped between Allow and Don't allow (a real run: two "Don't allow" taps and a BACK, ~20 s). The
agent loop now answers these at observation time (Agent._permission_dialog), before the model sees the screen, by
the owner's defaults (config `permission_prompts`; on a gatekeeper build, the phone's permission settings first):

- notifications: `allow` (default) | `deny`;
- location, camera, microphone, contacts, photos and media, phone, SMS, calendar, nearby devices, physical
  activity, body sensors: `once` (default: "Only this time", or the narrowest grant the dialog offers) | `ask` (the
  owner is asked on the phone, "Allow Kalshi to use your location?") | `while_using` | `deny`;
- location accuracy: `approximate` (default: the Approximate card is chosen before granting) | `precise`;
- photos and media: "Allow limited access" when offered, else "Only this time", else "Don't allow" (never "Allow
  all");
- contacts, SMS, phone, calendar, body sensors, nearby devices: where the dialog offers no one-time option, the
  lasting "Allow" asks the owner (`persistent_grants`: `ask` (default) | `allow`);
- before the task has an app, any request counts as another app's (it asks);
- **never "Allow all the time"**: if that's the only way to grant, the request is denied (the phone refuses it too);
- a request that itself talks about permissions ("don't allow notifications") is left to the model;
- a request from an app other than the task's own: ask (S2 of docs/design/phone-gatekeeper.md);
- under Don't ask (`never`): nobody can be asked, so anything that would ask is denied; `once` and `while_using`
  still grant.

On a yes the narrowest grant is tapped: "Only this time", else "While using the app", else "Allow". Each tap goes
through the usual checks (a gatekeeper phone enforces the same settings itself, signed on the phone), as a fast
step recorded with `source: "auto_dialog"`.

Recognized: a window of the permission controller (Google, AOSP, Samsung), or, whatever the package, a "Don't allow"
button together with the dialog's "Allow X to …" message (the gatekeeper app OCRs the dialogs Android hides from
accessibility and lists them as OCRText elements, the message with resource id `ocr_permission_message`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..permissions import PERMISSION_DEFAULTS as DEFAULTS  # noqa: F401  (the owner's defaults, per type)
from ..phone.adb import UIElement

# A request that says how to answer permission prompts ("don't allow notifications", "deny location access"): the
# model answers them, not the rule.
_KINDS = r"(?:notification|location|camera|microphone|\bmic\b|contacts|photos|media|nearby|bluetooth|calendar|sms)"
_ANSWERS = r"(?:don['’]?t allow|do not allow|disallow|deny|denied|allow|grant|block|refuse|reject|decline)"
TALKS_PERMISSIONS = re.compile(rf"\bpermissions?\b|\b{_ANSWERS}\W+(?:\w+\W+){{0,4}}?{_KINDS}"
                               rf"|{_KINDS}\w*\W+(?:\w+\W+){{0,3}}?{_ANSWERS}\b", re.I)


def request_mentions_permissions(task: str) -> bool:
    return bool(TALKS_PERMISSIONS.search(task or ""))


PACKAGES = re.compile(r"^(com\.google\.android|com\.android|com\.samsung\.android)\.permissioncontroller$"
                      r"|^com\.(google\.)?android\.packageinstaller$")
BUTTONS = {
    "one_time": re.compile(r"permission_allow_one_time_button$"),
    "foreground": re.compile(r"permission_allow_foreground_only_button$"),
    "allow": re.compile(r"permission_allow_button$"),
    "always": re.compile(r"permission_allow_always_button$"),
    "limited": re.compile(r"permission_allow_selected_button$"),
    "deny": re.compile(r"permission_deny_button$"),
    "deny_forever": re.compile(r"permission_deny_and_dont_ask_again_button$"),
    "precise": re.compile(r"location_accuracy_radio_fine$|accuracy_fine|precise_location", re.I),
    "approximate": re.compile(r"location_accuracy_radio_coarse$|accuracy_coarse|approximate_location", re.I),
}
# By text: the Precise/Approximate cards, and every button of a dialog the phone read with OCR (Android 14+ hides
# it from accessibility; class OCRText, no resource ids). English, Spanish, German.
TEXTS = {
    "precise": re.compile(r"^(precise|precisa|genau)$", re.I),
    "approximate": re.compile(r"^(approximate|aproximada|ungefähr)$", re.I),
    "one_time": re.compile(r"^(only this time|solo esta vez|nur dieses mal)$", re.I),
    "foreground": re.compile(r"^(while using the app|mientras la app está en uso|mientras se usa la app"
                             r"|bei nutzung der app|während der app-nutzung)$", re.I),
    "always": re.compile(r"^(allow all the time|permitir todo el tiempo|permitir siempre|immer zulassen)$", re.I),
    "limited": re.compile(r"^(allow limited access|select photos( and videos)?|permitir acceso limitado"
                          r"|eingeschränkten zugriff erlauben)$", re.I),
    "deny": re.compile(r"^(don['’]t allow|deny|no permitir|nicht zulassen)$", re.I),
    "allow": re.compile(r"^(allow|permitir|zulassen)$", re.I),
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
    ("physical_activity", re.compile(r"physical activity", re.I)),
    ("body_sensors", re.compile(r"body sensors", re.I)),
]
WORDS = {"notifications": "notifications", "camera": "camera", "microphone": "microphone", "location": "location",
         "contacts": "contacts", "nearby_devices": "nearby devices", "photos": "photos and media",
         "phone": "phone calls", "sms": "text messages", "calendar": "calendar",
         "physical_activity": "physical activity", "body_sensors": "body sensors", "other": "data"}
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


OCR_MESSAGE_ID = "ocr_permission_message"  # the gatekeeper app's OCR'd dialog title (class OCRText)
TITLE = re.compile(r"^Allow\s+.+?\s+to\s.+\?$", re.I | re.S)
# Kinds whose grant lasts: where the dialog offers no one-time option, the default "once" doesn't stretch to a lasting
# "Allow"; the owner is asked (unless they set persistent_grants: allow). The gatekeeper's rules list the same.
PERSISTENT_ASK = ("contacts", "sms", "phone", "calendar", "body_sensors", "nearby_devices")


def detect(package: str | None, elements: list[UIElement], gatekeeper: bool = False) -> Dialog | None:
    """A runtime permission dialog on screen, or None. Only the system's:
    - the permission controller's window: the package in front is the controller (never an app's resource ids,
      which a web page or an app can set to anything);
    - on a gatekeeper phone, its OCR reading of a hidden dialog (class OCRText, id `ocr_permission_message`; the
      phone strips both marks from every app's tree), when the package is missing or the controller;
    - an unidentified window (no package) the server's own OCR reads as an "Allow X to …?" title."""
    controller = bool(package and PACKAGES.search(package))
    if package and not controller:
        return None  # an app's screen, whatever it looks like
    buttons = {}
    message = ""
    trusted = controller
    for e in elements:
        if e.covered:
            continue  # under a higher window: not the dialog's
        rid = e.resource_id or ""
        for role, rx in BUTTONS.items():
            if rx.search(rid) and role not in buttons:
                buttons[role] = e
        for role, rx in TEXTS.items():
            if role not in buttons and rx.search((e.text or e.desc or "").strip()):
                buttons[role] = e
        if controller and MESSAGE.search(rid) and (e.text or e.desc):
            message = e.text or e.desc
        elif gatekeeper and e.cls == "OCRText" and rid == OCR_MESSAGE_ID and (e.text or e.desc):
            message, trusted = e.text or e.desc, True
        elif not message and package is None and e.cls == "OCRText" and TITLE.match((e.text or "").strip()):
            message, trusted = e.text, True  # the server's OCR of a window nobody identified
    if not trusted:
        return None
    if not buttons or not ("deny" in buttons or "deny_forever" in buttons):
        return None
    kind = next((k for k, rx in KINDS if rx.search(message)), "other")
    m = APP.search(message)
    return Dialog(app=(m.group("app").strip() if m else ""), kind=kind, message=message, buttons=buttons)


DOMAIN = re.compile(r"\.(com|net|org|io|app|co|tv|gg)$", re.I)


def same_app(a: str, b: str | None, package: str | None = None) -> bool:
    """The dialog's app name is the task's app: its label, the same with a web-style domain dropped ("Chess.com"
    for "Chess"), or its package's last part (com.chess)."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())  # noqa: E731
    if not a:
        return False
    names = {norm(a), norm(DOMAIN.sub("", a.strip()))} - {""}
    known = {norm(b), norm(DOMAIN.sub("", (b or "").strip()))}
    if package:
        known.add(norm(package.rsplit(".", 1)[-1]))
    return bool(names & (known - {""}))


def decide(dialog: Dialog, *, mode: str, prompts: dict, task_app_label: str | None,
           task_package: str | None = None) -> Plan:
    """What to do by the owner's settings. Denying is always safe, so it never needs the owner."""
    never = mode == "never"
    # The task's own app only: before the task has an app, a request is another app's (it asks).
    own = bool(task_app_label or task_package) and same_app(dialog.app, task_app_label, task_package)
    accuracy = (prompts.get("location_accuracy") or "approximate") if dialog.kind == "location" else None
    if dialog.kind == "notifications":
        if prompts.get("notifications", "allow") != "allow":
            return Plan("deny")
        if own:
            return Plan("allow")
        return Plan("deny" if never else "ask")
    if dialog.kind == "photos" and not any(r in dialog.buttons for r in ("limited", "one_time")):
        return Plan("deny")  # only "Allow all" (every photo): never by rule
    if not any(r in dialog.buttons for r in ("one_time", "foreground", "allow", "limited")):
        return Plan("deny")  # the only grant offered is "Allow all the time": never
    setting = prompts.get(dialog.kind, "once") if own else "ask"  # another app's request (S2) always asks
    if setting == "deny":
        return Plan("deny")
    if setting == "once" and dialog.kind in PERSISTENT_ASK and "one_time" not in dialog.buttons \
            and prompts.get("persistent_grants", "ask") != "allow":
        return Plan("deny" if never else "ask", "once", accuracy)  # only a lasting grant offered: the owner decides
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
    elif dialog.kind == "photos":
        # Some photos, never "Allow all" (nor a plain "Allow"): limited access, else the setting's grant.
        order = ("limited", "foreground", "one_time") if grant == "while_using" and not asked else ("limited", "one_time")
    elif dialog.kind == "notifications":
        order = ("allow",)
    elif grant == "once" and not asked:
        order = ("one_time", "foreground", "allow")  # Only this time, else the narrowest grant offered
    elif grant == "while_using" and not asked:
        order = ("foreground", "allow", "one_time")
    else:
        order = ("one_time", "foreground", "allow")  # never "Allow all the time"
    return next((dialog.buttons[r] for r in order if r in dialog.buttons), None)


def accuracy_card(dialog: Dialog, accuracy: str | None) -> UIElement | None:
    """The Precise/Approximate card to tap first, or None (no cards, or already chosen)."""
    card = dialog.buttons.get(accuracy) if accuracy else None
    return None if card is None or card.checked is True else card


def question(dialog: Dialog) -> str:
    return f"Allow {dialog.app or 'this app'} to use your {WORDS.get(dialog.kind, 'data')}?"
