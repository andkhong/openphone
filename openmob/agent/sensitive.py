"""Sensitive numbers: payment card numbers and US Social Security numbers (docs/safety.md, "Sensitive numbers").

The same detector runs on the phone (android/.../gate/SensitiveNumbers.kt) and in the Python reference engine
(tests/gatekeeper_ref.py); the shared fixture tests/fixtures/gatekeeper/sensitive_numbers.json pins all three to the
same answers, and the label patterns below must equal rules.json "sensitive_numbers" (a test checks).

- A **card number**: 13-19 digits, optionally grouped by spaces (one or more), dashes, pluses, dots, slashes or
  underscores (a grouped number starts with a 4-digit group), whose first digit is 2-6 (the card networks' range) and
  that passes the Luhn check. A run preceded by "+" (an international phone number) or by "<digit>." (a decimal
  fraction, such as a map coordinate) isn't one. Any Unicode decimal digit counts as its ASCII digit.
- A **Social Security number**: ddd-dd-dddd, ddd dd dddd or ddd.dd.dddd with a valid area (not 000, 666 or 9xx),
  group (not 00) and serial (not 0000). Where the field or the screen says SSN or Social Security, any of those or 9
  bare digits count, test values such as 000-12-3456 included.
- **In a link**, also after percent-decoding until it stops changing (at most 10 rounds), and a card number split over
  consecutive numeric query parameters (`?a=4111111&b=111111111`: each piece 3-12 digits, no decimals).

This guards against accidental leaks (a model copying a number it saw into a field or a link), not against a
determined adversary: base64, words, other encodings or a number spread over several requests aren't caught
(review L1, 2026-10-17).
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
_RUN = re.compile(r"(?<![0-9+])(?<![0-9]\.)[0-9](?:(?: +|[+\-./_])?[0-9])*")
_SEP = re.compile(r" +|[+\-./_]")
_SSN = re.compile(r"(?<![0-9\-.])([0-9]{3})([ \-.]?)([0-9]{2})\2([0-9]{4})(?![0-9]|[\-./][0-9])")
_QUERY_VALUE = re.compile(r"[?&;]([^=&;#]*)=([^&;#]*)")
_NUMERIC = re.compile(r"^[0-9][0-9 +\-_]*$")  # no dots: a decimal (a coordinate) is not a piece
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


def ascii_digits(text: str) -> str:
    """Every Unicode decimal digit (Arabic-Indic, Devanagari, …) as its ASCII digit; one character for one."""
    if text.isascii():
        return text
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in text)


def _norm(text: str) -> str:
    return ascii_digits(unicodedata.normalize("NFKC", text or ""))


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
        if labeled or (sep and _valid_ssn(area, group, serial)):
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
    """A URL percent-decoded until it stops changing (at most 10 rounds: %2534 is still a digit), NFKC-normalized."""
    text = url or ""
    for _ in range(10):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return _norm(text)


def split_card(text: str) -> bool:
    """A card number split over consecutive numeric query parameters (`?a=4111111&b=111111111`): two or more values in
    a row, each only digits (spaces, dashes, underscores or pluses between) with 3-12 digits (a decimal or a 13-digit
    timestamp is no piece), whose digits together are a card number."""
    pieces = []
    for _, value in _QUERY_VALUE.findall(text):
        v = value.strip()
        digits = "".join(ch for ch in v if "0" <= ch <= "9")
        pieces.append(digits if _NUMERIC.match(v) and 3 <= len(digits) <= 12 else None)
    for i in range(len(pieces)):
        joined = pieces[i]
        if joined is None:
            continue
        for j in range(i + 1, len(pieces)):
            if pieces[j] is None:
                break
            joined += pieces[j]
            if len(joined) > 19:
                break
            if len(joined) >= 13 and joined[0] in "23456" and luhn(joined):
                return True
    return False


def in_url(url: str | None) -> str | None:
    """CARD or SSN when a link carries a sensitive number anywhere (host, path, query or fragment, decoded), or a card
    number split over its query parameters."""
    text = decode_url(url)
    found = find(text, ssn_context=ssn_label(text))
    if found is None and split_card(text):
        return CARD
    return found


def describe(kind: str | None) -> str:
    return {CARD: "a card number", SSN: "a Social Security number"}.get(kind or "", "a sensitive number")


def redact(text: str | None) -> str:
    """`text` with every card number and SSN-shaped number replaced by REDACTED (outward copies: events, logs)."""
    if not text:
        return ""
    digits = ascii_digits(text)  # the same length: spans found in it are spans of `text`
    if not _DIGIT.search(digits):
        return text
    spans = card_spans(digits) + ssn_spans(digits, labeled=True)
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
