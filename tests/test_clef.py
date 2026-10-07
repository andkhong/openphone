import json

import httpx
import pytest

from conftest import FakeDriver, RecordingIO, ScriptedClient
from openmob.clef import ClefClient, ClefJudge, encode_image
from openmob.jev import JevError

ACCOUNT = "a" * 32


def client(handler, **kwargs):
    return ClefClient("not-a-secret", ACCOUNT, http=httpx.Client(
        base_url="https://api.cloudflare.com", transport=httpx.MockTransport(handler)), **kwargs)


def response_for(body):
    answers = {}
    for key, q in body["questions"].items():
        if q["type"] == "noul":
            answers[key] = {"noul": .2}
        else:
            choices = q["criteria"]
            first = next(iter(choices))
            answers[key] = {"choice": first, "probabilities": {k: float(k == first) for k in choices}, "confidence": 1.0}
    return {"answers": answers, "usage": {"input_tokens": 12, "output_tokens": 0}}


def test_hosted_wire_images_envelope_chunks_and_usage():
    sent = []
    def handler(request):
        assert request.url.path == f"/client/v4/accounts/{ACCOUNT}/ai/run/@cf/cloudflare/clef-flash"
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"success": True, "result": response_for(body)})
    c = client(handler)
    usage = []
    c.on_usage = usage.append
    image = encode_image(FakeDriver().screenshot().jpeg)
    questions = {f"q{i}": {"type": "noul", "instructions": "Risky?"} for i in range(85)}
    result = c.ask({"screen": "example"}, questions, images=[image])
    assert set(result) == set(questions)
    assert [len(b["questions"]) for b in sent] == [64, 21]
    assert all(b["images"] == [image] and b["state"] == {"screen": "example"} for b in sent)
    assert usage == [12, 12] and c.request_count == 2 and c.input_tokens == 24


def test_single_target_and_app_selection_do_not_invent_options():
    sent = []
    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json=response_for(body))
    judge = ClefJudge(client(handler))
    decision = judge.next_step("tap settings", {}, [], {"TAP": {"0": "Settings"}})
    assert decision.targets["TAP"] == ("0", 1.0, 1.0)
    assert "tap_target" not in sent[0]["questions"]
    judge.pick_package("Settings", {"settings": "Settings"})
    assert "images" not in sent[-1]
    assert set(sent[-1]["questions"]["app"]["criteria"]) == {"settings", "none"}


@pytest.mark.parametrize("payload", [
    {"success": False, "errors": [{"message": "do not echo"}]},
    {"answers": {}, "usage": {"input_tokens": 1}},
    {"answers": {"risk": {"noul": float("inf")}}, "usage": {"input_tokens": 1}},
    {"answers": {"risk": {"noul": -1}}, "usage": {"input_tokens": 1}},
])
def test_bad_results_raise_classifier_error(payload):
    # Infinity cannot be encoded by httpx's strict JSON serializer.
    c = client(lambda r: httpx.Response(200, content=json.dumps(payload)))
    with pytest.raises(JevError) as error:
        c.ask({}, {"risk": {"type": "noul", "instructions": "Risky?"}})
    assert "do not echo" not in str(error.value)


def test_retry_and_error_redaction(monkeypatch):
    monkeypatch.setattr("openmob.clef.time.sleep", lambda _: None)
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(429, text="secret request data") if len(calls) == 1 else httpx.Response(
            200, json=response_for(json.loads(request.content)))
    c = client(handler)
    assert c.ask({}, {"risk": {"type": "noul", "instructions": "Risky?"}})["risk"]["noul"] == .2
    assert len(calls) == 2
    with pytest.raises(JevError, match="HTTP 401") as error:
        client(lambda r: httpx.Response(401, text="secret request data")).ask({}, {"risk": {"type": "noul"}})
    assert "secret" not in str(error.value)


def test_bad_image_and_invalid_config_fail_locally():
    with pytest.raises(JevError):
        encode_image(b"not an image")
    with pytest.raises(ValueError):
        ClefClient("key", "bad/account")


@pytest.mark.parametrize("mode,sparse,expected_shots", [("off", False, 0), ("auto", False, 0),
                                                        ("auto", True, 1), ("always", False, 1)])
def test_agent_image_policy_preserves_lazy_capture(tmp_path, mode, sparse, expected_shots):
    from openmob.agent.loop import Agent, Observation
    from openmob.config import Config
    from openmob.phone.apps import AppResolver
    from test_fast_path import SETTINGS
    from test_latency_round_d import CountingPhone
    driver = CountingPhone([] if sparse else SETTINGS, {})
    cfg = Config(classifier_provider="clef", clef_images=mode)
    agent = Agent(ScriptedClient([]), driver, AppResolver(driver, label_cache=tmp_path / "labels.json"),
                  cfg, RecordingIO(), use_ocr=False)
    obs = Observation(None, driver.elements, "settings", capture=agent._screenshot)
    a = agent._classifier_images(obs)
    b = agent._classifier_images(obs)
    assert driver.shots == expected_shots
    assert bool(a) == bool(expected_shots) and a == b


def test_factory_configuration_and_environment_only_secrets(tmp_path, monkeypatch):
    from openmob.classifiers import make_judge
    from openmob.config import Config, load_config
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "not-a-secret")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    path = tmp_path / "config.yaml"
    path.write_text("classifier_provider: clef\nclef_images: always\n")
    cfg = load_config(path)
    judge = make_judge(cfg)
    assert isinstance(judge, ClefJudge)
    judge.client.close()
    assert make_judge(Config(jev_enabled=False, classifier_provider="clef")) is None
    path.write_text("clef_api_key: secret\n")
    with pytest.raises(ValueError, match="Unknown config keys"):
        load_config(path)


@pytest.mark.parametrize("image_mode", ["off", "auto", "always"])
def test_real_adapter_through_agent_fast_step_and_completion(tmp_path, image_mode):
    from openmob.agent.loop import Agent
    from openmob.config import Config
    from openmob.phone.apps import AppResolver
    from test_fast_path import SETTINGS, DISPLAY, DONE
    from test_latency_round_d import CountingPhone, FakeJev
    scripted = FakeJev([("TAP", 1, .95), ("DONE", None, .95)])
    sent = []
    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        answers = scripted.ask(body["state"], body["questions"])
        for key, question in body["questions"].items():
            if question["type"] == "choice":
                if key not in answers:
                    first = next(iter(question["criteria"]))
                    answers[key] = {"choice": first, "probabilities": {first: 1}, "confidence": 1}
                answer = answers[key]
                chosen = answer["choice"]
                p = answer["probabilities"][chosen]
                answer["probabilities"] = {k: p if k == chosen else (1 - p) / (len(question["criteria"]) - 1)
                                           for k in question["criteria"]}
        return httpx.Response(200, json={"success": True, "result": {
            "answers": answers, "usage": {"input_tokens": 25}}})
    phone = CountingPhone(SETTINGS, {"Display": DISPLAY})
    judge = ClefJudge(client(handler))
    cfg = Config(classifier_provider="clef", clef_images=image_mode, fast_path_done=False,
                 settle_delay=0, launch_settle_delay=0)
    model = ScriptedClient([DONE])
    agent = Agent(model, phone, AppResolver(phone, label_cache=tmp_path / "labels.json"),
                  cfg, RecordingIO(), judge=judge, use_ocr=False)
    result = agent.run("Open Settings and go to Display", go_home=False)
    assert result.success and result.fast_steps == 1 and result.done_via == "vision"
    assert ("tap", 500, 140) in phone.actions
    assert len(sent) == 2 and len(model.requests) == 1
    assert all(("images" in body) == (image_mode == "always") for body in sent)
    assert phone.shots == (2 if image_mode == "always" else 1)
    assert "Clef screen state" in str(model.requests[0])


def test_visual_risk_cannot_authorize_risky_action(tmp_path):
    from openmob.agent.loop import Agent
    from openmob.config import Config
    from openmob.phone.apps import AppResolver
    from conftest import tool_use
    from test_fast_path import el
    sent = []
    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        result = response_for(body)
        if "state" in result["answers"]:
            choices = body["questions"]["state"]["criteria"]
            result["answers"]["state"] = {"choice": "app_content", "confidence": 1,
                "probabilities": {k: float(k == "app_content") for k in choices}}
        if "risky" in result["answers"]:
            result["answers"]["risky"]["noul"] = 0
            result["answers"]["requested"]["noul"] = 1
        return httpx.Response(200, json=result)
    phone = FakeDriver(elements=[el(0, "Buy now")])
    phone.package = "example.shop"
    model = ScriptedClient([[tool_use("tap_element", "tap", index=0, target="Buy now")],
                            [tool_use("done", "done", success=False, summary="Declined")]])
    io = RecordingIO(answers=["no"])
    agent = Agent(model, phone, AppResolver(phone, label_cache=tmp_path / "labels.json"),
                  Config(classifier_provider="clef", clef_images="always", fast_path="off",
                         settle_delay=0, launch_settle_delay=0), io,
                  judge=ClefJudge(client(handler)), use_ocr=False)
    agent.run("Review my cart", go_home=False)
    assert io.questions and not any(a[0] == "tap" for a in phone.actions)
    assert any("risky" in b["questions"] and "images" in b for b in sent)
