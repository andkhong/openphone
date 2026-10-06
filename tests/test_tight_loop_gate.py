"""The tighter loop on a gatekeeper phone (tests/test_tight_loop.py has the rest).

The gatekeeper app OCRs the permission dialog Android 15 hides from accessibility (Samsung: no package, no event)
and lists it as OCRText elements, the title with resource id `ocr_permission_message`, over the app underneath. The
loop recognizes it and taps the owner's default with no model call; the tap is an ordinary act call, so the phone's
gate judges it (its permission-dialog rules allow the task's own app's notifications without a prompt). A "can't
see what's there" refusal (gate_screen_unstable) waits for the OCR to finish instead of costing a model step."""

import io as _io

from conftest import RecordingIO, ScriptedClient, tool_use
from PIL import Image

from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver
from openmob.phone.base import GateRefused
from openmob.phone.protocol import encode_element
from openmob.phone.remote import RemoteDriver


def el(i, text, rid="", cls="Button", covered=False):
    return UIElement(i, text, "", rid, cls, True, None, (40, 1200 + 150 * i, 1040, 1320 + 150 * i), covered=covered)


GAME = [el(0, "Play Bots"), el(1, "Play Online")]
DIALOG = [el(0, "Allow Chess.com to send you notifications?", "ocr_permission_message", "OCRText"),
          el(1, "Allow", cls="OCRText"), el(2, "Don't allow", cls="OCRText"),
          el(3, "Play Bots", covered=True), el(4, "Play Online", covered=True)]


class GateLink:
    """The phone's side: com.chess in front (current_app keeps naming it while the package-less dialog is up)."""

    closed = False

    def __init__(self, screen, unstable_taps=0, readable_after_reads=0):
        self.screen = screen
        self.calls = []
        self.unstable = unstable_taps
        self.blind_reads = readable_after_reads  # reads before the phone's OCR has the dialog

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "current_app":
            return "com.chess"
        if method == "ui_elements":
            if self.blind_reads > 0:
                self.blind_reads -= 1
                return [encode_element(e) for e in [el(0, "Play Bots", covered=True), el(1, "Play Online", covered=True)]]
            return [encode_element(e) for e in self.screen]
        if method == "screenshot":
            buf = _io.BytesIO()
            Image.new("RGB", (540, 1200), "white").save(buf, "JPEG")
            import base64
            return {"jpeg": base64.b64encode(buf.getvalue()).decode(), "width": 540, "height": 1200,
                    "device_width": 1080, "device_height": 2400}
        if method == "wait_idle":
            return {"idle": True, "waited_ms": 1}
        if method in ("launchable_packages",):
            return ["com.chess"]
        if method == "app_labels":
            return {"com.chess": "Chess"}
        if method == "wake_unlock":
            return None
        if method == "tap":
            if self.unstable:
                self.unstable -= 1
                self.blind_reads = 2
                raise GateRefused("gate_screen_unstable", "The phone can't see what's there. Look again.")
            hit = next(e for e in self.screen if e.bounds[1] <= params["y"] <= e.bounds[3] and not e.covered)
            if hit.text == "Allow":
                self.screen = GAME
            return None
        raise AssertionError(method)


def make(link, responses, tmp_path):
    driver = RemoteDriver(link)
    driver.capabilities = {"wait_idle", "gatekeeper"}
    io_ = RecordingIO()
    io_.steps = []
    io_.record_step = io_.steps.append
    client = ScriptedClient(responses)
    config = Config(settle_delay=0.05, launch_settle_delay=0.05, settle_quiet_ms=20, verify_taps=False)
    agent = Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"), config, io_, use_ocr=False)
    return agent, client, io_


def test_an_ocrd_dialog_is_answered_through_the_gate_with_no_model_call(tmp_path):
    link = GateLink(DIALOG)
    agent, client, io_ = make(link, [[tool_use("done", "a", summary="ok", success=True)]], tmp_path)
    agent.run("install chess and play 5 moves against a bot")
    taps = [p for m, p in link.calls if m == "tap"]
    assert len(taps) == 1 and taps[0]["mode"] == "auto"  # an ordinary act: the phone's gate judges it
    assert 1200 + 150 <= taps[0]["y"] <= 1320 + 150  # "Allow"
    assert io_.steps[0]["source"] == "auto_dialog" and io_.steps[0]["handled_by"] == "permission_rule"
    assert len(client.requests) == 1 and "Play Bots" in str(client.requests[0]["messages"])
    assert not io_.questions  # the server asked nobody


def test_cant_see_waits_for_the_phones_ocr_and_tries_once_more(tmp_path):
    link = GateLink(GAME, unstable_taps=1)
    agent, client, io_ = make(link, [
        [tool_use("tap_element", "a", index=0, target="Play Bots")],
        [tool_use("done", "b", summary="ok", success=True)],
    ], tmp_path)
    agent.run("play a bot")
    assert [m for m, _ in link.calls].count("tap") == 2 and not io_.steps[0]["is_error"]
    assert len(client.requests) == 2
