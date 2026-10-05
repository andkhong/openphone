"""iPhone driver: controls an iPhone through WebDriverAgent (Apple's XCUITest, as used by Appium).

iOS doesn't let apps control other apps, so this runs on a Mac with the iPhone attached over USB
(or paired over Wi-Fi) and WebDriverAgent installed on it with Xcode. It implements the same
Device interface as the adb driver, so `openmob bridge --ios` connects an iPhone to any OpenMob
server exactly like an Android phone.

Coordinates: the agent works in screenshot pixels, like on Android. WebDriverAgent works in
iOS points (1/3 of a pixel on recent iPhones), so taps and element bounds are converted here.

Installed-app listing needs `pymobiledevice3` over USB (WebDriverAgent can't list apps); without
it the agent opens apps through Spotlight search.
"""

from __future__ import annotations

import base64
import io
import xml.etree.ElementTree as ET

import httpx
from PIL import Image

from .adb import Screenshot, UIElement
from .base import CachedScreenshots, DeviceError

DEFAULT_WDA_URL = "http://localhost:8100"

# Element types worth tapping even without a label.
TAPPABLE = {
    "Button", "Cell", "Link", "TextField", "SecureTextField", "SearchField", "TextView", "Switch",
    "Toggle", "Slider", "Tab", "Icon", "MenuItem", "SegmentedControl", "PickerWheel", "Image",
}
# Key names the agent may use (shared vocabulary with Android) → what iOS can do with them.
BUTTONS = {"VOLUME_UP": "volumeUp", "VOLUME_DOWN": "volumeDown"}
KEYS = {"ENTER": "\n", "DELETE": "\b", "TAB": "\t"}


def parse_wda_source(xml_text: str, scale: float, limit: int = 150) -> list[UIElement]:
    """Turn WebDriverAgent's XML page source (points) into UIElements (pixels)."""
    start = xml_text.find("<")
    if start < 0:
        return []
    try:
        root = ET.fromstring(xml_text[start:])
    except ET.ParseError:
        return []
    elements: list[UIElement] = []
    for node in root.iter():
        if node.get("visible") == "false" or node.get("x") is None:
            continue
        cls = (node.get("type") or node.tag).removeprefix("XCUIElementType")
        if cls in {"Application", "Window"}:
            continue  # the whole screen, not something to tap
        if cls in {"Other", "Keyboard", "StatusBar"} and not node.get("label"):
            continue
        label = (node.get("label") or "").strip()
        name = (node.get("name") or "").strip()
        value = (node.get("value") or "").strip()
        clickable = cls in TAPPABLE
        text = label or value
        if not (text or name or clickable):
            continue
        try:
            x, y = float(node.get("x")), float(node.get("y"))
            w, h = float(node.get("width")), float(node.get("height"))
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        checked = None
        if cls in {"Switch", "Toggle"} and value in {"0", "1"}:
            checked = value == "1"
            text = label
        elements.append(
            UIElement(
                index=len(elements),
                text=text,
                desc=name if name and name != text else "",
                resource_id="",
                cls=cls,
                clickable=clickable,
                checked=checked,
                bounds=(round(x * scale), round(y * scale), round((x + w) * scale), round((y + h) * scale)),
            )
        )
        if len(elements) >= limit:
            break
    return elements


class IOSDriver(CachedScreenshots):
    def __init__(
        self,
        wda_url: str = DEFAULT_WDA_URL,
        udid: str | None = None,
        max_side: int = 1280,
        jpeg_quality: int = 80,
        http: httpx.Client | None = None,
    ):
        self.wda_url = wda_url.rstrip("/")
        self.udid = udid
        self.max_side = max_side
        self.jpeg_quality = jpeg_quality
        self._http = http or httpx.Client(base_url=self.wda_url, timeout=30)
        self._session: str | None = None
        self._scale: float | None = None  # screenshot pixels per iOS point
        self._init_screenshot_cache()

    # --- WebDriverAgent plumbing -----------------------------------------

    def _request(self, method: str, path: str, json: dict | None = None, timeout: float = 30) -> object:
        try:
            response = self._http.request(method, path, json=json, timeout=timeout)
        except httpx.HTTPError as exc:
            raise DeviceError(f"WebDriverAgent unreachable at {self.wda_url}: {exc}") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        value = data.get("value") if isinstance(data, dict) else None
        if response.status_code >= 400 or (isinstance(value, dict) and value.get("error")):
            error = value if isinstance(value, dict) else {}
            message = error.get("message") or error.get("error") or response.text[:200]
            raise DeviceError(f"WebDriverAgent: {message}")
        return value

    def _sessioned(self, method: str, path: str, json: dict | None = None, timeout: float = 30) -> object:
        """Call a session endpoint, opening (or reopening an expired) session as needed."""
        for attempt in range(2):
            if self._session is None:
                created = self._request("POST", "/session", {"capabilities": {"alwaysMatch": {}}})
                self._session = (created or {}).get("sessionId") if isinstance(created, dict) else None
                if not self._session:
                    raise DeviceError("WebDriverAgent did not open a session")
            try:
                return self._request(method, f"/session/{self._session}{path}", json, timeout)
            except DeviceError as exc:
                if attempt == 0 and "session" in str(exc).lower():
                    self._session = None  # expired after WDA restart: open a new one and retry once
                    continue
                raise
        raise DeviceError("WebDriverAgent session failed")

    def _points(self, x: int, y: int) -> tuple[float, float]:
        scale = self.scale()
        return round(x / scale, 1), round(y / scale, 1)

    def scale(self) -> float:
        """Pixels per point, from a screenshot's width over the window's width in points."""
        if self._scale is None:
            size = self._sessioned("GET", "/window/size") or {}
            shot = self.screenshot()
            width_points = float(size.get("width") or 0)
            self._scale = shot.device_width / width_points if width_points else 3.0
        return self._scale

    def _touch(self, steps: list[dict]) -> None:
        self._sessioned("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger1", "parameters": {"pointerType": "touch"}, "actions": steps,
        }]})

    # --- observation -----------------------------------------------------

    def _capture(self) -> Screenshot:
        encoded = self._request("GET", "/screenshot", timeout=30)
        if not isinstance(encoded, str):
            raise DeviceError("WebDriverAgent returned no screenshot")
        img = Image.open(io.BytesIO(base64.b64decode(encoded)))
        img.load()
        dw, dh = img.size
        ratio = min(1.0, self.max_side / max(dw, dh))
        if ratio < 1.0:
            img = img.resize((round(dw * ratio), round(dh * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=self.jpeg_quality)
        return Screenshot(buf.getvalue(), img.width, img.height, dw, dh)

    def ui_elements(self) -> list[UIElement]:
        try:
            source = self._sessioned("GET", "/source?format=xml", timeout=20)
            return parse_wda_source(source if isinstance(source, str) else "", self.scale())
        except DeviceError:
            return []

    def current_app(self) -> str | None:
        try:
            info = self._request("GET", "/wda/activeAppInfo") or {}
        except DeviceError:
            return None
        return info.get("bundleId") if isinstance(info, dict) else None

    # --- apps ------------------------------------------------------------

    def _installed_apps(self) -> dict[str, str]:
        """bundle id → display name, over USB with pymobiledevice3 (optional dependency)."""
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.installation_proxy import InstallationProxyService
        except ImportError:
            return {}
        try:
            lockdown = create_using_usbmux(serial=self.udid)
            apps = InstallationProxyService(lockdown=lockdown).get_apps(application_type="Any")
        except Exception:
            return {}
        names = {}
        for bundle_id, info in apps.items():
            if info.get("ApplicationType") == "Hidden":
                continue
            names[bundle_id] = info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle_id
        return names

    def launchable_packages(self) -> list[str]:
        return sorted(self._installed_apps())

    def app_labels(self, packages: list[str]) -> dict[str, str | None]:
        names = self._installed_apps()
        return {p: names.get(p) for p in packages}

    def launch_app(self, package: str) -> None:
        self._sessioned("POST", "/wda/apps/launch", {"bundleId": package})

    def open_url(self, url: str) -> None:
        self._sessioned("POST", "/url", {"url": url})

    # --- actions ---------------------------------------------------------

    def tap(self, x: int, y: int) -> None:
        px, py = self._points(x, y)
        self._touch([
            {"type": "pointerMove", "duration": 0, "x": px, "y": py},
            {"type": "pointerDown", "button": 0},
            {"type": "pause", "duration": 50},
            {"type": "pointerUp", "button": 0},
        ])

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        px, py = self._points(x, y)
        self._touch([
            {"type": "pointerMove", "duration": 0, "x": px, "y": py},
            {"type": "pointerDown", "button": 0},
            {"type": "pause", "duration": int(duration_ms)},
            {"type": "pointerUp", "button": 0},
        ])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        sx, sy = self._points(x1, y1)
        ex, ey = self._points(x2, y2)
        self._touch([
            {"type": "pointerMove", "duration": 0, "x": sx, "y": sy},
            {"type": "pointerDown", "button": 0},
            {"type": "pointerMove", "duration": int(duration_ms), "x": ex, "y": ey},
            {"type": "pointerUp", "button": 0},
        ])

    def type_text(self, text: str) -> None:
        active = None
        try:
            active = self._sessioned("GET", "/element/active")
        except DeviceError:
            pass
        element_id = next(iter(active.values()), None) if isinstance(active, dict) else None
        if element_id:
            kind = self._sessioned("GET", f"/element/{element_id}/attribute/type")
            if kind == "XCUIElementTypeSecureTextField":
                raise DeviceError("The focused field is a password field. Ask the user to type it themselves.")
        self._sessioned("POST", "/wda/keys", {"value": list(text)})

    def key(self, name: str) -> None:
        name = name.upper()
        if name == "HOME":
            self._request("POST", "/wda/homescreen")
        elif name in BUTTONS:
            self._sessioned("POST", "/wda/pressButton", {"name": BUTTONS[name]})
        elif name in KEYS:
            self._sessioned("POST", "/wda/keys", {"value": [KEYS[name]]})
        elif name in {"WAKEUP", "POWER"}:
            self.wake_unlock()
        elif name == "BACK":
            raise DeviceError("iOS has no Back key: tap the on-screen Back button (top left) or swipe from the left edge.")
        else:
            raise DeviceError(f"Key {name} isn't available on iOS (supported: HOME, ENTER, DELETE, TAB, VOLUME_UP, VOLUME_DOWN).")

    def wake_unlock(self) -> None:
        """Wake the screen. Phones with a passcode stay locked: the user must unlock them."""
        try:
            self._request("POST", "/wda/unlock")
        except DeviceError:
            pass

    def device_info(self) -> dict:
        """For the bridge's hello: a stable id, model, and iOS version."""
        info: dict = {}
        try:
            info = self._request("GET", "/wda/device/info") or {}
        except DeviceError:
            pass
        status: dict = {}
        try:
            status = self._request("GET", "/status") or {}
        except DeviceError:
            pass
        os_info = (status or {}).get("os") or {}
        return {
            "id": self.udid or info.get("uuid") or info.get("name") or self.wda_url,
            "model": info.get("model") or info.get("name") or "iPhone",
            "manufacturer": "Apple",
            "platform": "ios",
            "ios": os_info.get("version") or info.get("osVersion") or "",
        }
