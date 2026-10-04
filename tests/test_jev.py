import json

import httpx
import pytest

from openmob.jev import SCREEN_STATES, JevClient, JevError, JevJudge, describe_screen


def client_with(handler, **kw):
    http = httpx.Client(base_url="https://api.typesafe.ai", transport=httpx.MockTransport(handler))
    return JevClient("key", http=http, **kw)


def test_assess_screen_sends_choice_and_noul_and_parses_answer():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(200, json={
            "model": "jev-1.13.0",
            "answers": {
                "state": {"type": "choice", "choice": "loading",
                          "probabilities": {"loading": 0.91, "popup": 0.09}, "confidence": 0.82},
                "done": {"type": "noul", "noul": 0.03, "confidence": 0.9},
            },
        })

    judge = JevJudge(client_with(handler))
    a = judge.assess_screen("open the menu", describe_screen("com.netease.wwm", [], ["Loading", "42%"]))
    assert seen["path"] == "/v1/systemone"
    assert seen["body"]["model"] == "jev-latest"
    assert seen["body"]["questions"]["state"]["criteria"] == SCREEN_STATES
    assert seen["body"]["state"]["screen"]["text_on_screen"] == ["Loading", "42%"]
    assert (a.state, a.probability, a.confidence, a.task_done) == ("loading", 0.91, 0.82, 0.03)
    assert "loading" in a.summary()


def test_pick_package_adds_none_option_and_maps_it():
    def handler(request):
        criteria = json.loads(request.content)["questions"]["app"]["criteria"]
        assert "none" in criteria and "com.netease.wwm" in criteria
        return httpx.Response(200, json={"answers": {"app": {
            "type": "choice", "choice": "none", "probabilities": {"none": 0.7}, "confidence": 0.5}}})

    judge = JevJudge(client_with(handler))
    assert judge.pick_package("where winds meet", {"com.netease.wwm": "wwm"}) == (None, 0.7, 0.5)


def test_retries_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr("openmob.jev.time.sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"answers": {
            "risky": {"type": "noul", "noul": 0.8}, "requested": {"type": "noul", "noul": 0.1}}})

    judge = JevJudge(client_with(handler))
    assert judge.assess_tap("open the shop", "Buy 100 gems", {}) == (0.8, 0.1)
    assert len(calls) == 2


def test_auth_error_raises():
    judge = JevJudge(client_with(lambda r: httpx.Response(401, text="bad key")))
    with pytest.raises(JevError, match="401"):
        judge.assess_tap("x", "y", {})


def test_describe_screen_dedupes_and_trims():
    screen = describe_screen(None, ["Button"], ["Menu", "menu", "x", "Start"])
    assert screen == {"foreground_app": "unknown", "ui_elements": ["Button"], "text_on_screen": ["Menu", "Start"]}
