"""Differential fuzz fixtures: random screens and calls, with the Python reference engine's answers.

`python tests/fixtures/gatekeeper/build_fuzz.py` writes fuzz.json (seeded, so it's stable). JUnit runs every case
through the Kotlin engine and must get the same decision, code, kind, rule and money (GateScenarioTest.fuzz), so
the two engines are also compared outside the hand-written fixtures. pytest checks the file is current.
"""

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent))
import gatekeeper_ref  # noqa: E402
from gatekeeper_ref import Gatekeeper, Rules, run_scenario  # noqa: E402

W, H = 1080, 2400
PACKAGES = ["com.amazon.mShop.android.shopping", "com.whatsapp", "com.android.chrome", "com.android.settings",
            "com.chase.sig.android", "com.example.app", "com.android.systemui", "dev.openmob.companion",
            "com.google.android.gms", "com.example.shop", "com.google.android.permissioncontroller",
            "com.android.intentresolver", ""]
NEUTRAL = ["photo", "news", "hello", "Mom", "weather", "Today", "cable", "2 pack", "Profile", "Gestern", "Warenkorb",
           "Bestellung", "€12,99", "$4.50", "Total", "Settings", "OK", "Next"]


def vocab(rules: Rules) -> list[str]:
    w = rules.w
    words = (w["flow_start"] + w["terminal"] + w["send"] + w["post"] + w["delete"] + w["pre_commit"] + w["credential"]
             + w["navigation"] + rules.s["everyday"][:20] + rules.s["refuse"][:20] + rules.s["ask_pages"])
    return words + NEUTRAL * 3


def gen_node(rng, words, top, depth=0):
    left = rng.randrange(0, 600)
    t = top + rng.randrange(0, 150)
    bounds = [left, t, min(W, left + rng.randrange(80, 600)), min(H, t + rng.randrange(60, 250))]
    d = {"text": rng.choice(words) if rng.random() < 0.6 else "",
         "desc": rng.choice(words) if rng.random() < 0.2 else "",
         "id": rng.choice(["", "", "buy_now", "sendButton", "place_order", "title", "cc-number", "url_bar",
                           "search_box", "otp", "delete", "submit", "android:id/switch_widget"]),
         "cls": rng.choice(["android.widget.TextView", "android.widget.Button", "android.widget.EditText",
                            "android.widget.SeekBar", "android.view.View", "android.webkit.WebView"]),
         "bounds": bounds, "children": []}
    if rng.random() < 0.4:
        d["clickable"] = True
    if d["cls"].endswith("EditText"):
        d["editable"] = True
        d["focused"] = rng.random() < 0.5
        d["password"] = rng.random() < 0.15
        d["hint"] = rng.choice(["", "Search", "Card number", "Enter code", "Message"])
    if rng.random() < 0.1:
        d["checked"] = rng.random() < 0.5
    if rng.random() < 0.05:
        d["truncated"] = True
    if depth < 2 and rng.random() < 0.4:
        d["children"] = [gen_node(rng, words, t, depth + 1) for _ in range(rng.randrange(1, 3))]
    return d


def gen_screen(rng, words):
    windows = []
    for i in range(rng.choice([1, 1, 1, 2])):
        pkg = rng.choice(PACKAGES)
        top = 0 if i == 0 else rng.randrange(800, 1600)
        kids = [gen_node(rng, words, rng.randrange(top, H - 300)) for _ in range(rng.randrange(1, 7))]
        w = {"id": i + 1, "type": rng.choice(["application"] * 4 + ["system", "input_method"]), "layer": i + 1,
             "package": pkg, "title": rng.choice(["", "", "Keyguard", "Autofill", "Share", "Security", "Internet"]),
             "bounds": [0, top, W, H]}
        if pkg or rng.random() < 0.5:
            w["root"] = {"cls": "android.widget.FrameLayout", "bounds": [0, top, W, H], "children": kids}
        windows.append(w)
    return {"windows": windows, "quiet_ms": rng.choice([1000, 1000, 40]),
            "keyguard": {"locked": rng.random() < 0.05, "secure": True},
            "autofill_service": rng.choice(["", "com.google.android.gms"]),
            "resolve": {"https://x.example/": {"package": rng.choice(PACKAGES)},
                        "tel:1": {"package": rng.choice(["com.google.android.dialer", "com.evil"])}},
            "labels": {"com.whatsapp": "WhatsApp", "com.example.app": "Example", "com.chase.sig.android": "Chase"}}


def gen_call(rng, screen):
    def point():
        nodes = []

        def visit(n):
            nodes.append(n)
            for c in n.get("children", []):
                visit(c)
        for w in screen["windows"]:
            if w.get("root"):
                visit(w["root"])
        b = rng.choice(nodes)["bounds"] if nodes and rng.random() < 0.85 else [0, 0, W, H]
        return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
    kind = rng.choice(["tap"] * 6 + ["long_press", "swipe", "swipe", "type_text", "key", "key", "launch_app",
                                     "open_url", "screenshot", "stream_start", "wake_unlock", "bogus"])
    params = {}
    if kind in ("tap", "long_press"):
        params["x"], params["y"] = point()
    elif kind == "swipe":
        params["x1"], params["y1"] = point()
        params["x2"], params["y2"] = (params["x1"] + rng.choice([0, 10, 400]), params["y1"] - rng.choice([0, 10, 900]))
        params["duration_ms"] = rng.choice([50, 300])
    elif kind == "type_text":
        params["text"] = rng.choice(["hello", "javascript:alert(1)", "123456", "intent:x"])
    elif kind == "key":
        params["name"] = rng.choice(["ENTER", "BACK", "DELETE", "HOME"])
    elif kind == "launch_app":
        params["package"] = rng.choice(PACKAGES[:-1])
    elif kind == "open_url":
        params["url"] = rng.choice(["https://x.example/", "tel:1", "intent://x", "market://details?id=a"])
    if rng.random() < 0.4:
        params["mode"] = rng.choice(["ask", "auto", "trusted", "never", "plan"])
    if rng.random() < 0.1:
        params["hint"] = "risky"
    return {"method": kind, "params": params}


def build(n=400, seed=7):
    rng = random.Random(seed)
    rules = Rules.load()
    words = vocab(rules)
    screens, cases = {}, []
    for i in range(n):
        name = f"fuzz{i}"
        screens[name] = gen_screen(rng, words)
        steps = []
        if rng.random() < 0.2:
            steps.append({"policy": {"type": "never", "duration_ms": 3600000, "allow_money": rng.random() < 0.5},
                          "sig": "valid", "expect_accepted": True})
        for _ in range(rng.choice([1, 2])):
            steps.append({"call": gen_call(rng, screens[name]), "screen": name, "expect": {}})
        case = {"name": name, "tags": ["fuzz"], "steps": steps}
        # The reference engine's answers become the expectations.
        original = gatekeeper_ref.check

        def record(expect, adm):
            expect.update(decision=adm.decision, code=adm.code, kind=adm.kind, rule=adm.rule, money=adm.money)
            return []
        gatekeeper_ref.check = record
        try:
            run_scenario(case, screens, Gatekeeper(rules))
        finally:
            gatekeeper_ref.check = original
        cases.append(case)
    return screens, cases


def main():
    screens, cases = build()
    (HERE / "fuzz.json").write_text(json.dumps({"screens": screens, "cases": cases}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
