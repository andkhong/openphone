"""Faster mode's switch (docs/design/faster-mode.md): on only from the phone's own app, with that phone's screen-lock
signature; off with one tap from anywhere (phone app, notification, dashboard, API, MCP); every change in the audit
log; a session-only switch ends with the phone's connection."""

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
    phone.send(signed(signer, request))
    return request


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


def test_session_only_ends_with_the_phones_connection(hosted):
    h = hosted
    dev = h.ids.a1
    turn_on(h, h.pa1, h.signers.a1, scope="session")
    assert wait_for(lambda: h.manager.faster_scope(f"app-{dev}") == "session")
    # The phone reconnects (a new connection): the session it was approved for is over.
    again = h.connect(dev, h.keys.a1, h.signers.a1)
    assert again.welcome.wait(5)
    assert wait_for(lambda: h.manager.faster_scope(f"app-{dev}") == "off")
    assert wait_for(lambda: any(p.get("faster_mode") == "off" for p in again.permission_pushes))
    log = view(h, dev)["faster_log"]
    assert log[-1]["on"] is False and "session" in log[-1]["by"]


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


def test_a_key_may_drive_with_faster_mode_only_if_the_owner_allowed_it(hosted):
    h = hosted
    created = h.alice.post("/api/developer/keys", json={"name": "driver", "scopes": ["devices:drive"]}).json()
    assert created["faster_drive"] is False
    r = h.alice.patch(f"/api/developer/keys/{created['id']}", json={"faster_drive": True})
    assert r.status_code == 200 and r.json()["faster_drive"] is True
    audit = [r for r in h.ledger.audit_log() if r["action"] == "key.faster_drive"]
    assert audit and audit[0]["outcome"] == "on"
