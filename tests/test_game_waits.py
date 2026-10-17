"""Game waits (2026-10-17, measured on the owner's Samsung): a wait_until screen_changes on Tetris's ready, still menu
held for its whole 15-20 s timeout, and a Chess.com bot's reply went unseen because the board's pieces aren't in the
element list. Here:

- screen_changes (like the text conditions) ends early, not met, on a screen that has been still with nothing loading
  (waiting.SCREEN_STILL_EXIT_S), on the server's poll path and through the phone's wait_for (still_ms);
- on a screen with a large unlabeled area (waiting.canvas_region), screen_changes also compares frames of that area,
  sensitive enough for one chess move but not a clock, a cursor or a pulse (synthetic frames drawn with PIL);
- Faster mode's trigger includes sparse screens (a few labels around a play area), never text-heavy ones, and J3 may
  end the model's screen_changes wait on such a screen."""

import io
import threading
import time
from pathlib import Path

import pytest
from conftest import FakeDriver, ScriptedClient, tool_use
from PIL import Image, ImageDraw, ImageFont
from test_faster_mode import FakeImages, StepIO

from openmob.agent import faster, waiting
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.phone.adb import Screenshot, UIElement, parse_ui_xml
from openmob.phone.apps import AppResolver
from openmob.vision.ocr import TextBox

DEV = (1080, 2340)  # a 20:9 phone, as the owner's Samsung
IMG = (540, 1170)
BOARD = (0, 600, 1080, 1680)  # device px: a full-width board, ~46% of the screen
CHESS = "com.chess"
TETRIS = "com.n3twork.tetris"


def el(text="", bounds=(0, 0, 100, 100), desc="", cls="TextView", rid="", editable=False, password=False,
       focused=False, i=0, hint=""):
    return UIElement(i, text, desc, rid, cls, True, None, bounds, editable=editable, password=password,
                     focused=focused, hint=hint)


# --- synthetic frames --------------------------------------------------------------------------------------------

START = [(c, 6, True) for c in range(8)] + [(c, 1, False) for c in range(8)]  # pawns: (file 0-7, row 0-7 from top)


def move(pieces, frm, to):
    out = [p for p in pieces if p[:2] != frm]
    white = next(p[2] for p in pieces if p[:2] == frm)
    return [p for p in out if p[:2] != to] + [(to[0], to[1], white)]


def board_shot(pieces=START, clock="9:58", flicker=0, pulse=0, board=BOARD, menu=False):
    """A Chess.com-like board (light 238,238,210; dark 118,150,86; pawns with a dark outline) as a JPEG screenshot.
    `flicker`: a dot of that many device px; `pulse`: one square that many levels brighter; `menu`: a game menu
    (big buttons) instead of the board."""
    img = Image.new("RGB", IMG, (49, 46, 43))
    d = ImageDraw.Draw(img)
    fx, fy = IMG[0] / DEV[0], IMG[1] / DEV[1]
    if menu:
        for i, colour in enumerate(((70, 180, 60), (200, 120, 40), (60, 120, 200))):
            d.rounded_rectangle([60, 400 + 140 * i, 480, 500 + 140 * i], 20, fill=colour)
            d.text((200, 430 + 140 * i), ("LEVEL 2", "MARATHON", "SETTINGS")[i], fill="white",
                   font=ImageFont.load_default(size=24))
    else:
        bx1, by1, bx2, by2 = board[0] * fx, board[1] * fy, board[2] * fx, board[3] * fy
        sq = (bx2 - bx1) / 8
        for r in range(8):
            for c in range(8):
                colour = (238, 238, 210) if (r + c) % 2 == 0 else (118, 150, 86)
                if pulse and (r, c) == (3, 3):
                    colour = tuple(min(255, v + pulse) for v in colour)
                d.rectangle([bx1 + c * sq, by1 + r * sq, bx1 + (c + 1) * sq - 1, by1 + (r + 1) * sq - 1], fill=colour)
        for c, r, white in pieces:
            x, y = bx1 + c * sq, by1 + r * sq
            fill = (250, 250, 250) if white else (40, 40, 40)
            d.ellipse([x + sq * 0.36, y + sq * 0.18, x + sq * 0.64, y + sq * 0.46], fill=fill, outline=(70, 70, 70),
                      width=2)
            d.polygon([(x + sq * 0.3, y + sq * 0.82), (x + sq * 0.7, y + sq * 0.82), (x + sq * 0.58, y + sq * 0.42),
                       (x + sq * 0.42, y + sq * 0.42)], fill=fill, outline=(70, 70, 70), width=2)
        d.text((bx2 - 60, by1 + 4), clock, fill=(0, 0, 0), font=ImageFont.load_default(size=14))
        if flicker:
            x, y, s = bx1 + 3 * sq + 5, by1 + 5 * sq + 5, flicker * fx
            d.rectangle([x, y, x + s - 1, y + s - 1], fill=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return Screenshot(buf.getvalue(), *IMG, *DEV)


def moved_cells(a: Screenshot, b: Screenshot, region=BOARD) -> int:
    return waiting.region_moved_cells(waiting.region_frame(a, region), waiting.region_frame(b, region),
                                      waiting.REGION_CHANGED_CELL)


def changed(a, b, region=BOARD) -> bool:
    return waiting.region_changed(waiting.region_frame(a, region), waiting.region_frame(b, region))


def test_one_chess_move_counts_as_a_change():
    # The hardest case first: a white pawn from a light square to a light square (e2-e4), no move highlight.
    e4 = move(START, (4, 6), (4, 4))
    assert changed(board_shot(START), board_shot(e4))
    assert moved_cells(board_shot(START), board_shot(e4)) >= 3 * waiting.REGION_CHANGED_CELLS  # with room to spare
    # The bot's reply (a black pawn, dark square to dark square), measured on a play area larger than the board.
    e5 = move(e4, (4, 1), (4, 3))
    assert changed(board_shot(e4), board_shot(e5))
    assert changed(board_shot(e4), board_shot(e5), region=(0, 0, *DEV))
    # A half-width board still counts, with less room.
    half = (270, 900, 810, 1440)
    assert changed(board_shot(START, board=half), board_shot(move(START, (3, 6), (3, 4)), board=half))


def test_clock_digits_a_flicker_and_a_pulse_do_not():
    cell = DEV[0] / waiting.REGION_CELLS  # one cell of the play-area frame, in device px
    still = board_shot(START)
    assert not changed(board_shot(START, clock="9:58"), board_shot(START, clock="9:57"))
    assert not changed(board_shot(START, clock="9:59"), board_shot(START, clock="10:00"))  # four digits at once
    assert not changed(still, board_shot(START, flicker=int(cell)))  # a 1-cell flicker (a cursor blink)
    assert not changed(still, board_shot(START, pulse=15))  # a highlight pulsing
    assert not changed(still, board_shot(START))  # the same screen, encoded again
    # And they leave the screen "still" for the still exit.
    frame = lambda shot: waiting.region_frame(shot, BOARD)  # noqa: E731
    assert waiting.region_still(frame(board_shot(START, clock="9:59")), frame(board_shot(START, clock="10:00")))
    assert waiting.region_still(frame(still), frame(board_shot(START, flicker=int(cell))))
    assert not waiting.region_still(frame(still), frame(board_shot(move(START, (4, 6), (4, 4)))))


def test_the_synthetic_frames_can_be_inspected(tmp_path):
    """The frames above, written out (the scratch area: pytest's tmp_path) so a failure can be looked at."""
    for name, shot in (("start", board_shot()), ("e4", board_shot(move(START, (4, 6), (4, 4)))),
                       ("menu", board_shot(menu=True))):
        (tmp_path / f"{name}.jpg").write_bytes(shot.jpeg)
        assert Image.open(tmp_path / f"{name}.jpg").size == IMG


# --- the play area ------------------------------------------------------------------------------------------------

CHESS_LABELS = [  # Chess.com's game screen around the board: what the element list had on 2026-10-17
    el(desc="Back", bounds=(0, 120, 140, 260), cls="ImageButton"),
    el(desc="Chess.com logo", bounds=(300, 140, 780, 240), cls="ImageView"),
    el(desc="Settings", bounds=(940, 120, 1080, 260), cls="ImageButton"),
    el("Martinfoot (250)", bounds=(40, 480, 600, 580)),
    el("1.", bounds=(40, 300, 100, 380)), el("e4", bounds=(110, 300, 200, 380)),
    el("Guest", bounds=(40, 1700, 600, 1800)),
    el(desc="Options", bounds=(0, 2100, 270, 2250), cls="ImageButton"),
    el(desc="Back", bounds=(270, 2100, 540, 2250), cls="ImageButton"),
    el(desc="Forward", bounds=(540, 2100, 810, 2250), cls="ImageButton"),
]


def test_the_play_area_is_the_largest_unlabeled_rectangle():
    region = waiting.canvas_region(CHESS_LABELS, DEV)
    assert region is not None
    x1, y1, x2, y2 = region
    assert x1 <= 0 + 45 and x2 >= 1080 - 45 and y1 <= 600 and y2 >= 1680 - 45  # the board, within a grid cell
    assert y1 >= 580 - 100 and y2 <= 1700 + 100  # and not the labels around it
    # Tetris: one element, "Game view", covering the screen: a container, so the whole screen is the play area.
    assert waiting.canvas_region([el(desc="Game view", bounds=(0, 0, *DEV), cls="View")], DEV) == (0, 0, *DEV)
    assert waiting.canvas_region([], DEV) == (0, 0, *DEV)  # treeless
    # A 16:9 video player in portrait (~25%) under a list isn't one; nor is a screen full of rows.
    rows = [el(f"Video {i}", bounds=(0, 700 + 120 * i, 1080, 800 + 120 * i)) for i in range(14)]
    assert waiting.canvas_region([el(desc="Player", bounds=(0, 80, 1080, 688), cls="View")] + rows, DEV) is None
    assert waiting.canvas_region([el(f"Row {i}", bounds=(0, 120 * i, 1080, 120 * i + 110)) for i in range(20)],
                                 DEV) is None
    # An empty text field counts as a label (its cursor blinks), an OCR word doesn't.
    field = el(bounds=(0, 0, 1080, 2340 // 2), editable=True)
    assert waiting.canvas_region([field], DEV) is not None  # covers half: a container-sized field doesn't count
    words = [el(f"w{i}", bounds=(0, 100 * i, 1080, 100 * i + 90), cls="OCRText") for i in range(23)]
    assert waiting.canvas_region(words, DEV) == (0, 0, *DEV)
    assert waiting.canvas_region(CHESS_LABELS, None) is None


# --- the waits ----------------------------------------------------------------------------------------------------


class BoardPhone(FakeDriver):
    """Chess.com's game screen: the same labels throughout; the board's frame follows `frames`, [(after_s, shot)].
    Offers wait_for like the OpenMob app (which can't see the board), recording every call."""

    fast_ui_tree = True

    def __init__(self, frames, labels=CHESS_LABELS, package=CHESS, caps=("wait_idle", "wait_for")):
        super().__init__(packages=[package], device=DEV, image=IMG)
        self.package = package
        self.frames = frames
        self.labels_ = labels
        self.t0 = time.monotonic()
        self.capabilities = set(caps)
        self.wait_for_calls = []

    def _due(self, items):
        age = time.monotonic() - self.t0
        due = [x for x in items if x[0] <= age]
        return (due[-1] if due else items[0])[1]

    def ui_elements(self):
        return list(self.labels_)

    def screenshot(self, max_age=0.0, attempts=2):
        return self._due(self.frames)

    def wait_idle(self, quiet_ms, max_ms):
        time.sleep(min(quiet_ms, max_ms) / 1000)
        return {"waited_ms": quiet_ms, "idle": True}

    def wait_for(self, condition, timeout_ms):
        """The phone compares labels only: the board's change is invisible to it."""
        self.wait_for_calls.append(dict(condition))
        time.sleep(timeout_ms / 1000)
        return {"met": False, "waited_ms": timeout_ms, "seen": "", "package": self.package}


def run(phone, cond, **kw):
    return waiting.Waiter(phone, threading.Event()).run(cond, **kw)


def screen_changes(timeout_s=10):
    return waiting.parse({"until": "screen_changes", "timeout_s": timeout_s})


def test_the_bots_reply_on_the_board_is_seen_though_the_labels_dont_change():
    e4 = move(START, (4, 6), (4, 4))
    phone = BoardPhone([(0, board_shot(e4)), (0.6, board_shot(move(e4, (4, 1), (4, 3))))])
    cond = screen_changes()
    result = run(phone, cond, baseline=(CHESS, CHESS_LABELS), still_exit_s=waiting.still_exit_for(cond),
                 play_area=True, screen=DEV)
    assert result.met and 500 <= result.waited_ms < 2500, result
    assert "unlabeled area" in result.seen
    assert phone.wait_for_calls == []  # the server took the frames itself: the phone can't see the board


def test_without_the_play_area_the_labels_alone_miss_it():
    """2026-10-17's failure, kept as a regression marker: comparing labels only, the reply is invisible."""
    e4 = move(START, (4, 6), (4, 4))
    phone = BoardPhone([(0, board_shot(e4)), (0.3, board_shot(move(e4, (4, 1), (4, 3))))], caps=())
    result = run(phone, screen_changes(timeout_s=1.2), baseline=(CHESS, CHESS_LABELS))
    assert not result.met


def test_a_reply_while_the_model_was_deciding_counts_too():
    e4 = move(START, (4, 6), (4, 4))
    phone = BoardPhone([(0, board_shot(move(e4, (4, 1), (4, 3))))])  # the bot already replied
    result = run(phone, screen_changes(), baseline=(CHESS, CHESS_LABELS), play_area=True, screen=DEV,
                 baseline_shot=board_shot(e4))  # the screenshot the model saw
    assert result.met and result.waited_ms < 1500


def test_a_frame_change_wins_over_labels_that_say_nothing_changed():
    phone = BoardPhone([(0, board_shot(START)), (0.3, board_shot(move(START, (6, 7 - 1), (5, 5))))], caps=())
    base = (CHESS, list(CHESS_LABELS))
    assert waiting.signature(CHESS, phone.ui_elements()) == waiting.signature(*base)
    result = run(phone, screen_changes(timeout_s=5), baseline=base, play_area=True, screen=DEV)
    assert result.met


def test_a_ticking_clock_on_the_board_is_not_a_change_and_the_screen_stays_still():
    clocks = [(i * 0.5, board_shot(START, clock=f"9:{59 - i:02d}")) for i in range(12)]
    phone = BoardPhone(clocks)
    cond = screen_changes(timeout_s=10)
    result = run(phone, cond, baseline=(CHESS, CHESS_LABELS), still_exit_s=waiting.still_exit_for(cond),
                 play_area=True, screen=DEV)
    assert not result.met and result.stopped == "still"
    assert 3400 <= result.waited_ms < 5500


TETRIS_VIEW = [el(desc="Game view", bounds=(0, 0, *DEV), cls="View")]


def test_a_still_menu_ends_screen_changes_in_about_three_and_a_half_seconds():
    phone = BoardPhone([(0, board_shot(menu=True))], labels=TETRIS_VIEW, package=TETRIS)
    cond = screen_changes(timeout_s=15)
    result = run(phone, cond, baseline=(TETRIS, TETRIS_VIEW), still_exit_s=waiting.still_exit_for(cond),
                 play_area=True, screen=DEV)
    assert not result.met and result.stopped == "still"
    assert 3400 <= result.waited_ms < 5000, result.waited_ms
    text = result.text(cond)
    assert "the screen hasn't changed and nothing is loading: it may already be ready" in text
    assert "wait again" in text


def test_a_change_still_ends_the_wait_at_once():
    phone = BoardPhone([(0, board_shot(menu=True)), (0.4, board_shot(START))], labels=TETRIS_VIEW, package=TETRIS)
    cond = screen_changes(timeout_s=15)
    result = run(phone, cond, baseline=(TETRIS, TETRIS_VIEW), still_exit_s=waiting.still_exit_for(cond),
                 play_area=True, screen=DEV)
    assert result.met and result.waited_ms < 1500


class LabelPhone(FakeDriver):
    """A labeled screen (no play area: rows top to bottom); its labels follow `schedule`. wait_for like the OpenMob
    app: with still_ms (a wait_for_still phone) it ends on a still screen, like PhoneActions.waitFor."""

    fast_ui_tree = True

    def __init__(self, schedule, caps=("wait_idle",)):
        super().__init__(packages=["com.example"], device=DEV, image=IMG)
        self.package = "com.example"
        self.schedule = schedule
        self.t0 = time.monotonic()
        self.capabilities = set(caps)
        self.conditions = []

    def ui_elements(self):
        age = time.monotonic() - self.t0
        labels = [s for s in self.schedule if s[0] <= age][-1][1]
        return [el(t, bounds=(0, 100 * i, 1080, 100 * i + 90), i=i) for i, t in enumerate(labels)]

    def wait_idle(self, quiet_ms, max_ms):
        time.sleep(min(quiet_ms, max_ms) / 1000)
        return {"waited_ms": quiet_ms, "idle": True}

    def wait_for(self, condition, timeout_ms):
        self.conditions.append(dict(condition))
        start = time.monotonic()
        base = waiting.signature(self.package, self.ui_elements())
        sig, since = base, start
        while True:
            now = time.monotonic()
            cur = waiting.signature(self.package, self.ui_elements())
            if cur != sig:
                sig, since = cur, now
            out = {"met": cur != base, "waited_ms": int((now - start) * 1000), "seen": "", "package": self.package}
            still = "still_ms" in condition and (now - since) * 1000 >= condition["still_ms"]
            if out["met"] or still or (now - start) * 1000 >= timeout_ms:
                if "still_ms" in condition:
                    out.update(still=still and not out["met"], still_for_ms=int((now - since) * 1000))
                return out
            time.sleep(0.02)


ROWS = [f"Row {i}" for i in range(22)]


@pytest.mark.parametrize("caps", [("wait_idle",), ("wait_idle", "wait_for"), ("wait_idle", "wait_for", "wait_for_still")])
def test_screen_changes_on_a_still_labeled_screen_ends_early_on_every_path(caps):
    phone = LabelPhone([(0, ROWS)], caps=caps)
    cond = screen_changes(timeout_s=15)
    base = ("com.example", phone.ui_elements())
    result = run(phone, cond, baseline=base, still_exit_s=waiting.still_exit_for(cond), play_area=True, screen=DEV)
    assert not result.met and result.stopped == "still" and 3400 <= result.waited_ms < 5000, (caps, result)
    if "wait_for_still" in caps:
        assert phone.conditions and all(c.get("still_ms", 0) > 0 for c in phone.conditions)
        assert len(phone.conditions) == 1  # one call: the phone ended it on the still screen
    elif "wait_for" in caps:
        assert phone.conditions and all("still_ms" not in c for c in phone.conditions)  # an older app: chunks


@pytest.mark.parametrize("caps", [("wait_idle",), ("wait_idle", "wait_for", "wait_for_still")])
def test_a_label_change_still_returns_at_once(caps):
    phone = LabelPhone([(0, ROWS), (0.5, ROWS + ["New message"])], caps=caps)
    cond = screen_changes(timeout_s=15)
    result = run(phone, cond, baseline=("com.example", [el(t, bounds=(0, 100 * i, 1080, 100 * i + 90), i=i)
                                                         for i, t in enumerate(ROWS)]),
                 still_exit_s=waiting.still_exit_for(cond), play_area=True, screen=DEV)
    assert result.met and result.waited_ms < 1500


def test_something_loading_keeps_the_full_timeout():
    phone = LabelPhone([(0, ROWS[:10] + ["Loading…"])])
    cond = screen_changes(timeout_s=5)
    result = run(phone, cond, still_exit_s=waiting.still_exit_for(cond), play_area=True, screen=DEV)
    assert not result.met and result.stopped is None and result.waited_ms >= 4900


def test_the_wire_and_the_exits_per_condition():
    cond = screen_changes()
    assert cond.wire() == {"until": "screen_changes"}
    assert cond.wire(3500) == {"until": "screen_changes", "still_ms": 3500}
    assert waiting.still_exit_for(cond) == waiting.SCREEN_STILL_EXIT_S == 3.5
    assert waiting.still_exit_for(waiting.parse({"until": "text_appears", "target": "x"})) == waiting.STILL_EXIT_S
    assert waiting.still_exit_for(waiting.parse({"until": "loading_done"})) is None


def test_the_remote_driver_passes_the_still_answer_through():
    from openmob.phone.remote import RemoteDriver

    class Link:
        closed = False

        def call(self, method, params):
            assert method == "wait_for" and params["condition"]["still_ms"] == 3500
            return {"met": False, "waited_ms": 3500, "seen": "", "package": "p", "still": True, "still_for_ms": 3500}

    driver = RemoteDriver(Link())
    driver.capabilities = {"wait_for", "wait_for_still"}
    answer = driver.wait_for({"until": "screen_changes", "still_ms": 3500}, 10_000)
    assert answer["still"] is True and answer["still_for_ms"] == 3500


# --- Faster mode: sparse screens ------------------------------------------------------------------------------------


def _rows(texts, top=200, h=110, x2=1040):
    return [el(t, bounds=(40, top + h * i, x2, top + h * i + h - 10), i=i) for i, t in enumerate(texts)]


# Stock Settings' top level as an emulator shows it: a title, search, and each entry's name and summary.
SETTINGS = ([el("Settings", bounds=(48, 120, 400, 220)), el(desc="Search settings", bounds=(900, 120, 1032, 252))]
            + [e for i, (name, summary) in enumerate([
                ("Network & internet", "Mobile, Wi-Fi, hotspot"), ("Connected devices", "Bluetooth, pairing"),
                ("Apps", "Recent apps, default apps"), ("Notifications", "Notification history, conversations"),
                ("Battery", "100%"), ("Storage", "34% used - 21 GB free"), ("Sound & vibration", "Volume, haptics"),
                ("Display", "Dark theme, font size, brightness"), ("Wallpaper & style", "Colors, themed icons"),
                ("Accessibility", "Display, interaction, audio"), ("Security & privacy", "App security, device lock"),
                ("Location", "On - 5 apps have access"), ("Passwords & accounts", "Saved passwords, autofill")])
               for e in (el(name, bounds=(160, 300 + 160 * i, 1040, 360 + 160 * i)),
                         el(summary, bounds=(160, 362 + 160 * i, 1040, 420 + 160 * i)))])
# tests/fixtures/settings_ui.xml is a 3-element stub of that screen (title, search, one switch): structurally it is
# "a few labels over a large empty area", so only the frame check (nothing drawn there) keeps it from being sent.
SETTINGS_STUB = parse_ui_xml((Path(__file__).parent / "fixtures" / "settings_ui.xml").read_text())
MAPS = ([el(hint="Search here", bounds=(40, 120, 1040, 240), cls="EditText", editable=True),
         el(desc="Google Map", bounds=(0, 0, *DEV), cls="View")]
        + [el(t, bounds=(40 + 200 * i, 260, 230 + 200 * i, 340), cls="Button") for i, t in
           enumerate(["Restaurants", "Coffee", "Gas", "Groceries", "Hotels"])]
        + [el(desc=f"Cafe {c}", bounds=(100 + 90 * i, 900 + 40 * i, 160 + 90 * i, 960 + 40 * i), cls="View")
           for i, c in enumerate("ABCDEFGH")]
        + [el(t, bounds=(216 * i, 2200, 216 * (i + 1), 2340), cls="Button") for i, t in
           enumerate(["Explore", "Go", "Saved", "Contribute", "Updates"])])
YOUTUBE = ([el(desc="YouTube", bounds=(20, 100, 300, 180), cls="ImageView"),
            el(desc="Search", bounds=(900, 100, 1060, 180), cls="ImageButton"),
            el(desc="Player", bounds=(0, 200, 1080, 808), cls="View")]
           + _rows([f"How to play chess like a grandmaster, part {i} · 1.2M views · 3 days ago" for i in range(6)],
                   top=830, h=200)
           + [el(t, bounds=(216 * i, 2200, 216 * (i + 1), 2340), cls="Button") for i, t in
              enumerate(["Home", "Shorts", "Create", "Subscriptions", "You"])])
CHROME = ([el("example.com/article", rid="url_bar", bounds=(150, 100, 900, 200), cls="EditText", editable=True),
           el(desc="Tabs", bounds=(900, 100, 1000, 200)), el(desc="More options", bounds=(1000, 100, 1080, 200))]
          + _rows(["Long article paragraph " * 6] * 12, top=240, h=160))
GAME_SPARSE = [el(desc="Back", bounds=(0, 80, 160, 240), cls="ImageButton"),
               el("LEVEL 2", bounds=(380, 1900, 700, 2000))]


@pytest.mark.parametrize("name,elements", [("settings", SETTINGS), ("maps", MAPS), ("youtube", YOUTUBE),
                                           ("chrome", CHROME)])
def test_text_heavy_screens_are_never_sparse(name, elements):
    assert faster.labeled(elements)
    assert faster.sparse_region(elements, (1080, 2400)) is None, name
    assert not faster.eligible(elements, (1080, 2400)), name


@pytest.mark.parametrize("elements", [TETRIS_VIEW, GAME_SPARSE, CHESS_LABELS])
def test_game_screens_with_a_few_labels_are(elements):
    assert faster.sparse_region(elements, DEV) is not None
    assert faster.eligible(elements, DEV)
    assert faster.eligible([], DEV)  # treeless, as before


def test_a_typing_screen_or_a_password_field_is_not_sparse():
    field = el("me@example.com", bounds=(40, 300, 1040, 400), cls="EditText", editable=True, focused=True)
    assert faster.sparse_region(GAME_SPARSE + [field], DEV) is None
    pw = el(bounds=(40, 300, 1040, 400), cls="EditText", editable=True, password=True)
    assert faster.sparse_region(GAME_SPARSE + [pw], DEV) is None


def test_a_sparse_screen_needs_its_words_read_first():
    common = dict(blocked=None, sensitive_apps=[], locked=False)
    assert faster.refusal(TETRIS, GAME_SPARSE, ocr_ran=False, sparse=True, **common) == faster.NO_TEXT_CHECK
    assert faster.refusal(TETRIS, GAME_SPARSE, ocr_ran=True, sparse=True, **common) is None
    assert faster.refusal(TETRIS, GAME_SPARSE, ocr_ran=False, **common) is None  # labeled, not sparse: unchanged


class SparseGame(FakeDriver):
    """Tetris as the phone reads it: one "Game view" element over a drawn menu (or a blank screen)."""

    fast_ui_tree = True

    def __init__(self, blank=False, elements=TETRIS_VIEW):
        super().__init__(packages=[TETRIS], elements=list(elements), device=DEV, image=IMG)
        self.package = TETRIS
        self.blank = blank

    def screenshot(self, max_age=0.0, attempts=2):
        if self.blank:
            buf = io.BytesIO()
            Image.new("RGB", IMG, (250, 250, 250)).save(buf, "JPEG")
            return Screenshot(buf.getvalue(), *IMG, *DEV)
        return board_shot(menu=True)

    def wait_idle(self, quiet_ms, max_ms):
        time.sleep(min(quiet_ms, max_ms) / 1000)
        return {"waited_ms": quiet_ms, "idle": True}


@pytest.fixture
def words(monkeypatch):
    box = {"words": ["LEVEL 2", "MARATHON"]}

    def recognize(data, width, height, accurate=True):
        return [TextBox(w, 0.9, 10, 10 + 40 * i, 200, 40 + 40 * i) for i, w in enumerate(box["words"])]

    monkeypatch.setattr("openmob.agent.loop.ocr.recognize_text", recognize)
    return box


def make(tmp_path, responses, images, driver, use_ocr=True, **cfg):
    config = Config(settle_delay=0, launch_settle_delay=0, **cfg)
    agent = Agent(ScriptedClient(responses), driver, AppResolver(driver, label_cache=tmp_path / "l.json"), config,
                  StepIO(), use_ocr=use_ocr, images=images)
    agent.faster_mode = lambda: True
    return agent


DONE = [[tool_use("done", "d", summary="ok", success=True)]]


def test_a_sparse_game_screen_gets_the_j1_hint(tmp_path, words):
    images = FakeImages(states=["menu"])
    agent = make(tmp_path, DONE, images, SparseGame())
    agent.run("open Tetris and start a new game")
    assert [c[0] for c in images.calls] == ["state"]
    assert "Screen state from a quick screenshot check: menu" in str(agent.client.requests[0]["messages"])
    assert agent.io.steps[0]["jev_images"] == 1 and agent.io.steps[0]["jev_state"] == "menu"


def test_a_sparse_screen_with_a_card_number_in_its_play_area_is_never_sent(tmp_path, words):
    words["words"] = ["4111 1111 1111 1111"]  # read off the frame by OCR: the labels don't show it
    images = FakeImages(states=["menu"])
    agent = make(tmp_path, DONE, images, SparseGame())
    agent.run("open Tetris")
    assert images.calls == [] and agent.io.steps[0]["jev_image_skip"] == faster.SENSITIVE_NUMBER


def test_a_sparse_screen_without_ocr_or_with_nothing_drawn_sends_nothing(tmp_path, words):
    images = FakeImages(states=["menu"])
    agent = make(tmp_path, DONE, images, SparseGame(), use_ocr=False)
    agent.run("open Tetris")
    assert images.calls == [] and agent.io.steps[0]["jev_image_skip"] == faster.NO_TEXT_CHECK
    blank = make(tmp_path, DONE, images, SparseGame(blank=True))
    blank.run("open Tetris")
    assert images.calls == []  # a blank area under a few labels: an empty screen, not a canvas


def test_a_text_heavy_screen_still_sends_nothing(tmp_path, words):
    images = FakeImages(states=["content"])
    agent = make(tmp_path, DONE, images, SparseGame(elements=SETTINGS))
    agent.run("open wi-fi")
    assert images.calls == []
    # The 3-element Settings stub over a blank (white) screen: few labels, but nothing drawn in the empty area.
    stub = SparseGame(elements=SETTINGS_STUB, blank=True)
    stub.package = "com.android.settings"
    make(tmp_path, DONE, images, stub).run("open wi-fi")
    assert images.calls == []


WAIT = [[tool_use("wait_until", "w", until="screen_changes", timeout_s=15)]] + DONE


def test_j3_ready_ends_the_models_screen_changes_wait(tmp_path, words):
    images = FakeImages(states=["menu"], loading=[0.03])
    agent = make(tmp_path, WAIT, images, SparseGame())
    agent.run("open Tetris and start a new game")
    step = agent.io.steps[0]
    assert step["tool"] == "wait_until" and step["wait_met"] is False and step["wait_via"] == "jev_image"
    assert 900 <= step["waited_ms"] < 3000, step["waited_ms"]
    assert "quick screenshot check says it's ready" in step["outcome"]
    assert "loading" in [c[0] for c in images.calls]


def test_an_unsure_or_loading_j3_leaves_the_still_exit(tmp_path, words):
    images = FakeImages(states=["menu"], loading=[0.5])  # unsure: ignored
    agent = make(tmp_path, WAIT, images, SparseGame())
    agent.run("open Tetris and start a new game")
    step = agent.io.steps[0]
    assert step["wait_via"] != "jev_image" and 3400 <= step["waited_ms"] < 5500
    assert sum(1 for c in images.calls if c[0] == "loading") <= 2  # at most JEV_WAIT_READY_ASKS a wait
    off = make(tmp_path, WAIT, FakeImages(states=["menu"], loading=[0.03]), SparseGame(), jev_image_wait_ready=False)
    off.run("open Tetris and start a new game")
    assert off.io.steps[0]["wait_via"] != "jev_image" and off.io.steps[0]["waited_ms"] >= 3400


def test_the_models_wait_ends_on_a_still_labeled_screen(tmp_path):
    phone = LabelPhone([(0, ROWS)], caps=("wait_idle", "wait_for", "wait_for_still"))
    agent = Agent(ScriptedClient(WAIT), phone, AppResolver(phone, label_cache=tmp_path / "l.json"),
                  Config(settle_delay=0, launch_settle_delay=0), StepIO(), use_ocr=False)
    agent.run("wait for it")
    step = agent.io.steps[0]
    assert step["wait_met"] is False and 3400 <= step["waited_ms"] < 5000
    assert "may already be ready" in step["outcome"]
