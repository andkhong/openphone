"""IOSDriver against a fake WebDriverAgent (no iPhone or Xcode needed)."""

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from openmob.phone.base import DeviceError
from openmob.phone.ios import IOSDriver, parse_wda_source

# An iPhone 15-ish screen: 393x852 points at 3x = 1179x2556 pixels.
POINTS = (393, 852)
SCALE = 3

SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Settings" label="Settings" x="0" y="0" width="393" height="852" visible="true">
  <XCUIElementTypeButton type="XCUIElementTypeButton" name="Back" label="Settings" x="8" y="54" width="90" height="44" visible="true"/>
  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" name="Display &amp; Brightness" label="Display &amp; Brightness" x="20" y="120" width="200" height="22" visible="true"/>
  <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" name="Dark Mode" label="Dark Mode" value="0" x="320" y="200" width="51" height="31" visible="true"/>
  <XCUIElementTypeButton type="XCUIElementTypeButton" name="Hidden" label="Hidden" x="0" y="900" width="10" height="10" visible="false"/>
  <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="393" height="852" visible="true"/>
</XCUIElementTypeApplication>"""


def png(width, height):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


class FakeWDA:
    def __init__(self):
        self.calls = []
        self.session_valid = True
        self.active_type = "XCUIElementTypeTextField"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        ok = lambda value=None: httpx.Response(200, json={"value": value})  # noqa: E731
        if path == "/session" and request.method == "POST":
            self.session_valid = True
            return ok({"sessionId": "S1"})
        if path.startswith("/session/S1") and not self.session_valid:
            return httpx.Response(404, json={"value": {"error": "invalid session id", "message": "Session does not exist"}})
        if path == "/screenshot":
            return ok(png(POINTS[0] * SCALE, POINTS[1] * SCALE))
        if path.endswith("/window/size"):
            return ok({"width": POINTS[0], "height": POINTS[1]})
        if path.endswith("/source?format=xml"):
            return ok(SOURCE)
        if path == "/wda/activeAppInfo":
            return ok({"bundleId": "com.apple.Preferences"})
        if path.endswith("/element/active"):
            return ok({"element-6066-11e4-a52e-4f735466cecf": "E1"})
        if path.endswith("/element/E1/attribute/type"):
            return ok(self.active_type)
        if path == "/wda/device/info":
            return ok({"uuid": "UDID-1", "model": "iPhone", "name": "An's iPhone"})
        if path == "/status":
            return ok({"os": {"name": "iOS", "version": "26.0"}})
        return ok()


@pytest.fixture
def wda():
    fake = FakeWDA()
    driver = IOSDriver(http=httpx.Client(base_url="http://wda", transport=httpx.MockTransport(fake)))
    return driver, fake


def test_screenshot_is_downscaled_and_keeps_real_size(wda):
    driver, _ = wda
    shot = driver.screenshot()
    assert (shot.device_width, shot.device_height) == (1179, 2556)
    assert max(shot.width, shot.height) == 1280 and shot.jpeg[:2] == b"\xff\xd8"
    assert driver.scale() == 3.0


def test_tap_and_swipe_convert_pixels_to_points(wda):
    driver, fake = wda
    driver.tap(600, 300)
    actions = [b for m, p, b in fake.calls if p == "/session/S1/actions"][-1]["actions"][0]["actions"]
    assert actions[0] == {"type": "pointerMove", "duration": 0, "x": 200.0, "y": 100.0}
    assert [a["type"] for a in actions] == ["pointerMove", "pointerDown", "pause", "pointerUp"]
    driver.swipe(0, 1500, 0, 600, 400)
    swipe = [b for m, p, b in fake.calls if p == "/session/S1/actions"][-1]["actions"][0]["actions"]
    assert swipe[0]["y"] == 500.0 and swipe[2] == {"type": "pointerMove", "duration": 400, "x": 0.0, "y": 200.0}


def test_ui_elements_from_page_source_in_pixels(wda):
    driver, _ = wda
    elements = driver.ui_elements()
    labels = [e.label for e in elements]
    assert labels == ["Settings", "Display & Brightness", "Dark Mode"]  # hidden and unlabeled skipped
    back, _, switch = elements
    assert back.cls == "Button" and back.clickable and back.desc == "Back"
    assert back.bounds == (24, 162, 294, 294)  # points × 3
    assert switch.checked is False and switch.center == (1036, 646)


def test_parse_handles_garbage():
    assert parse_wda_source("not xml", 3) == []
    assert parse_wda_source("", 3) == []


def test_apps_keys_and_info(wda):
    driver, fake = wda
    assert driver.current_app() == "com.apple.Preferences"
    driver.launch_app("com.google.ios.youtube")
    assert ("POST", "/session/S1/wda/apps/launch", {"bundleId": "com.google.ios.youtube"}) in fake.calls
    driver.key("home")
    assert ("POST", "/wda/homescreen", None) in fake.calls
    driver.key("ENTER")
    assert ("POST", "/session/S1/wda/keys", {"value": ["\n"]}) in fake.calls
    with pytest.raises(DeviceError, match="no Back key"):
        driver.key("BACK")
    with pytest.raises(DeviceError, match="isn't available on iOS"):
        driver.key("APP_SWITCH")
    driver.open_url("https://wikipedia.org")
    assert ("POST", "/session/S1/url", {"url": "https://wikipedia.org"}) in fake.calls
    assert driver.device_info() == {"id": "UDID-1", "model": "iPhone", "manufacturer": "Apple", "platform": "ios", "ios": "26.0"}


def test_typing_refuses_password_fields(wda):
    driver, fake = wda
    driver.type_text("hello")
    assert ("POST", "/session/S1/wda/keys", {"value": list("hello")}) in fake.calls
    fake.active_type = "XCUIElementTypeSecureTextField"
    with pytest.raises(DeviceError, match="password"):
        driver.type_text("secret")


def test_expired_session_is_reopened_once(wda):
    driver, fake = wda
    driver.launch_app("com.apple.Preferences")
    fake.session_valid = False  # WebDriverAgent restarted
    original = fake.__call__

    def flaky(request):
        if request.url.path == "/session" and request.method == "POST":
            fake.session_valid = True
        return original(request)

    driver._http = httpx.Client(base_url="http://wda", transport=httpx.MockTransport(flaky))
    driver.launch_app("com.apple.Preferences")  # 404 invalid session → new session → retried
    assert sum(1 for m, p, _ in fake.calls if p == "/session") == 2


def test_unreachable_wda_is_a_device_error():
    def down(request):
        raise httpx.ConnectError("refused")

    driver = IOSDriver(http=httpx.Client(base_url="http://wda", transport=httpx.MockTransport(down)))
    with pytest.raises(DeviceError, match="unreachable"):
        driver.screenshot(attempts=1)
