"""What an action is, for the permission modes (see openmob/permissions.py): pure helpers the loop's
_guard and step gate use. Nothing here decides alone whether to ask; it only classifies.

- Words: the risky words a target names, minus phrases that are steps on the way ("Add to cart").
- Money: a money word; "terminal" words finish a purchase, "start" words begin a new one.
- Settings tiers: refuse | ask | allow (everyday) | None (not a Settings screen, or just navigation).
- Credentials: autofill and saved-password suggestions.
"""

from __future__ import annotations

import re

from ..config import Config
from ..permissions import (APP_INFO_ALLOWED, APP_INFO_ASK, APP_INFO_MARKERS, BROWSER_PACKAGE, KIND_WORDS,
                           SETTINGS_CHANGE_WORDS, SETTINGS_PACKAGE)
from ..phone.adb import UIElement

TITLE_BAND = 0.16  # the top of the screen, where a Settings page's title sits
LAUNCHER = re.compile(r"launcher|\.home$|trebuchet|lawnchair|nexuslauncher", re.IGNORECASE)
TAP_SLOP = 0.04  # a swipe shorter than this fraction of the screen's diagonal is really a tap


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(phrase.lower())}(?![\w-])", text) is not None


def words(text: str, vocabulary: list[str]) -> list[str]:
    low = text.lower()
    return [w for w in vocabulary if _has(low, w)]


def strip_skips(text: str, skips: list[str]) -> str:
    """The target without the on-the-way phrases ("Add to cart", "Proceed to checkout")."""
    low = text.lower()
    for phrase in sorted(skips, key=len, reverse=True):
        low = re.sub(rf"(?<![\w-]){re.escape(phrase.lower())}(?![\w-])", " ", low)
    return low


def kind_of(hits: list[str], money: list[str], credential: bool) -> str | None:
    """send | delete | post | pay | fill, or None (risky by some other word, or the classifier)."""
    if money:
        return "pay"
    if credential:
        return "fill"
    for kind, vocab in KIND_WORDS.items():
        if any(h in vocab or any(_has(h, v) for v in vocab) for h in hits):
            return kind
    return None


def is_share(hits: list[str]) -> bool:
    return any(h in ("share to", "forward to") for h in hits)


def is_browser(package: str | None, elements: list[UIElement]) -> bool:
    return bool(package and BROWSER_PACKAGE.search(package)) or any("WebView" in (e.cls or "") for e in elements)


def is_settings(package: str | None) -> bool:
    return bool(package and SETTINGS_PACKAGE.search(package))


def page_title(elements: list[UIElement]) -> str:
    """Text near the top of the screen: where a Settings page says what it is."""
    if not elements:
        return ""
    height = max(e.bounds[3] for e in elements) or 1
    top = [e for e in elements if e.bounds[1] < height * TITLE_BAND and (e.text or e.desc) and not e.editable]
    return " ".join(f"{e.text} {e.desc}" for e in top).lower()


def settings_tier(cfg: Config, package: str | None, target: str, element: UIElement | None,
                  elements: list[UIElement], blocked_label=None) -> str | None:
    """For a tap in Settings: "refuse", "ask", "allow" (everyday: never asks) or None (not Settings,
    or a plain tap that only opens a page). `blocked_label(text)`: names an app this task may not touch."""
    if not is_settings(package):
        return None
    target_low = target.lower()
    title = page_title(elements)
    if words(target_low, cfg.settings_refuse_terms) or words(title, cfg.settings_refuse_terms):
        return "refuse"
    screen = " ".join(f"{e.text} {e.desc}" for e in elements).lower()
    if any(m in title for m in APP_INFO_MARKERS) or ("force stop" in screen and "uninstall" in screen):
        if blocked_label and any(blocked_label(e.text) for e in elements if e.text):
            return "refuse"  # anything on a blocked app's info page
        if words(target_low, list(APP_INFO_ALLOWED)):
            return "allow"
        if words(target_low, list(APP_INFO_ASK)):
            return "ask"
        return None
    if words(target_low, cfg.settings_ask_terms):
        return "ask"
    if words(target_low, cfg.settings_everyday_terms) or words(title, cfg.settings_everyday_terms):
        return "allow"
    changing = (element is not None and element.checked is not None) or bool(SETTINGS_CHANGE_WORDS.search(target_low))
    return "ask" if changing else None


def is_credential(cfg: Config, target: str) -> bool:
    return bool(words(target, cfg.credential_terms))


def money_context(elements: list[UIElement], limit: int = 2) -> str:
    """Prices on screen, for the prompt: "Total $12.99"."""
    found = []
    for e in elements:
        text = (e.text or e.desc or "").strip()
        if re.search(r"([$€£¥₹]\s?\d|\d[.,]\d{2}\s?(usd|eur|gbp|€|\$)?\b)", text, re.IGNORECASE) and len(text) <= 60:
            if text not in found:
                found.append(text)
        if len(found) >= limit:
            break
    return ", ".join(found)


def short_swipe(args: dict, width: int, height: int) -> bool:
    try:
        dx, dy = float(args["x2"]) - float(args["x1"]), float(args["y2"]) - float(args["y1"])
    except (KeyError, TypeError, ValueError):
        return False
    return (dx * dx + dy * dy) ** 0.5 < TAP_SLOP * (width * width + height * height) ** 0.5


def is_launcher(package: str | None) -> bool:
    return bool(package and LAUNCHER.search(package))


def grant_text(kind: str, app: str, days: float, target: str | None = None) -> str:
    """What an "Always allow" grant lets agents do, in the prompt's words."""
    span = f"{days:g} days" if days >= 2 else ("24 hours" if days == 1 else f"{days * 24:g} hours")
    if kind == "send" and target:
        return f"Agents could share anything from {app} to {target} as you for {span}."
    return {
        "send": f"Agents could send any {app} message as you for {span}.",
        "post": f"Agents could post anything on {app} as you for {span}.",
        "delete": f"Agents could delete anything in {app} for {span}.",
        "pay": f"Agents could pay for anything in {app} without asking for {span}.",
        "fill": f"Agents could fill your saved passwords and details into {app} for {span}.",
    }[kind]
