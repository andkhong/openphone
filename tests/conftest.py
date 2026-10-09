import io
from types import SimpleNamespace

import pytest
from PIL import Image

from openmob.phone.adb import Screenshot


def make_png(width=1080, height=2400, color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


class FakeDriver:
    """Stands in for PhoneDriver; records actions."""

    def __init__(self, packages=None, elements=None, device=(1080, 2400), image=(540, 1200), labels=None):
        self.packages = packages or ["com.android.settings", "com.google.android.youtube"]
        self.labels = labels or {}
        self.label_reads = 0
        self.elements = elements or []
        self.device = device
        self.image = image
        self.actions = []
        self.package = "com.android.launcher3"

    def screenshot(self, max_age=0.0, attempts=2):
        buf = io.BytesIO()
        Image.new("RGB", self.image, "white").save(buf, "JPEG")
        return Screenshot(buf.getvalue(), *self.image, *self.device)

    def ui_elements(self):
        return list(self.elements)

    def current_app(self):
        return self.package

    def launchable_packages(self):
        return list(self.packages)

    def app_labels(self, packages):
        self.label_reads += 1
        return {p: self.labels.get(p) for p in packages}

    def wake_unlock(self):
        self.actions.append(("wake",))

    def launch_app(self, package):
        self.actions.append(("launch", package))
        self.package = package

    def open_url(self, url):
        self.actions.append(("url", url))

    # Settings deep links: off by default (an older app); a test turns it on to be offered open_settings_page.
    can_open_settings = False

    def open_settings(self, page, package=None):
        self.actions.append(("open_settings", page, package))
        self.package = "com.android.settings"
        return {"package": "com.android.settings"}

    def tap(self, x, y):
        self.actions.append(("tap", x, y))

    def long_press(self, x, y, duration_ms=800):
        self.actions.append(("long_press", x, y))

    def swipe(self, x1, y1, x2, y2, duration_ms=300):
        self.actions.append(("swipe", x1, y1, x2, y2, duration_ms))

    def type_text(self, text, replace=False):
        self.actions.append(("type", text))
        self.replaced = getattr(self, "replaced", []) + [replace]

    def key(self, name):
        self.actions.append(("key", name))


def tool_use(name, tool_id="t1", **inputs):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=inputs)


def text_block(text):
    return SimpleNamespace(type="text", text=text)


class ScriptedClient:
    """Fake anthropic client that replays canned responses and records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        import copy

        self.requests.append(copy.deepcopy(kwargs))
        content = self.responses.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
        return SimpleNamespace(content=content, stop_reason=stop)


class RecordingIO:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.questions = []
        self.events = []

    def ask(self, question):
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else "no"

    def event(self, kind, message):
        self.events.append((kind, message))


@pytest.fixture
def fake_driver():
    return FakeDriver()


def system_text(request) -> str:
    """The system prompt of a recorded Anthropic request (sent as one cached text block)."""
    system = request["system"]
    return system if isinstance(system, str) else "".join(b["text"] for b in system)
