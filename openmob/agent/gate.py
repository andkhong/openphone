"""What an action is, for the permission modes (see openmob/permissions.py): pure helpers the loop's
_guard and step gate use. Nothing here decides alone whether to ask; it only classifies.

- Words: the risky words a target names, minus phrases that are steps on the way ("Add to cart").
- Money: a money word; "terminal" words finish a purchase, "start" words begin a new one.
- Settings tiers: refuse | ask | allow (everyday) | None (not a Settings screen, or just navigation).
- Credentials: autofill and saved-password suggestions.
"""

from __future__ import annotations

import re
from typing import NamedTuple

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


# --- what an action really touches (docs/safety.md#what-is-guarded) ----------------------------------

TOUCH_SLOP_PX = 24  # Android's touch slop is 8 dp (about 24 px on a typical phone)
TAP_MOVE_PX = 2 * TOUCH_SLOP_PX  # a swipe that moves less than this is a tap (the gatekeeper's 2x slop)
TAP_MS = 150  # a swipe quicker than this is a tap, however far it moves
SCROLLERS = re.compile(r"RecyclerView|ScrollView|ListView|GridView|ViewPager|WebView", re.IGNORECASE)
SLIDER_CLASS = re.compile(r"SeekBar|Slider|RangeBar", re.IGNORECASE)
SLIDE_WORDS = ["slide to", "swipe to"]
# Apps where a tap on something without any label asks: shopping, payment, food, travel and rides.
MONEY_APP = re.compile(
    r"shop|store|amazon|ebay|etsy|walmart|target|aliexpress|temu|shein|bestbuy|ikea|zalando|flipkart|"
    r"doordash|ubereats|uber|lyft|grubhub|deliveroo|instacart|postmates|swiggy|zomato|bolt|grab|"
    r"booking|expedia|airbnb|hotels|trivago|kayak|skyscanner|ryanair|airline|"
    r"pay|wallet|bank|cash|venmo|zelle|revolut|coinbase|binance|checkout|billing", re.IGNORECASE)
PAYMENT_SCREEN_WORDS = ["checkout", "payment", "pay now", "place order", "place your order", "card number", "cvv",
                        "cvc", "billing address", "order total", "total due", "confirm purchase"]
# Fields where ENTER only searches: search boxes.
SEARCH_HINT = re.compile(r"search|query|SearchView", re.IGNORECASE)
URL_BAR = re.compile(r"url_bar|url_field|urlbar|omnibox|location_bar|address_bar|mozac_browser_toolbar_edit|"
                     r"search_or_type_url|url_edit", re.IGNORECASE)
# open_url: these schemes only (the gatekeeper's allowlist). Everything else is refused.
SAFE_SCHEMES = {"http", "https", "mailto", "tel", "geo", "sms", "smsto", "market"}
DANGEROUS_SCHEMES = ("javascript", "vbscript", "intent", "file", "content", "data", "android-app", "jar")


PRICE_ONLY_MONEY = ("subscribe", "join")  # money only with a price nearby (YouTube's Subscribe is free)

# Installing from the Play Store (owner, 2026-10-09): a free install runs unasked only when the request names the app
# on the store page; any other install asks, and is refused under Don't ask. A screen, an ad or a guessed package id
# can't pick what gets installed.
INSTALL_WORDS = re.compile(r"^\s*(install|get|instalar|installieren|installer|installa)\b", re.I)
_TITLE_SKIP = re.compile(r"^(install|open|uninstall|update|cancel|more|search|get|share|wishlist|play|buy|"
                         r"contains ads|in-app purchases|\d[\d.,]*\s*[kmb+]*.*|[\d.]+\s*★?.*)$", re.I)
_GENERIC = {"app", "apps", "the", "and", "for", "with", "free", "game", "games", "play", "learn", "online",
            "mobile", "pro", "lite", "com", "net", "org", "official", "new", "best", "android"}


def is_install(text: str) -> bool:
    """A Play Store install button's words ("Install", "Get")."""
    return bool(INSTALL_WORDS.search(text or ""))


def store_title(elements: list, button=None) -> str:
    """The app's name on a Play Store page: the topmost text above the install button that isn't a button,
    rating, count or badge. Empty when it can't be told."""
    top = button.bounds[1] if button is not None else None
    texts = [e for e in elements if e.text and len(e.text.strip()) >= 2 and not _TITLE_SKIP.match(e.text.strip())
             and (top is None or e.bounds[3] <= top)]
    return min(texts, key=lambda e: (e.bounds[1], e.bounds[0])).text.strip() if texts else ""


def names_app(request: str, title: str) -> bool:
    """The request names the store page's app: one of its title's distinctive words is a word of the request."""
    words = {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) >= 3 and w not in _GENERIC}
    asked = set(re.findall(r"[a-z0-9]+", (request or "").lower()))
    return bool(words & asked)
SELF_NAMES = ["note to self", "notes to self", "message yourself", "saved messages"]
LINK = re.compile(r"(?i)https?://|www\.|youtu\.be/|\b[a-z0-9-]+\.(?:com|be|org|net|io|tv|app|co)/")


def is_self_label(text: str, owner: list[str] | None = None) -> bool:
    """An app's self chat or the owner's own name (with an optional "(You)"). Nothing else ending in "you"."""
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    if not toks:
        return False
    if any(toks == re.findall(r"[a-z0-9]+", s) for s in SELF_NAMES):
        return True
    names = [re.findall(r"[a-z0-9]+", n.lower()) for n in owner or [] if n]
    return any(n and (toks == n or toks == n + ["you"]) for n in names)


def to_self(target: str, obs, owner: list[str] | None = None) -> bool:
    """Only a public-link share: the target itself is you (a share target) while a link is on screen. A chat's
    title never counts (anyone can name a group "Note to self")."""
    elements = getattr(obs, "elements", None) or []
    return is_self_label(target, owner) and any(LINK.search(f"{e.text} {e.desc}") for e in elements)


VOLATILE = re.compile(r"\d+(?:[.,:]\d+)*")


def volatile(text: str | None) -> str:
    """A label with its volatile numbers blanked (odds, prices, counts, times): the same element across reads of a
    live screen. The phone's gatekeeper blanks numbers the same way for its target fingerprints."""
    return VOLATILE.sub("#", text or "")


def readable_id(resource_id: str) -> str:
    """A resource id as words: "buy_now_button" -> "buy now button", "sendButton" -> "send Button"."""
    rid = (resource_id or "").rsplit("/", 1)[-1]
    rid = re.sub(r"([a-z])([A-Z])", r"\1 \2", rid)
    return re.sub(r"[_\-.:]+", " ", rid).strip()


def element_words(el: UIElement | None) -> str:
    """Everything an element says about itself: its text, description and resource id."""
    if el is None:
        return ""
    return " ".join(p for p in (el.text, el.desc, readable_id(el.resource_id)) if p).strip()


def labeled(el: UIElement | None) -> bool:
    return bool(el and (el.text or el.desc or el.resource_id))


def _area(el: UIElement) -> int:
    x1, y1, x2, y2 = el.bounds
    return max(0, x2 - x1) * max(0, y2 - y1)


def _contains(el: UIElement, x: float, y: float, margin: int = 0) -> bool:
    x1, y1, x2, y2 = el.bounds
    return x1 - margin <= x < x2 + margin and y1 - margin <= y < y2 + margin


def within(inner: UIElement, outer: UIElement) -> bool:
    a, b = inner.bounds, outer.bounds
    return a[0] >= b[0] and a[1] >= b[1] and a[2] <= b[2] and a[3] <= b[3]


class Hit(NamedTuple):
    element: UIElement | None  # the element the touch presses
    words: str  # everything it (and the label at the point) says: text, description, resource id
    labeled: bool  # something there says what it is
    shown: str  # how a prompt names it: its text or description, else its resource id


def hit(elements: list[UIElement], x: float, y: float) -> Hit:
    """What a touch at device point (x, y) lands on.

    The pressed element is the smallest clickable one there (else the smallest of any). The words are
    its own plus those of the smallest labeled element at the point (a button's text often sits in a
    child; a larger labeled parent adds its words too, which can only add caution). "Labeled": the
    pressed element, or something inside it at the point, has a text, description or id. Elements
    under a higher window (a dialog, the keyboard) don't count: the touch lands on that window."""
    under = sorted((e for e in elements if not e.covered and _contains(e, x, y)), key=_area)
    if not under:
        return Hit(None, "", False, "")
    pressed = next((e for e in under if e.clickable), under[0])
    named = next((e for e in under if labeled(e)), None)
    parts: list[str] = []
    for e in (named, pressed):
        w = element_words(e)
        if w and w not in parts:
            parts.append(w)
    is_labeled = labeled(pressed) or (named is not None and _area(named) <= _area(pressed))
    shown = next((e.text or e.desc for e in (named, pressed) if e is not None and (e.text or e.desc)), "")
    return Hit(pressed, " ".join(parts), is_labeled, shown or readable_id((named or pressed).resource_id))


def swipe_is_tap(elements: list[UIElement], x1: float, y1: float, x2: float, y2: float, duration_ms: float) -> bool:
    """A swipe Android would deliver as a tap (or a long press) at its start: it barely moves, it is too
    quick to be a drag, or it starts and ends on the same clickable (non-scrolling) control."""
    if ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 <= TAP_MOVE_PX or duration_ms < TAP_MS:
        return True
    start = hit(elements, x1, y1).element
    return bool(start is not None and start.clickable and not SCROLLERS.search(start.cls or "")
                and _contains(start, x2, y2, margin=TOUCH_SLOP_PX))


def is_slider(el: UIElement | None, text: str) -> bool:
    return bool(el is not None and SLIDER_CLASS.search(el.cls or "")) or bool(words(text, SLIDE_WORDS))


def is_money_app(package: str | None) -> bool:
    return bool(package and MONEY_APP.search(package))


def payment_context(elements: list[UIElement]) -> bool:
    """A checkout or payment screen: a price, or payment words on screen (card fields, "Place order")."""
    if money_context(elements, limit=1):
        return True
    screen = " ".join(f"{e.text} {e.desc}" for e in elements).lower()
    return bool(words(screen, PAYMENT_SCREEN_WORDS))


def money_screen(cfg: Config, elements: list[UIElement]) -> bool:
    """Money words anywhere on screen (minus on-the-way phrases), or a payment screen."""
    screen = strip_skips(" ".join(f"{e.text} {e.desc}" for e in elements), cfg.confirm_skip_phrases)
    return bool(words(screen, cfg.money_keywords)) or payment_context(elements)


def focused_field(elements: list[UIElement]) -> UIElement | None:
    """The text field typing and ENTER go to: the focused one (newer apps say), else the only one."""
    fields = [e for e in elements if e.editable or e.password or "EditText" in (e.cls or "")]
    focused = [e for e in fields if e.focused]
    if focused:
        return focused[0]
    return fields[0] if len(fields) == 1 else None


def is_search_field(el: UIElement | None) -> bool:
    return bool(el is not None and any(SEARCH_HINT.search(v or "")
                                       for v in (el.resource_id, el.cls, el.desc, el.hint)))


OTP_FIELD = re.compile(r"\b(otp|one[\s_-]?time|verification|verify|2fa|two[\s_-]?factor|auth(entication)?[\s_-]?code|"
                       r"security[\s_-]?code|sms[\s_-]?code|login[\s_-]?code|confirmation[\s_-]?code|passcode|"
                       r"pin[\s_-]?code|totp|mfa)\b", re.IGNORECASE)


def is_otp_field(el: UIElement | None) -> bool:
    """A text field that looks like it takes a one-time or verification code (by its own hint, resource id or
    description). The drive level never types into one (docs/design/mcp-drive.md §4); the loop may."""
    if el is None:
        return False
    return any(OTP_FIELD.search(v or "") for v in (el.hint, el.resource_id, el.desc))


def is_url_bar(el: UIElement | None, package: str | None) -> bool:
    """A browser's address bar: its id says so, or it is a browser's field holding a URL."""
    if el is None or not (el.editable or "EditText" in (el.cls or "")):
        return False
    return bool(URL_BAR.search(el.resource_id or "")) or bool(
        BROWSER_PACKAGE.search(package or "") and looks_like_url(el.text))


def _clean_url(url: str) -> str:
    # Browsers drop tabs and newlines anywhere in a URL, and leading spaces and control characters,
    # so "  java\tscript:" is still javascript:.
    url = re.sub(r"[\t\n\r]", "", url or "")
    return re.sub("^[\\x00-\\x20\\u00a0\\ufeff]+", "", url)


def url_scheme(url: str) -> str | None:
    m = re.match(r"([A-Za-z][A-Za-z0-9+.\-]*):", _clean_url(url))
    return m.group(1).lower() if m else None


def looks_like_url(text: str | None) -> bool:
    text = _clean_url(text or "").strip()
    if not text or " " in text:
        return False
    return url_scheme(text) is not None or bool(re.match(r"(www\.)?[\w-]+(\.[\w-]+)+(/|$|:\d)", text))


def dangerous_text(text: str | None) -> str | None:
    """The scheme, when text starts a dangerous URL (javascript:, intent:, file:, content:, data:)."""
    scheme = url_scheme(text or "")
    return scheme if scheme in DANGEROUS_SCHEMES else None


def web_url(url: str) -> str:
    """A bare domain ("example.com/page") as an https link; anything else unchanged."""
    url = (url or "").strip()
    return f"https://{url}" if url_scheme(url) is None and looks_like_url(url) else url


def url_refusal(url: str) -> str | None:
    """Why open_url may not open this, or None. Only web links and a few safe schemes are allowed."""
    scheme = url_scheme(url)
    if scheme is None:
        return "a link must start with https:// (or http://)."
    if scheme in DANGEROUS_SCHEMES:
        return f"{scheme}: links can run code or reach into apps, so they're never opened."
    if scheme not in SAFE_SCHEMES:
        return (f"{scheme}: links go straight into apps, so only web links (https://) and tel, mailto, sms, "
                "geo and market links are opened.")
    return None
