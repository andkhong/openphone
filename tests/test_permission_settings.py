"""Per-phone permission settings over every front door: the ceiling, loosening only with the phone's
screen-lock-signed approval, tightening from anywhere, "Always allow", API key max_mode, and MCP."""

import base64
import time

import httpx
import pytest
from test_api_v1 import api, run_task, wait_status  # noqa: F401  (api is a fixture)
from test_mcp import call, mcp, ok, text
from test_phone_security import hosted  # noqa: F401  (a fixture)
from test_remote import new_signer, wait_for

from openmob import permissions as perm
from openmob.phone.approval_keys import approval_message


def signed(signer, request, extra=None, approved=True):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    now = int(time.time())
    sig = signer.sign(approval_message(request["id"], request["task_id"], True, now, extra, request.get("digest")),
                      ec.ECDSA(hashes.SHA256()))
    message = {"type": "approval", "id": request["id"], "approved": approved, "timestamp": now,
               "signature": base64.b64encode(sig).decode()}
    if extra == "always":
        message["always"] = True
    return message


# --- the rules --------------------------------------------------------------------------------


def test_tightening_and_loosening_are_told_apart():
    cats = {"messaging": ["whatsapp"]}
    base = perm.default_settings()
    assert perm.apply_patch(base, {"default_mode": "ask"}, cats)[1] == []
    assert perm.apply_patch(base, {"ceiling": "plan"}, cats)[1] == []
    assert perm.apply_patch(base, {"default_mode": "trusted"}, cats)[1] == ["ceiling:trusted"]
    assert perm.apply_patch(base, {"never": "1h"}, cats)[1] == ["never:1h"]
    assert perm.apply_patch(base, {"trusted": [{"app": "com.whatsapp", "kinds": ["send"]}]}, cats)[1]
    on = perm.apply_patch(base, {"never": "24h"}, cats)[0]
    assert perm.effective_ceiling(on) == "never"
    off, loosens = perm.apply_patch({**on, "default_mode": "never"}, {"never": "off"}, cats)
    assert loosens == [] and off["default_mode"] == "auto" and perm.effective_ceiling(off) == "auto"
    with pytest.raises(perm.PermissionsInvalid):
        perm.apply_patch(base, {"default_mode": "never"}, cats)  # never needs a duration (and the phone)
    with pytest.raises(perm.PermissionsInvalid):
        perm.apply_patch(base, {"trusted": [{"category": "messaging", "kinds": ["pay"]}]}, cats)


# --- dashboard + phone (hosted, phones with screen-lock keys) --------------------------------------


def settings_of(h, device_id):
    return h.alice.get(f"/api/devices/app-{device_id}/permissions").json()


def test_dashboard_tightens_freely_but_loosening_waits_for_the_phone(hosted):
    h = hosted
    dev = h.ids.a1
    assert settings_of(h, dev)["default_mode"] == "auto" and settings_of(h, dev)["ceiling"] == "auto"
    r = h.alice.patch(f"/api/devices/app-{dev}/permissions", json={"default_mode": "ask"})
    assert r.status_code == 200 and r.json()["settings"]["default_mode"] == "ask"
    # A chat can't pick a mode looser than the ceiling.
    r = h.alice.post(f"/api/devices/app-{dev}/command", json={"text": "check the cart", "permission_mode": "never"})
    assert r.status_code == 403 and r.json()["code"] == "mode_not_allowed"
    r = h.alice.patch(f"/api/devices/app-{dev}/permissions", json={"never": "1h", "default_mode": "never"})
    assert r.status_code == 202 and r.json()["status"] == "needs_phone_approval"
    approval_id = r.json()["approval_id"]
    assert wait_for(lambda: any(q.get("kind") == "mode_change" for q in h.pa1.approval_requests))
    request = next(q for q in h.pa1.approval_requests if q["kind"] == "mode_change")
    assert "for 1 hour" in request["question"] and request["id"] == approval_id
    assert not any(q.get("kind") == "mode_change" for q in h.pa2.approval_requests)  # only that phone
    poll = f"/api/devices/app-{dev}/permissions/approvals/{approval_id}"
    assert h.alice.get(poll).json()["status"] == "pending"
    # Unsigned, signed by another key, or answered on another phone: ignored.
    h.pa1.send({"type": "approval", "id": approval_id, "approved": True})
    h.pa1.send(signed(new_signer(), request))
    h.pa2.send(signed(h.signers.a2, request))
    assert not wait_for(lambda: h.alice.get(poll).json()["status"] != "pending", timeout=1.0)
    assert settings_of(h, dev)["ceiling"] == "auto"
    h.pa1.send(signed(h.signers.a1, request))
    assert wait_for(lambda: h.alice.get(poll).json()["status"] == "approved")
    now = settings_of(h, dev)
    assert now["ceiling"] == "never" and now["default_mode"] == "never" and now["never_until"] > time.time() + 3000
    # Never: no prompt, and the purchase it would make is refused (money never runs unasked).
    before = len(h.pa1.approval_requests)
    h.alice.post(f"/api/devices/app-{dev}/command", json={"text": "check the cart"})
    assert wait_for(lambda: h.status(dev) in ("succeeded", "failed"))
    assert len(h.pa1.approval_requests) == before and not any(a[0] == "tap" for a in h.pa1.driver.actions)
    # "Turn off" on the phone: an unsigned tightening, back to auto.
    h.pa1.send({"type": "permissions_off"})
    assert wait_for(lambda: settings_of(h, dev)["ceiling"] == "auto")
    assert settings_of(h, dev)["default_mode"] == "auto"


def test_purchases_under_never_only_with_the_signed_allow_money_flag(hosted):
    h = hosted
    dev = h.ids.a1
    h.pa1.driver.package = "com.example.shop"
    poll = f"/api/devices/app-{dev}/permissions/approvals/{{}}"

    def enable(body):
        r = h.alice.patch(f"/api/devices/app-{dev}/permissions", json=body)
        assert r.status_code == 202 and r.json()["status"] == "needs_phone_approval"
        approval_id = r.json()["approval_id"]
        assert wait_for(lambda: any(q["id"] == approval_id for q in h.pa1.approval_requests))
        request = next(q for q in h.pa1.approval_requests if q["id"] == approval_id)
        h.pa1.send(signed(h.signers.a1, request))
        assert wait_for(lambda: h.alice.get(poll.format(approval_id)).json()["status"] == "approved")
        return request

    def buy():
        before = len(h.pa1.driver.actions)
        h.alice.post(f"/api/devices/app-{dev}/command", json={"text": "check the cart", "permission_mode": "never"})
        assert wait_for(lambda: list(h.manager.get(f"app-{dev}").coordinator.task_info.values())[-1]["status"]
                        in ("succeeded", "failed"))
        return any(a[0] == "tap" for a in h.pa1.driver.actions[before:])

    # Don't ask without the flag: the purchase is refused (no prompt either).
    request = enable({"never": "1h"})
    assert "purchases" not in request["question"] and not settings_of(h, dev)["never_money"]
    asked = len(h.pa1.approval_requests)
    assert not buy() and len(h.pa1.approval_requests) == asked
    # With it: explicit wording, the flag inside the signed action, and only the phone's signature sets it.
    r = h.alice.patch(f"/api/devices/app-{dev}/permissions", json={"never": "1h", "allow_money": True})
    assert r.status_code == 202
    approval_id = r.json()["approval_id"]
    assert wait_for(lambda: any(q["id"] == approval_id for q in h.pa1.approval_requests))
    request = next(q for q in h.pa1.approval_requests if q["id"] == approval_id)
    assert request["question"] == "Allow agents to act without asking on this phone including purchases for 1 hour?"
    assert request["change"]["allow_money"] is True and '"allow_money": true' in request["action"]["input"]
    h.pa1.send({"type": "approval", "id": approval_id, "approved": True})  # unsigned
    h.pa1.send(signed(new_signer(), request))  # another key
    h.pa2.send(signed(h.signers.a2, request))  # another phone
    assert not wait_for(lambda: h.alice.get(poll.format(approval_id)).json()["status"] != "pending", timeout=1.0)
    assert not settings_of(h, dev)["never_money"]
    h.pa1.send(signed(h.signers.a1, request))
    assert wait_for(lambda: h.alice.get(poll.format(approval_id)).json()["status"] == "approved")
    assert settings_of(h, dev)["never_money"] is True
    # The phone's banner is told (it then reads "…including purchases").
    assert wait_for(lambda: h.pa1.permission_pushes and h.pa1.permission_pushes[-1]["never_money"] is True)
    assert buy()
    # A task in a tighter mode still asks: the flag only matters under never.
    asked = len(h.pa1.approval_requests)
    h.alice.post(f"/api/devices/app-{dev}/command", json={"text": "check the cart", "permission_mode": "auto"})
    assert wait_for(lambda: len(h.pa1.approval_requests) > asked)
    h.pa1.send({"type": "approval", "id": h.pa1.approval_requests[-1]["id"], "approved": False})
    # It ends with the never window.
    raw = h.store.device_permissions(dev)
    h.store.set_device_permissions(dev, {**raw, "never_until": time.time() - 1})
    assert settings_of(h, dev)["never_money"] is False


def test_allow_money_can_only_be_asked_for_with_never(hosted):
    h = hosted
    dev = f"app-{h.ids.a1}"
    r = h.alice.patch(f"/api/devices/{dev}/permissions", json={"allow_money": True})
    assert r.status_code == 400 and settings_of(h, h.ids.a1)["never_money"] is False


def test_never_expires_back_to_auto(hosted):
    h = hosted
    sid = f"app-{h.ids.a1}"
    h.manager.permissions.set(sid, {**perm.default_settings(), "default_mode": "never",
                                    "never_until": time.time() + 3600})
    assert h.manager.check_mode(sid, None) == "never"
    raw = h.store.device_permissions(h.ids.a1)
    h.store.set_device_permissions(h.ids.a1, {**raw, "never_until": time.time() - 1})
    assert h.manager.check_mode(sid, None) == "auto"
    assert settings_of(h, h.ids.a1)["default_mode"] == "auto"


def test_a_phone_without_an_approval_key_cant_loosen(hosted):
    h = hosted
    import uuid

    old = str(uuid.uuid4())
    key = h.pair(h.alice, old)
    p = h.connect(old, key)
    assert p.welcome.wait(5)
    r = h.alice.patch(f"/api/devices/app-{old}/permissions", json={"never": "1h"})
    assert r.status_code == 403 and r.json()["code"] == "mode_not_allowed"
    assert h.alice.patch(f"/api/devices/app-{old}/permissions", json={"default_mode": "ask"}).status_code == 200


def test_an_out_of_date_app_is_told_to_update_instead_of_approving_in_vain(hosted):
    # The phone has a key but an app from before action digests: its yes would be refused, so the
    # dashboard says "update the app" up front, and its approval prompts carry can_approve: false.
    h = hosted
    import uuid

    old = str(uuid.uuid4())
    signer = new_signer()
    key = h.pair(h.alice, old, signer)
    p = h.connect(old, key, signer, legacy_signing=True)
    assert p.welcome.wait(5)
    r = h.alice.patch(f"/api/devices/app-{old}/permissions", json={"default_mode": "trusted", "ceiling": "trusted"})
    assert r.status_code == 403 and r.json()["code"] == "mode_not_allowed"
    assert "latest OpenMob app" in r.json()["detail"]
    assert settings_of(h, old)["ceiling"] == "auto"
    # Tightening still works from the dashboard.
    assert h.alice.patch(f"/api/devices/app-{old}/permissions", json={"default_mode": "ask"}).status_code == 200


def test_always_allow_creates_a_trusted_entry_only_when_signed(hosted):
    h = hosted
    dev = h.ids.a1
    assert h.alice.post(f"/api/devices/app-{h.ids.a2}/trust").status_code == 200  # so it's asked too
    h.pa1.driver.package = "com.example.shop"
    h.alice.post(f"/api/devices/app-{dev}/command", json={"text": "check the cart"})
    assert wait_for(lambda: h.status(dev) == "needs_confirmation")
    assert wait_for(lambda: h.pa2.approval_requests)
    request = h.pa1.approval_requests[-1]
    offer = request["always"]
    assert offer["kind"] == "pay" and offer["days"] == 1 and offer["confirm_twice"]
    assert "always" not in h.pa2.approval_requests[-1]  # only on the phone that would act
    # A signature over the plain approval doesn't cover "always": ignored.
    h.pa1.send({**signed(h.signers.a1, request), "always": True})
    assert not wait_for(lambda: h.status(dev) == "succeeded", timeout=1.0)
    h.pa1.send(signed(h.signers.a1, request, extra="always"))
    assert wait_for(lambda: h.status(dev) == "succeeded")
    (entry,) = settings_of(h, dev)["trusted"]
    assert entry["app"] == "com.example.shop" and entry["kinds"] == ["pay"]
    assert entry["expires_at"] <= time.time() + 86400 + 5
    # The next purchase in that app doesn't ask.
    before = len(h.pa1.approval_requests)
    h.alice.post(f"/api/devices/app-{dev}/command", json={"text": "check the cart"})
    assert wait_for(lambda: list(h.manager.get(f"app-{dev}").coordinator.task_info.values())[-1]["status"]
                    == "succeeded")
    assert len(h.pa1.approval_requests) == before


def test_api_loosening_returns_needs_phone_approval(hosted):
    h = hosted
    key = h.alice.post("/api/developer/keys", json={"name": "bot", "max_mode": "never"}).json()["key"]
    v1 = httpx.Client(base_url=h.base, headers={"Authorization": f"Bearer {key}"})
    dev = f"app-{h.ids.a1}"
    r = v1.patch(f"/v1/devices/{dev}/permissions", json={"default_mode": "plan"})
    assert r.status_code == 200 and r.json()["settings"]["default_mode"] == "plan"
    r = v1.patch(f"/v1/devices/{dev}/permissions", json={"never": "1h", "allow_money": True})
    assert r.status_code == 202 and "including purchases" in r.json()["question"]
    assert v1.get(f"/v1/devices/{dev}/permissions").json()["never_money"] is False
    r = v1.patch(f"/v1/devices/{dev}/permissions", json={"never": "24h"})
    assert r.status_code == 202 and r.json()["code"] == "needs_phone_approval"
    approval = r.json()["approval_id"]
    assert v1.get(f"/v1/devices/{dev}/permissions/approvals/{approval}").json()["status"] == "pending"
    assert v1.post(f"/v1/devices/{dev}/tasks", json={"instruction": "x", "permission_mode": "never"}).json()[
        "code"] == "mode_not_allowed"
    # A key with max_mode auto may not even ask for it.
    low = h.alice.post("/api/developer/keys", json={"name": "low"}).json()["key"]
    r = httpx.patch(f"{h.base}/v1/devices/{dev}/permissions", json={"never": "1h"},
                    headers={"Authorization": f"Bearer {low}"})
    assert r.status_code == 403 and r.json()["code"] == "mode_not_allowed"


# --- API keys: max_mode (api fixture: phones without approval keys) ----------------------------------


def test_key_max_mode_caps_api_tasks(api):
    r = api.alice.v1.post(f"/v1/devices/{api.alice.device}/tasks",
                          json={"instruction": "open maps", "permission_mode": "never"})
    assert r.status_code == 403 and r.json()["code"] == "mode_not_allowed" and "Tell the user" in r.json()["detail"]
    task = run_task(api.alice, "open maps", permission_mode="ask")
    assert task["permission_mode"] == "ask"
    keys = api.alice.console.get("/api/developer/keys").json()
    assert next(k for k in keys if k["id"] == api.alice.key_id)["max_mode"] == "auto"
    r = api.alice.console.patch(f"/api/developer/keys/{api.alice.key_id}", json={"max_mode": "trusted"})
    assert r.json()["max_mode"] == "trusted"
    # The key may now ask for trusted, but the phone's ceiling (auto) still says no.
    r = api.alice.v1.post(f"/v1/devices/{api.alice.device}/tasks",
                          json={"instruction": "open maps", "permission_mode": "trusted"})
    assert r.status_code == 403 and "phone" in r.json()["detail"]
    wait_status(api.alice, task["id"], "succeeded", "failed")


def test_api_tightening_applies_and_loosening_an_old_phone_is_refused(api):
    dev = api.alice.device
    r = api.alice.v1.patch(f"/v1/devices/{dev}/permissions", json={"default_mode": "ask"})
    assert r.status_code == 200 and r.json()["settings"]["default_mode"] == "ask"
    assert api.alice.v1.get(f"/v1/devices/{dev}/permissions").json()["key_max_mode"] == "auto"
    r = api.alice.v1.patch(f"/v1/devices/{dev}/permissions", json={"default_mode": "auto"})
    assert r.status_code == 200  # back up to the ceiling: not past it
    assert api.bob.v1.get(f"/v1/devices/{dev}/permissions").status_code == 404


def test_orchestrations_check_the_mode_up_front(api):
    r = api.alice.v1.post("/v1/orchestrations", json={"permission_mode": "never", "steps": [
        {"id": "a", "device_id": api.alice.device, "instruction": "open maps"}]})
    assert r.status_code == 403 and r.json()["code"] == "mode_not_allowed"


# --- MCP: what calling agents read ------------------------------------------------------------------


def test_mcp_tools_explain_permission_modes(api):
    tools = {t.name: t for t in mcp(api, api.alice, lambda c: c.list_tools()).tools}
    for name in ("run_task", "start_orchestration"):
        tool = tools[name]
        assert "permission_mode" in tool.input_schema["properties"]
        assert "mode_not_allowed" in tool.description and "max_mode" in tool.description
        assert "never" in tool.input_schema["properties"]["permission_mode"]["description"]
    assert "needs_phone_approval" in tools["change_permissions"].description
    assert "can't approve" in tools["change_permissions"].description


def test_mcp_refusals_are_stable_and_say_report_dont_retry(api):
    refused = call(api, api.alice, "run_task", instruction="open maps", permission_mode="never", wait_seconds=0)
    assert refused.is_error and "mode_not_allowed: " in text(refused)
    assert "don't retry" in text(refused)
    refused = call(api, api.alice, "change_permissions", never="1h")
    assert refused.is_error and "mode_not_allowed" in text(refused)
    applied = ok(call(api, api.alice, "change_permissions", default_mode="ask"))
    assert applied["status"] == "applied"


def test_mcp_next_hints_for_each_state(api):
    from openmob.platform.mcp import _next
    from openmob.platform.principal import Principal

    p = Principal(id="k", account_id="a", scopes=frozenset(), name="n")
    waiting = _next({"status": "needs_confirmation"}, p)
    assert "phone" in waiting and "get_task" in waiting and "wait_seconds" in waiting
    assert "Don't start the task again" in waiting and "approve=false" in waiting
    assert "answer_question" in _next({"status": "needs_input"}, p)
    final = _next({"status": "failed"}, p)
    assert "refused" in final and "don't start the task again" in final
    assert "get_task" in _next({"status": "running"}, p)
    task = ok(call(api, api.alice, "run_task", instruction="check the cart", wait_seconds=10))
    assert task["status"] == "needs_confirmation" and task["next"] == waiting
    assert task["permission_mode"] == "auto"


def test_the_dashboard_offers_also_allow_purchases_off_by_default(hosted):
    page = hosted.alice.get("/").text
    assert "Also allow purchases" in page and "allow_money: true" in page
    assert "let neverMoney = false;" in page  # unchecked by default, and reset after each request
    assert "including purchases" in page  # the chip and banner say so while it's on
