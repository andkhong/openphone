"""Risky actions are approved only through the screen-lock prompt: a typed or spoken "yes" (from the
phone app, the dashboard, or the agent typing into the OpenMob app itself) never approves. And no
task may operate the OpenMob app."""

import httpx
from conftest import FakeDriver
from test_remote import TOKEN, FakePhone, _last_status, risky, wait_for  # noqa: F401  (risky: fixture)


def test_a_yes_command_does_not_approve(risky):
    url, manager = risky
    driver = FakeDriver()
    phone = FakePhone(url, driver)
    assert phone.welcome.wait(5)
    phone.send({"type": "command", "text": "check the cart"})
    assert wait_for(lambda: phone.approval_requests)
    phone.send({"type": "command", "text": "yes"})  # the in-chat Yes button, voice, or the agent typing
    assert wait_for(lambda: any("screen lock" in m for k, m in phone.events if k == "status"))
    assert _last_status(manager) == "needs_confirmation" and not any(a[0] == "tap" for a in driver.actions)
    base = url.replace("ws://", "http://")
    r = httpx.post(f"{base}/api/devices/app-xyz/command?t={TOKEN}", json={"text": "yes go ahead"})
    assert r.json() == {"result": "approval_required"}  # the dashboard can't approve by text either
    assert _last_status(manager) == "needs_confirmation"
    phone.send({"type": "approval", "id": phone.approval_requests[-1]["id"], "approved": True})  # screen lock
    assert wait_for(lambda: _last_status(manager) == "succeeded")
    assert any(a[0] == "tap" for a in driver.actions)
    phone.disconnect()


def test_no_declines_and_stop_stops_during_a_confirmation(risky):
    url, manager = risky
    driver = FakeDriver()
    phone = FakePhone(url, driver)
    assert phone.welcome.wait(5)
    phone.send({"type": "command", "text": "check the cart"})
    assert wait_for(lambda: phone.approval_requests)
    phone.send({"type": "command", "text": "no thanks"})
    assert wait_for(lambda: _last_status(manager) in ("succeeded", "failed"))
    assert not any(a[0] == "tap" for a in driver.actions)
    phone.send({"type": "command", "text": "check the cart again"})
    assert wait_for(lambda: len(phone.approval_requests) == 2)
    phone.send({"type": "command", "text": "stop"})
    assert wait_for(lambda: _last_status(manager) == "cancelled")
    phone.disconnect()


def test_the_agent_never_operates_the_openmob_app(risky):
    url, manager = risky
    driver = FakeDriver()
    driver.package = "dev.openmob.companion"  # the OpenMob app is on screen (and stays there)
    phone = FakePhone(url, driver)
    assert phone.welcome.wait(5)
    session = manager.get("app-xyz")
    task = session.coordinator.enqueue("check the cart", "dashboard")
    assert wait_for(lambda: session.coordinator.task(task)["status"] in ("succeeded", "failed", "cancelled"))
    assert ("key", "HOME") in driver.actions  # it tried to leave the app first
    assert session.coordinator.task(task)["status"] == "failed" and not any(a[0] == "tap" for a in driver.actions)
    phone.disconnect()
