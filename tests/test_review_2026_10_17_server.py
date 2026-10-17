"""Fixes from the batched security review of 2026-10-17, server side (review S). Each proof of concept from the review
(scratchpad/review-s/) is here as a regression test that now asserts the safe behaviour:

- M1: every OCR word counts for Faster mode's never-sent checks, whatever its confidence; 8+ digits never leave.
- M2: one-time codes and System UI windows (the shade, a heads-up) are never sent.
- M3: an image request is never queued, never sent after its deadline, nor after Faster mode was turned off.
- L2: a frame taken while another app came to the front is discarded.
- L3: the black-frame check judges the app area (the bars and OpenMob's border don't make a secure frame "not black").
- L4: a key's Faster mode allowance is cleared on every stored phone, revocation included; a key sees only its own.
- L5: the reconnect window survives a restart; a grant whose drop wasn't recorded (a crash) isn't resumed.
- L6: the steps row's anchor (the tapped element's words) is redacted.
- L7: a call refused with gate_prompt_open is re-sent only while the screen is still the one it was decided on.
- Info: a TypeSafe error body never reaches the feed.
- Server side of review P M4: a card number typed in pieces asks.
"""

import io as iolib
import threading
import time

import httpx
import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use
from PIL import Image
from test_faster_mode import DONE, GAME, FakeImages, GameDriver, StepIO, make
from test_faster_mode_switch import resume_message, session_on, turn_on, view
from test_financial_safeguards import run_loop, text_field, typed
from test_gatekeeper_server import _Link
from test_permission_settings import signed
from test_phone_security import hosted  # noqa: F401  (a fixture)
from test_remote import wait_for

from openmob.agent import faster
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.jev import JevError, JevImageJudge, black_frame, prepare_image
from openmob.phone.adb import Screenshot, UIElement
from openmob.phone.apps import AppResolver
from openmob.phone.base import GateRefused
from openmob.phone.remote import RemoteDriver
from openmob.vision.ocr import TextBox

BANK = "com.chase.sig.android"


def el(text, cls="OCRText", **kw):
    return UIElement(0, text, "", kw.pop("rid", ""), cls, True, None, (0, 0, 10, 10), **kw)


def refusal(package, elements):
    return faster.refusal(package, elements, blocked=lambda p: False, sensitive_apps=[], locked=False, ocr_ran=True)


# --- M1: low-confidence OCR words, misread numbers ------------------------------------------------------------------


def test_a_low_confidence_card_number_still_stops_the_image(tmp_path, monkeypatch):
    """PoC test_low_confidence_card_number_is_not_checked_and_image_is_sent: OCR reads the card at 0.25 (under
    OCR_MIN_CONFIDENCE). It no longer goes to the model, but it still counts for the never-sent check."""
    monkeypatch.setattr("openmob.agent.loop.ocr.recognize_text",
                        lambda *a, **k: [TextBox("4111 1111 1111 1111", 0.25, 10, 10, 300, 40)])
    images = FakeImages(states=["menu"])
    agent, _ = make(tmp_path, DONE, images)
    agent.run("start the game")
    assert images.calls == []
    assert any(r.get("jev_image_skip") == "sensitive_number" for r in agent.io.steps)


@pytest.mark.parametrize("text", ["4111 1111 1111 1112", "Acct 12345678", "1234-5678-9012", "1234.5678.90",
                                  "٤١١١١١١١١١"])
def test_eight_or_more_digits_never_leave_whatever_luhn_says(text):
    assert refusal(GAME, [el(text)]) == faster.SENSITIVE_NUMBER


@pytest.mark.parametrize("text", ["Score 1200", "Level 7", "3 lives", "12:45", "x2 combo"])
def test_ordinary_game_numbers_still_go(text):
    assert refusal(GAME, [el("Play"), el(text)]) is None


# --- M2: one-time codes, the shade and heads-ups ------------------------------------------------------------------


def test_a_one_time_code_on_a_canvas_screen_is_never_sent():
    """PoC test_refusal_passes_otp_and_message_on_canvas: a heads-up "Your bank code is 482913" over a game."""
    elements = [el("Messages"), el("Your bank code is 482913. Don't share it."), el("Score 1200")]
    assert refusal(GAME, elements) == faster.OTP_TEXT
    for text in ("G-482913 is your Google verification code", "Your OTP: 4829", "Passcode 773120",
                 "PIN 4821", "Use 482913 to sign in"):
        assert refusal(GAME, [el(text)]) in (faster.OTP_TEXT, faster.SENSITIVE_NUMBER), text
    # Split over OCR boxes: the joined words still count.
    assert refusal(GAME, [el("Your verification"), el("code is"), el("4829")]) == faster.OTP_TEXT


def test_the_notification_shade_or_a_heads_up_is_never_sent():
    assert refusal(faster.SYSTEMUI, [el("Play")]) == faster.NOTIFICATION
    heads_up = UIElement(1, "", "", "com.android.systemui:id/notification_title", "TextView", False, None,
                         (0, 0, 10, 10))
    assert refusal(GAME, [el("Play"), heads_up]) == faster.NOTIFICATION


# --- M3: never queued, never late, never after "off" --------------------------------------------------------------


class SlowClient:
    def __init__(self, delay=0.5):
        self.sent = []  # (time, image) when a request actually goes out
        self.delay = delay

    def ask_image(self, state, questions, image, model, timeout_s):
        self.sent.append((time.monotonic(), image))
        time.sleep(self.delay)
        return {"loading": {"noul": 0.1}}


def test_no_image_is_sent_after_its_deadline():
    """PoC test_queued_image_still_sent_after_deadline: 4 calls with a 50 ms deadline against a 0.5 s server. Before
    the fix, 2 queued images went out after every caller had given up; now nothing is queued."""
    c = SlowClient()
    j = JevImageJudge(c, "jev-1.14.0", timeout_s=0.05)
    for i in range(4):
        with pytest.raises(JevError):
            j.loading("com.game", b"img%d" % i)
    gave_up = time.monotonic()
    time.sleep(1.3)
    assert [img for t, img in c.sent if t > gave_up] == []
    assert len(c.sent) <= j.max_in_flight


def test_no_image_is_sent_after_faster_mode_turns_off():
    c = SlowClient(delay=0.0)
    j = JevImageJudge(c, "jev-1.14.0", timeout_s=0.5)
    j.still_ok = lambda: False  # turned off between the check and the POST
    with pytest.raises(JevError, match="off"):
        j.loading("com.game", b"img")
    assert c.sent == []
    j.still_ok = lambda: True
    assert j.loading("com.game", b"img").value == "ready" and len(c.sent) == 1


def test_a_slot_freed_by_a_finished_request_is_reused():
    c = SlowClient(delay=0.0)
    j = JevImageJudge(c, "jev-1.14.0", timeout_s=0.5)
    for _ in range(5):
        j.loading("com.game", b"img")
    assert len(c.sent) == 5 and j._in_flight == 0


def test_the_loop_wires_its_live_switch_into_the_worker(tmp_path, words):
    images = FakeImages(states=["menu"])
    agent, switch = make(tmp_path, DONE, images)
    agent.run("start the game")
    assert images.calls and images.still_ok() is True
    switch["on"] = False
    assert images.still_ok() is False


@pytest.fixture
def words(monkeypatch):
    monkeypatch.setattr("openmob.agent.loop.ocr.recognize_text",
                        lambda *a, **k: [TextBox("Play", 0.9, 10, 10, 200, 40)])
    monkeypatch.setattr("openmob.agent.loop.JEV_READY_POLL_S", 0.01)


# --- L2: the package read after the frame ---------------------------------------------------------------------------


def test_a_frame_taken_as_a_bank_came_to_the_front_is_discarded(tmp_path, words):
    """PoC test_j3_checks_the_package_read_before_the_frame: the screenshot itself brings a banking app up, right
    after current_app() was read. That frame was sent labeled as the game; now it is discarded (app_changed)."""

    class Switching(GameDriver):
        n = 0

        def screenshot(self, max_age=0.0, attempts=2):
            shot = super().screenshot(max_age, attempts)
            Switching.n += 1
            if Switching.n == 3:  # the first J3 frame
                self.package = BANK
            return shot

    driver = Switching()
    shots_at_send = []
    images = FakeImages(states=["loading"], loading=[0.9, 0.1], on_call=lambda kind: shots_at_send.append(Switching.n))
    agent, _ = make(tmp_path, DONE, images, driver=driver, sensitive_apps=[r"chase"])
    agent.run("start the game")
    assert images.calls[0][:2] == ("state", GAME)
    # Every frame sent was taken before the bank came up (frame 3 and later never went).
    assert shots_at_send and max(shots_at_send) < 3, (shots_at_send, images.calls)
    assert any(r.get("jev_image_skip") == faster.APP_CHANGED for r in agent.io.steps)


# --- L3: the black-frame check on the app area ----------------------------------------------------------------------


def secure_capture(w=540, h=1200, pill=True):
    """A FLAG_SECURE capture as a phone returns it: the app area black, but the status bar, the navigation bar,
    OpenMob's glowing border and its Stop pill drawn."""
    img = Image.new("RGB", (w, h), "black")
    px = img.load()
    for y in range(h):
        for x in range(w):
            if y < int(h * 0.035) or y > int(h * 0.955) or x < 4 or x > w - 5:
                px[x, y] = (90, 160, 255)
    if pill:
        for y in range(int(h * 0.04), int(h * 0.04) + 40):
            for x in range(w - 110, w - 14):
                px[x, y] = (230, 60, 60)
    buf = iolib.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def test_a_secure_window_with_the_bars_and_indicator_is_still_black():
    assert prepare_image(secure_capture(), 768).black is True
    # A real screen isn't black, and nor is a dark game with a lit-up area.
    img = Image.new("RGB", (540, 1200), "black")
    img.paste((200, 200, 40), (100, 300, 440, 700))
    assert black_frame(img) is False
    assert black_frame(Image.new("RGB", (540, 1200), (40, 120, 200))) is False


def test_the_loop_skips_a_secure_capture_as_black(tmp_path, words):
    class Secure(GameDriver):
        def screenshot(self, max_age=0.0, attempts=2):
            self.shots += 1
            return Screenshot(secure_capture(*self.image), *self.image, *self.device)

    images = FakeImages(states=["menu"])
    agent, _ = make(tmp_path, DONE, images, driver=Secure())
    agent.run("start the game")
    assert images.calls == []
    assert any(r.get("jev_image_skip") == faster.BLACK_FRAME for r in agent.io.steps)


# --- L6: the anchor in the steps row ------------------------------------------------------------------------------


def test_the_steps_row_anchor_is_redacted(tmp_path):
    """PoC test_redact_misses_anchor_like_nested: a tap on "Visa 4111 1111 1111 1111" stored its text."""
    card = UIElement(0, "Visa 4111 1111 1111 1111", "Card 4111111111111111", "", "android.widget.Button", True, None,
                     (40, 400, 1040, 500))
    driver = FakeDriver(elements=[card], packages=["com.example.app"])
    driver.package = "com.example.app"
    io = StepIO(["yes", "yes"])
    client = ScriptedClient([[tool_use("tap_element", "t", index=0, note={"card": "4111 1111 1111 1111"})],
                             [tool_use("done", "z", summary="ok", success=True)]])
    a = Agent(client, driver, AppResolver(driver, {}, label_cache=tmp_path / "l.json"),
              Config(settle_delay=0, launch_settle_delay=0), io, use_ocr=False)
    a.run("pick my card")
    row = next(r for r in io.steps if r["tool"] == "tap_element")
    assert "4111" not in str(row), row
    assert "<redacted>" in row["anchor"]["text"] and "<redacted>" in row["anchor"]["desc"]


# --- L7: a stale act isn't re-sent while a prompt is open ---------------------------------------------------------

OPEN = GateRefused("gate_prompt_open", "Waiting for the owner to answer a prompt on the phone.",
                   rule="builtin:prompt_open")


def gk_driver(replies):
    driver = RemoteDriver(_Link(replies))
    driver.capabilities = {"gatekeeper", "gate_cancel"}
    driver.prompt_waiter = lambda exc, waited: True
    return driver


def test_a_call_isnt_resent_once_the_screen_or_app_changed():
    driver = gk_driver([OPEN, None])
    driver.prompt_recheck = lambda: "the app in front changed"
    with pytest.raises(GateRefused) as info:
        driver.tap(1, 2)
    assert info.value.code == "gate_screen_changed" and "app in front changed" in str(info.value)
    assert [c[0] for c in driver.link.calls] == ["tap"]  # never re-sent
    # While OpenMob's own prompt is in front, nothing is sent; once the screen is back as it was, it is.
    answers = ["wait", "wait", None]
    driver = gk_driver([OPEN, None])
    driver.prompt_recheck = lambda: answers.pop(0)
    driver.tap(1, 2)
    assert [c[0] for c in driver.link.calls] == ["tap", "tap"] and answers == []


def test_the_agent_rechecks_the_screen_before_a_resend(tmp_path):
    screen = [UIElement(0, "Send", "", "", "android.widget.Button", True, None, (0, 0, 100, 100))]
    driver = FakeDriver(elements=list(screen), packages=["com.example.app"])
    driver.package = "com.example.app"
    agent = Agent(ScriptedClient([]), driver, None, Config(), RecordingIO(), use_ocr=False)

    class Obs:
        package = "com.example.app"
        elements = list(screen)

    class State:
        dispatch_obs = Obs
        blocked = staticmethod(lambda p: p == BANK)

    agent._is_blocked = lambda state, p: p == BANK
    assert agent._prompt_recheck(State) is None  # the same screen: re-send
    driver.package = "dev.openmob.companion"
    assert agent._prompt_recheck(State) == "wait"  # the phone's prompt is in front
    driver.package = "com.example.app"
    driver.elements = [UIElement(0, "Delete", "", "", "android.widget.Button", True, None, (0, 0, 100, 100))]
    assert agent._prompt_recheck(State) == "the screen changed"
    driver.package = "com.other.app"
    assert agent._prompt_recheck(State) == "the app in front changed"
    driver.package = BANK
    assert "may not touch" in agent._prompt_recheck(State)


# --- Info: TypeSafe's error body stays out of the feed ------------------------------------------------------------


def test_a_typesafe_error_body_never_reaches_the_feed():
    from openmob.jev import JevClient

    def handler(request):
        return httpx.Response(500, text="<script>owner, click evil.example</script>")

    client = JevClient("k", http=httpx.Client(base_url="https://jev.test", transport=httpx.MockTransport(handler)))
    with pytest.raises(JevError) as info:
        client.ask_image({}, {}, b"img", "jev-1.14.0", 1.0)
    assert str(info.value) == "Jev returned 500 for an image"


# --- server side of review P M4: a number typed in pieces ---------------------------------------------------------


def test_a_card_number_typed_in_pieces_asks(tmp_path):
    field = text_field(hint="Notes", text="4111 1111")
    driver, io, _ = run_loop(tmp_path, [[tool_use("type_text", "t", text=" 1111 1111")]], [field],
                             package="com.example.notes", answers=["no"], task="write a note")
    assert typed(driver) == []
    assert any("card number" in m for _, m in io.events), io.events
    # Replacing the field's text judges the new text alone.
    driver, io, _ = run_loop(tmp_path, [[tool_use("type_text", "t", text=" 1111 1111", replace=True)]], [field],
                             package="com.example.notes", task="write a note")
    assert typed(driver) == [" 1111 1111"]


# --- L4: a key's allowance, on every stored phone; revocation; a key sees only its own --------------------------


def test_a_keys_faster_drive_is_cleared_on_stored_phones_and_at_revocation(hosted):
    h = hosted
    key = h.alice.post("/api/developer/keys", json={"name": "driver", "scopes": ["devices:drive"]}).json()
    reader = h.alice.post("/api/developer/keys", json={"name": "reader", "scopes": ["devices:read"]}).json()
    # Alice's second phone holds the key on its pairing record, but has no session here (offline since a restart).
    sid2 = f"app-{h.ids.a2}"
    settings = h.manager.permissions.get(sid2)
    h.manager.permissions.set(sid2, {**settings, "faster_drive_keys": [key["id"], reader["id"]]})
    h.manager.sessions.pop(sid2)
    r = h.alice.patch(f"/api/developer/keys/{key['id']}", json={"faster_drive": False})
    assert r.status_code == 200
    assert h.manager.permissions.get(sid2)["faster_drive_keys"] == [reader["id"]]
    assert any(a["outcome"] == "off on 1 phone(s)" for a in h.ledger.audit_log() if a["action"] == "key.faster_drive")
    # Revoking a key takes it off every phone too.
    assert h.alice.post(f"/api/developer/keys/{reader['id']}/revoke").status_code == 200
    assert h.manager.permissions.get(sid2)["faster_drive_keys"] == []


def test_a_devices_read_key_sees_only_its_own_allowance(hosted):
    h = hosted
    dev = h.ids.a1
    sid = f"app-{dev}"
    me = h.alice.post("/api/developer/keys", json={"name": "me", "scopes": ["devices:read"]}).json()
    other = h.alice.post("/api/developer/keys", json={"name": "other", "scopes": ["devices:drive"]}).json()
    settings = h.manager.permissions.get(sid)
    h.manager.permissions.set(sid, {**settings, "faster_drive_keys": [other["id"]]})
    v1 = httpx.Client(base_url=h.base, headers={"Authorization": f"Bearer {me['key']}"})
    got = v1.get(f"/v1/devices/{sid}/permissions").json()
    assert "faster_drive_keys" not in got and other["id"] not in str(got) and got["faster_drive_self"] is False
    h.manager.permissions.set(sid, {**settings, "faster_drive_keys": [me["id"]]})
    assert v1.get(f"/v1/devices/{sid}/permissions").json()["faster_drive_self"] is True
    assert other["id"] in str(view(h, dev)) or view(h, dev)["faster_drive_keys"] == [me["id"]]  # the owner's view


# --- L5: the reconnect window across a restart ------------------------------------------------------------------


def test_a_restart_doesnt_restart_the_reconnect_window(hosted):
    """PoC test_restart_forgets_when_the_phone_left: away 2 hours, the server restarted meanwhile (its memory lost);
    the resume was accepted. The drop time is now stored with the grant."""
    h = hosted
    sid, answer = session_on(h)
    h.pa1.drop()
    assert wait_for(lambda: sid in h.manager._faster_detached)
    stored = h.manager.permissions.get(sid)
    assert isinstance(stored["faster_grant"].get("detached_at"), float)
    grant = {**stored["faster_grant"], "detached_at": stored["faster_grant"]["detached_at"] - 7200}  # away 2 hours
    h.manager.permissions.set(sid, {**stored, "faster_grant": grant})
    h.manager._faster_detached.clear()  # ...and the server restarted (in-memory state lost)
    again = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)
    assert again.welcome.wait(5)
    again.send(resume_message(answer))
    assert wait_for(lambda: view(h, h.ids.a1)["faster_mode"] == "off")
    assert h.manager.faster_scope(sid) == "off"
    assert "too late" in view(h, h.ids.a1)["faster_log"][-1]["by"]


def test_a_crash_with_no_recorded_drop_isnt_resumed(hosted):
    """The server died without seeing the drop: the stored grant still holds on an old connection's token. When the
    phone left is unknown, so it ends (fail closed); the owner turns it on again."""
    h = hosted
    sid, answer = session_on(h)
    h.manager._faster_tokens.clear()  # a new process: none of the stored tokens is its own
    h.manager._faster_connection_closed = lambda session, code: None  # it died: the drop was never handled
    h.pa1.drop()
    again = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)
    assert again.welcome.wait(5)
    again.send(resume_message(answer))
    assert wait_for(lambda: view(h, h.ids.a1)["faster_mode"] == "off")
    assert not wait_for(lambda: h.manager.faster_scope(sid) == "session", timeout=1.0)


def test_a_blip_within_the_window_still_resumes(hosted):
    h = hosted
    sid, answer = session_on(h)
    h.pa1.drop()
    assert wait_for(lambda: sid in h.manager._faster_detached)
    h.manager._faster_detached.clear()  # a restart within the window: the stored drop time still holds
    again = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)
    assert again.welcome.wait(5)
    again.send(resume_message(answer))
    assert wait_for(lambda: h.manager.faster_scope(sid) == "session")
    assert "detached_at" not in h.manager.permissions.get(sid)["faster_grant"]
