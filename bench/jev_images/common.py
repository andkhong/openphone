"""Shared pieces of the Jev image benchmark: the questions, image preparation, and today's text description."""

from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image

HERE = Path(__file__).parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from openmob.jev import describe_screen  # noqa: E402
from openmob.phone.adb import parse_ui_xml  # noqa: E402

LABELS = HERE / "labels.jsonl"
RESULTS = HERE / "results"

# J1: one choice over seven screen states. The same words go to Jev and to Haiku.
STATES = {
    "loading": "A loading, splash, or downloading screen: a spinner, progress bar, grey placeholder blocks, 'Loading', "
               "or a logo with nothing to press. Nothing can be done except wait.",
    "menu": "A game's or app's title or main menu: a few large choices such as Play, Settings, Quit, or 'Play vs computer'.",
    "content": "The app's or web page's normal view (for a game: the gameplay screen, the board) with nothing covering it. "
               "Settings lists, search results, articles, and account pages are content.",
    "game_over": "A game-over or results screen after a round: 'Game over', a final score, Retry.",
    "dialog": "A dialog, pop-up, or permission prompt covering the screen, with buttons such as Allow, Claim, Continue, or a close X.",
    "login": "A sign-in or login form asking for an email, username, or password.",
    "home_screen": "The phone's home screen, app drawer, or lock screen.",
}
STATE_Q = "Which kind of phone screen is shown in `screen`?"
LOADING_Q = ("Is `screen` still loading, so the agent should wait: a spinner, progress bar, grey skeleton placeholders, "
             "or a splash logo with nothing to press?")
SENSITIVE_Q = ("Does `screen` show personal financial or identity data: an account balance, a card number, bank transactions, "
               "pay amounts, or a social security or tax ID number? Prices of products for sale don't count.")
DIALOGS = {
    "system": "The real Android system permission dialog, shown by the Android permission controller over the app, "
              "asking to allow an app access (location, contacts, notifications, camera).",
    "app_drawn": "A permission-style prompt drawn by the app or web page itself: a lookalike of the Android permission "
                 "dialog, or the app's own 'turn on location?' prompt. Not the Android system's dialog.",
    "none": "No permission request is shown.",
}
DIALOG_Q = "Is a permission request shown in `screen`, and if so, is it the Android system's own dialog or one drawn by the app?"
LANDING_Q = "Does `screen` show search results for exactly the query in `query` (not a different query, no results, or only suggestions)?"


def landing_instructions(query: str) -> dict:
    return {"query": query, "question": LANDING_Q}


def box_q(desc: str) -> str:
    return f"{desc} in `screen`"


def visible_q(desc: str) -> str:
    return f"Is there {desc} in `screen`?"


def load_labels() -> list[dict]:
    return [json.loads(line) for line in LABELS.read_text().splitlines() if line.strip()]


@dataclass
class Prepared:
    jpeg: bytes
    width: int  # sent image size
    height: int
    scale: float  # sent px per device px
    off_x: int  # padding added on the left/top of the sent image
    off_y: int

    def to_device(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.off_x) / self.scale, (y - self.off_y) / self.scale


def prepare(png_path: Path, long_side: int, min_side: int = 280, quality: int = 80) -> Prepared:
    """Downscale so the long side is `long_side`; pad the short side up to `min_side` (Jev's limit)."""
    img = Image.open(png_path).convert("RGB")
    w, h = img.size
    scale = long_side / max(w, h)
    size = (round(w * scale), round(h * scale))
    img = img.resize(size, Image.LANCZOS)
    off_x = off_y = 0
    if size[0] < min_side or size[1] < min_side:
        pw, ph = max(size[0], min_side), max(size[1], min_side)
        off_x, off_y = (pw - size[0]) // 2, (ph - size[1]) // 2
        canvas = Image.new("RGB", (pw, ph), (0, 0, 0))
        canvas.paste(img, (off_x, off_y))
        img = canvas
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return Prepared(buf.getvalue(), img.width, img.height, scale, off_x, off_y)


def elements(images: Path, item: dict):
    xml = (images / item["xml"]).read_text() if item.get("xml") and (images / item["xml"]).exists() else ""
    return parse_ui_xml(xml)


@lru_cache(maxsize=None)
def _ocr(path: str):
    from openmob.vision import ocr
    data = Path(path).read_bytes()
    img = Image.open(io.BytesIO(data))
    return ocr.recognize_text(data, img.width, img.height)


def screen_text(images: Path, item: dict) -> tuple[dict, list, bool, list]:
    """Today's Jev text description (agent/loop.py _describe): element labels; OCR words only when no element
    has text (games). Returns (description, the elements, used_ocr, the OCR words as (text, bounds))."""
    els = elements(images, item)
    labels = [el.label for el in els]
    words: list[str] = []
    used_ocr = not any(el.text or el.desc for el in els)
    ocr_els = []
    if used_ocr:
        for b in _ocr(str(images / item["file"])):
            if b.confidence >= 0.3 and b.text.strip():
                words.append(b.text.strip())
                ocr_els.append((b.text.strip(), (b.x1, b.y1, b.x2, b.y2)))
    return describe_screen(item.get("package"), labels, words), els, used_ocr, ocr_els


def labeled_count(images: Path, item: dict) -> int:
    return sum(1 for el in elements(images, item) if el.text or el.desc)
