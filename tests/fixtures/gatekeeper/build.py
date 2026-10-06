"""Builds the gatekeeper's shared scenario fixtures: screens.json, scenarios.json and adversarial.json.

Run `python tests/fixtures/gatekeeper/build.py` after editing; the JSON files are committed. Both the Python
reference (tests/test_gatekeeper_conformance.py, tests/test_gatekeeper_adversarial.py) and the Kotlin engine
(android/app/src/test/.../GateScenarioTest.kt) run every scenario, so the two can't drift apart.

A scenario is a list of steps run against one fresh gatekeeper:
  {"call": {"method", "params"}, "screen": <name>, "reverify_screen"?: <name>, "expect": {decision, code?, kind?,
   rule?, money?}}       admit, then (if allowed) reverify right before the side effect
  {"approve": "last", "sig": "valid" | "bad", "grant"?: bool, "binding"?: "other"}   the owner's screen-lock yes
  {"deny": "last"}       {"advance_ms": n}    {"set_wall_ms": n} (the wall clock: must change nothing)
  {"reboot": {"saved": bool, "elapsed_after": n}}    {"policy": {...record}, "sig": "valid" | "bad" | "none",
   "expect_accepted": bool}    {"instruction": "send hi to Mom"}    {"stop": true}
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
W, H = 1080, 2400
FULL = [0, 0, W, H]


def node(text="", desc="", id="", cls="android.widget.TextView", bounds=(0, 0, 0, 0), children=(), **flags):
    d = {"text": text, "desc": desc, "id": id, "cls": cls, "bounds": list(bounds), "children": list(children)}
    d.update(flags)
    return d


def button(text, id, bounds, desc="", cls="android.widget.Button"):
    return node(text=text, desc=desc, id=id, cls=cls, bounds=bounds, clickable=True)


def row(text, top, id="android:id/title", checked=None):
    """A Settings preference row: a clickable layout with a title (and maybe a switch)."""
    kids = [node(text=text, id=id, bounds=(48, top + 20, 800, top + 80))]
    if checked is not None:
        kids.append(node(id="android:id/switch_widget", cls="android.widget.Switch", bounds=(900, top + 20, 1040, top + 100),
                         checked=checked, clickable=False))
    return node(cls="android.widget.LinearLayout", bounds=(0, top, W, top + 120), clickable=True, children=kids,
                **({"checked": checked} if checked is not None else {}))


def app(package, children, title="", id=1, layer=1):
    return {"id": id, "type": "application", "layer": layer, "package": package, "title": title, "bounds": FULL,
            "root": node(cls="android.widget.FrameLayout", bounds=FULL, children=children)}


def screen(*windows, **extra):
    return {"windows": list(windows), **extra}


AMAZON = "com.amazon.mShop.android.shopping"
LABELS = {AMAZON: "Amazon", "com.whatsapp": "WhatsApp", "com.chase.sig.android": "Chase", "com.android.chrome": "Chrome",
          "com.android.settings": "Settings", "dev.openmob.companion": "OpenMob", "com.chess": "Chess.com",
          "com.google.android.gm": "Gmail", "com.google.android.contacts": "Contacts", "com.example.app": "Example"}


def settings(title, rows, activity="com.android.settings.SubSettings", extra_nodes=()):
    kids = [node(text=title, id="com.android.settings:id/collapsing_toolbar", bounds=(48, 150, 900, 260))]
    kids += [row(*r) if isinstance(r, tuple) else row(r, 420 + 140 * i) for i, r in enumerate(rows)]
    kids += list(extra_nodes)
    return screen(app("com.android.settings", kids, title=title), activity=activity, labels=LABELS)


SCREENS = {
    "launcher": screen(app("com.google.android.apps.nexuslauncher", [button("Amazon", "", (0, 1800, 270, 2000))]),
                       labels=LABELS, resolve={
                           "https://www.chase.com/": {"package": "com.chase.sig.android", "candidates": ["com.chase.sig.android", "com.android.chrome"]},
                           "https://example.com/": {"package": "com.android.chrome"},
                           "https://www.amazon.com/dp/B0": {"package": AMAZON},
                           "tel:5551234": {"package": "com.google.android.dialer"},
                           "tel:*21*5551234%23": {"package": "com.evil.tel"},
                           "upi://pay?pa=x@y": {"package": "com.google.android.apps.nbu.paisa.user"},
                           "intent://scan/#Intent;scheme=zxing;end": {"package": ""},
                           "market://details?id=com.chess": {"package": "com.android.vending"},
                       }),
    "amazon_product": screen(app(AMAZON, [
        node(id="nav-search-keywords", cls="android.widget.EditText", hint="Search Amazon", editable=True,
             bounds=(40, 120, 1040, 220), clickable=True),
        node(text="USB-C cable, 2 pack, braided", bounds=(40, 400, 1040, 480)),
        node(text="$12.99", bounds=(40, 500, 400, 560)),
        node(text="In stock. Free delivery tomorrow", bounds=(40, 580, 1040, 640)),
        button("Add to cart", "add-to-cart-button", (40, 1500, 1040, 1620)),
        button("Buy now", "buy-now-button", (40, 1660, 1040, 1780)),
        node(cls="androidx.recyclerview.widget.RecyclerView", bounds=(0, 1900, W, 2300), children=[
            node(text="Customers also bought", bounds=(40, 1920, 1040, 1980))]),
        node(cls="android.widget.ImageButton", bounds=(940, 2300, 1060, 2390), clickable=True),
    ]), labels=LABELS),
    "amazon_checkout": screen(app(AMAZON, [
        node(text="Review your order", bounds=(40, 150, 1040, 230)),
        node(text="Order total: $12.99", bounds=(40, 600, 1040, 680)),
        node(text="Deliver to Ann, 1 Main St", bounds=(40, 700, 1040, 760)),
        button("Place your order", "placeYourOrder", (40, 1600, 1040, 1720)),
        node(cls="android.widget.ImageView", bounds=(40, 1800, 300, 1900), clickable=True),
    ]), labels=LABELS),
    "amazon_checkout_card": screen(app(AMAZON, [
        node(text="Add a card", bounds=(40, 150, 1040, 230)),
        node(id="cc-number", cls="android.widget.EditText", hint="Card number", editable=True, focused=True,
             bounds=(40, 400, 1040, 500)),
        node(text="Total $12.99", bounds=(40, 600, 1040, 680)),
        button("Pay now", "pay-button", (40, 1600, 1040, 1720)),
    ]), labels=LABELS),
    "amazon_autofill": screen(
        app(AMAZON, [node(id="cc-number", cls="android.widget.EditText", hint="Card number", editable=True,
                          focused=True, bounds=(40, 400, 1040, 500)),
                     node(text="Total $12.99", bounds=(40, 600, 1040, 680))]),
        {"id": 7, "type": "system", "layer": 5, "package": "com.google.android.gms", "title": "Autofill",
         "bounds": [0, 1700, W, 2000], "root": node(cls="android.widget.FrameLayout", bounds=(0, 1700, W, 2000), children=[
             button("Visa •••• 4242", "autofill_dataset", (40, 1720, 1040, 1820))])},
        autofill_service="com.google.android.gms", labels=LABELS),
    "slide_to_pay": screen(app("com.example.shop", [
        node(text="Total $49.00", bounds=(40, 600, 1040, 680)),
        node(desc="Slide to pay", id="slide_pay", cls="android.widget.SeekBar", bounds=(40, 1600, 1040, 1720),
             clickable=True)]), labels=LABELS),
    "chase": screen(app("com.chase.sig.android", [button("Transfer", "transfer", (40, 1600, 1040, 1720))]),
                    labels=LABELS),
    "login_form": screen(app("com.example.app", [
        node(id="username", cls="android.widget.EditText", editable=True, bounds=(40, 400, 1040, 500)),
        node(id="password", cls="android.widget.EditText", editable=True, password=True, focused=True,
             bounds=(40, 520, 1040, 620)),
        button("Sign in", "login", (40, 700, 1040, 800))]), labels=LABELS),
    "otp_form": screen(app("com.example.app", [
        node(id="otp_code", cls="android.widget.EditText", hint="Enter the 6-digit code", editable=True,
             focused=True, bounds=(40, 400, 1040, 500))]), labels=LABELS),
    "chrome_url_js": screen(app("com.android.chrome", [
        node(text="javascript:alert(document.cookie)", id="com.android.chrome:id/url_bar",
             cls="android.widget.EditText", editable=True, focused=True, bounds=(100, 100, 1000, 200))]),
        labels=LABELS),
    "chrome_url_bank": screen(app("com.android.chrome", [
        node(text="https://www.chase.com/", id="com.android.chrome:id/url_bar", cls="android.widget.EditText",
             editable=True, focused=True, bounds=(100, 100, 1000, 200))]),
        labels=LABELS, resolve={"https://www.chase.com/": {"package": "com.chase.sig.android"}}),
    "chrome_search": screen(app("com.android.chrome", [
        node(text="usb c cable", id="com.android.chrome:id/search_box_text", cls="android.widget.EditText",
             hint="Search or type web address", editable=True, focused=True, bounds=(100, 100, 1000, 200))]),
        labels=LABELS),
    "web_checkout_enter": screen(app("com.android.chrome", [
        node(cls="android.webkit.WebView", bounds=(0, 200, W, H), children=[
            node(text="Checkout", bounds=(40, 220, 1040, 300)),
            node(text="Total $89.00", bounds=(40, 320, 1040, 380)),
            node(id="email", cls="android.widget.EditText", hint="Email", editable=True, focused=True,
                 bounds=(40, 400, 1040, 500)),
            button("Pay $89.00", "submit", (40, 1600, 1040, 1720))])]), labels=LABELS),
    "whatsapp_chat": screen(app("com.whatsapp", [
        node(text="Mom", bounds=(140, 120, 600, 200)),
        node(text="hi", id="com.whatsapp:id/entry", cls="android.widget.EditText", hint="Message", editable=True,
             focused=True, bounds=(40, 2200, 900, 2300)),
        node(desc="Send", id="com.whatsapp:id/send", cls="android.widget.ImageButton", bounds=(920, 2200, 1060, 2300),
             clickable=True)]), labels=LABELS),
    "gmail_list": screen(app("com.google.android.gm", [
        node(text="Inbox", bounds=(40, 120, 600, 200)),
        node(desc="Delete", id="com.google.android.gm:id/delete", cls="android.widget.ImageButton",
             bounds=(900, 120, 1040, 220), clickable=True),
        node(cls="android.widget.LinearLayout", bounds=(0, 300, W, 500), clickable=True, children=[
            node(text="Weekly newsletter", bounds=(40, 320, 1040, 380))])]), labels=LABELS),
    "settings_main": settings("Settings", ["Network & internet", "Connected devices", "Apps", "Notifications",
                                           "Battery", "System", "Security & privacy"],
                              activity="com.android.settings.Settings"),
    "settings_internet": settings("Internet", [("Wi-Fi", 420, "android:id/title", True), "Airplane mode", "Hotspot & tethering"]),
    "settings_security": settings("Security & privacy", ["Device unlock", "App security", "Wireless debugging"],
                                  activity="com.android.settings.Settings$SecurityDashboardActivity"),
    "settings_dev": settings("Developer options", [("Wireless debugging", 420, "android:id/title", False)],
                             activity="com.android.settings.Settings$DevelopmentSettingsDashboardActivity"),
    "settings_keyboards": settings("Manage on-screen keyboards", [("Evil Keyboard", 420, "android:id/title", False)],
                                   activity="com.android.settings.SubSettings"),
    "settings_datetime": settings("Date & time", [("Set time automatically", 420, "android:id/title", True),
                                                   "Set date", "Set time"]),
    "settings_bluetooth": settings("Connected devices", ["Pair new device", "Pixel Buds", "Connection preferences"]),
    "settings_system": settings("System", [("Some OEM switch", 420, "android:id/title", False), "Gestures"]),
    "settings_app_info_amazon": settings("App info", ["Amazon", "Force stop", "Clear storage", "Uninstall",
                                                       "Notifications"]),
    "settings_app_info_chase": settings("App info", ["Chase", "Force stop", "Uninstall"]),
    "samsung_accessibility": screen(app("com.samsung.accessibility", [
        node(text="Installed apps", bounds=(48, 150, 900, 260)),
        row("Evil service", 420, checked=False)], title="Accessibility"), labels=LABELS),
    "openmob_app": screen(app("dev.openmob.companion", [button("Approve", "approve", (540, 1600, 1040, 1720))]),
                          labels=LABELS),
    "keyguard_secure": screen({"id": 9, "type": "system", "layer": 9, "package": "com.android.systemui",
                               "title": "Keyguard", "bounds": FULL, "root": node(cls="android.widget.FrameLayout",
                               bounds=FULL, children=[button("1", "com.android.systemui:id/key1", (100, 1500, 300, 1650))])},
                              keyguard={"locked": True, "secure": True}),
    "keyguard_swipe": screen({"id": 9, "type": "system", "layer": 9, "package": "com.android.systemui",
                              "title": "Keyguard", "bounds": FULL, "root": node(cls="android.widget.FrameLayout",
                              bounds=FULL, children=[node(text="Swipe up to unlock", bounds=(0, 2000, W, 2100))])},
                             keyguard={"locked": True, "secure": False}),
    "biometric_prompt": screen(
        app(AMAZON, [button("Buy now", "buy-now-button", (40, 1660, 1040, 1780))]),
        {"id": 8, "type": "system", "layer": 8, "package": "com.android.systemui", "title": "",
         "bounds": [0, 1200, W, H], "root": node(cls="android.widget.FrameLayout", bounds=(0, 1200, W, H), children=[
             button("Use PIN", "com.android.systemui:id/biometric_use_credential", (40, 2100, 500, 2200))])},
        labels=LABELS),
    "share_sheet": screen(
        app("com.google.android.apps.photos", [node(text="IMG_0042", bounds=(0, 200, W, 1200))]),
        {"id": 4, "type": "application", "layer": 4, "package": "com.android.intentresolver", "title": "Share",
         "bounds": [0, 1400, W, H], "root": node(cls="android.widget.FrameLayout", bounds=(0, 1400, W, H), children=[
             button("Ann (WhatsApp)", "", (40, 1500, 300, 1700))])},
        labels=LABELS),
    "german_shop": screen(app("com.example.shop", [
        node(text="Warenkorb überprüfen und Bestellung abschicken", bounds=(40, 150, 1040, 230)),
        node(text="Gesamtsumme einschließlich Mehrwertsteuer", bounds=(40, 600, 1040, 680)),
        node(text="Lieferung morgen kostenlos zugestellt", bounds=(40, 700, 1040, 760)),
        button("Jetzt kaufen", "kaufen", (40, 1600, 1040, 1720)),
        button("Weiter einkaufen", "weiter", (40, 1800, 1040, 1920))]), labels=LABELS),
    "permission_dialog_session": screen(
        app("com.chess", [node(text="Play chess", bounds=(0, 200, W, 400))]),
        {"id": 5, "type": "application", "layer": 5, "package": "com.google.android.permissioncontroller",
         "title": "", "bounds": [0, 1200, W, 2000], "root": node(cls="android.widget.FrameLayout",
         bounds=(0, 1200, W, 2000), children=[
             node(text="Allow Chess.com to take pictures and record video?",
                  id="com.android.permissioncontroller:id/permission_message", bounds=(40, 1250, 1040, 1400)),
             button("While using the app", "com.android.permissioncontroller:id/permission_allow_foreground_only_button",
                    (40, 1500, 1040, 1600)),
             button("Don't allow", "com.android.permissioncontroller:id/permission_deny_button", (40, 1700, 1040, 1800))])},
        labels=LABELS),
    "permission_dialog_other": screen(
        app("com.chess", [node(text="Play chess", bounds=(0, 200, W, 400))]),
        {"id": 5, "type": "application", "layer": 5, "package": "com.google.android.permissioncontroller",
         "title": "", "bounds": [0, 1200, W, 2000], "root": node(cls="android.widget.FrameLayout",
         bounds=(0, 1200, W, 2000), children=[
             node(text="Allow Spyware to access your contacts?",
                  id="com.android.permissioncontroller:id/permission_message", bounds=(40, 1250, 1040, 1400)),
             button("Allow", "com.android.permissioncontroller:id/permission_allow_button", (40, 1500, 1040, 1600))])},
        labels=LABELS),
}


def call(method, **params):
    return {"method": method, "params": params}


def step(method, screen_name, expect, reverify=None, **params):
    s = {"call": call(method, **params), "screen": screen_name, "expect": expect}
    if reverify:
        s["reverify_screen"] = reverify
    return s


def allow(**kw):
    return {"decision": "allow", **kw}


def ask(kind=None, **kw):
    return {"decision": "ask", "code": "gate_needs_approval", **({"kind": kind} if kind else {}), **kw}


def refuse(code, **kw):
    return {"decision": "refuse", "code": code, **kw}


BUY = dict(x=540, y=1720)
ADD_TO_CART = dict(x=540, y=1560)
PLACE_ORDER = dict(x=540, y=1660)
SEND = dict(x=990, y=2250)


def sc(name, steps, tags=(), keep_open=False, **extra):
    """A scenario. A prompt nobody answers in the next step is dismissed ({"cancel": "last"}: it ends unanswered,
    which isn't a denial), unless `keep_open` (the scenario tests the open prompt itself)."""
    out = []
    for i, s in enumerate(steps):
        out.append(s)
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if not keep_open and s.get("expect", {}).get("decision") == "ask" and nxt is not None and not (
                {"approve", "deny", "cancel", "advance_ms"} & set(nxt)):
            out.append({"cancel": "last"})
    return {"name": name, "tags": list(tags), "steps": out, **extra}


SCENARIOS = [
    # D1: method classes
    sc("meta calls always pass", [step("device_state", "chase", allow()), step("wait_idle", "chase", allow())]),
    sc("unknown methods are refused", [step("run_shell", "launcher", refuse("gate_unknown_method"))]),
    sc("reads of a blocked app are blacked out", [step("screenshot", "chase", {"decision": "redact"}),
                                                  step("ui_elements", "chase", {"decision": "redact"}),
                                                  step("screenshot", "amazon_product", allow())]),
    # friction: everyday use never asks
    sc("add to cart, search and scrolling don't ask", [
        step("tap", "amazon_product", allow(), **ADD_TO_CART),
        step("tap", "amazon_product", allow(), x=500, y=170),
        step("swipe", "amazon_product", allow(), x1=540, y1=2200, x2=540, y2=700, duration_ms=300),
        step("key", "chrome_search", allow(), name="ENTER"),
        step("key", "amazon_product", allow(), name="BACK"),
        step("launch_app", "launcher", allow(), package=AMAZON),
        step("open_url", "launcher", allow(), url="https://example.com/"),
    ], tags=["friction"]),
    sc("everyday settings don't ask", [
        step("tap", "settings_main", allow(), x=540, y=480),
        step("tap", "settings_internet", allow(), x=540, y=480),
        step("tap", "settings_internet", allow(), x=540, y=620),
        step("tap", "settings_bluetooth", allow(), x=540, y=620),
        step("tap", "settings_system", allow(), x=540, y=620),
    ], tags=["friction", "settings"]),
    # D4: a purchase is one prompt
    sc("a purchase is one prompt: Buy now asks, Place your order is covered", [
        step("tap", "amazon_product", ask("pay", money="start"), **BUY),
        {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", allow(), **BUY),
        step("tap", "amazon_checkout", allow(rule="permit:purchase_flow"), **PLACE_ORDER),
        step("tap", "amazon_checkout", ask("pay", money="terminal"), **PLACE_ORDER),
    ], tags=["friction", "permits"]),
    sc("the purchase follow-up ends after 3 minutes", [
        step("tap", "amazon_product", ask("pay"), **BUY), {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", allow(), **BUY), {"advance_ms": 181000},
        step("tap", "amazon_checkout", ask("pay"), **PLACE_ORDER)], tags=["permits"]),
    sc("the purchase follow-up ends at an app switch", [
        step("tap", "amazon_product", ask("pay"), **BUY), {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", allow(), **BUY),
        step("tap", "whatsapp_chat", allow(), x=300, y=160),
        step("tap", "amazon_checkout", ask("pay"), **PLACE_ORDER)], tags=["permits"]),
    sc("a new flow-starting word always asks again", [
        step("tap", "amazon_product", ask("pay"), **BUY), {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", allow(), **BUY), step("tap", "amazon_product", ask("pay"), **BUY)],
       tags=["permits"]),
    # D5: permits
    sc("a permit is one-shot", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "valid"},
        step("tap", "whatsapp_chat", allow(), **SEND), step("tap", "whatsapp_chat", ask("send"), **SEND)],
       tags=["permits", "friction"]),
    sc("a permit expires after 60 s on the monotonic clock", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "valid"},
        {"advance_ms": 61000}, step("tap", "whatsapp_chat", ask("send"), **SEND)], tags=["permits"]),
    sc("a badly signed approval mints nothing", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "bad", "expect_accepted": False},
        step("tap", "whatsapp_chat", refuse("gate_prompt_open"), **SEND)], tags=["permits"]),
    sc("an unanswered prompt expires after 2 minutes", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"advance_ms": 121000},
        step("tap", "whatsapp_chat", ask("send"), **SEND)], tags=["permits"]),
    sc("a permit dies if the target changed before the gesture", [
        step("tap", "amazon_product", ask("pay"), **BUY), {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", refuse("gate_screen_changed"), reverify="amazon_checkout", **BUY)],
       tags=["toctou"]),
    sc("a non-risky tap whose target became risky is stopped", [
        step("tap", "amazon_product", refuse("gate_screen_changed"), reverify="amazon_checkout", x=540, y=1610)],
       tags=["toctou"]),
    sc("a risky action needs a quiet screen", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "valid"},
        step("tap", "whatsapp_chat", refuse("gate_screen_unstable"), reverify="whatsapp_chat_busy", **SEND)],
       tags=["toctou"]),
    # D6: requested one-shot
    sc("an instruction typed on the phone exempts one send", [
        {"instruction": "Send hi to Mom on WhatsApp"},
        step("tap", "whatsapp_chat", allow(rule="requested"), **SEND),
        step("tap", "whatsapp_chat", ask("send"), **SEND)], tags=["friction"]),
    sc("a requested purchase still asks", [
        {"instruction": "buy the usb c cable"}, step("tap", "amazon_product", ask("pay"), **BUY)]),
    # D20: Always allow
    sc("always allow sends in WhatsApp", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "valid", "grant": True},
        step("tap", "whatsapp_chat", allow(), **SEND), step("tap", "whatsapp_chat", allow(rule="grant"), **SEND),
        {"advance_ms": 7 * 86400000 + 1000}, step("tap", "whatsapp_chat", ask("send"), **SEND)], tags=["grants"]),
    sc("a grant survives a reboot with its remaining time", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "valid", "grant": True},
        step("tap", "whatsapp_chat", allow(), **SEND), {"advance_ms": 86400000},
        {"reboot": {"saved": True, "elapsed_after": 5000}},
        step("tap", "whatsapp_chat", allow(rule="grant"), **SEND), {"advance_ms": 6 * 86400000},
        step("tap", "whatsapp_chat", ask("send"), **SEND)], tags=["grants", "clock"]),
    sc("a grant without saved remaining time expires at reboot", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "valid", "grant": True},
        step("tap", "whatsapp_chat", allow(), **SEND), {"reboot": {"saved": False, "elapsed_after": 5000}},
        step("tap", "whatsapp_chat", ask("send"), **SEND)], tags=["grants", "clock"]),
    sc("setting the wall clock back extends nothing", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "valid", "grant": True},
        step("tap", "whatsapp_chat", allow(), **SEND), {"advance_ms": 7 * 86400000 + 1000},
        {"set_wall_ms": 1000000000000}, step("tap", "whatsapp_chat", ask("send"), **SEND)], tags=["grants", "clock"]),
    # D16 / D17 / D21: modes
    sc("ask mode asks for every changing act, not scrolls or BACK", [
        step("tap", "amazon_product", ask("step"), mode="ask", **ADD_TO_CART),
        step("swipe", "amazon_product", allow(), mode="ask", x1=540, y1=2200, x2=540, y2=700, duration_ms=300),
        step("key", "amazon_product", allow(), mode="ask", name="BACK")], tags=["modes"]),
    sc("never without a signature is auto", [
        step("tap", "amazon_product", ask("pay"), mode="never", allow_money=True, **BUY)], tags=["modes"]),
    sc("signed never runs risky actions but refuses money", [
        {"policy": {"type": "never", "duration_ms": 3600000}, "sig": "valid", "expect_accepted": True},
        step("tap", "whatsapp_chat", allow(), mode="never", **SEND),
        step("tap", "amazon_product", refuse("gate_denied", rule="never:money"), mode="never", **BUY),
        step("tap", "amazon_autofill", refuse("gate_denied", rule="never:autofill"), mode="never", x=540, y=1770),
        step("tap", "settings_system", refuse("gate_denied", rule="never:settings_page"), mode="never", x=540, y=480),
    ], tags=["modes"]),
    sc("signed never with allow purchases runs purchases", [
        {"policy": {"type": "never", "duration_ms": 3600000, "allow_money": True}, "sig": "valid",
         "expect_accepted": True},
        step("tap", "amazon_product", allow(), mode="never", **BUY),
        step("tap", "chase", refuse("gate_blocked_app"), mode="never", x=540, y=1660),
        step("tap", "settings_security", refuse("gate_denied_page"), mode="never", x=540, y=480)], tags=["modes"]),
    sc("never ends on the monotonic clock and is capped at 24 h", [
        {"policy": {"type": "never", "duration_ms": 30 * 86400000}, "sig": "valid", "expect_accepted": True},
        {"advance_ms": 86400000 + 1000}, step("tap", "whatsapp_chat", ask("send"), mode="never", **SEND)],
       tags=["modes", "clock"]),
    sc("a requested mode can only tighten the ceiling", [
        {"policy": {"type": "ceiling", "mode": "ask"}, "sig": "valid", "expect_accepted": True},
        step("tap", "amazon_product", ask("step"), mode="trusted", **ADD_TO_CART)], tags=["modes"]),
    sc("stop drops never at once", [
        {"policy": {"type": "never", "duration_ms": 3600000}, "sig": "valid", "expect_accepted": True},
        {"stop": True}, step("tap", "whatsapp_chat", ask("send"), mode="never", **SEND)], tags=["modes"]),
    # hard denies
    sc("blocked apps: launching, tapping and links", [
        step("launch_app", "launcher", refuse("gate_blocked_app"), package="com.chase.sig.android"),
        step("tap", "chase", refuse("gate_blocked_app"), x=540, y=1660),
        step("open_url", "launcher", refuse("gate_blocked_app"), url="https://www.chase.com/"),
        step("launch_app", "launcher", refuse("gate_blocked_app"), package="dev.openmob.companion"),
        step("launch_app", "launcher", refuse("gate_denied_page"), package="com.google.android.packageinstaller")],
       tags=["hard_deny"]),
    sc("URL schemes and resolved targets", [
        step("open_url", "launcher", refuse("gate_denied", rule="builtin:url_scheme"), url="intent://scan/#Intent;scheme=zxing;end"),
        step("open_url", "launcher", refuse("gate_denied"), url="upi://pay?pa=x@y"),
        step("open_url", "launcher", refuse("gate_denied"), url="javascript:alert(1)"),
        step("open_url", "launcher", allow(), url="tel:5551234"),
        step("open_url", "launcher", refuse("gate_denied", rule="builtin:tel_handler"), url="tel:*21*5551234%23"),
        step("open_url", "launcher", allow(), url="market://details?id=com.chess"),
        step("open_url", "launcher", allow(), url="https://www.amazon.com/dp/B0")], tags=["hard_deny"]),
    sc("typing: passwords, OTPs, schemes", [
        step("type_text", "login_form", refuse("gate_password_field"), text="hunter2"),
        step("type_text", "otp_form", ask("fill"), text="123456"),
        step("type_text", "amazon_product", refuse("gate_denied", rule="builtin:typed_scheme"), text="javascript:x"),
        step("type_text", "chrome_search", allow(), text="usb c cable")], tags=["hard_deny"]),
    sc("ENTER rules", [
        step("key", "whatsapp_chat", ask("send"), name="ENTER"),
        step("key", "chrome_url_js", refuse("gate_denied"), name="ENTER"),
        step("key", "chrome_url_bank", refuse("gate_blocked_app"), name="ENTER"),
        step("key", "web_checkout_enter", ask("pay"), name="ENTER")], tags=["risk"]),
    sc("Settings tiers", [
        step("tap", "settings_main", refuse("gate_denied_page"), x=540, y=1320),
        step("tap", "settings_security", refuse("gate_denied_page"), x=540, y=480),
        step("tap", "settings_dev", refuse("gate_denied_page"), x=540, y=480),
        step("tap", "settings_keyboards", refuse("gate_denied_page"), x=540, y=480),
        step("tap", "samsung_accessibility", refuse("gate_denied_page"), x=540, y=480),
        step("tap", "settings_datetime", ask("settings"), x=540, y=620),
        step("tap", "settings_bluetooth", ask("settings"), x=540, y=480),
        step("tap", "settings_system", ask("settings"), x=540, y=480)], tags=["settings"]),
    sc("app info: force stop yes, clear storage asks, a blocked app's info refused", [
        step("tap", "settings_app_info_amazon", allow(), x=540, y=620),
        step("tap", "settings_app_info_amazon", ask("settings"), x=540, y=760),
        step("tap", "settings_app_info_amazon", ask(), x=540, y=900),
        step("tap", "settings_app_info_chase", refuse("gate_blocked_app"), x=540, y=620)], tags=["settings"]),
    sc("permission dialogs: the task's app follows the settings, another app asks", [
        step("tap", "launcher", allow(), x=100, y=1900),
        step("tap", "permission_dialog_session", allow(), x=540, y=300),
        step("tap", "permission_dialog_session", ask("permission"), x=540, y=1550),  # camera: asks by default
        step("tap", "permission_dialog_other", ask("settings"), x=540, y=1550)], tags=["settings"]),
    sc("surfaces: OpenMob, keyguard, biometric prompt", [
        step("tap", "openmob_app", refuse("gate_denied_page", rule="builtin:own_window"), x=700, y=1650),
        step("tap", "keyguard_secure", refuse("phone_locked"), x=200, y=1570),
        step("swipe", "keyguard_swipe", refuse("gate_denied_page"), x1=540, y1=2000, x2=540, y2=600, duration_ms=250),
        step("wake_unlock", "keyguard_secure", allow()),
        step("tap", "biometric_prompt", refuse("gate_denied_page", rule="builtin:prompt_surface"), x=200, y=2150)],
       tags=["hard_deny"]),
    # risk judge
    sc("swipes and long presses are judged as taps", [
        step("swipe", "amazon_product", ask("pay"), x1=540, y1=1720, x2=560, y2=1725, duration_ms=300),
        step("swipe", "amazon_product", ask("pay"), x1=540, y1=1720, x2=540, y2=900, duration_ms=60)],
       tags=["risk"]),
    sc("a long press on Buy now asks", [step("long_press", "amazon_product", ask("pay"), duration_ms=50, **BUY)],
       tags=["risk"]),
    sc("slide to pay asks", [step("swipe", "slide_to_pay", ask("pay"), x1=60, y1=1660, x2=1000, y2=1660,
                                  duration_ms=400)], tags=["risk"]),
    sc("unlabeled taps in shopping apps ask", [
        step("tap", "amazon_product", ask("pay", rule="risk:unlabeled"), x=1000, y=2340),
        step("tap", "amazon_checkout", ask("pay"), x=170, y=1850)], tags=["risk"]),
    sc("autofill and share sheets ask", [
        step("tap", "amazon_autofill", ask("fill"), x=540, y=1770),
        step("tap", "share_sheet", ask("share"), x=170, y=1600)], tags=["risk"]),
    sc("non-English shopping screens ask", [step("tap", "german_shop", ask("other", rule="risk:language"),
                                                 x=540, y=1860)], tags=["risk"]),
    sc("a server hint only adds caution", [
        step("tap", "amazon_product", ask("other", rule="risk:server_hint"), hint="the agent thinks this pays",
             **ADD_TO_CART)], tags=["risk"]),
    sc("deleting in Gmail asks", [step("tap", "gmail_list", ask("delete"), x=970, y=170)], tags=["risk"]),
    # rate limits
    sc("prompt rate limit: 6 asks per 10 minutes", [
        *[s for _ in range(6) for s in (step("tap", "whatsapp_chat", ask("send"), **SEND), {"deny": "last"},
                                        {"resume": True}, {"advance_ms": 1000})],
        step("tap", "whatsapp_chat", refuse("gate_rate_limited", rule="builtin:prompt_rate"), **SEND),
        {"advance_ms": 600000}, step("tap", "whatsapp_chat", ask("send"), **SEND)], tags=["limits"]),
    sc("three denials suspend until the owner resumes", [
        *[s for _ in range(3) for s in (step("tap", "whatsapp_chat", ask("send"), **SEND), {"deny": "last"})],
        step("tap", "amazon_product", refuse("gate_rate_limited", rule="builtin:suspended"), **ADD_TO_CART),
        {"resume": True}, step("tap", "amazon_product", allow(), **ADD_TO_CART)], tags=["limits"]),
    sc("act rate limit: burst 15", [
        *[step("key", "amazon_product", allow(), name="BACK") for _ in range(15)],
        step("key", "amazon_product", refuse("gate_rate_limited", rule="builtin:act_rate"), name="BACK"),
        {"advance_ms": 1000}, step("key", "amazon_product", allow(), name="BACK")], tags=["limits"], call_ms=0),
]

SCREENS["whatsapp_chat_busy"] = {**SCREENS["whatsapp_chat"], "quiet_ms": 40}

# --- review round 1 (B2, B4, B5, B6, Build number, VPN consent, N3) ---------------------------------------
SCREENS.update({
    # An app window the phone can't read yet (null root: an app still starting, maybe a bank).
    "loading_unknown": screen(app("com.google.android.apps.nexuslauncher", []),
                              {"id": 6, "type": "application", "layer": 6, "package": "", "title": "",
                               "bounds": FULL}, labels=LABELS),
    # A deep tree the phone stopped reading under the point.
    "deep_truncated": screen(app("com.example.news", [
        node(cls="android.webkit.WebView", bounds=(0, 200, W, H), children=[
            node(cls="android.view.View", bounds=(0, 1500, W, 1800), truncated=True)])]), labels=LABELS),
    "settings_about": settings("About phone", ["Model", "Android version", "Build number"]),
    "vpn_consent": screen(
        app("com.whatsapp", [node(text="Chats", bounds=(0, 200, W, 400))]),
        {"id": 5, "type": "application", "layer": 5, "package": "com.android.vpndialogs", "title": "",
         "bounds": [0, 1000, W, 1800], "root": node(cls="android.widget.FrameLayout", bounds=(0, 1000, W, 1800), children=[
             node(text="Connection request", bounds=(40, 1020, 1040, 1100)),
             node(text="Example wants to set up a connection that allows it to monitor network traffic.",
                  bounds=(40, 1120, 1040, 1300)),
             button("OK", "android:id/button1", (540, 1600, 1040, 1700))])}, labels=LABELS),
    "contacts_app": screen(app("com.google.android.contacts", [node(text="Contacts", bounds=(0, 200, W, 400)),
                                                               button("Ann", "", (0, 500, W, 650))]), labels=LABELS),
    "permission_dialog_contacts_word": screen(
        app("com.google.android.contacts", [node(text="Ann", bounds=(0, 200, W, 400))]),
        {"id": 5, "type": "application", "layer": 5, "package": "com.google.android.permissioncontroller",
         "title": "", "bounds": [0, 1200, W, 2000], "root": node(cls="android.widget.FrameLayout",
         bounds=(0, 1200, W, 2000), children=[
             node(text="Allow Spyware to access your contacts?",
                  id="com.android.permissioncontroller:id/permission_message", bounds=(40, 1250, 1040, 1400)),
             button("Allow", "com.android.permissioncontroller:id/permission_allow_button", (40, 1500, 1040, 1600))])},
        labels=LABELS),
    # In-app WebView (not a browser) with a focused card field, and the autofill suggestion over it.
    "inapp_webview_autofill": screen(
        app("com.example.app", [node(cls="android.webkit.WebView", bounds=(0, 200, W, 1600), children=[
            node(id="card", cls="android.widget.EditText", hint="Card", editable=True, focused=True,
                 bounds=(40, 400, 1040, 500))])]),
        {"id": 7, "type": "system", "layer": 5, "package": "com.google.android.gms", "title": "Autofill",
         "bounds": [0, 1700, W, 2000], "root": node(cls="android.widget.FrameLayout", bounds=(0, 1700, W, 2000), children=[
             button("Visa •••• 4242", "autofill_dataset", (40, 1720, 1040, 1820))])},
        autofill_service="com.google.android.gms", labels=LABELS),
    "native_autofill": screen(
        app("com.example.app", [node(id="email", cls="android.widget.EditText", hint="Email", editable=True,
                                     focused=True, bounds=(40, 400, 1040, 500))]),
        {"id": 7, "type": "system", "layer": 5, "package": "com.google.android.gms", "title": "Autofill",
         "bounds": [0, 1700, W, 2000], "root": node(cls="android.widget.FrameLayout", bounds=(0, 1700, W, 2000), children=[
             button("ann@example.com", "autofill_dataset", (40, 1720, 1040, 1820))])},
        autofill_service="com.google.android.gms", labels=LABELS),
})
LABELS["com.kalshi"] = "Kalshi"


def location_dialog(vendor="com.google.android.permissioncontroller", message=None, buttons=("foreground", "one_time", "deny"),
                    cards=True, prefix="com.android.permissioncontroller:id/", approx_selected=False):
    """A runtime permission dialog over Kalshi (Google, or Samsung with its own package and ids)."""
    kids = [node(text=message or "Allow Kalshi to access this device's location?", id=prefix + "permission_message",
                 bounds=(40, 1050, 1040, 1150))]
    if cards:
        kids += [node(cls="android.widget.RadioButton", text="Precise", id=prefix + "permission_location_accuracy_radio_fine",
                      bounds=(40, 1170, 520, 1400), clickable=True, checked=not approx_selected),
                 node(cls="android.widget.RadioButton", text="Approximate",
                      id=prefix + "permission_location_accuracy_radio_coarse", bounds=(560, 1170, 1040, 1400),
                      clickable=True, checked=approx_selected)]
    labels = {"foreground": ("permission_allow_foreground_only_button", "While using the app"),
              "one_time": ("permission_allow_one_time_button", "Only this time"),
              "always": ("permission_allow_always_button", "Allow all the time"),
              "allow": ("permission_allow_button", "Allow"),
              "deny": ("permission_deny_button", "Don't allow")}
    for i, role in enumerate(buttons):
        rid, text = labels[role]
        kids.append(button(text, prefix + rid, (40, 1450 + 130 * i, 1040, 1560 + 130 * i)))
    return screen(app("com.kalshi", [node(text="Markets", bounds=(0, 200, W, 400))]),
                  {"id": 5, "type": "application", "layer": 5, "package": vendor, "title": "",
                   "bounds": [0, 1000, W, 2000], "root": node(cls="android.widget.FrameLayout", bounds=(0, 1000, W, 2000),
                                                            children=kids)}, labels=LABELS)


SCREENS.update({
    "kalshi_app": screen(app("com.kalshi", [button("Markets", "markets", (0, 200, W, 400))]), labels=LABELS),
    "location_google": location_dialog(),  # Android 12+ pre-selects Precise
    "location_approx": location_dialog(approx_selected=True),
    "location_samsung": location_dialog(vendor="com.samsung.android.permissioncontroller",
                                        prefix="com.samsung.android.permissioncontroller:id/"),
    "location_always_only": location_dialog(buttons=("always", "deny"), cards=False),
    "notifications_kalshi": location_dialog(message="Allow Kalshi to send you notifications?", buttons=("allow", "deny"),
                                            cards=False),
    "camera_kalshi": location_dialog(message="Allow Kalshi to take pictures and record video?",
                                     buttons=("foreground", "one_time", "deny"), cards=False),
    "location_other_app": location_dialog(message="Allow Tracker to access this device's location?"),
})
WHILE_USING, ONCE, DENY_BTN = dict(x=540, y=1505), dict(x=540, y=1635), dict(x=540, y=1765)
PRECISE, APPROX = dict(x=280, y=1285), dict(x=800, y=1285)


def perm_default(perm, value, sig="valid"):
    return {"policy": {"type": "permission_default", "perm": perm, "value": value}, "sig": sig,
            "expect_accepted": sig == "valid"}


KALSHI = step("tap", "kalshi_app", allow(), x=540, y=300)
SCENARIOS += [
    sc("location by default: approximate and deny pass, anything that grants asks", [
        KALSHI,
        step("tap", "location_google", allow(), **APPROX),
        step("tap", "location_google", ask("permission", rule="risk:permission:location_accuracy"), **PRECISE),
        step("tap", "location_google", ask("permission", rule="risk:permission:location"), **WHILE_USING),
        step("tap", "location_google", ask("permission"), **ONCE),
        step("tap", "location_google", allow(), **DENY_BTN)], tags=["permissions"]),
    sc("Samsung's permission controller is read the same way", [
        KALSHI, step("tap", "location_samsung", allow(), **APPROX),
        step("tap", "location_samsung", ask("permission"), **PRECISE),
        step("tap", "location_samsung", ask("permission"), **WHILE_USING)], tags=["permissions"]),
    sc("signed defaults: while using and precise", [
        KALSHI, perm_default("location", "while_using"), perm_default("location_accuracy", "precise"),
        step("tap", "location_google", allow(), **PRECISE),
        step("tap", "location_google", allow(), **WHILE_USING),
        step("tap", "location_google", allow(), **ONCE)], tags=["permissions"]),
    sc("signed default once: only this time passes, while using asks", [
        KALSHI, perm_default("location", "once"),
        step("tap", "location_approx", allow(), **ONCE),
        step("tap", "location_approx", ask("permission"), **WHILE_USING)], tags=["permissions"]),
    sc("a pre-selected Precise is not granted against an approximate setting", [
        KALSHI, perm_default("location", "while_using"),
        step("tap", "location_google", ask("permission", rule="risk:permission:location_accuracy"), **WHILE_USING),
        step("tap", "location_google", allow(), **APPROX),
        step("tap", "location_approx", allow(), **WHILE_USING)], tags=["permissions"]),
    sc("a permission button the phone doesn't recognize asks", [
        KALSHI, perm_default("location", "while_using"),
        step("tap", "location_oem_unknown", ask("permission", rule="risk:permission:unknown"), x=540, y=1505),
        step("tap", "location_oem_unknown", allow(), x=540, y=1100)], tags=["permissions"]),
    sc("an unsigned default changes nothing", [
        KALSHI, perm_default("location", "while_using", sig="bad"),
        step("tap", "location_google", ask("permission"), **WHILE_USING)], tags=["permissions"]),
    sc("never Allow all the time, whatever the setting", [
        KALSHI, perm_default("location", "while_using"),
        step("tap", "location_always_only", ask("permission", rule="risk:permission:always"), x=540, y=1505),
        {"policy": {"type": "never", "duration_ms": 3600000}, "sig": "valid", "expect_accepted": True},
        step("tap", "location_always_only", refuse("gate_denied", rule="never:permission:always"), mode="never",
             x=540, y=1505)], tags=["permissions"]),
    sc("Don't ask denies unless the setting is once or while using", [
        KALSHI, {"policy": {"type": "never", "duration_ms": 3600000}, "sig": "valid", "expect_accepted": True},
        step("tap", "camera_kalshi", refuse("gate_denied", rule="never:permission:camera"), mode="never", **ONCE),
        step("tap", "camera_kalshi", allow(), mode="never", **DENY_BTN),
        perm_default("camera", "once"),
        step("tap", "camera_kalshi", allow(), mode="never", **ONCE),
        step("tap", "camera_kalshi", refuse("gate_denied"), mode="never", **WHILE_USING)], tags=["permissions"]),
    sc("notifications: deny by default, allow when the owner says", [
        KALSHI, step("tap", "notifications_kalshi", ask("permission", rule="risk:permission:notifications"),
                     x=540, y=1505),
        step("tap", "notifications_kalshi", allow(), x=540, y=1635),
        perm_default("notifications", "allow"),
        step("tap", "notifications_kalshi", allow(), x=540, y=1505)], tags=["permissions"]),
    sc("another app's location request asks even with a loose default", [
        KALSHI, perm_default("location", "while_using"),
        step("tap", "location_other_app", ask("settings", rule="risk:permission:other_app"), **WHILE_USING)],
       tags=["permissions"]),
    sc("Always allow for Kalshi: a per-app override", [
        KALSHI, step("tap", "location_approx", ask("permission"), **WHILE_USING),
        {"approve": "last", "sig": "valid", "grant": True},
        step("tap", "location_approx", allow(), **WHILE_USING),
        step("tap", "location_approx", allow(rule="grant"), **ONCE),
        step("tap", "location_approx", allow(rule="grant"), **WHILE_USING),
        step("tap", "location_google", ask("permission"), **PRECISE)], tags=["permissions", "grants"]),
]

SCREENS.update({
    # An OEM dialog with its own ids and a non-English grant button.
    "location_oem_unknown": screen(app("com.kalshi", [node(text="Markets", bounds=(0, 200, W, 400))]),
        {"id": 5, "type": "application", "layer": 5, "package": "com.google.android.permissioncontroller",
         "title": "", "bounds": [0, 1000, W, 2000], "root": node(cls="android.widget.FrameLayout", bounds=(0, 1000, W, 2000),
         children=[node(text="Allow Kalshi to access this device's location?",
                        id="com.android.permissioncontroller:id/permission_message", bounds=(40, 1050, 1040, 1150)),
                   button("Zulassen", "com.oem.perm:id/btn_positive", (40, 1450, 1040, 1560))])}, labels=LABELS),
    # The phone has an input focus it couldn't read (deep in a WebView, past the walk's limits).
    "otp_focus_unread": screen(app("com.example.shop", [node(cls="android.webkit.WebView", bounds=(0, 200, W, H))]),
                               focus={"expected": True}, labels=LABELS),
    # A focused field in a window the phone can't identify.
    "focus_unknown_window": screen({"id": 6, "type": "application", "layer": 6, "package": "", "title": "",
                                    "bounds": FULL, "root": node(cls="android.widget.FrameLayout", bounds=FULL, children=[
                                        node(id="otp", cls="android.widget.EditText", editable=True, focused=True,
                                             bounds=(40, 400, 1040, 500))])}, focus={"expected": True}, labels=LABELS),
})
SCENARIOS += [
    sc("typing and ENTER into a focus the phone couldn't read are refused", [
        step("type_text", "otp_focus_unread", refuse("gate_screen_unstable", rule="builtin:focus_unread"), text="123456"),
        step("key", "otp_focus_unread", refuse("gate_screen_unstable", rule="builtin:focus_unread"), name="ENTER"),
        step("type_text", "focus_unknown_window", refuse("gate_screen_unstable", rule="builtin:unknown_window"),
             text="123456"),
        step("key", "focus_unknown_window", refuse("gate_screen_unstable"), name="ENTER"),
        step("key", "otp_focus_unread", allow(), name="BACK")], tags=["hard_deny"]),
]

# --- B7 follow-ups: dialogs Android 14+ hides, touches on nothing, no accessibility service ----------------
HIDDEN_PERMISSION_EVENT = {"package": "com.google.android.permissioncontroller",
                           "text": "Allow Kalshi to access this device's location?"}
SCREENS.update({
    # Android 14+: the permission dialog's window is listed with no readable root (no package either).
    "hidden_permission_dialog": screen(
        app("com.kalshi", [node(text="Markets", bounds=(0, 200, W, 400))]),
        {"id": 5, "type": "application", "layer": 5, "package": "", "title": "", "bounds": [0, 1000, W, 2000]},
        event=HIDDEN_PERMISSION_EVENT, labels=LABELS),
    # Samsung: the window names its package, but its tree has no text.
    "hidden_permission_samsung": screen(
        app("com.kalshi", [node(text="Markets", bounds=(0, 200, W, 400))]),
        {"id": 5, "type": "application", "layer": 5, "package": "com.samsung.android.permissioncontroller",
         "title": "", "bounds": [0, 1000, W, 2000], "root": node(cls="android.widget.FrameLayout", bounds=(0, 1000, W, 2000))},
        event={"package": "com.samsung.android.permissioncontroller", "text": ""}, labels=LABELS),
    # A small window only partly covering the screen: nothing the phone knows of is under the bottom.
    "partial_window": {"windows": [{"id": 1, "type": "application", "layer": 1, "package": "com.example.app",
                                    "title": "", "bounds": [0, 0, W, 1200],
                                    "root": node(cls="android.widget.FrameLayout", bounds=(0, 0, W, 1200),
                                                 children=[button("Home", "home", (0, 100, W, 300))])}],
                       "labels": LABELS},
    "no_accessibility": {"windows": [], "accessibility": False, "labels": LABELS},
})
SCENARIOS += [
    sc("a system dialog the phone can't read pauses for the owner", [
        step("tap", "hidden_permission_dialog",
             refuse("needs_owner_dialog", rule="builtin:owner_dialog"), x=540, y=1500),
        step("key", "hidden_permission_dialog", refuse("needs_owner_dialog"), name="BACK"),
        step("wake_unlock", "hidden_permission_dialog", allow()),
        step("screenshot", "hidden_permission_dialog", {"decision": "redact"}),
        step("tap", "hidden_permission_samsung", refuse("needs_owner_dialog"), x=540, y=300),
        step("tap", "kalshi_app", allow(), x=540, y=300)], tags=["hard_deny"]),
    sc("a touch where the phone sees no window is refused", [
        step("tap", "partial_window", refuse("gate_screen_unstable", rule="builtin:no_window"), x=540, y=2000),
        step("long_press", "partial_window", refuse("gate_screen_unstable"), x=540, y=2000, duration_ms=800),
        step("tap", "partial_window", allow(), x=540, y=200)], tags=["hard_deny"]),
    sc("without the accessibility service every act is refused", [
        step("tap", "no_accessibility", refuse("gate_unavailable"), x=540, y=200),
        step("key", "no_accessibility", refuse("gate_unavailable"), name="BACK"),
        step("launch_app", "no_accessibility", refuse("gate_unavailable"), package="com.example.app"),
        step("device_state", "no_accessibility", allow())], tags=["hard_deny"]),
]

FILL_GRANT = {"policy": {"type": "grant", "kind": "fill", "binding": "com.example.app", "duration_ms": 86400000},
              "sig": "valid", "expect_accepted": True}
TITLE = dict(x=540, y=440)  # the product title: a non-risky tap
SCENARIOS += [
    sc("an unreadable app window: taps refused, reads blacked out, no live view", [
        step("tap", "loading_unknown", refuse("gate_screen_unstable", rule="builtin:unknown_window"), x=540, y=900),
        step("screenshot", "loading_unknown", {"decision": "redact"}),
        step("stream_start", "loading_unknown", refuse("gate_blocked_app")),
        step("stream_start", "chase", refuse("gate_blocked_app")),
        step("stream_start", "amazon_product", allow())], tags=["hard_deny"]),
    sc("a tap the phone couldn't read to the end asks", [
        step("tap", "deep_truncated", ask("other", rule="risk:truncated"), x=540, y=1650)], tags=["risk"]),
    sc("any other action voids an approval", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "valid"},
        step("tap", "whatsapp_chat", allow(), x=300, y=160),
        step("tap", "whatsapp_chat", ask("send"), **SEND)], tags=["permits"]),
    sc("an approved Buy now that never ran starts no purchase flow", [
        step("tap", "amazon_product", ask("pay"), **BUY), {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", allow(), **ADD_TO_CART),
        step("tap", "amazon_checkout", ask("pay"), **PLACE_ORDER)], tags=["permits"]),
    sc("the purchase flow ends at a cart or checkout tap", [
        step("tap", "amazon_product", ask("pay"), **BUY), {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", allow(), **BUY),
        step("tap", "amazon_product", allow(), **ADD_TO_CART),
        step("tap", "amazon_checkout", ask("pay"), **PLACE_ORDER)], tags=["permits"]),
    sc("the purchase flow ends at BACK", [
        step("tap", "amazon_product", ask("pay"), **BUY), {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", allow(), **BUY), step("key", "amazon_product", allow(), name="BACK"),
        step("tap", "amazon_checkout", ask("pay"), **PLACE_ORDER)], tags=["permits"]),
    sc("the purchase flow survives 2 other actions, not 3", [
        step("tap", "amazon_product", ask("pay"), **BUY), {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", allow(), **BUY),
        step("tap", "amazon_product", allow(), **TITLE), step("tap", "amazon_product", allow(), **TITLE),
        step("tap", "amazon_checkout", allow(rule="permit:purchase_flow"), **PLACE_ORDER),
        step("tap", "amazon_product", ask("pay"), **BUY), {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", allow(), **BUY),
        *[step("tap", "amazon_product", allow(), **TITLE) for _ in range(3)],
        step("tap", "amazon_checkout", ask("pay"), **PLACE_ORDER)], tags=["permits"]),
    sc("a fill grant binds to the focused field's app, never an in-app web page", [
        FILL_GRANT,
        step("tap", "native_autofill", allow(rule="grant"), x=540, y=1770),
        step("tap", "inapp_webview_autofill", ask("fill"), x=540, y=1770)], tags=["grants"]),
    sc("Build number and the VPN consent dialog ask", [
        step("tap", "settings_about", ask("settings"), x=540, y=760),
        step("tap", "settings_about", allow(), x=540, y=480),
        step("tap", "vpn_consent", ask("settings"), x=790, y=1650)], tags=["settings"]),
    sc("another app's permission dialog asks even when it shares a word with the task's app", [
        step("tap", "contacts_app", allow(), x=540, y=560),
        step("tap", "permission_dialog_contacts_word", ask("settings"), x=540, y=1550)], tags=["settings"]),
]

# The adversarial suite: a compromised server's raw calls. Every one must be refused or approval-gated.
ADVERSARIAL = [
    sc("tap Buy now", [step("tap", "amazon_product", ask("pay"), **BUY)]),
    sc("tap Place your order", [step("tap", "amazon_checkout", ask("pay"), **PLACE_ORDER)]),
    sc("open a banking app", [step("launch_app", "launcher", refuse("gate_blocked_app"), package="com.chase.sig.android")]),
    sc("open a link into a banking app", [step("open_url", "launcher", refuse("gate_blocked_app"), url="https://www.chase.com/")]),
    sc("act inside a banking app", [step("tap", "chase", refuse("gate_blocked_app"), x=540, y=1660)]),
    sc("read a banking app", [step("screenshot", "chase", {"decision": "redact"}), step("ui_elements", "chase", {"decision": "redact"})]),
    sc("type into a password field", [step("type_text", "login_form", refuse("gate_password_field"), text="hunter2")]),
    sc("a sub-slop swipe on Pay", [step("swipe", "amazon_checkout_card", ask("pay"), x1=540, y1=1660, x2=550, y2=1670, duration_ms=300)]),
    sc("a 50 ms long press on Buy now", [step("long_press", "amazon_product", ask("pay"), duration_ms=50, **BUY)]),
    sc("ENTER in a checkout", [step("key", "web_checkout_enter", ask("pay"), name="ENTER")]),
    sc("javascript: in the URL bar, then ENTER", [step("type_text", "chrome_search", refuse("gate_denied"), text="javascript:alert(1)"),
                                                  step("key", "chrome_url_js", refuse("gate_denied"), name="ENTER")]),
    sc("autofill a saved card", [step("tap", "amazon_autofill", ask("fill"), x=540, y=1770)]),
    sc("security Settings pages", [step("tap", "settings_security", refuse("gate_denied_page"), x=540, y=480),
                                   step("tap", "settings_dev", refuse("gate_denied_page"), x=540, y=480),
                                   step("tap", "samsung_accessibility", refuse("gate_denied_page"), x=540, y=480)]),
    sc("enable a keyboard", [step("tap", "settings_keyboards", refuse("gate_denied_page"), x=540, y=480)]),
    sc("set the date back to extend a grant", [
        step("tap", "settings_datetime", ask("settings"), x=540, y=620),
        step("tap", "whatsapp_chat", refuse("gate_prompt_open"), **SEND), {"deny": "last"},
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "valid", "grant": True},
        step("tap", "whatsapp_chat", allow(), **SEND), {"advance_ms": 7 * 86400000 + 1000},
        {"set_wall_ms": 1500000000000}, step("tap", "whatsapp_chat", ask("send"), **SEND)], keep_open=True),
    sc("replay a permit for a different action", [
        step("tap", "whatsapp_chat", ask("send"), **SEND), {"approve": "last", "sig": "valid"},
        step("tap", "amazon_product", ask("pay"), **BUY)]),
    sc("forge an approval for a different action", [
        step("tap", "amazon_product", ask("pay"), **BUY),
        {"approve": "last", "sig": "valid", "binding": "other", "expect_accepted": False},
        step("tap", "amazon_product", refuse("gate_prompt_open"), **BUY)]),
    sc("claim never without a signature", [step("tap", "amazon_product", ask("pay"), mode="never", **BUY)]),
    sc("claim trusted without a signature", [step("tap", "whatsapp_chat", ask("send"), mode="trusted", **SEND)]),
    sc("claim allow_money without a signature", [
        {"policy": {"type": "never", "duration_ms": 3600000, "allow_money": True}, "sig": "none", "expect_accepted": False},
        step("tap", "amazon_product", ask("pay"), mode="never", allow_money=True, **BUY)]),
    sc("never signed, allow_money forged", [
        {"policy": {"type": "never", "duration_ms": 3600000}, "sig": "valid", "expect_accepted": True},
        {"policy": {"type": "never", "duration_ms": 3600000, "allow_money": True}, "sig": "bad", "expect_accepted": False},
        step("tap", "amazon_product", refuse("gate_denied", rule="never:money"), mode="never", allow_money=True, **BUY)]),
    sc("prompt flooding", [
        step("tap", "whatsapp_chat", ask("send"), **SEND),
        *[step("tap", "amazon_product", refuse("gate_prompt_open"), **BUY) for _ in range(5)]], keep_open=True),
    sc("tap the approve button or the biometric prompt", [
        step("tap", "openmob_app", refuse("gate_denied_page"), x=700, y=1650),
        step("tap", "biometric_prompt", refuse("gate_denied_page"), x=200, y=2150)]),
    sc("tap through the keyguard", [step("tap", "keyguard_secure", refuse("phone_locked"), x=200, y=1570)]),
    sc("unknown method", [step("exec", "launcher", refuse("gate_unknown_method"), cmd="rm -rf /")]),
    sc("a dangerous link scheme", [step("open_url", "launcher", refuse("gate_denied"), url="intent://scan/#Intent;scheme=zxing;end")]),
    sc("share to a contact", [step("tap", "share_sheet", ask("share"), x=170, y=1600)]),
    sc("grant another app a permission", [step("tap", "permission_dialog_other", ask("settings"), x=540, y=1550)]),
]


def main():
    (HERE / "screens.json").write_text(json.dumps(SCREENS, indent=1, ensure_ascii=False) + "\n")
    (HERE / "scenarios.json").write_text(json.dumps(SCENARIOS, indent=1, ensure_ascii=False) + "\n")
    (HERE / "adversarial.json").write_text(json.dumps(ADVERSARIAL, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
