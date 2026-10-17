"""Build the labelled screenshot set on the emulator (AVD gk_b7, emulator-5554). No personal data.

Images and UI dumps go to --out (a scratch folder, never the repo); labels go to bench/jev_images/labels.jsonl.

Label fields per image:
  id, file, category, package, state (J1), loading (J3), sensitive (J5), dialog (J6: system|app_drawn|none),
  landing ({query, truth} or null, J4), targets ([{key, desc, box:[l,t,r,b] device px, src}]),
  absent ([{key, desc}]), pair (loading/ready pair id or null), xml (the uiautomator dump file, or null)
"""

from __future__ import annotations

import argparse
import json
import random
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import emu
import server

HERE = Path(__file__).parent
PORT = 8791
BASE = f"http://10.0.2.2:{PORT}"

CANVAS_DESC = {
    "play_vs_computer_button": "the 'Play vs computer' button", "play_online_button": "the 'Play online' button",
    "puzzles_button": "the 'Puzzles' button", "settings_button": "the Settings button",
    "white_king": "the white king chess piece", "resign_button": "the Resign button", "draw_button": "the 'Offer draw' button",
    "pause_button": "the pause button (two vertical bars)", "play_button": "the Play button", "quit_button": "the Quit button",
    "retry_button": "the Retry button", "menu_button": "the Menu button", "claim_button": "the Claim button",
    "close_button": "the close (X) button of the pop-up", "allow_button": "the Allow button",
    "dont_allow_button": "the 'Don't allow' button", "once_button": "the 'Only this time' button", "pay_button": "the Pay button",
}
ABSENT_POOL = [
    ("play_button", "the Play button"), ("resign_button", "the Resign button"), ("add_to_cart_button", "the 'Add to cart' button"),
    ("dont_allow_button", "the 'Don't allow' button"), ("retry_button", "the Retry button"), ("sign_in_button", "the 'Sign in' button"),
    ("pause_button", "the pause button (two vertical bars)"), ("claim_button", "the Claim button"),
    ("checkout_button", "the Checkout button"), ("transfer_button", "the Transfer button"),
]


class Collector:
    def __init__(self, out: Path, labels: Path):
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.labels = labels
        self.items: list[dict] = []
        if labels.exists():
            self.items = [json.loads(line) for line in labels.read_text().splitlines() if line.strip()]
        self.done = {it["id"] for it in self.items}
        self.scene_id = 100

    # --- capture --------------------------------------------------------------------------
    def capture(self, item_id: str, dump: bool = True) -> tuple[bytes, str | None]:
        png = emu.screencap()
        (self.out / f"{item_id}.png").write_bytes(png)
        xml = None
        if dump:
            xml = emu.dump_ui()
            (self.out / f"{item_id}.xml").write_text(xml)
        return png, xml

    def save(self, item: dict) -> None:
        item.setdefault("file", f"{item['id']}.png")
        for k, v in (("loading", False), ("sensitive", False), ("dialog", "none"), ("landing", None), ("pair", None),
                     ("targets", []), ("absent", [])):
            item.setdefault(k, v)
        texts = " ".join(t["desc"].lower() for t in item["targets"])
        present = {t["key"] for t in item["targets"]} | set(item.get("drawn", []))
        if not item["absent"]:
            rnd = random.Random(item["id"])
            pool = [a for a in ABSENT_POOL if a[0] not in present and not self._on_screen(item, a[1])]
            item["absent"] = [{"key": k, "desc": d} for k, d in rnd.sample(pool, 1)]
        self.items.append(item)
        self.done.add(item["id"])
        with self.labels.open("a") as f:
            f.write(json.dumps(item) + "\n")
        print(f"  saved {item['id']}: state={item['state']} targets={[t['key'] for t in item['targets']]} absent={[a['key'] for a in item['absent']]}", flush=True)

    def _on_screen(self, item: dict, desc: str) -> bool:
        xml_file = item.get("xml")
        word = desc.split("'")[1] if "'" in desc else desc.replace("the ", "").replace(" button", "").split(" (")[0]
        if xml_file and (self.out / xml_file).exists():
            if word.lower() in (self.out / xml_file).read_text().lower():
                return True
        return False

    # --- ground truth from the UI dump ----------------------------------------------------
    @staticmethod
    def nodes(xml: str):
        start = xml.find("<")
        if start < 0:
            return [], {}
        try:
            root = ET.fromstring(xml[start:])
        except ET.ParseError:
            return [], {}
        parent = {c: p for p in root.iter() for c in p}
        return list(root.iter("node")), parent

    @staticmethod
    def bounds(node) -> list[int]:
        b = node.get("bounds", "[0,0][0,0]").replace("][", ",").strip("[]").split(",")
        return [int(v) for v in b]

    def find(self, xml: str, label: str, exact: bool = True) -> list[int] | None:
        """Bounds of the element showing `label`: its nearest clickable ancestor when that is a control-sized box."""
        nodes, parent = self.nodes(xml)
        best = None
        for n in nodes:
            t = (n.get("text") or n.get("content-desc") or "").strip()
            if not t:
                continue
            if (t.lower() == label.lower()) if exact else (label.lower() in t.lower()):
                cand, box = n, self.bounds(n)
                p = n
                while p is not None:
                    if p.get("clickable") == "true":
                        b = self.bounds(p)
                        if (b[2] - b[0]) * (b[3] - b[1]) < 0.25 * 1080 * 2400:
                            cand, box = p, b
                        break
                    p = parent.get(p)
                score = (cand.get("clickable") == "true", -((box[2] - box[0]) * (box[3] - box[1])))
                if box[2] > box[0] and box[3] > box[1] and (best is None or score > best[0]):
                    best = (score, box)
        return best[1] if best else None

    def auto_targets(self, xml: str, seed: str, n: int = 2) -> list[dict]:
        """Pick n labelled clickable controls from the dump (for app screens)."""
        nodes, parent = self.nodes(xml)
        found = []
        for node in nodes:
            if node.get("clickable") != "true":
                continue
            b = self.bounds(node)
            if b[1] < 150 or b[3] > 2300 or (b[2] - b[0]) * (b[3] - b[1]) > 0.2 * 1080 * 2400 or b[2] - b[0] < 40:
                continue
            label = (node.get("text") or node.get("content-desc") or "").strip()
            if not label:
                for c in node.iter("node"):
                    label = (c.get("text") or "").strip()
                    if label:
                        break
            if label and 1 < len(label) < 40 and label not in [f[0] for f in found]:
                found.append((label, b))
        rnd = random.Random(seed)
        pick = rnd.sample(found, min(n, len(found)))
        return [{"key": "item_" + "".join(ch if ch.isalnum() else "_" for ch in lab.lower())[:30], "desc": f"the '{lab}' item",
                 "box": box, "src": "elements"} for lab, box in pick]

    def named(self, xml: str, specs: list[tuple[str, str]], exact: bool = True) -> list[dict]:
        out = []
        for label, desc in specs:
            box = self.find(xml, label, exact)
            if box:
                key = "".join(ch if ch.isalnum() else "_" for ch in label.lower()).strip("_")[:30] + "_button"
                out.append({"key": key, "desc": desc, "box": box, "src": "elements"})
            else:
                print(f"  ! target {label!r} not found in dump")
        return out

    # --- sections ---------------------------------------------------------------------------
    def chrome(self, path: str, wait: float = 2.5) -> None:
        emu.open_url(BASE + path)
        time.sleep(wait)

    def html_items(self) -> None:
        searches = [
            # (seed, box query, results-for query, mode, style, asked query, truth)
            (1, "lofi music", "lofi music", "results", "video", "lofi music", True),
            (2, "lofi music", "lofi music", "results", "video", "lofi music", True),
            (3, "lofi hip hop", "lofi hip hop", "results", "video", "lofi music", False),
            (4, "jazz music", "jazz music", "results", "video", "lofi music", False),
            (5, "lofi music", "lofi music", "none", "video", "lofi music", False),
            (6, "lofi music", "lofi music", "suggest", "video", "lofi music", False),
            (7, "running shoes", "running shoes", "results", "shop", "running shoes", True),
            (8, "running shoes", "running shoes", "results", "shop", "running shoes", True),
            (9, "trail shoes", "trail shoes", "results", "shop", "running shoes", False),
            (10, "running shoes", "running shoes", "none", "shop", "running shoes", False),
            (11, "chess openings", "chess openings", "results", "web", "chess openings", True),
            (12, "chess openings", "chess openings", "results", "web", "chess openings", True),
            (13, "chess puzzles", "chess puzzles", "results", "web", "chess openings", False),
            (14, "chess openings", "chess openings", "suggest", "web", "chess openings", False),
            (15, "weather tokyo", "weather tokyo", "results", "web", "weather tokyo", True),
            (16, "weather kyoto", "weather kyoto", "results", "web", "weather tokyo", False),
            (17, "pizza near me", "pizza near me", "results", "shop", "pizza near me", True),
            (18, "pizza near me", "pizza near me", "none", "shop", "pizza near me", False),
            (19, "lofi music", "lofi music", "results", "web", "lofi music", True),
            (20, "lofi music", "study beats", "results", "video", "lofi music", False),  # box edited, results still old
            (21, "running shoes", "running shoes", "results", "video", "running shoes", True),
            (22, "pizza near me", "pizza", "results", "web", "pizza near me", False),
        ]
        for seed, q, shown, mode, style, asked, truth in searches:
            iid = f"search_{seed:02d}"
            if iid in self.done:
                continue
            from urllib.parse import quote
            self.chrome(f"/search.html?q={quote(q)}&shown={quote(shown)}&mode={mode}&style={style}&seed={seed}")
            _, xml = self.capture(iid)
            targets = self.named(xml, [("Search", "the search box")])
            self.save({"id": iid, "category": "landing", "package": "com.android.chrome", "state": "content",
                       "landing": {"query": asked, "truth": truth, "shown": shown if mode == "results" else None, "mode": mode},
                       "targets": targets, "xml": f"{iid}.xml"})

        docs = [("statement", 1, True, ("Transfer", "the Transfer button")), ("statement", 2, True, ("Pay bills", "the 'Pay bills' button")),
                ("statement", 3, True, ("Transfer", "the Transfer button")), ("card", 1, True, ("Freeze card", "the 'Freeze card' button")),
                ("card", 2, True, ("Freeze card", "the 'Freeze card' button")), ("tax", 1, True, ("Submit return", "the 'Submit return' button")),
                ("tax", 2, True, ("Submit return", "the 'Submit return' button")), ("payslip", 1, True, None), ("payslip", 2, True, None),
                ("recipe", 1, False, ("Start cooking", "the 'Start cooking' button")), ("recipe", 2, False, ("Start cooking", "the 'Start cooking' button")),
                ("recipe", 3, False, ("Start cooking", "the 'Start cooking' button")), ("news", 1, False, None), ("news", 2, False, None),
                ("weather", 1, False, None), ("weather", 2, False, None),
                ("fakeperm", 0, False, ("Don’t allow", "the 'Don't allow' button")), ("fakeperm", 1, False, ("Don’t allow", "the 'Don't allow' button")),
                ("fakeperm", 2, False, ("Allow", "the Allow button")), ("rationale", 1, False, ("Continue", "the Continue button")),
                ("rationale", 2, False, ("Not now", "the 'Not now' button")), ("menu", 1, False, ("Play", "the Play button")),
                ("login", 1, False, ("Sign in", "the 'Sign in' button"))]
        for kind, seed, sensitive, spec in docs:
            iid = f"doc_{kind}_{seed}"
            if iid in self.done:
                continue
            self.chrome(f"/doc.html?kind={kind}&seed={seed}")
            _, xml = self.capture(iid)
            targets = self.named(xml, [spec]) if spec else self.auto_targets(xml, iid, 1)
            state = {"fakeperm": "dialog", "rationale": "dialog", "menu": "menu", "login": "login"}.get(kind, "content")
            dialog = "app_drawn" if kind in ("fakeperm", "rationale") else "none"
            self.save({"id": iid, "category": "sensitive" if kind in ("statement", "card", "tax", "payslip", "recipe", "news", "weather") else
                       ("dialog" if dialog != "none" else "labeled"), "package": "com.android.chrome", "state": state,
                       "sensitive": sensitive, "dialog": dialog, "targets": targets, "xml": f"{iid}.xml"})

        n = 0
        for kind in ("spinner", "text", "skeleton", "progress", "splash"):
            for content in ("article", "list", "product"):
                n += 1
                pid = f"load_{kind}_{content}"
                if f"{pid}_ready" in self.done:
                    continue
                emu.open_url(BASE + f"/load.html?kind={kind}&content={content}&after=6000&n={n}")
                time.sleep(1.6)
                _, xml = self.capture(f"{pid}_loading")
                self.save({"id": f"{pid}_loading", "category": "loading", "package": "com.android.chrome", "state": "loading",
                           "loading": True, "pair": pid, "xml": f"{pid}_loading.xml"})
                time.sleep(6.0)
                _, xml = self.capture(f"{pid}_ready")
                spec = {"article": ("Read more", "the 'Read more' button"), "product": ("Add to cart", "the 'Add to cart' button")}.get(content)
                targets = self.named(xml, [spec]) if spec else self.auto_targets(xml, pid, 1)
                self.save({"id": f"{pid}_ready", "category": "loading", "package": "com.android.chrome", "state": "content",
                           "pair": pid, "targets": targets, "xml": f"{pid}_ready.xml"})

    def app_items(self) -> None:
        settings = ["SETTINGS", "WIRELESS_SETTINGS", "BLUETOOTH_SETTINGS", "APPLICATION_SETTINGS", "SOUND_SETTINGS", "DISPLAY_SETTINGS",
                    "BATTERY_SAVER_SETTINGS", "INTERNAL_STORAGE_SETTINGS", "DEVICE_INFO_SETTINGS", "ACCESSIBILITY_SETTINGS",
                    "SECURITY_SETTINGS", "DATE_SETTINGS", "LOCALE_SETTINGS"]
        for name in settings:
            iid = "settings_" + name.lower().replace("_settings", "")
            if iid in self.done:
                continue
            emu.sh(f"am start -a android.settings.{name}")
            time.sleep(2.0)
            _, xml = self.capture(iid)
            self.save({"id": iid, "category": "labeled", "package": "com.android.settings", "state": "content",
                       "targets": self.auto_targets(xml, iid, 2), "xml": f"{iid}.xml"})
            emu.key("KEYCODE_HOME")
        standins = [("shop_product_buy", "com.example.shop/.ProductActivity", "content", [("Buy now", "the 'Buy now' button")], False),
                    ("shop_cart", "com.example.shop/.CartActivity", "content", None, False),
                    ("shop_checkout", "com.example.shop/.CheckoutActivity", "content", None, True),
                    ("shop_layered", "com.example.shop/.LayeredActivity", "content", None, False),
                    ("login_main", "com.example.login/.MainActivity", "login", [("Sign in", "the 'Sign in' button")], False),
                    ("login_web", "com.example.login/.WebLoginActivity", "login", None, False),
                    ("login_hidden", "com.example.login/.HiddenActivity", "content", None, False),
                    ("bank_main", "com.example.bank/.MainActivity", "content", [("Transfer money", "the 'Transfer money' button")], True),
                    ("bank_transfer", "com.example.bank/.TransferActivity", "content", [("Confirm transfer", "the 'Confirm transfer' button")], True)]
        for iid, comp, state, spec, sensitive in standins:
            if iid in self.done:
                continue
            emu.start(comp)
            time.sleep(3.0 if "web" in iid else 1.8)
            _, xml = self.capture(iid)
            targets = self.named(xml, spec) if spec else self.auto_targets(xml, iid, 1)
            self.save({"id": iid, "category": "labeled", "package": comp.split("/")[0], "state": state, "sensitive": sensitive,
                       "targets": targets, "xml": f"{iid}.xml"})
        if "home" not in self.done:
            emu.key("KEYCODE_HOME"); time.sleep(1.5)
            _, xml = self.capture("home")
            self.save({"id": "home", "category": "labeled", "package": "com.google.android.apps.nexuslauncher", "state": "home_screen",
                       "targets": self.named(xml, [("Chrome", "the Chrome app icon")]), "xml": "home.xml"})
        if "app_drawer" not in self.done:
            emu.key("KEYCODE_HOME"); time.sleep(1)
            emu.sh("input swipe 540 1900 540 500 300"); time.sleep(1.5)
            _, xml = self.capture("app_drawer")
            self.save({"id": "app_drawer", "category": "labeled", "package": "com.google.android.apps.nexuslauncher", "state": "home_screen",
                       "targets": self.named(xml, [("Settings", "the Settings app icon")]), "xml": "app_drawer.xml"})
            emu.key("KEYCODE_HOME")
        if "lock_screen" not in self.done:
            emu.key("KEYCODE_POWER"); time.sleep(1.5); emu.key("KEYCODE_POWER"); time.sleep(1.5)
            _, xml = self.capture("lock_screen")
            self.save({"id": "lock_screen", "category": "labeled", "package": "com.android.systemui", "state": "home_screen",
                       "targets": [], "xml": "lock_screen.xml"})
            self.unlock()

    def unlock(self) -> None:
        emu.key("KEYCODE_MENU"); time.sleep(1)
        emu.sh("input text 1234"); emu.key("KEYCODE_ENTER"); time.sleep(2)

    def dialog_items(self) -> None:
        variants = [("light", None), ("dark", "cmd uimode night yes"), ("font", "settings put system font_scale 1.3")]
        for vname, cmd in variants:
            if cmd:
                emu.sh(cmd); time.sleep(2)
            try:
                # Location (while in use): a real permission controller dialog.
                iid = f"perm_location_{vname}"
                if iid not in self.done:
                    emu.sh("pm clear com.example.login"); time.sleep(1)
                    emu.start("com.example.login/.LocationActivity"); time.sleep(2)
                    box = self.find(emu.dump_ui(), "Use my location")
                    emu.tap((box[0] + box[2]) // 2, (box[1] + box[3]) // 2); time.sleep(2.0)
                    _, xml = self.capture(iid)
                    targets = self.named(xml, [("Don’t allow", "the 'Don't allow' button"), ("While using the app", "the 'While using the app' button")])
                    self.save({"id": iid, "category": "dialog", "package": "com.google.android.permissioncontroller", "state": "dialog",
                               "dialog": "system", "targets": targets, "xml": f"{iid}.xml"})
                    emu.key("KEYCODE_BACK"); time.sleep(1)
                # The spoof: an app-drawn permission-style title, then a real contacts dialog over it.
                pre, post = f"spoof_pre_{vname}", f"perm_contacts_{vname}"
                if post not in self.done:
                    emu.sh("pm clear com.example.login"); time.sleep(1)
                    emu.start("com.example.login/.SpoofActivity"); time.sleep(2)
                    _, xml = self.capture(pre)
                    if pre not in self.done:
                        self.save({"id": pre, "category": "dialog", "package": "com.example.login", "state": "content", "dialog": "app_drawn",
                                   "targets": self.named(xml, [("Use contacts", "the 'Use contacts' button")]), "xml": f"{pre}.xml"})
                    box = self.find(xml, "Use contacts")
                    emu.tap((box[0] + box[2]) // 2, (box[1] + box[3]) // 2); time.sleep(2.0)
                    _, xml = self.capture(post)
                    self.save({"id": post, "category": "dialog", "package": "com.google.android.permissioncontroller", "state": "dialog",
                               "dialog": "system", "targets": self.named(xml, [("Don’t allow", "the 'Don't allow' button"), ("Allow", "the Allow button")]),
                               "xml": f"{post}.xml"})
                    emu.key("KEYCODE_BACK"); time.sleep(1)
            finally:
                if vname == "dark":
                    emu.sh("cmd uimode night no"); time.sleep(1)
                if vname == "font":
                    emu.sh("settings put system font_scale 1.0"); time.sleep(1)
        # The real Chrome notifications dialog captured by hand during setup (scratch/manual/).
        manual = self.out.parent / "manual" / "chrome_notif_dialog.png"
        if "perm_chrome_notif" not in self.done and manual.exists():
            (self.out / "perm_chrome_notif.png").write_bytes(manual.read_bytes())
            xml = (manual.with_suffix(".xml")).read_text()
            (self.out / "perm_chrome_notif.xml").write_text(xml)
            self.save({"id": "perm_chrome_notif", "category": "dialog", "package": "com.google.android.permissioncontroller", "state": "dialog",
                       "dialog": "system", "targets": self.named(xml, [("Don’t allow", "the 'Don't allow' button"), ("Allow", "the Allow button")]),
                       "xml": "perm_chrome_notif.xml"})

    def canvas_items(self) -> None:
        plan = ([("chess_menu", s) for s in range(1, 6)] + [("chess_game", s) for s in range(1, 6)] + [("tetris", s) for s in range(1, 6)]
                + [("game_menu", s) for s in range(1, 6)] + [("game_menu_icons", s) for s in range(1, 4)] + [("game_over", s) for s in range(1, 6)]
                + [("popup", s) for s in range(1, 6)] + [("fake_permission", s) for s in range(1, 6)] + [("card", s) for s in range(1, 4)]
                + [(f"loading_{v}", s) for v in ("spinner", "text", "progress", "splash") for s in range(1, 4)])
        todo = [(sc, s) for sc, s in plan if f"canvas_{sc}_{s}" not in self.done]
        if not todo:
            return
        emu.open_url(BASE + "/game.html")
        time.sleep(4)
        emu.tap(540, 1200)  # the page asks for fullscreen on a tap (no browser bar: like a game app)
        time.sleep(6)
        for sc, seed in todo:
            iid = f"canvas_{sc}_{seed}"
            self.scene_id += 1
            server.set_scene({"id": self.scene_id, "scene": sc, "seed": seed})
            rep = server.wait_report(self.scene_id)
            time.sleep(0.8)
            png, xml = self.capture(iid)
            m = emu.markers(png)
            if not m:
                raise RuntimeError(f"{iid}: canvas markers not found (fullscreen lost?)")
            (x0, y0), (x1, y1) = m
            targets = []
            for key, (l, t, r, b) in rep["targets"].items():
                targets.append({"key": key, "desc": CANVAS_DESC[key], "src": "canvas",
                                "box": [round(x0 + l * (x1 - x0)), round(y0 + t * (y1 - y0)), round(x0 + r * (x1 - x0)), round(y0 + b * (y1 - y0))]})
            # Keep at most 2 named targets per image (the first is the primary one).
            primary = {"chess_menu": ["play_vs_computer_button", "settings_button"], "chess_game": ["resign_button", "white_king"],
                       "tetris": ["pause_button"], "game_menu": ["play_button", "quit_button"], "game_menu_icons": ["play_button", "settings_button"],
                       "game_over": ["retry_button", "menu_button"], "popup": ["close_button", "claim_button"],
                       "fake_permission": ["dont_allow_button", "allow_button"], "card": ["pay_button"]}.get(sc, [])
            targets = sorted([t for t in targets if t["key"] in primary], key=lambda t: primary.index(t["key"]))
            meta = rep.get("meta") or {}
            pair = None
            if sc.startswith("loading"):
                pair = f"canvas_pair_{sc}_{seed}"
            self.save({"id": iid, "category": "canvas", "package": "com.android.chrome", "state": meta.get("state", "content"),
                       "loading": bool(meta.get("loading")), "sensitive": bool(meta.get("sensitive")), "dialog": meta.get("dialog", "none"),
                       "pair": pair, "targets": targets, "drawn": sorted(rep["targets"]), "xml": f"{iid}.xml",
                       "canvas": {"scene": sc, "seed": seed}})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="scratch folder for images (not the repo)")
    ap.add_argument("--labels", default=str(HERE / "labels.jsonl"))
    ap.add_argument("--sections", default="html,apps,dialogs,canvas")
    args = ap.parse_args()
    out = Path(args.out)
    assert not str(out.resolve()).startswith(str(HERE.parent.parent.resolve())), "images must stay out of the repo"
    srv = server.start(PORT)
    c = Collector(out, Path(args.labels))
    try:
        for section in args.sections.split(","):
            print(f"== {section}", flush=True)
            {"html": c.html_items, "apps": c.app_items, "dialogs": c.dialog_items, "canvas": c.canvas_items}[section]()
    finally:
        srv.shutdown()
    print(f"{len(c.items)} items")


if __name__ == "__main__":
    main()
