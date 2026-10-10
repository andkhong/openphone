"""Permission modes in the agent loop (openmob/permissions.py, agent/gate.py): when a task asks its
owner, what runs without asking, and what is refused. A fake phone and a recording IO stand in."""

import time

import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob import permissions as perm
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver

WA = "com.whatsapp"
SHOP = "com.amazon.mShop.android.shopping"
DONE = [tool_use("done", "z", summary="ok", success=True)]


class IO(RecordingIO):
    """Records prompts with their details (like the Coordinator's confirm) and their telemetry."""

    def __init__(self, answers=None):
        super().__init__(answers)
        self.details, self.prompts = [], []

    def confirm(self, question, details=None):
        self.details.append(details)
        return self.ask(question)

    def record_prompt(self, prompt):
        self.prompts.append(prompt)


def el(i, text, cls="Button", bounds=None, checked=None, editable=False):
    return UIElement(i, text, "", "", cls, True, checked, bounds or (0, 300 + 100 * i, 1000, 380 + 100 * i),
                     editable=editable)


def place_targets(steps, elements):
    """Coordinate taps are judged by what is really at the point, so put a button labeled like each
    tap's target on screen (unless one is there already) and aim the tap at it."""
    elements = list(elements or [])
    for step in steps:
        for t in step:
            target = t.input.get("target")
            if t.name not in ("tap", "long_press") or not target or "x" not in t.input:
                continue
            button = next((e for e in elements if e.text == target), None)
            if button is None:
                top = 1600 + 90 * sum(e.bounds[1] >= 1600 for e in elements)
                button = UIElement(len(elements), target, "", "", "Button", True, None, (0, top, 1000, top + 80))
                elements.append(button)
            t.input["x"], t.input["y"] = button.center[0] // 2, button.center[1] // 2  # screenshot is half size
    return elements


def agent(steps, tmp_path, io=None, package=WA, elements=None, settings=None, mode="auto", **cfg):
    elements = place_targets(steps, elements)
    driver = FakeDriver(packages=[WA, SHOP, "com.android.settings", "com.google.android.apps.messaging"],
                        elements=elements or [], labels={WA: "WhatsApp", SHOP: "Amazon"})
    driver.package = package
    config = Config(settle_delay=0, launch_settle_delay=0, **cfg)
    resolver = AppResolver(driver, {"whatsapp": WA, "amazon": SHOP}, label_cache=tmp_path / "l.json")
    io = io or IO()
    resolver.ensure_labels()  # display names, as a connected phone has them
    a = Agent(ScriptedClient([*steps, DONE, DONE]), driver, resolver, config, io, use_ocr=False)
    store = {"s": perm.normalize(settings or perm.default_settings())}
    perms = perm.TaskPermissions(mode, lambda: store["s"])
    return a, driver, io, perms, store


def taps(driver):
    return [a for a in driver.actions if a[0] == "tap"]


def tap(target, i="a"):
    return [tool_use("tap", i, x=10, y=10, target=target)]


# --- auto ------------------------------------------------------------------------------------


def test_auto_requested_send_runs_once_then_asks(tmp_path):
    a, driver, io, perms, _ = agent([tap("Send"), tap("Send", "b")], tmp_path, IO(["no"]))
    a.run("send hi to mom", permissions=perms)
    assert len(taps(driver)) == 1 and len(io.questions) == 1  # the second send isn't the one asked for


def test_auto_exemption_only_in_the_tasks_app(tmp_path):
    steps = [[tool_use("launch_app", "l", app="whatsapp")], [tool_use("launch_app", "m", app="amazon")], tap("Send")]
    a, driver, io, perms, _ = agent(steps, tmp_path, IO(["no"]), package="com.android.launcher3")
    a.run("send hi to mom", permissions=perms)
    assert io.questions and "Amazon" in io.questions[0] and not taps(driver)


def test_auto_money_asks_even_when_requested_with_the_price(tmp_path):
    screen = [el(0, "Order total: $12.99", cls="TextView"), el(1, "Buy now")]
    a, driver, io, perms, _ = agent([tap("Buy now")], tmp_path, IO(["no"]), package=SHOP, elements=screen)
    a.run("buy the usb cable", permissions=perms)
    assert len(io.questions) == 1 and "$12.99" in io.questions[0] and "Amazon" in io.questions[0]
    assert not taps(driver)


def test_on_the_way_steps_never_ask(tmp_path):
    a, driver, io, perms, _ = agent([tap("Add to cart"), tap("Proceed to checkout", "b")], tmp_path, package=SHOP)
    a.run("put the cable in my cart", permissions=perms)
    assert not io.questions and len(taps(driver)) == 2


def test_one_prompt_per_purchase_then_one_terminal_follow_up(tmp_path):
    steps = [tap("Buy now"), tap("Place order", "b"), tap("Place order", "c")]
    a, driver, io, perms, _ = agent(steps, tmp_path, IO(["yes", "no"]), package=SHOP)
    a.run("buy the usb cable", permissions=perms)
    # Buy now asked; the one Place order follow-up is covered; a second one asks again.
    assert len(io.questions) == 2 and len(taps(driver)) == 2


def test_start_flow_words_and_terminal_approvals_mint_nothing(tmp_path):
    a, driver, io, perms, _ = agent([tap("Buy now"), tap("Subscribe $4.99/month", "b")], tmp_path, IO(["yes", "no"]),
                                    package=SHOP)
    a.run("buy the cable", permissions=perms)
    assert len(io.questions) == 2  # a new flow (subscribe) asks again
    a, driver, io, perms, _ = agent([tap("Place order"), tap("Pay now", "b")], tmp_path, IO(["yes", "no"]),
                                    package=SHOP)
    a.run("buy the cable", permissions=perms)
    assert len(io.questions) == 2  # approving a terminal tap covers no follow-up


def test_follow_up_ends_on_an_app_switch_and_after_three_minutes(tmp_path, monkeypatch):
    steps = [tap("Buy now"), [tool_use("launch_app", "l", app="whatsapp")], [tool_use("launch_app", "m", app="amazon")],
             tap("Place order", "b")]
    a, driver, io, perms, _ = agent(steps, tmp_path, IO(["yes", "no"]), package=SHOP)
    a.run("buy the cable", permissions=perms)
    assert len(io.questions) == 2
    # Past the follow-up window (shrunk here from 3 minutes so the test needn't wait): it asks again.
    monkeypatch.setattr("openmob.agent.loop.FOLLOWUP_SECONDS", 0.2)
    a, driver, io, perms, _ = agent([tap("Buy now"), [tool_use("wait", "w", seconds=0.5)], tap("Place order", "b")],
                                    tmp_path, IO(["yes", "no"]), package=SHOP)
    a.run("buy the cable", permissions=perms)
    assert len(io.questions) == 2


def test_always_allow_is_offered_with_what_it_grants(tmp_path):
    a, _, io, perms, _ = agent([tap("Send")], tmp_path, IO(["no"]))
    a.run("open whatsapp", permissions=perms)
    offer = io.details[0]["always"]
    assert offer["app"] == WA and offer["kind"] == "send" and offer["days"] == 30
    assert offer["text"] == "Agents could send any WhatsApp message as you for 30 days."
    a, _, io, perms, _ = agent([tap("Buy now")], tmp_path, IO(["no"]), package=SHOP)
    a.run("open amazon", permissions=perms)
    offer = io.details[0]["always"]
    assert offer["kind"] == "pay" and offer["days"] == 1 and offer["confirm_twice"] is True


def test_always_allow_scoping(tmp_path):
    # No offer for autofill in a browser; in a native app it's bound to that app.
    a, _, io, perms, _ = agent([tap("Use saved password")], tmp_path, IO(["no"]), package="com.android.chrome")
    a.run("log in", permissions=perms)
    assert io.questions and "always" not in io.details[0]
    a, _, io, perms, _ = agent([tap("Use saved password")], tmp_path, IO(["no"]))
    a.run("log in", permissions=perms)
    assert io.details[0]["always"]["kind"] == "fill" and io.details[0]["always"]["app"] == WA
    # A share is bound to its target; Settings changes get no offer.
    a, _, io, perms, _ = agent([tap("Share to Mom")], tmp_path, IO(["no"]))
    a.run("open whatsapp", permissions=perms)
    assert io.details[0]["always"]["target"] == "share to mom"
    screen = [el(0, "Apps", cls="TextView", bounds=(0, 50, 500, 120)), el(1, "Turn on Smart Storage", cls="Switch",
                                                                             checked=False)]
    a, _, io, perms, _ = agent([[tool_use("tap_element", "a", index=1)]], tmp_path, IO(["no"]),
                               package="com.android.settings", elements=screen)
    a.run("open settings", permissions=perms)
    assert io.questions and "always" not in io.details[0]


# --- trusted entries -------------------------------------------------------------------------


def entry(**kw):
    return {"expires_at": time.time() + 3600, **kw}


def test_app_entries_count_in_auto_categories_only_in_trusted(tmp_path):
    msgs = "com.google.android.apps.messaging"
    settings = {**perm.default_settings(), "ceiling": "trusted",
                "trusted": [entry(category="messaging", kinds=["send"]), entry(app=WA, kinds=["send"])]}
    a, driver, io, perms, _ = agent([tap("Send")], tmp_path, settings=settings)
    a.run("open whatsapp", permissions=perms)
    assert not io.questions and taps(driver)  # app entry, in auto
    a, driver, io, perms, _ = agent([tap("Send")], tmp_path, IO(["no"]), package=msgs, settings=settings)
    a.run("open messages", permissions=perms)
    assert io.questions  # a category entry doesn't count in auto
    a, driver, io, perms, _ = agent([tap("Send")], tmp_path, package=msgs, settings=settings, mode="trusted")
    a.run("open messages", permissions=perms)
    assert not io.questions and taps(driver)


def test_trusted_scope_kind_expiry_and_pay(tmp_path):
    settings = {**perm.default_settings(), "ceiling": "trusted",
                "trusted": [entry(app=WA, kinds=["send"]), {"app": SHOP, "kinds": ["pay"], "expires_at": time.time() - 1}]}
    a, _, io, perms, _ = agent([tap("Delete chat")], tmp_path, IO(["no"]), settings=settings, mode="trusted")
    a.run("open whatsapp", permissions=perms)
    assert io.questions  # another kind
    a, _, io, perms, _ = agent([tap("Buy now")], tmp_path, IO(["no"]), package=SHOP, settings=settings, mode="trusted")
    a.run("open amazon", permissions=perms)
    assert io.questions  # the pay entry expired
    with pytest.raises(perm.PermissionsInvalid):
        perm.validate_entry({"category": "shopping", "kinds": ["pay"]}, {"shopping": ["shop"]})
    live = {**settings, "trusted": [entry(app=SHOP, kinds=["pay"])]}
    a, driver, io, perms, _ = agent([tap("Buy now")], tmp_path, package=SHOP, settings=live, mode="trusted")
    a.run("open amazon", permissions=perms)
    assert not io.questions and taps(driver)
    stored = perm.validate_entry({"app": SHOP, "kinds": ["pay"], "expires_at": time.time() + 30 * 86400}, {})
    assert stored["expires_at"] <= time.time() + 7 * 86400 + 1  # pay grants last 7 days at most


def test_share_grants_are_bound_to_their_target(tmp_path):
    settings = {**perm.default_settings(), "trusted": [perm.always_entry(WA, "send", "Share to Mom")]}
    a, driver, io, perms, _ = agent([tap("Share to Mom"), tap("Share to Boss", "b")], tmp_path, IO(["no"]),
                                    settings=settings)
    a.run("open whatsapp", permissions=perms)
    assert len(taps(driver)) == 1 and len(io.questions) == 1


# --- ask every step ---------------------------------------------------------------------------


def test_ask_mode_asks_every_change_and_groups_typing(tmp_path):
    field = el(0, "Message", cls="EditText", editable=True)
    steps = [[tool_use("tap_element", "a", index=0)], [tool_use("type_text", "b", text="hi mom", submit=True)],
             [tool_use("scroll", "c", direction="down")], [tool_use("press_key", "d", key="BACK")],
             [tool_use("type_text", "e", text="again")], [tool_use("press_key", "f", key="ENTER")],
             tap("Send", "g")]
    a, driver, io, perms, _ = agent(steps, tmp_path, IO(["yes"] * 5), elements=[field], mode="ask")
    result = a.run("send hi to mom", permissions=perms)
    # Focusing the field, scrolling and BACK don't ask; typing + its submit is one prompt; Send one more.
    assert len(io.questions) == 3 and "hi mom" in io.questions[0] and "press Enter" in io.questions[0]
    assert ("key", "ENTER") in driver.actions and len(taps(driver)) == 2
    assert result.prompts == 3 and [p["kind"] for p in io.prompts] == ["step", "step", "step"]


def test_ask_mode_launches_and_denials(tmp_path):
    a, driver, io, perms, _ = agent([[tool_use("launch_app", "a", app="whatsapp")]], tmp_path, IO(["no"]),
                                    package="com.android.launcher3", mode="ask")
    a.run("open whatsapp", permissions=perms)
    assert io.questions and not any(x[0] == "launch" for x in driver.actions)


# --- plan first -----------------------------------------------------------------------------


PLAN = [tool_use("propose_plan", "p", steps=[{"text": "Open the chat with Mom", "app": "WhatsApp"},
                                             {"text": "Send the message 'hi'", "app": "WhatsApp"}])]


def test_plan_first_then_covered_steps_run(tmp_path):
    a, driver, io, perms, _ = agent([tap("Chats"), PLAN, tap("Send", "b")], tmp_path, IO(["yes"]), mode="plan")
    result = a.run("send hi to mom", permissions=perms)
    assert len(io.questions) == 1 and "1. Open the chat with Mom" in io.questions[0]
    assert io.details[0]["kind"] == "plan" and len(taps(driver)) == 1  # the tap before the plan was refused
    assert any(t["name"] == "propose_plan" for t in a.client.requests[0]["tools"])  # offered only in plan mode
    assert result.prompts == 1


def test_plan_deviation_and_money_ask(tmp_path):
    a, driver, io, perms, _ = agent([PLAN, tap("Delete chat", "b"), tap("Send", "c"), tap("Send", "d")], tmp_path,
                                    IO(["yes", "no", "no"]), mode="plan")
    a.run("send hi to mom", permissions=perms)
    assert len(io.questions) == 3  # plan; the off-plan delete; a second send (each step covers once)
    plan = [tool_use("propose_plan", "p", steps=[{"text": "Buy the cable", "app": "Amazon"}])]
    a, driver, io, perms, _ = agent([plan, tap("Buy now", "b")], tmp_path, IO(["yes", "no"]), package=SHOP,
                                    mode="plan")
    a.run("buy the cable", permissions=perms)
    assert len(io.questions) == 2 and not taps(driver)  # money always asks


def test_plan_rejected_ends_the_task_and_changes_revise_it(tmp_path):
    a, driver, io, perms, _ = agent([PLAN], tmp_path, IO(["no"]), mode="plan")
    result = a.run("send hi to mom", permissions=perms)
    assert not result.success and "rejected" in result.summary
    a, driver, io, perms, _ = agent([PLAN, PLAN, tap("Send")], tmp_path, IO(["skip the first step", "yes"]), mode="plan")
    a.run("send hi to mom", permissions=perms)
    assert len(io.questions) == 2 and taps(driver) and io.prompts[0]["decision"] == "changes"


# --- never -----------------------------------------------------------------------------------


def never_settings(seconds=3600):
    return {**perm.default_settings(), "never_until": time.time() + seconds}


def test_never_runs_without_prompts_but_refuses_money_and_autofill(tmp_path):
    a, driver, io, perms, _ = agent([tap("Send"), tap("Delete chat", "b"), tap("Use saved password", "c")],
                                    tmp_path, settings=never_settings(), mode="never")
    a.run("open whatsapp", permissions=perms)
    assert not io.questions and len(taps(driver)) == 2  # autofill refused
    a, driver, io, perms, _ = agent([tap("Buy now")], tmp_path, package=SHOP, settings=never_settings(), mode="never")
    a.run("buy the cable", permissions=perms)
    assert not io.questions and not taps(driver)
    # A per-app pay entry no longer lets money run under never: only "Also allow purchases" does.
    paid = {**never_settings(), "trusted": [entry(app=SHOP, kinds=["pay"])]}
    a, driver, io, perms, _ = agent([tap("Buy now")], tmp_path, package=SHOP, settings=paid, mode="never")
    a.run("buy the cable", permissions=perms)
    assert not io.questions and not taps(driver)
    allowed = {**never_settings(), "never_money": True}
    a, driver, io, perms, _ = agent([tap("Buy now")], tmp_path, package=SHOP, settings=allowed, mode="never")
    a.run("buy the cable", permissions=perms)
    assert not io.questions and taps(driver)


def test_allow_purchases_goes_only_with_never_and_ends_with_it():
    cats = perm.DEFAULT_APP_CATEGORIES
    base = perm.default_settings()
    on, loosens = perm.apply_patch(base, {"never": "1h", "allow_money": True}, cats)
    assert on["never_money"] and "money:1h" in loosens
    assert perm.describe_loosening(loosens) == ("Allow agents to act without asking on this phone including "
                                                "purchases for 1 hour?")
    assert perm.describe_loosening(perm.apply_patch(base, {"never": "1h"}, cats)[1]) == (
        "Allow agents to act without asking on this phone for 1 hour?")
    assert perm.apply_patch(base, {"never": "1h"}, cats)[0]["never_money"] is False  # off by default
    with pytest.raises(perm.PermissionsInvalid):
        perm.apply_patch(on, {"allow_money": True}, cats)  # only together with turning never on
    # Extending never without it (or allow_money false, or never off) turns purchases off: tightenings.
    assert perm.apply_patch(on, {"never": "24h"}, cats)[0]["never_money"] is False
    off, loosens = perm.apply_patch(on, {"allow_money": False}, cats)
    assert not off["never_money"] and loosens == [] and off["never_until"]
    assert perm.apply_patch(on, {"never": "off"}, cats) == ({**perm.normalize(base)}, [])
    # Re-asking for purchases always needs the phone, even while they are on.
    assert "money:1h" in perm.apply_patch(on, {"never": "1h", "allow_money": True}, cats)[1]
    # It expires with the never window.
    assert perm.normalize({**on, "never_until": time.time() - 1})["never_money"] is False
    assert perm.public(on)["never_money"] is True


def test_never_expiry_reverts_to_auto_and_blocks_sensitive_apps(tmp_path):
    a, driver, io, perms, store = agent([tap("Send")], tmp_path, IO(["no"]), settings=never_settings(), mode="never")
    store["s"] = {**store["s"], "never_until": time.time() - 1}  # expired before the tap
    assert perms.mode() == "auto"
    a.run("open whatsapp", permissions=perms)
    assert io.questions
    assert perm.normalize({"default_mode": "never", "never_until": time.time() - 1})["default_mode"] == "auto"
    bank = "com.chase.sig.android"
    a, driver, io, perms, _ = agent([[tool_use("launch_app", "a", app=bank)]], tmp_path, settings=never_settings(),
                                    mode="never")
    a.driver.packages.append(bank)
    result = a.run("check my balance", permissions=perms)
    assert not any(x == ("launch", bank) for x in driver.actions) and not io.questions
    assert result is not None


# --- Settings tiers ---------------------------------------------------------------------------


def settings_screen(title, *items):
    return [el(0, title, cls="TextView", bounds=(0, 60, 600, 140))] + [el(i + 1, t, **kw) for i, (t, kw) in
                                                                       enumerate(items)]


@pytest.mark.parametrize("mode", ["auto", "never"])
def test_security_critical_settings_are_refused_in_every_mode(tmp_path, mode):
    screen = settings_screen("Developer options", ("Wireless debugging", {"cls": "Switch", "checked": False}))
    a, driver, io, perms, _ = agent([[tool_use("tap_element", "a", index=1)]], tmp_path, package="com.android.settings",
                                    elements=screen, settings=never_settings(), mode=mode)
    a.run("turn on wireless debugging", permissions=perms)
    assert not taps(driver) and not io.questions


def test_everyday_settings_never_ask_and_new_pairing_asks(tmp_path):
    screen = settings_screen("Display", ("Dark theme", {"cls": "Switch", "checked": False}))
    a, driver, io, perms, _ = agent([[tool_use("tap_element", "a", index=1)]], tmp_path, package="com.android.settings",
                                    elements=screen)
    a.run("turn on dark mode", permissions=perms)
    assert taps(driver) and not io.questions
    screen = settings_screen("Connected devices", ("Pair new device", {}))
    a, driver, io, perms, _ = agent([[tool_use("tap_element", "a", index=1)]], tmp_path, IO(["no"]),
                                    package="com.android.settings", elements=screen)
    a.run("pair my headphones", permissions=perms)
    assert io.questions and not taps(driver)
    screen = settings_screen("Some vendor page", ("Enable turbo", {"cls": "Switch", "checked": False}))
    a, driver, io, perms, _ = agent([[tool_use("tap_element", "a", index=1)]], tmp_path, IO(["no"]),
                                    package="com.android.settings", elements=screen)
    a.run("enable turbo", permissions=perms)
    assert io.questions  # an unrecognized page's change asks


def test_app_info_page(tmp_path):
    screen = settings_screen("App info", ("WhatsApp", {"cls": "TextView"}), ("Force stop", {}), ("Uninstall", {}))
    a, driver, io, perms, _ = agent([[tool_use("tap_element", "a", index=2)], [tool_use("tap_element", "b", index=3)]],
                                    tmp_path, IO(["no"]), package="com.android.settings", elements=screen)
    a.run("stop whatsapp", permissions=perms)
    assert len(taps(driver)) == 1 and len(io.questions) == 1  # force stop runs; uninstall asks
    a, driver, io, perms, _ = agent([[tool_use("tap_element", "a", index=2)]], tmp_path,
                                    package="com.android.settings", elements=screen)
    a.run("stop whatsapp", blocked=lambda p: p == WA, permissions=perms)
    assert not taps(driver) and not io.questions  # a blocked app's info page: refused


# --- telemetry ---------------------------------------------------------------------------------


def test_prompts_are_counted_with_decision_and_latency(tmp_path):
    a, driver, io, perms, _ = agent([tap("Delete chat"), tap("Send", "b")], tmp_path, IO(["yes", "no thanks"]))
    result = a.run("open whatsapp", permissions=perms)
    assert result.prompts == 2 and result.mode == "auto"
    assert [p["decision"] for p in io.prompts] == ["approved", "denied"]
    assert all(isinstance(p["latency_ms"], int) for p in io.prompts) and io.prompts[0]["action_kind"] == "delete"


def test_prompt_report_summarizes_the_budget():
    from openmob.prompt_report import format_report, summarize

    prompts = [{"decision": "approved", "latency_ms": 1500, "action_kind": "pay"},
               {"decision": "approved", "latency_ms": 5000, "action_kind": "send"},
               {"decision": "denied", "latency_ms": 9000, "action_kind": "send"}]
    tasks = [{"prompts": 0, "mode": "auto"}, {"prompts": 1, "mode": "auto"}, {"prompts": 2, "mode": "ask"}]
    s = summarize(prompts, tasks)
    assert s["median_prompts_per_task"] == 1 and s["fast_approval_share"] == 0.5
    assert "OVER" in format_report(s) and "Median prompts per task: 1" in format_report(s)


def test_low_risk_flows_dont_ask_on_any_phone(tmp_path):
    # YouTube's free Subscribe doesn't ask; in a shopping app it's money.
    a, driver, io, perms, _ = agent([tap("Subscribe")], tmp_path, package="com.google.android.youtube")
    a.run("subscribe to pewdiepie", permissions=perms)
    assert not io.questions and len(taps(driver)) == 1
    a, driver, io, perms, _ = agent([tap("Subscribe")], tmp_path, IO(["no"]), package=SHOP)
    a.run("subscribe", permissions=perms)
    assert len(io.questions) == 1
    # A chat's title never makes a send "to yourself" (anyone can name a group "Note to self"): it asks.
    title = UIElement(0, "Note to self", "", "", "TextView", False, None, (0, 50, 1000, 120))
    link = UIElement(1, "https://youtube.com/@PewDiePie", "", "", "TextView", False, None, (0, 1200, 1000, 1280))
    a, driver, io, perms, _ = agent([tap("Send")], tmp_path, IO(["no"]), elements=[title, link])
    a.run("share his channel with myself", permissions=perms)
    assert len(io.questions) == 1
    from openmob.agent import gate
    assert not gate.is_self_label("Miss you") and gate.is_self_label("Ann Lee (You)", ["Ann Lee"])


def test_everyday_setting_skips_everyday_confirmations_but_not_money(tmp_path):
    settings = {**perm.default_settings(), "everyday": True}
    a, driver, io, perms, _ = agent([tap("Send")], tmp_path, settings=settings)
    a.run("message Bob", permissions=perms)
    assert not io.questions and len(taps(driver)) == 1
    a, driver, io, perms, _ = agent([tap("Buy now")], tmp_path, IO(["no"]), package=SHOP, settings=settings)
    a.run("buy it", permissions=perms)
    assert len(io.questions) == 1 and not taps(driver)
    new, loosens = perm.apply_patch(perm.default_settings(), {"everyday": True}, {})
    assert loosens == ["everyday:on"] and "everyday actions" in perm.describe_loosening(loosens)
    assert perm.apply_patch(new, {"everyday": False}, {})[1] == []  # turning it off is free


def test_permission_prompt_defaults_loosen_only_with_the_phone():
    from openmob import permissions as perm

    cur = perm.default_settings()
    assert perm.normalize(cur)["permission_prompts"]["location"] == "once"
    assert perm.normalize(cur)["permission_prompts"]["notifications"] == "allow"
    assert perm.normalize(cur)["permission_prompts"]["location_accuracy"] == "approximate"
    new, loosens = perm.apply_patch(cur, {"permission_prompts": {"location": "while_using",
                                                                 "location_accuracy": "precise"}}, {})
    assert loosens == ["permission:location:while_using", "permission:location_accuracy:precise"]
    assert "precise location" in perm.describe_loosening(loosens)
    tightened, loosens = perm.apply_patch(new, {"permission_prompts": {"location": "deny"}}, {})
    assert loosens == [] and tightened["permission_prompts"]["location"] == "deny"
    for bad in ({"location": "always"}, {"teleport": "ask"}, ["location"]):
        with pytest.raises(perm.PermissionsInvalid):
            perm.apply_patch(cur, {"permission_prompts": bad}, {})
    assert perm.normalize({"permission_prompts": {"camera": "always"}})["permission_prompts"]["camera"] == "once"
