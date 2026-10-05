"""Model adapters: request and response conversion for each provider, against fake clients that
return payloads in the shape each provider documents. No live API calls."""

import base64
import json
from types import SimpleNamespace

import pytest
from conftest import FakeDriver, RecordingIO, tool_use

from openmob.agent.loop import Agent
from openmob.agent.models import (
    AnthropicAdapter, GeminiAdapter, ModelError, ModelRequest, ModelRouter, ModelUnavailable,
    OpenAIAdapter, OpenAICompatibleAdapter, model_listing,
)
from openmob.agent.tools import TOOL_SPECS
from openmob.config import Config, load_config, normalize_model
from openmob.phone.apps import AppResolver

JPEG = base64.b64encode(b"\xff\xd8fake-jpeg").decode()

# One finished step in the neutral shape the loop keeps: request + screen, a tool call, its result
# (an error) followed by the new screen, then a nudge string.
HISTORY = [
    {"role": "user", "content": [{"type": "text", "text": "Request: open settings"},
                                 {"type": "image", "media_type": "image/jpeg", "data": JPEG}]},
    {"role": "assistant", "content": [{"type": "text", "text": "Opening it."},
                                      {"type": "tool_call", "id": "call_1", "name": "launch_app", "input": {"app": "Settings"}}]},
    {"role": "user", "content": [{"type": "tool_result", "call_id": "call_1", "is_error": True,
                                  "content": [{"type": "text", "text": "No installed app clearly matches."}]},
                                 {"type": "text", "text": "SCREEN: foreground app launcher"},
                                 {"type": "image", "media_type": "image/jpeg", "data": JPEG}]},
]


def request(messages=HISTORY, model="m", tools=TOOL_SPECS[:2]):
    return ModelRequest(model=model, system="You operate a phone.", tools=tools, messages=messages, max_tokens=512)


class Recorder:
    """Records kwargs of one SDK method and returns a canned response."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


# --- Anthropic -----------------------------------------------------------------


def test_anthropic_request_caches_system_and_tools_and_converts_blocks():
    create = Recorder(SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Tapping."),
                 SimpleNamespace(type="tool_use", id="toolu_2", name="tap", input={"x": 1, "y": 2})],
        stop_reason="tool_use", model="claude-sonnet-5-5",
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=3000, cache_creation_input_tokens=0)))
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    response = AnthropicAdapter(client).complete(request())

    sent = create.calls[0]
    assert sent["system"] == [{"type": "text", "text": "You operate a phone.", "cache_control": {"type": "ephemeral"}}]
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in sent["tools"][0]
    assert sent["tools"][0]["input_schema"] == TOOL_SPECS[0]["parameters"]
    image = sent["messages"][0]["content"][1]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": JPEG}}
    assert sent["messages"][1]["content"][1] == {"type": "tool_use", "id": "call_1", "name": "launch_app", "input": {"app": "Settings"}}
    result = sent["messages"][2]["content"][0]
    assert result["tool_use_id"] == "call_1" and result["is_error"] is True
    assert result["content"] == [{"type": "text", "text": "No installed app clearly matches."}]

    assert response.text == ["Tapping."] and response.stop_reason == "tool_use"
    assert [(c.id, c.name, c.input) for c in response.tool_calls] == [("toolu_2", "tap", {"x": 1, "y": 2})]
    assert response.model == "claude-sonnet-5-5"
    assert (response.usage.input, response.usage.output, response.usage.cache_read) == (100, 20, 3000)


def test_anthropic_keeps_unknown_blocks_for_its_own_next_request():
    thinking = SimpleNamespace(type="thinking", model_dump=lambda exclude_none: {"type": "thinking", "thinking": "hm", "signature": "s"})
    block = AnthropicAdapter.block_in(thinking)
    assert block == {"type": "opaque", "provider": "anthropic", "data": {"type": "thinking", "thinking": "hm", "signature": "s"}}
    assert AnthropicAdapter.block_out(block)["type"] == "thinking"
    # Other providers skip it.
    assert OpenAIAdapter.messages("", [{"role": "assistant", "content": [block]}]) == [{"role": "assistant", "content": ""}]


# --- OpenAI ----------------------------------------------------------------------


def openai_response(message, finish="tool_calls", usage=None, model="gpt-5.5-2026-08-01"):
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], model=model, usage=usage)


def test_openai_request_uses_function_tools_tool_messages_and_image_urls():
    create = Recorder(openai_response(SimpleNamespace(
        content=None, refusal=None,
        tool_calls=[SimpleNamespace(id="call_9", type="function",
                                    function=SimpleNamespace(name="tap", arguments='{"x": 5, "y": 6, "target": "OK"}'))]),
        usage=SimpleNamespace(prompt_tokens=5000, completion_tokens=40, prompt_tokens_details=SimpleNamespace(cached_tokens=4096))))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    response = OpenAIAdapter(client).complete(request(model="gpt-5.5"))

    sent = create.calls[0]
    assert sent["model"] == "gpt-5.5" and sent["max_completion_tokens"] == 512 and "max_tokens" not in sent
    assert sent["tools"][0] == {"type": "function", "function": {
        "name": TOOL_SPECS[0]["name"], "description": TOOL_SPECS[0]["description"], "parameters": TOOL_SPECS[0]["parameters"]}}
    msgs = sent["messages"]
    assert msgs[0] == {"role": "system", "content": "You operate a phone."}
    assert msgs[1]["role"] == "user" and msgs[1]["content"][1] == {
        "type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{JPEG}"}}
    assert msgs[2] == {"role": "assistant", "content": "Opening it.", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "launch_app", "arguments": json.dumps({"app": "Settings"})}}]}
    # The tool result is a tool message (text only), and the new screen follows as a user message.
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": "Error: No installed app clearly matches."}
    assert msgs[4]["role"] == "user" and [p["type"] for p in msgs[4]["content"]] == ["text", "image_url"]

    assert response.stop_reason == "tool_use" and response.text == []
    assert [(c.id, c.name, c.input) for c in response.tool_calls] == [("call_9", "tap", {"x": 5, "y": 6, "target": "OK"})]
    assert response.model == "gpt-5.5-2026-08-01"
    assert (response.usage.input, response.usage.output, response.usage.cache_read) == (904, 40, 4096)


def test_openai_text_refusal_and_bad_arguments():
    plain = OpenAIAdapter.response(openai_response(SimpleNamespace(content="All done.", refusal=None, tool_calls=None), finish="stop"))
    assert plain.text == ["All done."] and plain.stop_reason == "end_turn" and plain.usage is None
    refused = OpenAIAdapter.response(openai_response(SimpleNamespace(content=None, refusal="I can't help with that.", tool_calls=None), finish="stop"))
    assert refused.stop_reason == "refusal" and refused.text == ["I can't help with that."]
    broken = OpenAIAdapter.response(openai_response(SimpleNamespace(content=None, refusal=None, tool_calls=[
        SimpleNamespace(id="c", function=SimpleNamespace(name="tap", arguments="{not json"))])))
    assert broken.tool_calls[0].input == {}  # the tool then reports the missing arguments
    long = OpenAIAdapter.response(openai_response(SimpleNamespace(content="", refusal=None, tool_calls=None), finish="length"))
    assert long.stop_reason == "max_tokens"


def test_openai_compatible_sends_max_tokens_and_uses_configured_capabilities():
    create = Recorder(openai_response(SimpleNamespace(content="ok", refusal=None, tool_calls=None), finish="stop"))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    OpenAICompatibleAdapter(client).complete(request(model="qwen3-vl"))
    assert create.calls[0]["max_tokens"] == 512 and "max_completion_tokens" not in create.calls[0]


# --- Gemini ----------------------------------------------------------------------


def gemini_response(parts, finish="STOP", usage=None):
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=parts), finish_reason=SimpleNamespace(name=finish))],
        model_version="gemini-3.5-pro-002", usage_metadata=usage, prompt_feedback=None)


def test_gemini_request_shape_and_thought_signatures_round_trip():
    signature = b"\x01sig"
    generate = Recorder(gemini_response(
        [SimpleNamespace(function_call=SimpleNamespace(id=None, name="tap", args={"x": 3, "y": 4}),
                         thought_signature=signature, thought=None, text=None)],
        usage=SimpleNamespace(prompt_token_count=3000, candidates_token_count=30, thoughts_token_count=200,
                              cached_content_token_count=2048)))
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    adapter = GeminiAdapter(client)
    response = adapter.complete(request(model="gemini-3.5-pro"))

    sent = generate.calls[0]
    assert sent["model"] == "gemini-3.5-pro"
    config = sent["config"]
    assert config["system_instruction"] == "You operate a phone." and config["max_output_tokens"] == 512
    assert config["automatic_function_calling"] == {"disable": True}
    decl = config["tools"][0]["function_declarations"][0]
    assert decl == {"name": TOOL_SPECS[0]["name"], "description": TOOL_SPECS[0]["description"],
                    "parameters_json_schema": TOOL_SPECS[0]["parameters"]}
    contents = sent["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[0]["parts"][1] == {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64decode(JPEG)}}
    assert contents[1]["parts"][1] == {"function_call": {"name": "launch_app", "args": {"app": "Settings"}, "id": "call_1"}}
    # The function response carries the function's name (Gemini matches on it) and the error.
    assert contents[2]["parts"][0] == {"function_response": {
        "name": "launch_app", "response": {"error": "No installed app clearly matches."}, "id": "call_1"}}
    assert "inline_data" in contents[2]["parts"][2]

    assert response.stop_reason == "tool_use" and response.model == "gemini-3.5-pro-002"
    call = response.content[0]
    assert call["name"] == "tap" and call["input"] == {"x": 3, "y": 4} and call["id"].startswith("gemini_")
    assert (response.usage.input, response.usage.output, response.usage.cache_read) == (952, 230, 2048)

    # Next request: the call goes back with its signature and without our made-up id.
    follow = [*HISTORY, {"role": "assistant", "content": response.content},
              {"role": "user", "content": [{"type": "tool_result", "call_id": call["id"], "content": [{"type": "text", "text": "Tapped."}]}]}]
    contents = GeminiAdapter.contents(follow)
    assert contents[3]["parts"][0] == {"function_call": {"name": "tap", "args": {"x": 3, "y": 4}}, "thought_signature": signature}
    assert contents[4]["parts"][0] == {"function_response": {"name": "tap", "response": {"output": "Tapped."}}}


def test_gemini_text_safety_and_nudge_string():
    done = GeminiAdapter.response(gemini_response([SimpleNamespace(text="Finished.", function_call=None, thought=None, thought_signature=None)]))
    assert done.text == ["Finished."] and done.stop_reason == "end_turn" and done.usage is None
    blocked = GeminiAdapter.response(gemini_response([], finish="SAFETY"))
    assert blocked.stop_reason == "refusal"
    nothing = GeminiAdapter.response(SimpleNamespace(candidates=[], prompt_feedback=SimpleNamespace(block_reason="OTHER"),
                                                     usage_metadata=None, model_version=None))
    assert nothing.stop_reason == "refusal" and nothing.content == []
    assert GeminiAdapter.contents([{"role": "user", "content": "Continue by calling a tool."}]) == [
        {"role": "user", "parts": [{"text": "Continue by calling a tool."}]}]


# --- errors, routing, config -------------------------------------------------------


def test_provider_errors_become_neutral_model_errors():
    class APIStatusError(Exception):
        status_code = 529

    def boom(**kwargs):
        raise APIStatusError("anthropic overloaded for claude-sonnet-5-5")

    client = SimpleNamespace(messages=SimpleNamespace(create=boom))
    with pytest.raises(ModelError) as info:
        AnthropicAdapter(client).complete(request())
    assert str(info.value) == "The model request failed (APIStatusError 529)."


def test_config_models_name_their_provider_and_keep_secrets_out(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "model: gpt-5.5\n"
        "models:\n"
        "  - {id: gpt-5.5, label: GPT}\n"
        "  - {id: gemini-3.5-pro, label: Gemini}\n"
        "  - {id: qwen3-vl, label: Local, provider: openai_compatible, base_url: 'http://localhost:11434/v1', context: 64000}\n"
        "api_tiers:\n"
        "  fast: {model: gemini-3.5-flash}\n"
        "  balanced: {model: gpt-5.5}\n"
        "  best: {model: claude-opus-5-5}\n"
    )
    for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = load_config(path)
    assert [m["provider"] for m in cfg.models] == ["openai", "google", "openai_compatible"]
    assert cfg.api_tiers["fast"]["provider"] == "google" and cfg.api_tiers["best"]["provider"] == "anthropic"
    listing = model_listing(cfg)
    assert listing["default"] == "gpt-5.5"
    assert listing["models"] == [
        {"id": "gpt-5.5", "label": "GPT", "provider": "openai", "available": True},
        {"id": "gemini-3.5-pro", "label": "Gemini", "provider": "google", "available": False},  # no key
        {"id": "qwen3-vl", "label": "Local", "provider": "openai_compatible", "available": True},  # base_url
    ]
    with pytest.raises(ValueError, match="api_key_env"):
        normalize_model({"id": "gpt-5.5", "api_key": "sk-oops"})
    with pytest.raises(ValueError, match="provider"):
        normalize_model({"id": "mystery-model"})


def test_models_that_cannot_see_the_screen_are_not_offered():
    cfg = Config(models=[{"id": "text-only", "label": "T", "provider": "openai_compatible", "vision": False,
                          "base_url": "http://x/v1"}])
    assert model_listing(cfg)["models"][0]["available"] is False
    assert not ModelRouter(cfg).available("text-only")


def test_router_refuses_a_model_without_a_key_and_agent_reports_it(tmp_path):
    cfg = Config(settle_delay=0, launch_settle_delay=0, model="gpt-5.5")
    router = ModelRouter(cfg)  # no OPENAI_API_KEY in this Config
    with pytest.raises(ModelUnavailable):
        router.route("gpt-5.5")
    driver = FakeDriver()
    agent = Agent(router, driver, AppResolver(driver, label_cache=tmp_path / "l.json"), cfg, RecordingIO(), use_ocr=False)
    result = agent.run("open settings")
    assert not result.success and "isn't set up" in result.summary and "openai" not in result.summary.lower()


@pytest.mark.parametrize("provider", ["openai", "google"])
def test_the_agent_runs_end_to_end_on_other_providers(tmp_path, provider):
    """The same loop, tools and memory text drive a phone through a non-Anthropic adapter."""
    cfg = Config(settle_delay=0, launch_settle_delay=0)
    if provider == "openai":
        model = "gpt-5.5"
        replies = [
            openai_response(SimpleNamespace(content=None, refusal=None, tool_calls=[SimpleNamespace(
                id="c1", function=SimpleNamespace(name="tap", arguments='{"x": 100, "y": 200, "target": "Wi-Fi"}'))])),
            openai_response(SimpleNamespace(content=None, refusal=None, tool_calls=[SimpleNamespace(
                id="c2", function=SimpleNamespace(name="done", arguments='{"summary": "Wi-Fi is open.", "success": true}'))])),
        ]
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            return replies[len(calls) - 1]
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    else:
        model = "gemini-3.5-flash"
        replies = [
            gemini_response([SimpleNamespace(function_call=SimpleNamespace(id="fc1", name="tap", args={"x": 100, "y": 200, "target": "Wi-Fi"}),
                                             thought_signature=b"s", thought=None, text=None)]),
            gemini_response([SimpleNamespace(function_call=SimpleNamespace(id="fc2", name="done", args={"summary": "Wi-Fi is open.", "success": True}),
                                             thought_signature=None, thought=None, text=None)]),
        ]
        calls = []

        def generate_content(**kwargs):
            calls.append(kwargs)
            return replies[len(calls) - 1]
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    driver = FakeDriver()
    agent = Agent(ModelRouter(cfg, {provider: client}), driver, AppResolver(driver, label_cache=tmp_path / "l.json"),
                  cfg, RecordingIO(), use_ocr=False)
    result = agent.run("open wifi settings", model=model)
    assert result.success and result.summary == "Wi-Fi is open." and ("tap", 200, 400) in driver.actions
    assert len(calls) == 2 and calls[0]["model"] == model
    assert result.usage.by_model == {}  # the fakes report no usage


def test_agent_with_a_plain_client_still_serves_claude(tmp_path):
    from conftest import ScriptedClient

    cfg = Config(settle_delay=0, launch_settle_delay=0)
    client = ScriptedClient([[tool_use("done", "a", summary="ok", success=True)]])
    driver = FakeDriver()
    agent = Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"), cfg, RecordingIO(), use_ocr=False)
    assert agent.model_available("claude-opus-5-5") and not agent.model_available("gpt-5.5")
    assert agent.run("check").success
