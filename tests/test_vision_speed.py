"""Vision-step speedups: cache-friendly history, right-sized screenshots, short replies, streaming,
and the per-step token fields that measure them."""

import copy
import hashlib
import io
import json
from types import SimpleNamespace

from conftest import FakeDriver, RecordingIO, tool_use
from PIL import Image

from openmob.agent.loop import OMITTED, Agent
from openmob.agent.models import AnthropicAdapter, ModelRequest
from openmob.config import Config
from openmob.history import History
from openmob.phone.adb import Screenshot, UIElement
from openmob.phone.apps import AppResolver
from openmob.runner import Coordinator
from openmob.timing_report import format_report, summarize
from openmob.voice.tts import Speaker

IMAGE_TOKENS = 1500
LOOKBACK = 20  # Anthropic checks this many blocks before a breakpoint for an earlier cache entry


def _units(kwargs) -> list[tuple[str, int]]:
    """A request as the cache sees it: tools, system, then every message block, in order. Each unit
    is (hash of its content without cache_control, tokens, marked)."""
    out = []

    def add(block, role=""):
        marked = isinstance(block, dict) and "cache_control" in block
        clean = {k: v for k, v in block.items() if k != "cache_control"} if isinstance(block, dict) else block
        text = json.dumps([role, clean], sort_keys=True)
        tokens = IMAGE_TOKENS if isinstance(clean, dict) and clean.get("type") == "image" else max(len(text) // 4, 1)
        if isinstance(clean, dict) and clean.get("type") == "tool_result":
            tokens += sum(IMAGE_TOKENS for b in clean.get("content") or [] if b.get("type") == "image")
        out.append((hashlib.sha256(text.encode()).hexdigest(), tokens, marked))

    for tool in kwargs.get("tools") or []:
        add(tool)
    for block in kwargs.get("system") or []:
        add(block)
    for message in kwargs["messages"]:
        content = message["content"]
        for block in [content] if isinstance(content, str) else content:
            add(block, message["role"])
    return out


class CachingClient:
    """A fake Anthropic client that replays scripted replies and reports usage the way prompt caching
    does: a request reads the longest prefix an earlier request wrote at one of its breakpoints
    (looking back up to 20 blocks from each of this request's breakpoints), and writes a cache entry
    at each of its own breakpoints."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.usages = []
        self.cache: set[str] = set()
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        units = _units(kwargs)
        prefixes, h = [], hashlib.sha256()
        for unit_hash, _, _ in units:
            h.update(unit_hash.encode())
            prefixes.append(h.copy().hexdigest())
        tokens = [t for _, t, _ in units]
        marks = [i for i, (_, _, m) in enumerate(units) if m]
        read_end = -1
        for b in marks:
            for i in range(b, max(b - LOOKBACK, -1), -1):
                if prefixes[i] in self.cache:
                    read_end = max(read_end, i)
                    break
        cache_read = sum(tokens[: read_end + 1])
        written = sum(tokens[: marks[-1] + 1]) - cache_read if marks and marks[-1] > read_end else 0
        for b in marks:
            self.cache.add(prefixes[b])
        usage = SimpleNamespace(input_tokens=sum(tokens) - cache_read - written, output_tokens=20,
                                cache_read_input_tokens=cache_read, cache_creation_input_tokens=written)
        self.usages.append(usage)
        content = self.responses.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
        return SimpleNamespace(content=content, stop_reason=stop, usage=usage, model=kwargs["model"])


def uncached(value):
    """A request without its cache marks."""
    if isinstance(value, dict):
        return {k: uncached(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [uncached(v) for v in value]
    return value


def labeled_elements(n=10):
    return [UIElement(i, f"Item {i}", "", "", "TextView", True, None, (0, 100 * i, 1080, 100 * i + 90))
            for i in range(n)]


def make_agent(client, tmp_path, driver=None, io=None, **cfg):
    driver = driver or FakeDriver()
    config = Config(settle_delay=0, launch_settle_delay=0, fast_path="off", **cfg)
    return Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"), config, io or RecordingIO(),
                 use_ocr=False)


def scroll_script(n):
    return [[tool_use("scroll", f"s{i}", direction="down")] for i in range(n)] + [
        [tool_use("done", "d", summary="Done.", success=True)]]


# --- 1. cache-friendly history -----------------------------------------------------


def test_batch_trimming_keeps_earlier_messages_byte_identical(tmp_path):
    client = CachingClient(scroll_script(7))
    make_agent(client, tmp_path).run("scroll a lot")
    stripped = [uncached(r["messages"]) for r in client.requests]
    # Each request starts with the previous one's messages, except right after a batch trim (every
    # third step once there are 4 screenshots: 4 -> 2, then 3, 4, 5 -> 2 ...).
    extends = [stripped[i][:len(stripped[i - 1])] == stripped[i - 1] for i in range(1, len(stripped))]
    assert extends == [True, True, True, False, True, True, False]
    for request in client.requests:
        images = sum(1 for m in request["messages"] if isinstance(m["content"], list)
                     for b in m["content"] if b["type"] == "image")
        assert 1 <= images <= 4  # between keep_screenshots and history_max_screenshots
    assert OMITTED in json.dumps(stripped[-1])


def test_rolling_trimming_is_still_available(tmp_path):
    client = CachingClient(scroll_script(4))
    make_agent(client, tmp_path, history_trim="rolling", history_cache=False).run("scroll a lot")
    for request in client.requests:
        images = sum(1 for m in request["messages"] if isinstance(m["content"], list)
                     for b in m["content"] if b["type"] == "image")
        assert images <= 2
        assert "cache_control" not in json.dumps(request["messages"])


def test_history_is_read_from_the_cache_on_later_steps(tmp_path):
    def run(**cfg):
        client = CachingClient(scroll_script(7))
        agent = make_agent(client, tmp_path, **cfg)
        agent.run("scroll a lot")
        return client, agent

    cached, agent = run()
    before, _ = run(history_trim="rolling", history_cache=False)
    prompt = lambda u: u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens  # noqa: E731
    # Before: only system and tools come from the cache; the conversation is new input every step.
    assert all(u.cache_read_input_tokens < prompt(before.usages[0]) for u in before.usages)
    # After: from the second step on, most of the prompt is a cache read (the trim steps re-write once).
    later = cached.usages[1:]
    hits = [u for u in later if u.cache_read_input_tokens >= prompt(cached.usages[0])]
    assert len(hits) >= len(later) - 2
    assert sum(u.input_tokens for u in cached.usages) < sum(u.input_tokens for u in before.usages)
    assert agent.usage.model_cache_read == sum(u.cache_read_input_tokens for u in cached.usages)


def test_breakpoints_stay_within_anthropics_limit_and_off_the_agents_history():
    messages = [{"role": "user", "content": "Request: x"},
                {"role": "assistant", "content": [{"type": "tool_call", "id": "a", "name": "wait", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "call_id": "a", "content": [{"type": "text", "text": "ok"}]},
                                             {"type": "text", "text": "SCREEN: …"}]}]
    original = copy.deepcopy(messages)
    tools = [{"name": "wait", "description": "d", "parameters": {"type": "object"}}]
    kwargs = AnthropicAdapter(None).request_kwargs(ModelRequest("m", "sys", tools, messages, cache_history=True))
    assert json.dumps(kwargs).count("cache_control") == 4
    assert kwargs["messages"][0]["content"][-1]["cache_control"] and kwargs["messages"][2]["content"][-1]["cache_control"]
    assert messages == original
    plain = AnthropicAdapter(None).request_kwargs(ModelRequest("m", "sys", tools, messages))
    assert json.dumps(plain["messages"]).count("cache_control") == 0


# --- 2. right-sized screenshots --------------------------------------------------------


def sent_image(request) -> Image.Image:
    import base64

    blocks = [b for m in request["messages"] if isinstance(m["content"], list) for b in m["content"]]
    return Image.open(io.BytesIO(base64.b64decode([b for b in blocks if b["type"] == "image"][-1]["source"]["data"])))


def test_downscale_keeps_the_device_mapping():
    from openmob.phone.adb import downscale

    buf = io.BytesIO()
    Image.new("RGB", (576, 1280), "white").save(buf, "JPEG")
    full = Screenshot(buf.getvalue(), 576, 1280, 1080, 2400)
    small = downscale(full, 768)
    assert (small.width, small.height) == (346, 768) and Image.open(io.BytesIO(small.jpeg)).size == (346, 768)
    assert (small.device_width, small.device_height) == (1080, 2400)
    assert small.to_device(173, 384) == (540, 1200) and small.to_device(345, 767) == (1077, 2397)
    assert small.to_image(540, 1200) == (173, 384)
    assert downscale(full, 2000) is full and downscale(full, 0) is full


def test_labeled_screens_get_a_smaller_screenshot_and_taps_map_back(tmp_path):
    driver = FakeDriver(elements=labeled_elements(10), image=(576, 1280))
    client = CachingClient([[tool_use("tap", "t", x=173, y=384, target="middle")],
                            [tool_use("done", "d", summary="Done.", success=True)]])
    agent = make_agent(client, tmp_path, driver=driver)
    agent.run("tap the middle")
    assert sent_image(client.requests[0]).size == (346, 768)
    screen = next(b["text"] for b in client.requests[0]["messages"][0]["content"]
                  if b["type"] == "text" and b["text"].startswith("SCREEN:"))
    # Element centers are given in the small image: [1] at device (540, 145) -> (173, 46).
    assert "screenshot is 346x768 px" in screen and "[1] TextView 'Item 1' @ (173,46)" in screen
    assert ("tap", 540, 1200) in driver.actions


def test_games_ocr_and_sparse_screens_keep_the_full_screenshot(tmp_path):
    for elements in ([], labeled_elements(5),
                     labeled_elements(10) + [UIElement(10, "Score", "", "", "OCRText", True, None, (0, 0, 50, 50))]):
        driver = FakeDriver(elements=elements, image=(576, 1280))
        client = CachingClient([[tool_use("tap", "t", x=288, y=640, target="middle")],
                                [tool_use("done", "d", summary="Done.", success=True)]])
        make_agent(client, tmp_path, driver=driver).run("tap the middle")
        assert sent_image(client.requests[0]).size == (576, 1280)
        assert ("tap", 540, 1200) in driver.actions


def test_right_sizing_can_be_switched_off(tmp_path):
    driver = FakeDriver(elements=labeled_elements(10), image=(576, 1280))
    client = CachingClient([[tool_use("done", "d", summary="Done.", success=True)]])
    make_agent(client, tmp_path, driver=driver, vision_max_side_labeled=0).run("look")
    assert sent_image(client.requests[0]).size == (576, 1280)


# --- 3. short replies ----------------------------------------------------------------


def test_vision_steps_ask_for_brief_replies_with_a_smaller_cap(tmp_path):
    from conftest import system_text, text_block

    client = CachingClient([[text_block("Settings is open."), tool_use("done", "d", summary="Done.", success=True)]])
    io = RecordingIO()
    make_agent(client, tmp_path, io=io).run("open settings")
    assert client.requests[0]["max_tokens"] == 512
    assert "Reply briefly" in system_text(client.requests[0])
    assert ("thought", "Settings is open.") in io.events  # the dashboard still gets the thought

    client = CachingClient([[tool_use("done", "d", summary="Done.", success=True)]])
    make_agent(client, tmp_path, vision_brief=False, vision_max_tokens=0).run("open settings")
    assert client.requests[0]["max_tokens"] == 1024 and "Reply briefly" not in system_text(client.requests[0])


def test_a_reply_cut_off_by_the_cap_is_asked_again_in_full(tmp_path):
    class Truncating(CachingClient):
        def create(self, **kwargs):
            response = super().create(**kwargs)
            if kwargs["max_tokens"] == 512:
                response.stop_reason = "max_tokens"
                response.content = [tool_use("type_text", "x", text="half a long tex")]
            return response

    client = Truncating([[], [tool_use("done", "d", summary="Done.", success=True)]])
    agent = make_agent(client, tmp_path)
    result = agent.run("write a long note")
    assert result.success and [r["max_tokens"] for r in client.requests] == [512, 1024]
    assert ("type", "half a long tex") not in agent.driver.actions
    assert agent.usage.model_calls == 2  # both calls are counted


# --- 5. per-step tokens --------------------------------------------------------------


def test_steps_record_their_model_tokens_and_image_side(tmp_path):
    import time

    c = Coordinator(Speaker(enabled=False), history=History(tmp_path / "h.sqlite"))
    c.device_id = "phone-1"
    client = CachingClient(scroll_script(2))
    c.agent = make_agent(client, tmp_path, io=c)
    c.start()
    task = c.enqueue("scroll", "dashboard")
    deadline = time.time() + 5
    while (c.task(task) or {}).get("status") not in ("succeeded", "failed") and time.time() < deadline:
        time.sleep(0.01)
    steps = c.history.steps(None, task)
    assert len(steps) == 3
    for step, usage in zip(steps, client.usages):
        assert step["input_tokens"] == usage.input_tokens and step["output_tokens"] == 20
        assert step["cache_read_tokens"] == usage.cache_read_input_tokens
        assert step["cache_write_tokens"] == usage.cache_creation_input_tokens
        assert step["vision_image_side"] == 1200  # FakeDriver's 540x1200 screenshot
    report = format_report(summarize(c.history.step_timings()))
    assert "tokens per model step (3)" in report and "screenshot 1200 px" in report
