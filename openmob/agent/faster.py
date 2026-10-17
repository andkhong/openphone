"""Faster mode: what may go to TypeSafe as an image, and when (docs/design/faster-mode.md).

Faster mode is the phone owner's opt-in (signed on the phone with its screen lock, DeviceManager). While it is on, the
loop may send a small screenshot (jev_image_side, 768 px) of a screen *without usable text* (a canvas game, a web
canvas, an app that draws its own UI) to Jev, to tell a loading screen from a ready one (J3) and to name the screen's
state (J1). It never taps from an image (step 1).

`refusal` is the one check every image passes before any request, on the server. It answers why an image is NOT sent
(a short reason, recorded on the step as `jev_image_skip`), or None when it may be. The order is cheapest first; the
black-frame check runs on the prepared image (`black_frame`).
"""

from __future__ import annotations

import re

from ..phone.adb import UIElement
from . import permission_dialogs, sensitive
from .fast import OCR_CLASS

# Reasons an image isn't sent (the steps table's jev_image_skip). Only the first that applies is recorded.
OFF = "off"  # Faster mode is off for this phone (or this server disabled it)
DRIVE = "drive"  # a drive session whose key the owner didn't allow images for
NO_JEV = "no_jev"  # no TypeSafe key on this server
LABELED = "labeled"  # the screen has usable text: Jev text reads it, no image needed (not a refusal)
UNKNOWN_WINDOW = "unknown_window"  # no app identified in front
BLOCKED_APP = "blocked_app"  # an app this task may not touch
SENSITIVE_APP = "sensitive_app"  # banking, payments, wallets, password managers (config.sensitive_apps), always
SYSTEM_DIALOG = "system_dialog"  # a permission controller or other hidden system dialog
LOCKED = "locked"  # the lock screen is up
PASSWORD_FIELD = "password_field"  # a password field is on screen
SENSITIVE_NUMBER = "sensitive_number"  # a card number or SSN in the screen's text (agent/sensitive.py)
NO_TEXT_CHECK = "no_text_check"  # no OCR on this server, so the screen's words couldn't be checked first
BLACK_FRAME = "black_frame"  # the screenshot is black: a secure window or a blacked-out read
NO_FRAME = "no_frame"  # no screenshot could be taken
OTP_TEXT = "otp_text"  # a one-time code (4-8 digits near "code", "verification", "OTP", "passcode", "PIN")
NOTIFICATION = "notification"  # the notification shade, a heads-up or another System UI window is showing
APP_CHANGED = "app_changed"  # the app in front changed between the package check and the frame (review S L2)

SYSTEMUI = "com.android.systemui"
# Any run of 8 or more digits (single separators allowed), whatever Luhn says: a misread or partly read card or
# account number still never leaves (review S M1). \d also matches other scripts' digits.
_LONG_DIGITS = re.compile(r"\d(?:[ .\-_/]?\d){7,}")
# A one-time code: 4-8 digits within 60 characters of a code word (review S M2). Plain game scores ("Score 1200")
# don't count; "Your code is 482913" does.
_OTP_NUMBER = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
_OTP_WORDS = re.compile(r"(?i)(?<![a-z])(code|codes|verification|verify|otp|passcode|pin|one[- ]?time|2fa|"
                        r"authenticat\w*|security code|log ?in|sign[- ]?in|confirm)(?![a-z])|(?<![A-Za-z])G-\d")


def labeled(elements: list[UIElement]) -> bool:
    """Accessibility elements with text: the screen Jev text can read (Faster mode leaves it alone)."""
    return any(el.cls != OCR_CLASS and (el.text or el.desc) for el in elements)


def screen_text(elements: list[UIElement]) -> list[str]:
    return [s for el in elements for s in (el.text, el.desc, el.hint) if s]


def long_digits(text: str | None) -> bool:
    """A run of 8 or more digits (single separators allowed)."""
    return bool(text and _LONG_DIGITS.search(text))


def otp_text(text: str | None) -> bool:
    """A 4-8 digit number within 60 characters of a code word (a one-time code, a PIN)."""
    if not text:
        return False
    for m in _OTP_NUMBER.finditer(text):
        near = text[max(0, m.start() - 60):m.end() + 60]
        if _OTP_WORDS.search(near):
            return True
    return False


def system_ui(package: str | None, elements: list[UIElement]) -> bool:
    """The notification shade, a heads-up or another System UI window: the foreground is System UI, or the element
    list holds a System UI node (the server has no window list; what it can see is the package and the ids)."""
    if package == SYSTEMUI:
        return True
    return any((el.resource_id or "").startswith(SYSTEMUI + ":") for el in elements)


def refusal(package: str | None, elements: list[UIElement], *, blocked, sensitive_apps: list[str],
            locked: bool, ocr_ran: bool) -> str | None:
    """Why this screen's image may not go to TypeSafe, or None. `elements`: the screen's accessibility elements and
    its OCR words (OCRText); `blocked(package)`: this task's blocked apps; `ocr_ran`: the words were read from this
    very screenshot (without them a treeless screen can't be checked for sensitive numbers, so nothing is sent)."""
    if not package:
        return UNKNOWN_WINDOW
    if permission_dialogs.PACKAGES.search(package):
        return SYSTEM_DIALOG
    if blocked is not None and blocked(package):
        return BLOCKED_APP
    if any(re.search(p, package, re.IGNORECASE) for p in sensitive_apps):
        return SENSITIVE_APP
    if locked:
        return LOCKED
    if system_ui(package, elements):
        return NOTIFICATION
    if any(el.password for el in elements if el.cls != OCR_CLASS):
        return PASSWORD_FIELD
    # Hidden or covered windows the phone can't read: every labeled element covered by a higher window.
    real = [el for el in elements if el.cls != OCR_CLASS and (el.text or el.desc)]
    if real and all(el.covered for el in real):
        return SYSTEM_DIALOG
    texts = screen_text(elements)
    ssn_context = any(sensitive.ssn_label(t) for t in texts)
    for el in elements:
        card_field = sensitive.card_label(f"{el.text} {el.desc} {el.hint} {el.resource_id}") and el.editable
        for text in (el.text, el.desc, el.hint):
            if text and (sensitive.find(text, ssn_context=ssn_context, card_field=card_field) or long_digits(text)):
                return SENSITIVE_NUMBER
    # The words across elements, joined: a card number split over OCR boxes ("4111 1111" "1111 1111").
    joined = " ".join(texts)
    if sensitive.find(joined, ssn_context=ssn_context) or long_digits(joined):
        return SENSITIVE_NUMBER
    if any(otp_text(t) for t in texts) or otp_text(joined):
        return OTP_TEXT
    if not ocr_ran and not labeled(elements):
        return NO_TEXT_CHECK
    return None
