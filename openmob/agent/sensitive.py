"""Sensitive numbers: payment card numbers and US Social Security numbers (docs/safety.md, "Sensitive numbers").

The same detector runs on the phone (android/.../gate/SensitiveNumbers.kt) and in the Python reference engine
(tests/gatekeeper_ref.py); the shared fixture tests/fixtures/gatekeeper/sensitive_numbers.json pins all three to the
same answers, and the label patterns below must equal rules.json "sensitive_numbers" (a test checks).

- A **card number**: 13-19 digits, optionally grouped by single spaces, dashes or pluses (a grouped number starts
  with a 4-digit group), whose first digit is 2-6 (the card networks' range) and that passes the Luhn check. A run
  preceded by "+" (an international phone number) or by "<digit>." (a decimal fraction, such as a map coordinate)
  isn't one.
- A **Social Security number**: ddd-dd-dddd with a valid area (not 000, 666 or 9xx), group (not 00) and serial (not
  0000). Where the field or the screen says SSN or Social Security, any ddd-dd-dddd, ddd dd dddd or 9 bare digits
  count, test values such as 000-12-3456 included.
- **Card details**: in a field labeled as a card field (card number, CVV, expiry...), any text with 3 or more digits.

Typing one asks the owner (never covered by a task approval or "everyday"), and is refused under Don't ask and in
drive sessions; a link carrying one is refused outright. Outward copies of text (events, the feed, logs) have them
replaced by REDACTED."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

REDACTED = "<redacted>"

# Must equal rules.json "sensitive_numbers.ssn_label" and "payment_field" (tests/test_sensitive_numbers.py checks).
SSN_LABEL = r"(?i)(^|[^a-z])ssn([^a-z]|$)|social.?security"
CARD_LABEL = r"(?i)card.?number|cc.?num|credit.?card|cvv|cvc|security.?code|expir|iban|card.?holder|cc.?exp|cc.?csc"

_SSN_LABEL = re.compile(SSN_LABEL)
_CARD_LABEL = re.compile(CARD_LABEL)
_RUN = re.compile(r"(?<![0-9+])(?<![0-9]\.)[0-9](?:[ +\-]?[0-9])*")
_SEP = re.compile(r"[ +\-]")
_SSN = re.compile(r"(?<![0-9\-])([0-9]{3})([ \-]?)([0-9]{2})\2([0-9]{4})(?![0-9])")
_DIGIT = re.compile(r"[0-9]")

CARD = "card"
SSN = "ssn"


def luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def card_spans(text: str) -> list[tuple[int, int]]:
    """Where card numbers are in `text` (start, end)."""
    out = []
    for m in _RUN.finditer(text):
        run = m.group()
        groups = _SEP.split(run)
        total, end = 0, 0
        for k, g in enumerate(groups):
            total += len(g)
            end += len(g) + (1 if k else 0)
            if total > 19 or (k > 0 and len(groups[0]) != 4):
                break
            if total < 13:
                continue
            digits = "".join(groups[:k + 1])
            if digits[0] in "23456" and luhn(digits):
                out.append((m.start(), m.start() + end))
                break
    return out


def _valid_ssn(area: str, group: str, serial: str) -> bool:
    return area not in ("000", "666") and area[0] != "9" and group != "00" and serial != "0000"


def ssn_spans(text: str, labeled: bool) -> list[tuple[int, int]]:
    """Where SSNs are in `text`. Unlabeled: only ddd-dd-dddd with a valid area, group and serial."""
    out = []
    for m in _SSN.finditer(text):
        area, sep, group, serial = m.groups()
        if labeled or (sep == "-" and _valid_ssn(area, group, serial)):
            out.append(m.span())
    return out


def ssn_label(text: str | None) -> bool:
    return bool(_SSN_LABEL.search(text or ""))


def card_label(text: str | None) -> bool:
    return bool(_CARD_LABEL.search(text or ""))


def find(text: str | None, ssn_context: bool = False, card_field: bool = False) -> str | None:
    """CARD or SSN when `text` holds a sensitive number, else None. `ssn_context`: the field or screen says SSN or
    Social Security. `card_field`: the field is a card field (card number, CVV, expiry), where any 3 digits count."""
    text = _norm(text or "")
    if card_spans(text):
        return CARD
    if ssn_spans(text, ssn_context):
        return SSN
    if card_field and len(_DIGIT.findall(text)) >= 3:
        return CARD
    return None


def decode_url(url: str | None) -> str:
    """A URL percent-decoded until it stops changing (at most 3 rounds: %2534 is still a digit), NFKC-normalized."""
    text = url or ""
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return _norm(text)


def in_url(url: str | None) -> str | None:
    """CARD or SSN when a link carries a sensitive number anywhere (host, path, query or fragment, decoded)."""
    text = decode_url(url)
    return find(text, ssn_context=ssn_label(text))


def describe(kind: str | None) -> str:
    return {CARD: "a card number", SSN: "a Social Security number"}.get(kind or "", "a sensitive number")


def redact(text: str | None) -> str:
    """`text` with every card number and SSN-shaped number replaced by REDACTED (outward copies: events, logs)."""
    if not text or not _DIGIT.search(text):
        return text or ""
    spans = card_spans(text) + ssn_spans(text, labeled=True)
    if not spans:
        return text
    out, last = [], 0
    for start, end in sorted(spans):
        if start < last:
            continue
        out.append(text[last:start])
        out.append(REDACTED)
        last = end
    out.append(text[last:])
    return "".join(out)
