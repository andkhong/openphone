"""Faster mode's switch (docs/design/faster-mode.md): on only from the phone's own app, with that phone's screen-lock
signature; off with one tap from anywhere (phone app, notification, dashboard, API, MCP); every change in the audit
log; a session-only switch ends with the phone's connection."""

import json
import time

import httpx
import pytest
from test_permission_settings import signed
from test_phone_security import hosted  # noqa: F401  (a fixture)
from test_remote import new_signer, wait_for

from openmob import permissions as perm


def view(h, device_id):
    return h.alice.get(f"/api/devices/app-{device_id}/permissions").json()


def faster_request(h, phone):
    """The phone app's "Turn on Faster mode": the server answers with a mode_change approval for that phone."""
    before = len(phone.approval_requests)
    phone.send({"type": "faster_mode_request", "scope": phone.scope})
    assert wait_for(lambda: len(phone.approval_requests) > before)
    request = phone.approval_requests[-1]
    assert request["kind"] == "mode_change"
    return request


def turn_on(h, phone, signer, scope="phone"):
    phone.scope = scope
    request = faster_request(h, phone)
    answer = signed(signer, request)
    phone.send(answer)
    return request, answer


def test_the_dashboard_the_api_and_mcp_can_only_turn_it_off(hosted):
    h = hosted
    dev = h.ids.a1
    for value in ("phone", "session", "on", True):
        r = h.alice.patch(f"/api/devices/app-{dev}/permissions", json={"faster_mode": value})
        assert r.status_code == 422, value  # the dashboard's schema has only "off"
    key = h.alice.post("/api/developer/keys", json={"name": "bot", "max_mode": "never"}).json()["key"]
    v1 = httpx.Client(base_url=h.base, headers={"Authorization": f"Bearer {key}"})
    assert v1.patch(f"/v1/devices/app-{dev}/permissions", json={"faster_mode": "phone"}).status_code == 422
    # The server itself (any caller that isn't the phone's own request) is refused, whatever the key allows.
    session = h.manager.get(f"app-{dev}")
    for origin in ("server", "dashboard", "api"):
        with pytest.raises(perm.ModeNotAllowed, match="only be turned on in the OpenMob app"):
            h.manager.change_permissions(session, {"faster_mode": "phone"}, origin=origin)
    # Even "from the phone", only the switch itself: nothing else rides along with it.
    with pytest.raises(perm.ModeNotAllowed):
        h.manager.change_permissions(session, {"faster_mode": "phone", "never": "7d"}, origin="phone")
    assert view(h, dev)["faster_active"] == "off" and h.manager.faster_on(f"app-{dev}") is False
    assert not any(q.get("kind") == "mode_change" for q in h.pa1.approval_requests)
    # Off is accepted from each of them (a tightening).
    assert h.alice.patch(f"/api/devices/app-{dev}/permissions", json={"faster_mode": "off"}).status_code == 200
    assert v1.patch(f"/v1/devices/app-{dev}/permissions", json={"faster_mode": "off"}).status_code == 200


def test_turning_it_on_needs_the_phones_own_screen_lock_signature(hosted):
    h = hosted
    dev = h.ids.a1
    h.pa1.scope = "phone"
    request = faster_request(h, h.pa1)
    # The approval shows the warning (the phone words its own prompt from the signed change, too).
    assert "sends screenshots of what the agent is looking at to TypeSafe" in request["question"]
    assert request["change"]["faster_mode"] == "phone" and request["change"]["loosens"] == ["faster:phone"]
    assert not any(q.get("kind") == "mode_change" for q in h.pa2.approval_requests)  # only that phone is asked
    # Unsigned, signed by another key, or signed by the owner's other phone: ignored.
    h.pa1.send({"type": "approval", "id": request["id"], "approved": True})
    h.pa1.send(signed(new_signer(), request))
    h.pa2.send(signed(h.signers.a2, request))
    assert not wait_for(lambda: h.manager.faster_on(f"app-{dev}"), timeout=1.0)
    assert view(h, dev)["faster_active"] == "off"
    h.pa1.send(signed(h.signers.a1, request))
    assert wait_for(lambda: h.manager.faster_on(f"app-{dev}"))
    now = view(h, dev)
    assert now["faster_active"] == "phone" and now["faster_mode"] == "phone"
    assert now["faster_log"][-1]["on"] is True and now["faster_log"][-1]["by"] == "the phone (screen lock)"
    assert wait_for(lambda: any(p.get("faster_mode") == "phone" for p in h.pa1.permission_pushes))
    assert h.manager.faster_on(f"app-{h.ids.a2}") is False  # the other phone stays off
    audit = [r for r in h.ledger.audit_log() if r["action"] == "device.faster_mode"]
    assert audit and audit[0]["outcome"].startswith("on:phone by the phone")


def test_a_phone_without_an_approval_key_cant_turn_it_on(hosted):
    h = hosted
    import uuid

    dev = str(uuid.uuid4())
    key = h.pair(h.alice, dev)  # paired with no screen-lock key
    phone = h.connect(dev, key)
    assert phone.welcome.wait(5)
    phone.send({"type": "faster_mode_request", "scope": "phone"})
    assert not wait_for(lambda: any(q.get("kind") == "mode_change" for q in phone.approval_requests), timeout=1.0)
    assert h.manager.faster_on(f"app-{dev}") is False


def test_off_from_the_phone_app_its_notification_the_dashboard_or_the_api(hosted):
    h = hosted
    dev = h.ids.a1
    key = h.alice.post("/api/developer/keys", json={"name": "bot"}).json()["key"]
    v1 = httpx.Client(base_url=h.base, headers={"Authorization": f"Bearer {key}"})
    offs = [
        ("notification", lambda: h.pa1.send({"type": "faster_mode_off", "via": "notification"}),
         "the phone's notification"),
        ("app", lambda: h.pa1.send({"type": "faster_mode_off", "via": "app"}), "the phone app"),
        ("dashboard", lambda: h.alice.patch(f"/api/devices/app-{dev}/permissions", json={"faster_mode": "off"}),
         "Your dashboard"),
        ("api", lambda: v1.patch(f"/v1/devices/app-{dev}/permissions", json={"faster_mode": "off"}), "bot"),
    ]
    for name, off, by in offs:
        turn_on(h, h.pa1, h.signers.a1)
        assert wait_for(lambda: h.manager.faster_on(f"app-{dev}")), name
        off()
        assert wait_for(lambda: not h.manager.faster_on(f"app-{dev}")), name
        log = view(h, dev)["faster_log"]
        assert log[-1]["on"] is False and log[-1]["by"] == by, (name, log[-1])
    assert wait_for(lambda: h.pa1.permission_pushes[-1].get("faster_mode") == "off")
    actions = [r["outcome"] for r in h.ledger.audit_log() if r["action"] == "device.faster_mode"]
    assert sum(o.startswith("off by") for o in actions) == 4 and sum(o.startswith("on:") for o in actions) == 4


def test_a_disabled_server_never_turns_it_on(hosted):
    h = hosted
    dev = h.ids.a1
    h.manager.cfg.faster_mode = "disabled"
    try:
        h.pa1.send({"type": "faster_mode_request", "scope": "phone"})
        assert not wait_for(lambda: any(q.get("kind") == "mode_change" for q in h.pa1.approval_requests),
                            timeout=1.0)
        assert h.manager.faster_on(f"app-{dev}") is False
    finally:
        h.manager.cfg.faster_mode = "phone_opt_in"


def test_the_agent_asks_the_live_setting_before_each_image(hosted):
    h = hosted
    dev = h.ids.a1
    agent = h.manager.get(f"app-{dev}").coordinator.agent
    assert agent.faster_mode is not None and agent.faster_mode() is False
    turn_on(h, h.pa1, h.signers.a1)
    assert wait_for(lambda: agent.faster_mode() is True)
    h.pa1.send({"type": "faster_mode_off", "via": "notification"})
    assert wait_for(lambda: agent.faster_mode() is False)


# --- session only: until Disconnect or an hour idle; a blip keeps it if the phone re-confirms its grant -------------


def session_on(h):
    request, answer = turn_on(h, h.pa1, h.signers.a1, scope="session")
    sid = f"app-{h.ids.a1}"
    assert wait_for(lambda: h.manager.faster_scope(sid) == "session")
    # The limits are inside the signed change (the action's input the phone signed the digest of).
    change = json.loads(request["action"]["input"].split("\n", 1)[1])
    assert change["faster_mode"] == "session" and change["faster_idle_s"] == 3600 and change["faster_resume_s"] == 300
    assert change["faster_until"] > time.time() + 86_000
    return sid, answer


def resume_message(answer, **override):
    return {"type": "faster_mode_resume", "approval_id": answer["id"], "signature": answer["signature"],
            "timestamp": answer["timestamp"], **override}


def test_a_network_blip_keeps_a_session_only_faster_mode(hosted):
    h = hosted
    sid, answer = session_on(h)
    h.pa1.drop()  # a network blip, not the owner's Disconnect
    assert wait_for(lambda: h.manager.faster_scope(sid) == "off")  # off while the phone is away
    again = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)
    assert again.welcome.wait(5)
    assert wait_for(lambda: any(p.get("faster_resume") is True for p in again.permission_pushes))
    assert h.manager.faster_scope(sid) == "off"  # not until the phone re-confirms
    again.send(resume_message(answer))
    assert wait_for(lambda: h.manager.faster_scope(sid) == "session")
    assert wait_for(lambda: again.permission_pushes[-1].get("faster_mode") == "session")


def test_a_resume_with_another_signature_or_too_late_ends_it(hosted):
    h = hosted
    sid, answer = session_on(h)
    h.pa1.drop()
    again = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)
    assert again.welcome.wait(5)
    forged = signed(new_signer(), {"id": answer["id"], "task_id": "x", "digest": "0" * 64})
    again.send(resume_message(answer, signature=forged["signature"]))
    assert wait_for(lambda: view(h, h.ids.a1)["faster_mode"] == "off")
    again.send(resume_message(answer))  # the real one, now that it ended: still off
    assert not wait_for(lambda: h.manager.faster_scope(sid) == "session", timeout=1.0)
    # Too late: on again, then away longer than the grant's 5 minute window.
    phone = again
    phone.scope = "session"
    request = faster_request(h, phone)
    answer = signed(h.signers.a1, request)
    phone.send(answer)
    assert wait_for(lambda: h.manager.faster_scope(sid) == "session")
    phone.drop()
    assert wait_for(lambda: sid in h.manager._faster_detached)
    h.manager._faster_detached[sid] -= 301
    last = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)
    assert last.welcome.wait(5)
    last.send(resume_message(answer))
    assert wait_for(lambda: view(h, h.ids.a1)["faster_mode"] == "off")
    assert "too late" in view(h, h.ids.a1)["faster_log"][-1]["by"]


def test_disconnect_ends_a_session_only_faster_mode(hosted):
    h = hosted
    sid, answer = session_on(h)
    h.pa1.disconnect()  # the owner's Disconnect (a normal close)
    assert wait_for(lambda: view(h, h.ids.a1)["faster_mode"] == "off")
    assert view(h, h.ids.a1)["faster_log"][-1]["by"] == "the owner's Disconnect"
    again = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)
    assert again.welcome.wait(5)
    again.send(resume_message(answer))  # the old grant can't bring it back
    assert not wait_for(lambda: h.manager.faster_scope(sid) == "session", timeout=1.0)


def test_an_hour_idle_ends_a_session_only_faster_mode(hosted):
    h = hosted
    sid, _ = session_on(h)
    assert h.manager.faster_on(sid)  # task activity
    h.manager._faster_active[sid] = time.time() - 3601
    assert h.manager.faster_scope(sid) == "off"
    assert view(h, h.ids.a1)["faster_mode"] == "off"
    assert "idle" in view(h, h.ids.a1)["faster_log"][-1]["by"]
    assert wait_for(lambda: h.pa1.permission_pushes[-1].get("faster_mode") == "off")


def test_the_phone_scope_survives_reconnects(hosted):
    h = hosted
    turn_on(h, h.pa1, h.signers.a1, scope="phone")
    sid = f"app-{h.ids.a1}"
    assert wait_for(lambda: h.manager.faster_scope(sid) == "phone")
    h.pa1.disconnect()
    again = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)
    assert again.welcome.wait(5)
    assert h.manager.faster_scope(sid) == "phone"


# --- a key's drive sessions: asked from the dashboard, approved on the phone, per key; off from anywhere -----------


def test_a_keys_drive_sessions_need_the_phones_screen_lock_per_key(hosted):
    h = hosted
    dev = h.ids.a1
    sid = f"app-{dev}"
    key = h.alice.post("/api/developer/keys", json={"name": "driver", "scopes": ["devices:drive"]}).json()
    other = h.alice.post("/api/developer/keys", json={"name": "other", "scopes": ["devices:drive"]}).json()
    # The developer console can't turn it on, and neither can the server or a key.
    r = h.alice.patch(f"/api/developer/keys/{key['id']}", json={"faster_drive": True})
    assert r.status_code == 403
    session = h.manager.get(sid)
    for origin in ("server", "phone", "api"):
        with pytest.raises(perm.ModeNotAllowed):
            h.manager.change_permissions(session, {"faster_drive": {"key": key["id"], "on": True}}, origin=origin)
    # The dashboard asks; the phone words it and signs.
    r = h.alice.post(f"/api/devices/{sid}/faster-drive", json={"key_id": key["id"]})
    assert r.status_code == 202, r.text
    assert wait_for(lambda: any("drive sessions" in q["question"] for q in h.pa1.approval_requests))
    request = next(q for q in h.pa1.approval_requests if "drive sessions" in q["question"])
    assert "“driver”" in request["question"]
    h.pa1.send({"type": "approval", "id": request["id"], "approved": True})  # unsigned: ignored
    assert not wait_for(lambda: key["id"] in view(h, dev)["faster_drive_keys"], timeout=1.0)
    h.pa1.send(signed(h.signers.a1, request))
    assert wait_for(lambda: key["id"] in view(h, dev)["faster_drive_keys"])
    assert other["id"] not in view(h, dev)["faster_drive_keys"]  # per key
    # Allowed only while Faster mode itself is on for the phone.
    assert h.manager.faster_drive_allowed(sid, key["id"]) is False
    turn_on(h, h.pa1, h.signers.a1)
    assert wait_for(lambda: h.manager.faster_drive_allowed(sid, key["id"]) is True)
    assert h.manager.faster_drive_allowed(sid, other["id"]) is False
    # The developer console turns it off (on every phone), at once.
    r = h.alice.patch(f"/api/developer/keys/{key['id']}", json={"faster_drive": False})
    assert r.status_code == 200
    assert key["id"] not in view(h, dev)["faster_drive_keys"]
    assert h.manager.faster_drive_allowed(sid, key["id"]) is False
    actions = [a["outcome"] for a in h.ledger.audit_log() if a["action"] in ("device.faster_mode", "key.faster_drive")]
    assert any(o.startswith("on:drive:") for o in actions) and any(o.startswith("off on 1") for o in actions)


def test_the_dashboard_says_when_faster_mode_is_on_but_inactive(hosted):
    h = hosted
    dev = h.ids.a1
    turn_on(h, h.pa1, h.signers.a1)
    assert wait_for(lambda: view(h, dev)["faster_active"] == "phone")
    # This test server's agents have no TypeSafe key: on, but nothing can be sent.
    assert view(h, dev)["faster_inactive"] == "no TypeSafe key on this server"
    agent = h.manager.get(f"app-{dev}").coordinator.agent
    agent.images, agent.use_ocr = object(), False
    assert "no OCR on this server" in view(h, dev)["faster_inactive"]
    agent.use_ocr = True
    assert view(h, dev)["faster_inactive"] is None


def test_the_safety_page_is_public_and_says_it(hosted):
    from openmob.permissions import FASTER_WARNING

    r = httpx.get(f"{hosted.base}/safety")
    assert r.status_code == 200 and 'id="faster-mode"' in r.text
    assert FASTER_WARNING in " ".join(r.text.split())


