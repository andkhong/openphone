"""Reference implementation of the phone gatekeeper's policy engine (docs/design/phone-gatekeeper.md, Phase 1).

The phone runs the Kotlin engine (android/app/src/main/java/dev/openmob/companion/gate/). This module is the
same engine in Python, reading the same rule file (android/app/src/main/resources/gatekeeper/rules.json), so
the server-side tests can put a *simulated gatekeeper phone* in front of a fake compromised server. Both
implementations run the shared scenario fixtures (tests/fixtures/gatekeeper/*.json): pytest through this
module (test_gatekeeper_conformance.py), and JUnit through the Kotlin engine (GateScenarioTest.kt). A fixture
that passes in one and fails in the other is a divergence, and fails CI.

Nothing in the server imports this: the phone is the enforcement point.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field, replace as dc_replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "android/app/src/main/resources/gatekeeper/rules.json"


def _settings_pages() -> dict:
    """The server's copy of the phone's open_settings table (key -> page with action, title, tier, needs_package)."""
    import sys

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from openmob.phone.settings_pages import PAGES

    return PAGES
MODES = {"ask": 0, "plan": 1, "auto": 1, "trusted": 2, "never": 3}
ACTION_PARAMS = ("x", "y", "x1", "y1", "x2", "y2", "duration_ms", "text", "replace", "name", "package", "url", "page")


# --- text ---------------------------------------------------------------------------------------

def norm(text: str | None) -> str:
    """NFKD, diacritics stripped, lower-cased."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def tokens(text: str | None) -> list[str]:
    return [t for t in re.split(r"[\W_]+", norm(text)) if t]


def id_tokens(resource_id: str | None) -> list[str]:
    """A resource id as words: "com.x:id/buyNowButton" -> [buy, now, button]."""
    rid = (resource_id or "").rsplit("/", 1)[-1]
    rid = re.sub(r"([a-z])([A-Z])", r"\1 \2", rid)
    return tokens(rid)


def has_phrase(toks: list[str], phrase: str) -> bool:
    p = tokens(phrase)
    if not p:
        return False
    n = len(p)
    return any(toks[i:i + n] == p for i in range(len(toks) - n + 1))


def strip_phrases(toks: list[str], phrases: list[str]) -> list[str]:
    """The tokens with every occurrence of the phrases removed (longest first)."""
    out = list(toks)
    for phrase in sorted(phrases, key=lambda s: -len(tokens(s))):
        p = tokens(phrase)
        n = len(p)
        i = 0
        while i <= len(out) - n and n:
            if out[i:i + n] == p:
                del out[i:i + n]
            else:
                i += 1
    return out


def any_phrase(toks: list[str], phrases: list[str]) -> str | None:
    for phrase in phrases:
        if has_phrase(toks, phrase):
            return phrase
    return None


VOLATILE = re.compile(r"\d+(?:[.,:]\d+)*")
AMOUNT = re.compile(r"[$€£¥₹]\s?\d+(?:[.,]\d+)*|\d+(?:[.,]\d+)*\s?[¢€$£]")
MONEY_LABELS = ["total", "cost", "price", "amount", "subtotal", "you pay", "pay", "order total", "payout"]


def vol(text: str | None) -> str:
    """A label with its volatile numbers (odds, prices, counts, times) blanked: the same element across reads."""
    return VOLATILE.sub("#", text or "")


def amounts(text: str | None) -> list[str]:
    return [re.sub(r"\s+", "", a) for a in AMOUNT.findall(text or "")]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- rules --------------------------------------------------------------------------------------

class Rules:
    def __init__(self, data: dict):
        self.d = data
        self.meta = set(data["methods"]["meta"])
        self.read = set(data["methods"]["read"])
        self.act = set(data["methods"]["act"])
        self.own = set(data["own_packages"])
        self.sensitive = [re.compile(p) for p in data["sensitive_apps"]]
        self.installers = set(data["installer_packages"])
        self.money_app = re.compile(data["money_app"])
        self.cat = {k: re.compile(v) for k, v in data["categories"].items()}
        self.w = data["words"]
        s = data["settings"]
        self.settings_pkg = re.compile(s["packages"])
        self.s = s
        self.surf = data["surfaces"]
        self.limits = data["limits"]
        self.lexicon = set(data["language"]["lexicon"].split())

    @classmethod
    def load(cls) -> "Rules":
        return cls(json.loads(RULES_PATH.read_text()))

    def rx(self, key: str) -> re.Pattern:
        return re.compile(self.d[key])

    def blocked(self, package: str | None) -> bool:
        p = (package or "").lower()
        return bool(p) and any(r.search(p) for r in self.sensitive)

    def is_cat(self, cat: str, package: str | None) -> bool:
        return bool(package) and bool(self.cat[cat].search(package))


# --- screen model -------------------------------------------------------------------------------

@dataclass
class Node:
    text: str = ""
    desc: str = ""
    id: str = ""
    cls: str = ""
    bounds: tuple = (0, 0, 0, 0)
    clickable: bool = False
    long_clickable: bool = False
    editable: bool = False
    password: bool = False
    hint: str = ""
    checked: bool | None = None
    focused: bool = False
    truncated: bool = False  # the phone stopped reading below this node (its children aren't all here)
    children: list = field(default_factory=list)

    @classmethod
    def of(cls, d: dict) -> "Node":
        return cls(text=d.get("text", ""), desc=d.get("desc", ""), id=d.get("id", ""), cls=d.get("cls", ""),
                   bounds=tuple(d.get("bounds", (0, 0, 0, 0))), clickable=bool(d.get("clickable")),
                   long_clickable=bool(d.get("long_clickable")), editable=bool(d.get("editable")),
                   password=bool(d.get("password")), hint=d.get("hint", ""), checked=d.get("checked"),
                   focused=bool(d.get("focused")), truncated=bool(d.get("truncated")),
                   children=[Node.of(c) for c in d.get("children", [])])

    def contains(self, x: float, y: float) -> bool:
        l, t, r, b = self.bounds
        return l <= x < r and t <= y < b

    def fp(self, bounds: bool) -> str:
        parts = [self.id, vol(self.text), vol(self.desc), self.cls]
        if bounds:
            parts.append(",".join(str(v) for v in self.bounds))
        return "/".join(parts)


@dataclass
class Window:
    id: int
    type: str
    layer: int
    package: str
    title: str
    bounds: tuple
    root: Node | None
    since: int = 0  # when the phone first saw this window (uptime ms)

    @classmethod
    def of(cls, d: dict) -> "Window":
        return cls(id=int(d.get("id", 0)), type=d.get("type", "application"), layer=int(d.get("layer", 0)),
                   package=d.get("package", ""), title=d.get("title", ""),
                   bounds=tuple(d.get("bounds", (0, 0, 1080, 2400))),
                   root=Node.of(d["root"]) if d.get("root") else None, since=int(d.get("since", 0)))


@dataclass
class Screen:
    windows: list
    keyguard_locked: bool = False
    keyguard_secure: bool = True
    quiet_ms: int = 1000
    activity: str = ""
    autofill_service: str = ""
    resolve: dict = field(default_factory=dict)
    focus_expected: bool = False  # the phone has a live input focus (typing and ENTER go there)
    focus: dict = field(default_factory=dict)  # that focus resolved directly: {window, node, ancestors}
    available: bool = True  # the accessibility service is bound (else the phone can't see the screen at all)
    event: dict = field(default_factory=dict)  # the last window-state event: {package, text}
    owner: list = field(default_factory=list)  # the owner's own names (profile, paired account): "self" recipients
    labels: dict = field(default_factory=dict)
    # The phone's own OCR of a system dialog Android hides from accessibility (a permission dialog): text lines
    # [{text, bounds, conf}]. None: not read (no such dialog, or OCR unavailable).
    ocr: list | None = None

    @classmethod
    def of(cls, d: dict) -> "Screen":
        kg = d.get("keyguard") or {}
        return cls(windows=[Window.of(w) for w in d.get("windows", [])], keyguard_locked=bool(kg.get("locked")),
                   keyguard_secure=bool(kg.get("secure", True)), quiet_ms=int(d.get("quiet_ms", 1000)),
                   activity=d.get("activity", ""), autofill_service=d.get("autofill_service", ""),
                   resolve=d.get("resolve", {}), labels=d.get("labels", {}),
                   focus_expected=bool((d.get("focus") or {}).get("expected")),
                   focus=d.get("focus") or {},
                   available=d.get("accessibility", True) is not False, event=d.get("event") or {},  # {package, text, at}
                   owner=list(d.get("owner") or []), ocr=d.get("ocr"))

    def ordered(self) -> list[Window]:
        return sorted(self.windows, key=lambda w: -w.layer)


def walk(node: Node | None, out: list, limit: int = 2000):
    if node is None or len(out) >= limit:
        return out
    out.append(node)
    for c in node.children:
        walk(c, out, limit)
    return out


def contains(bounds, x, y) -> bool:
    l, t, r, b = bounds
    return l <= x < r and t <= y < b


@dataclass
class Target:
    window: Window
    path: list  # root .. deepest node containing the point

    @property
    def hit(self) -> Node | None:
        return self.path[-1] if self.path else None

    @property
    def pressed(self) -> Node | None:
        for n in reversed(self.path):
            if n.clickable or n.long_clickable:
                return n
        return self.hit

    def label_tokens(self) -> list[str]:
        parts = []
        pressed, hit = self.pressed, self.hit
        if pressed is not None and (pressed.clickable or pressed.long_clickable):
            for n in walk(pressed, [], 40):  # a button's label often sits in its children
                parts += [n.text, n.desc]
        elif pressed is not None:
            parts += [pressed.text, pressed.desc]
        if hit is not None and hit is not pressed:
            parts += [hit.text, hit.desc]
        return tokens(" ".join(p for p in parts if p))

    def ids(self) -> list[str]:
        out = []
        for n in (self.pressed, self.hit):
            if n is not None and n.id and n.id not in out:
                out.append(n.id)
        return out

    def labeled(self) -> bool:
        return bool(self.label_tokens() or self.ids())

    def in_webview(self) -> bool:
        return any("WebView" in n.cls for n in self.path)

    def shown(self) -> str:
        for n in (self.pressed, self.hit):
            if n is not None and (n.text or n.desc):
                return (n.text or n.desc).strip()
        if self.pressed is not None:
            for n in walk(self.pressed, [], 40):
                if n.text or n.desc:
                    return (n.text or n.desc).strip()
        ids = self.ids()
        return " ".join(id_tokens(ids[0])) if ids else ""

    def fp(self, bounds: bool) -> str:
        p, h = self.pressed, self.hit
        return "|".join([str(self.window.id), self.window.package, p.fp(bounds) if p else "", h.fp(bounds) if h else ""])


def hit_test(rules: Rules, screen: Screen, x: float, y: float) -> Target | None:
    """The window that catches a touch at (x, y) (highest layer first; our own keep-awake overlay never
    catches touches), and the path to the deepest node there."""
    for w in screen.ordered():
        if w.type == "accessibility_overlay" and w.package in rules.own:
            continue
        if not contains(w.bounds, x, y):
            continue
        path = []
        node = w.root
        while node is not None and node.contains(x, y):
            path.append(node)
            node = next((c for c in reversed(node.children) if c.contains(x, y)), None)
        return Target(w, path)
    return None


def focused_field(screen: Screen) -> tuple[Window, Node, list] | None:
    """The focused text input (the one type_text and ENTER go to) with its window and ancestors."""
    for w in screen.ordered():
        found = _find_focused(w.root, [])
        if found:
            return w, found[-1], found
    # Not in the walked tree (e.g. a Jetpack Compose text field): the node the phone's live input focus resolves
    # to, with its parent chain, counts if its window is one the phone listed.
    f = screen.focus
    if f.get("node") is not None:
        w = next((x for x in screen.windows if x.id == int(f.get("window", -1))), None)
        if w is not None:
            node = Node.of(f["node"])
            return w, node, [Node.of(a) for a in f.get("ancestors", [])] + [node]
    return None


def field_ref(w: Window, node: Node) -> str:
    """Which field the gate judged: its window, bounds, id and class (GateScreen.fieldRef)."""
    return f"{w.id}|{','.join(str(int(v)) for v in node.bounds)}|{node.id}|{node.cls}"


def _find_focused(node: Node | None, path: list) -> list | None:
    if node is None:
        return None
    path = path + [node]
    if node.focused and (node.editable or node.password):
        return path
    for c in node.children:
        if (r := _find_focused(c, path)) is not None:
            return r
    return None


def screen_tokens(screen: Screen) -> list[str]:
    parts = []
    for w in screen.windows:
        if w.type in ("application", "system"):
            for n in walk(w.root, []):
                parts += [n.text, n.desc]
    return tokens(" ".join(p for p in parts if p))


def screen_text(screen: Screen) -> str:
    parts = []
    for w in screen.windows:
        for n in walk(w.root, []):
            parts += [n.text, n.desc]
    return " ".join(p for p in parts if p)


def payment_context(rules: Rules, screen: Screen) -> bool:
    field_rx = rules.rx("payment_field")
    for w in screen.windows:
        for n in walk(w.root, []):
            if (n.editable or "EditText" in n.cls) and (field_rx.search(n.id or "") or field_rx.search(n.hint or "")):
                return True
    text = screen_text(screen)
    if rules.rx("currency").search(text):
        toks = tokens(text)
        return any_phrase(toks, rules.d["payment_words"]) is not None
    return False


def language_covered(rules: Rules, screen: Screen) -> bool:
    """Is the screen in a language the shipped lexicon covers? Judged by script (mostly non-Latin letters: no),
    then by real words only: lower-case alphabetic words of 4+ letters. Ticker labels ("DUN · 42%", "YES 63¢"),
    abbreviations and names don't count, so they never make a screen "uncovered"."""
    text = " ".join(f"{n.text} {n.desc}" for w in screen.windows if w.type in ("application", "system")
                    for n in walk(w.root, []))
    letters = [c for c in norm(text) if c.isalpha()]
    if len(letters) >= 20 and sum(1 for c in letters if not ("a" <= c <= "z")) / len(letters) >= 0.4:
        return False
    words = [norm(t) for t in re.split(r"[\W_\d]+", text) if len(t) >= 4 and t.isalpha() and t == t.lower()]
    if len(words) < rules.d["language"]["min_tokens"]:
        return True
    known = sum(1 for t in words if t in rules.lexicon)
    return known / len(words) >= rules.d["language"]["threshold"]


def money_amounts(window: Window, target) -> list[str]:
    """The currency amounts a money commit is about: in the target's own label, and on the window's total, cost,
    price or amount lines. A permit for a money commit binds them: any change asks again."""
    texts = []
    if target is not None:
        for n in ([target.pressed] if target.pressed is not None else []) + walk(target.pressed, [], 40)[1:] \
                + ([target.hit] if target.hit is not None else []):
            texts += [n.text, n.desc]
    for n in walk(window.root, []):
        line = f"{n.text} {n.desc}"
        if amounts(line) and any_phrase(tokens(line), MONEY_LABELS):
            texts.append(line)
    return sorted(set(a for t in texts for a in amounts(t)))


LINK = re.compile(r"(?i)https?://|www\.|youtu\.be/|\b[a-z0-9-]+\.(?:com|be|org|net|io|tv|app|co)/")


def shares_link(rules: Rules, window: Window) -> bool:
    """The share sheet shares a link or text with a link (its preview shows a URL), not a photo, file or contact."""
    return any(LINK.search(f"{n.text} {n.desc}") for n in walk(window.root, []))


def is_self(rules: Rules, label: list[str], screen: Screen) -> bool:
    """A recipient that is the owner, for public-link shares only (the one place the owner accepted a label-based
    exemption: a spoofed "self" can at most receive a public link). The owner's own names (the paired account), with
    an optional "(You)" the apps add, or an app's self chat ("Note to self", "Message yourself", "Saved messages").
    Nothing else ending in "you" counts."""
    if not label:
        return False
    if any(label == tokens(p) for p in rules.w["self"]):
        return True
    names = [tokens(n) for n in screen.owner if tokens(n)]
    return any(label == n or label == n + ["you"] for n in names)


def row_label(t: "Target") -> list[str]:
    """The words of the row a pressed button sits in (its siblings), without the button's own: who a "Send" next
    to a name in an in-app share list sends to."""
    if t.pressed is None or t.pressed not in t.path:
        return []
    i = t.path.index(t.pressed)
    if i == 0:
        return []
    row = t.path[i - 1]
    own = set(id(n) for n in walk(t.pressed, [], 40))
    return tokens(" ".join(f"{n.text} {n.desc}" for n in walk(row, [], 60) if id(n) not in own))


def priced(screen: Screen, window: Window) -> bool:
    """A price or currency on the target's window, or a Google Play billing sheet on screen."""
    if any(w.package == "com.android.vending" and w.type in ("application", "system") and w is not window
           for w in screen.windows):
        return True
    return any(amounts(f"{n.text} {n.desc}") for n in walk(window.root, []))


def page_title(window: Window) -> list[str]:
    """What a Settings page calls itself: its window title, and headers near the top that aren't rows."""
    parts = [window.title]
    top = window.bounds[1] + (window.bounds[3] - window.bounds[1]) * 0.16

    def visit(node: Node, clickable_above: bool):
        inside = clickable_above or node.clickable
        if not inside and not node.editable and node.bounds[1] < top and (node.text or node.desc):
            parts.append(node.text or node.desc)
        for c in node.children:
            visit(c, inside)
    if window.root is not None:
        visit(window.root, False)
    return tokens(" ".join(p for p in parts if p))


def url_scheme(url: str) -> str | None:
    url = re.sub(r"[\t\n\r]", "", url or "")
    url = re.sub("^[\\x00-\\x20\\u00a0\\ufeff]+", "", url)
    m = re.match(r"([A-Za-z][A-Za-z0-9+.\-]*):", url)
    return m.group(1).lower() if m else None


def looks_like_url(text: str | None) -> bool:
    text = (text or "").strip()
    if not text or " " in text:
        return False
    return url_scheme(text) is not None or bool(re.match(r"(www\.)?[\w-]+(\.[\w-]+)+(/|$|:\d)", text))


def binding(method: str, params: dict, fp: str) -> str:
    parts = []
    for k in ACTION_PARAMS:
        if k in params and params[k] is not None:
            v = params[k]
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, (int, float)):
                v = str(int(round(v)))
            parts.append(f"{k}={v}")
    return sha256(f"gk1-action|{method}|{'&'.join(parts)}|{fp}")


# --- decisions ----------------------------------------------------------------------------------

@dataclass
class Risk:
    kind: str  # pay | send | post | delete | share | settings | fill | other | step
    reason: str
    money: str | None = None  # start | terminal
    always: bool = False  # in the "always asks" class: refused under never
    grant: str | None = None  # the grant binding ("Always allow" offered), or None
    wording: str = ""


@dataclass
class Judgement:
    refuse: tuple | None = None  # (code, rule, message)
    package: str = ""
    fp: str = ""
    strict_fp: str = ""
    risk: Risk | None = None
    changing: bool = False
    payment: bool = False
    app: bool = False  # the target is an app window (an app switch ends a purchase flow)
    label: list = field(default_factory=list)  # the target's words (a cart or checkout word ends a purchase flow)
    money_fp: str = ""  # strict_fp plus the currency amounts a money commit is about (any change voids a permit)
    wording: str = ""  # how a prompt names this action from what the phone sees (a deep link names its page)
    field_ref: str = ""  # type_text, ENTER, DELETE: the field the gate judged (window, bounds, id, class)


@dataclass
class Admission:
    decision: str  # allow | refuse | ask | redact
    code: str = ""
    rule: str = ""
    message: str = ""
    kind: str = ""
    package: str = ""
    approval_id: str = ""
    wording: str = ""
    grant_offer: dict | None = None
    binding: str = ""
    strict: bool = False  # reverify needs the same fingerprint (bounds included) and a quiet screen
    permit: str | None = None
    method: str = ""
    params: dict = field(default_factory=dict)
    money: str | None = None
    plain: str = ""  # the binding with every number blanked (tells a price change from a moved target)
    level: str = ""  # an ask's confirmation level: tap (Allow/Deny) | fingerprint (the screen lock)
    field_ref: str = ""  # type_text, ENTER, DELETE: the field the gate judged; the phone types only into it (M2, L3)


@dataclass
class Grant:
    kind: str
    binding: str
    duration_ms: int
    issued: int  # monotonic ms
    canon: str


@dataclass
class Pending:
    approval_id: str
    binding: str
    created: int
    risk: Risk
    package: str
    task: str
    grant_offer: dict | None
    counted: bool = False
    digest: str = ""  # a task prompt: the request text's digest
    text: str = ""  # a task prompt: the request text (the grant's scope comes from it)
    step: bool = False  # a just-in-time task prompt: approving it also permits the step that raised it
    level: str = "fingerprint"  # tap: an on-phone Allow/Deny signed with the tap key; fingerprint: the screen lock  # shown to the owner (only shown prompts count toward the prompt limit)


class TestVerifier:
    """Fixtures sign with sha256("test-key:" + message); the phone uses its Keystore key."""

    @staticmethod
    def sign(message: str) -> str:
        return sha256("test-key:" + message)

    def verify(self, message: str, signature: str) -> bool:
        return bool(signature) and signature == self.sign(message)


class TapVerifier(TestVerifier):
    """The tap key (not bound to the screen lock): only OpenMob's own Allow button signs with it."""

    @staticmethod
    def sign(message: str) -> str:
        return sha256("test-tap-key:" + message)


class Clock:
    def __init__(self):
        self.elapsed = 10_000
        self.wall = 1_800_000_000_000


def policy_canon(record: dict) -> str:
    keys = sorted(k for k in record if record[k] is not None)
    return ";".join(f"{k}={_canon_value(record[k])}" for k in keys)


def _canon_value(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


class Gatekeeper:
    def __init__(self, rules: Rules | None = None, clock: Clock | None = None, verifier=None, device: str = "dev",
                 pages=None):
        self.r = rules or Rules.load()
        # The Settings pages open_settings can open: the app's fixed table (SettingsPages.kt; the server's copy is
        # openmob/phone/settings_pages.py, checked against the Kotlin file by a test). Tests inject other tables.
        self.pages = pages or _settings_pages().get
        self.clock = clock or Clock()
        self.verifier = verifier or TestVerifier()
        self.tap_verifier = TapVerifier()
        self.prompt_closed = -10**9  # when the last prompt closed (monotonic ms)
        self.everyday = False  # "Don't confirm everyday actions": signed by the owner; tap-level steps then run
        self.device = device
        self.permits: dict[str, int] = {}  # binding -> expiry (monotonic ms)
        self.pending: dict[str, Pending] = {}
        self.followup: dict | None = None  # {package, task, expires, acts}
        self.start_permits: dict[str, dict] = {}  # binding -> {package, task}: a flow-starting money approval
        self.credits: list[dict] = []  # {kind, expires}
        self.task_grants: dict[str, dict] = {}  # task id -> {digest, expires, used, apps}: the owner approved the task
        self.own_requests: list[dict] = []  # requests typed or spoken on this phone: {digest, expires}
        self.grants: list[Grant] = []
        self.ceiling = "auto"
        self.perm_defaults: dict[str, str] = {}  # the owner's signed (or tightened) permission-dialog defaults
        self.accuracy_choice: tuple | None = None  # ("<window id>|<title>", precise|approximate): the card tapped
        self.armed: tuple | None = None  # (binding, card): recorded once that tap's gesture completes
        self.never: dict | None = None  # {issued, duration_ms, allow_money}
        self.tokens = float(self.r.limits["act_burst"])
        self.last_refill = self.clock.elapsed
        self.asks: list[int] = []
        self.denials: list[int] = []
        self.suspended = False
        self.last_app = ""
        self.hwm = self.clock.elapsed
        self.events: list[dict] = []

    # --- clock and expiry -------------------------------------------------------------

    def now(self) -> int:
        e = self.clock.elapsed
        self.hwm = max(self.hwm, e)
        return e

    def _expire(self, now: int) -> None:
        for aid, p in list(self.pending.items()):
            if now - p.created >= self.r.limits["pending_ms"]:
                del self.pending[aid]
                self.prompt_closed = now
                self.events.append({"state": "expired", "approval_id": aid})
        self.permits = {b: e for b, e in self.permits.items() if e > now}
        if self.followup and self.followup["expires"] <= now:
            self.followup = None
        self.credits = [c for c in self.credits if c["expires"] > now]
        self.grants = [g for g in self.grants if g.issued + g.duration_ms > now]
        if self.never and self.never["issued"] + self.never["duration_ms"] <= now:
            self.never = None

    def persist(self) -> dict:
        now = self.now()
        return {"hwm": self.hwm, "grants": [{"canon": g.canon, "remaining": g.issued + g.duration_ms - now}
                                            for g in self.grants],
                "never": None if not self.never else self.never["issued"] + self.never["duration_ms"] - now}

    def reboot(self, saved: dict | None, elapsed_after: int) -> None:
        """The phone restarted: the monotonic clock starts over. Grants count down from the remaining time
        saved before the reboot; one whose remaining time can't be read expires (fail closed)."""
        self.clock.elapsed = elapsed_after
        now = elapsed_after
        rebooted = now < self.hwm
        grants = []
        for g in self.grants:
            remaining = None
            if saved:
                remaining = next((s["remaining"] for s in saved["grants"] if s["canon"] == g.canon), None)
            if not rebooted:
                grants.append(g)
            elif remaining is not None and remaining > 0:
                grants.append(Grant(g.kind, g.binding, g.duration_ms, now - (g.duration_ms - remaining), g.canon))
        self.grants = grants
        if self.never and rebooted:
            remaining = saved.get("never") if saved else None
            self.never = ({**self.never, "issued": now - (self.never["duration_ms"] - remaining)}
                          if remaining and remaining > 0 else None)
        # In-memory state does not survive a restart.
        self.permits, self.pending, self.followup, self.credits = {}, {}, None, []
        self.start_permits = {}
        self.task_grants, self.own_requests = {}, []
        self.tokens, self.last_refill = float(self.r.limits["act_burst"]), now
        self.prompt_closed = -10**9
        self.asks, self.denials = [], []
        self.last_app, self.suspended = "", False
        self.hwm = now

    # --- signed owner policy ----------------------------------------------------------

    def add_policy(self, record: dict, message: str, signature: str) -> bool:
        """A record signed with the owner's screen-lock key (never from the server). A bad signature is
        dropped: the built-ins (ceiling auto) apply."""
        canon = policy_canon(record)
        if message != f"gk1|policy|{canon}" or not self.verifier.verify(message, signature):
            return False
        return self._apply_policy(record, canon)

    def _apply_policy(self, record: dict, canon: str) -> bool:
        now = self.now()
        lim = self.r.limits
        kind = record.get("type")
        if kind == "ceiling":
            mode = record.get("mode")
            if mode not in ("ask", "plan", "auto", "trusted"):
                return False
            self.ceiling = mode
            return True
        if kind == "never":
            duration = min(int(record.get("duration_ms", 0)), lim["never_max_ms"])
            if duration <= 0:
                return False
            self.never = {"issued": now, "duration_ms": duration, "allow_money": bool(record.get("allow_money"))}
            return True
        if kind == "everyday":
            self.everyday = bool(record.get("on"))
            return True
        if kind == "permission_default":
            perm, value = record.get("perm"), record.get("value")
            if value not in self._perm_values(perm):
                return False
            self.perm_defaults[perm] = value
            return True
        if kind == "grant":
            g_kind = record.get("kind")
            if g_kind not in ("send", "post", "delete", "pay", "share", "settings", "fill", "permission") \
                    or not record.get("binding"):
                return False
            cap = lim["pay_grant_max_ms"] if g_kind == "pay" else lim["grant_max_ms"]
            duration = min(int(record.get("duration_ms", 0)), cap)
            if duration <= 0:
                return False
            self.grants.append(Grant(g_kind, str(record["binding"]), duration, now, canon))
            return True
        return False

    def _perm_values(self, perm) -> list:
        """The settings a permission takes, tightest first."""
        v = self.r.d["permission_dialog"]["values"]
        return v.get(perm, v["default"]) if perm else []

    def perm_setting(self, perm: str) -> str:
        d = self.r.d["permission_dialog"]["defaults"]
        return self.perm_defaults.get(perm) or d.get(perm, d["default"])

    def everyday_off(self) -> None:
        """A tightening, from anywhere: everyday actions are confirmed again."""
        self.everyday = False

    def tighten_permission(self, perm: str, value: str) -> bool:
        """A tightening (deny, ask, approximate…) is taken from anywhere, unsigned; loosening needs a signed record."""
        values = self._perm_values(perm)
        if value not in values or values.index(value) >= values.index(self.perm_setting(perm)):
            return False
        self.perm_defaults[perm] = value
        return True

    def mode(self, requested: str | None) -> str:
        ceiling = "never" if self.never else self.ceiling
        if requested not in MODES:
            requested = ceiling
        mode = min((requested, ceiling), key=lambda m: MODES[m])
        return "auto" if mode == "plan" else mode

    # --- the owner's own instruction (D6) ---------------------------------------------

    def note_instruction(self, text: str) -> None:
        toks = tokens(text)
        now = self.now()
        self.own_requests.append({"digest": sha256(" ".join(text.split())),
                                  "expires": now + self.r.limits["own_request_ms"]})
        for kind in ("send", "post", "delete"):
            if any_phrase(toks, self.r.w[kind]):
                self.credits.append({"kind": kind, "expires": now + self.r.limits["requested_ms"]})

    # --- kill switch ------------------------------------------------------------------

    def resume(self) -> None:
        """The owner resumed after a suspension (the notification's Resume)."""
        self.suspended = False
        self.denials = []

    def revoke_all(self) -> None:
        for aid in list(self.pending):
            self.events.append({"state": "expired", "approval_id": aid})
        self.permits, self.pending, self.followup, self.credits = {}, {}, None, []
        self.start_permits = {}
        self.task_grants, self.own_requests = {}, []
        self.never = None
        self.suspended = False

    # --- admit ------------------------------------------------------------------------

    def admit(self, method: str, params: dict, screen: Screen) -> Admission:
        self._judged_field = ""
        a = self._admit(method, params, screen)
        if a.decision in ("allow", "ask"):
            a.field_ref = self._judged_field
        return a

    def _admit(self, method: str, params: dict, screen: Screen) -> Admission:
        now = self.now()
        self._expire(now)
        params = params or {}
        screen = self.with_ocr(screen)
        if method in self.r.meta:
            return Admission("allow", method=method, params=params)
        if method in self.r.d["methods"]["task"]:
            return self.request_task(params)
        if method in self.r.read or method.startswith("stream_"):
            hidden = self._hidden(screen)
            if method in ("screenshot", "ui_elements", "wait_for") and hidden:  # wait_for reads the element list
                return Admission("redact", rule=hidden, method=method, params=params)
            if method == "stream_start" and hidden:
                return self._refuse("gate_blocked_app", hidden, "A protected app is on screen, so the live view "
                                    "can't start now.", method, params)
            return Admission("allow", method=method, params=params)
        if method not in self.r.act:
            return self._refuse("gate_unknown_method", "builtin:methods", f"Unknown method {method}.", method, params)
        if not screen.available:
            return self._refuse("gate_unavailable", "builtin:no_accessibility",
                                "OpenMob's accessibility service is off, so the phone can't check this. Turn it on.",
                                method, params)
        if method != "wake_unlock" and screen.keyguard_locked and screen.keyguard_secure:
            return self._refuse("phone_locked", "builtin:keyguard", "The phone is locked. Unlock it to continue.",
                                method, params)
        if method != "wake_unlock" and (dialog := self.owner_dialog(screen)):
            return self._refuse("needs_owner_dialog", "builtin:owner_dialog", dialog, method, params)
        j = self.judge(method, params, screen)
        self._judged_field = j.field_ref
        if j.refuse:
            if self.pending and j.refuse[1] == "builtin:own_window":
                # A gesture that would land on the phone's own prompt while it waits for the owner: wait, don't give
                # up (Phase 5 item 1).
                return self._refuse("gate_prompt_open", "builtin:prompt_open",
                                    "Waiting for the owner to answer a prompt on the phone.", method, params, j.package)
            return self._refuse(*j.refuse, method, params, package=j.package)
        if self.suspended:
            return self._refuse("gate_rate_limited", "builtin:suspended",
                                "Paused on the phone after repeated denials. The owner can resume.", method, params)
        money_fp = j.money_fp if (j.risk is not None and j.risk.money) else j.strict_fp
        bind = binding(method, params, money_fp)
        plain = binding(method, params, j.strict_fp)
        if self.pending:
            same = next((p for p in self.pending.values() if p.binding == bind), None)
            if same is not None:  # the same action asked again: the prompt already showing, not a new one
                return Admission("ask", code="gate_needs_approval", rule=f"risk:{same.risk.reason}",
                                 kind=same.risk.kind, approval_id=same.approval_id, wording=same.risk.wording,
                                 grant_offer=same.grant_offer, binding=bind, money=same.risk.money, plain=plain,
                                 message="Waiting for the owner's approval on the phone.", method=method,
                                 params=params, package=j.package)
            return self._refuse("gate_prompt_open", "builtin:prompt_open",
                                "Waiting for the owner to answer a prompt on the phone.", method, params)
        if not self._take_token(now):
            return self._refuse("gate_rate_limited", "builtin:act_rate", "Too many actions: wait a second.",
                                method, params)
        if method != "wake_unlock" and bind not in self.permits:
            # Any other action voids every outstanding approval: what the owner saw may no longer be true (a
            # quantity "+", another item scrolled into place). The honest server re-sends at once.
            self.permits.clear()
            self.start_permits.clear()
        self._track_followup(method, params, j)
        if j.app and j.package:
            self.last_app = j.package
        self.note_task_app(params, j.package, j.app)
        mode = self.mode(params.get("mode"))
        risk = j.risk
        hint = str(params.get("hint") or "").strip()
        if risk is None and hint and mode != "never":
            risk = Risk("other", "server_hint", wording=self._wording(method, params, j, screen))
        if mode == "ask" and j.changing and risk is None:
            risk = Risk("step", "mode:ask", wording=self._wording(method, params, j, screen))
        base = dict(method=method, params=params, package=j.package, plain=plain)
        if risk is None:
            return Admission("allow", strict=j.payment, binding=bind, **base)
        if mode == "never":
            if risk.money and not (self.never and self.never["allow_money"]):
                return self._refuse("gate_denied", "never:money",
                                    "Purchases are off while agents act without asking.", method, params, j.package)
            if risk.always:
                return self._refuse("gate_denied", f"never:{risk.reason}",
                                    "Nobody can be asked while agents act without asking, so this is refused.",
                                    method, params, j.package)
            return Admission("allow", strict=True, binding=bind, kind=risk.kind, money=risk.money, **base)
        if bind in self.permits:
            return Admission("allow", strict=True, binding=bind, permit=bind, kind=risk.kind, money=risk.money, **base)
        if mode != "ask" and (why := self._covered(risk, j.package, params)):
            return Admission("allow", strict=True, binding=bind, kind=risk.kind, rule=why, money=risk.money, **base)
        if mode != "ask" and self._task_covers(risk, params, j.package, screen):
            return Admission("allow", strict=True, binding=bind, kind=risk.kind, rule="task_grant", **base)
        if mode != "ask" and self.everyday and self.level(risk) == "tap" and risk.kind not in ("permission",
                                                                                             "uninstall", "call"):
            return Admission("allow", strict=True, binding=bind, kind=risk.kind, rule="everyday", **base)
        # Task approval, just in time: the first step that needs a tap-level confirmation asks for the task instead
        # (worded from the request, with this step as the detail). A request typed on this phone needs no prompt.
        task = str(params.get("task") or "")
        text = " ".join(str(params.get("task_text") or "").split())[:300]
        lazy = (mode != "ask" and task and text and self.level(risk) == "tap" and risk.kind not in ("task", "step")
                and risk.kind in self._task_kinds(text))
        if lazy and any(o["digest"] == sha256(text) and o["expires"] > now for o in self.own_requests):
            self._mint_task(task, sha256(text), text)
            self.note_task_app(params, j.package if j.app else self.last_app, True)
            if self._task_covers(risk, params, j.package, screen):
                return Admission("allow", strict=True, binding=bind, kind=risk.kind, rule="task_grant:own_request",
                                 **base)
        window = self.r.limits["ask_window_ms"]
        self.asks = [t for t in self.asks if now - t < window]
        if len(self.asks) >= self.r.limits["asks_per_window"]:
            wait = max(1, -(-(self.asks[0] + window - now) // 60000))
            return self._refuse("gate_rate_limited", "builtin:prompt_rate",
                                f"Too many approval prompts on the phone: try again in {wait} min.",
                                method, params, j.package)
        aid = "gk" + uuid.uuid4().hex  # gate prompts are "gk…"; the legacy approval path refuses such ids
        if lazy and (task not in self.task_grants or self.task_grants[task]["digest"] != sha256(text)
                     or now >= self.task_grants[task]["expires"]):
            wording = f"Agent wants to: {text}. Allow the steps this needs? Next: {risk.wording}"
            p = Pending(aid, bind, now, Risk("task", "task", wording=wording), j.package if j.app else self.last_app,
                        task, None, level="tap")
            p.digest, p.text, p.step = sha256(text), text, True
            self.pending[aid] = p
            self.events.append({"state": "pending", "approval_id": aid})
            return Admission("ask", code="gate_needs_approval", rule=f"risk:task:{risk.reason}", kind="task",
                             approval_id=aid, wording=wording, binding=bind, level="tap",
                             message="Waiting for the owner's approval of this task on the phone.", **base)
        offer = self._offer(risk, mode)
        self.pending[aid] = Pending(aid, bind, now, risk, j.package, str(params.get("task") or ""), offer,
                                    level=self.level(risk))
        self.events.append({"state": "pending", "approval_id": aid})
        return Admission("ask", code="gate_needs_approval", rule=f"risk:{risk.reason}", kind=risk.kind,
                         approval_id=aid, wording=risk.wording, grant_offer=offer, binding=bind, money=risk.money,
                         level=self.level(risk),
                         message="Waiting for the owner's approval on the phone.", **base)

    # --- task approval (by owner request): one prompt for the task, its non-money risky steps then run --------

    def request_task(self, params: dict) -> Admission:
        """The server asks for a task grant at task start: the phone shows the request itself ("Agent wants to: …")
        and the owner approves the task once. Requests typed or spoken on this phone need no prompt."""
        now = self.now()
        task = str(params.get("task") or "")
        text = " ".join(str(params.get("text") or "").split())[:300]
        source = " ".join(str(params.get("source") or "").split())[:60]
        method = "task_grant"
        if not task or not text:
            return self._refuse("gate_denied", "builtin:task_request", "A task grant needs the task id and its "
                                "request text.", method, params)
        if self.suspended:  # paused after repeated denials: no task prompt either (review M5)
            return self._refuse("gate_rate_limited", "builtin:suspended",
                                "Paused on the phone after repeated denials. The owner can resume.", method, params)
        digest = sha256(text)
        bind = sha256(f"gk1-task|{task}|{digest}")
        g = self.task_grants.get(task)
        if g is not None and g["digest"] == digest and now < g["expires"]:
            return Admission("allow", rule="task_grant", kind="task", method=method, params=params, binding=bind)
        if self.everyday:  # everyday steps run unconfirmed anyway: no task prompt
            return Admission("allow", rule="everyday", kind="task", method=method, params=params, binding=bind)
        if any(o["digest"] == digest and o["expires"] > now for o in self.own_requests):
            self._mint_task(task, digest, text)
            return Admission("allow", rule="task_grant:own_request", kind="task", method=method, params=params,
                             binding=bind)
        same = next((p for p in self.pending.values() if p.binding == bind), None)
        if same is not None:
            return Admission("ask", code="gate_needs_approval", rule="risk:task", kind="task",
                             approval_id=same.approval_id, wording=same.risk.wording, binding=bind, method=method,
                             params=params, message="Waiting for the owner's approval on the phone.")
        if self.pending:
            return self._refuse("gate_prompt_open", "builtin:prompt_open",
                                "Waiting for the owner to answer a prompt on the phone.", method, params)
        window = self.r.limits["ask_window_ms"]
        self.asks = [t for t in self.asks if now - t < window]
        if len(self.asks) >= self.r.limits["asks_per_window"]:
            return self._refuse("gate_rate_limited", "builtin:prompt_rate",
                                "Too many approval prompts on the phone: try again later.", method, params)
        wording = f"Agent wants to: {text}. Allow the steps this needs?"
        risk = Risk("task", "task", wording=wording)
        aid = "gk" + uuid.uuid4().hex
        self.pending[aid] = Pending(aid, bind, now, risk, "", task, None, level="tap")
        self.pending[aid].digest = digest
        self.pending[aid].text = text
        self.events.append({"state": "pending", "approval_id": aid})
        return Admission("ask", code="gate_needs_approval", rule="risk:task", kind="task", approval_id=aid,
                         wording=wording, binding=bind, method=method, params=params, level="tap",
                         message="Waiting for the owner's approval of this task on the phone.")

    def _mint_task(self, task: str, digest: str, text: str = "") -> None:
        """The grant covers what the request names: the first app the task works in plus apps the request names,
        and only the kinds of risky step its words ask for (a "share" request doesn't cover deleting or posting)."""
        self.task_grants[task] = {"digest": digest, "expires": self.now() + self.r.limits["task_grant_ms"],
                                  "used": 0, "apps": set(), "first": "", "words": tokens(text),
                                  "kinds": self._task_kinds(text)}

    def _task_kinds(self, text: str) -> set:
        """The kinds of risky step a request's words ask for."""
        toks = tokens(text)
        kinds = {"other", "fill"}
        for kind, extra in (("send", ["share", "message", "text", "reply", "forward", "email", "tell"]),
                            ("share", ["share", "send", "forward"]), ("post", ["share"]),
                            ("delete", ["unsubscribe", "clear", "remove", "empty", "uninstall"]),
                            ("call", ["call", "dial", "phone", "ring"])):
            if any_phrase(toks, self.r.w.get(kind, []) + extra):
                kinds.add(kind)
        return kinds

    @staticmethod
    def level(risk: Risk) -> str:
        """Three confirmation levels (owner principle): a fingerprint only for money and dangerous actions (saved
        cards and passwords in a browser or web page, OTP or card fields, permission grants); a simple on-phone
        Allow/Deny for everyday-but-consequential ones (messages and shares to others, posts, deletes, native
        autofill, unrecognized Settings changes, menus the server worries about). Low-risk actions never get here."""
        if risk.money or (risk.kind == "fill" and risk.grant is None) or risk.kind in ("settings", "call_code"):
            return "fingerprint"  # a Settings change the phone didn't recognize fails closed (D2): never a tap
        return "tap"

    def note_task_app(self, params: dict, package: str, app: bool) -> None:
        """The first app a granted task works in is the app it's about."""
        g = self.task_grants.get(str(params.get("task") or ""))
        if g is not None and app and package and not g["first"]:
            g["first"] = package

    def _task_covers(self, risk: Risk, params: dict, package: str, screen: Screen | None = None) -> bool:
        """Under the task's grant: the tap-level risky steps of the kinds its request asks for, in its first app or
        an app the request names (the share sheet and other system surfaces count as the app they serve). Never
        money, Settings, browser or WebView autofill, or permission dialogs (and never the hard denies)."""
        g = self.task_grants.get(str(params.get("task") or ""))
        if g is None or self.now() >= g["expires"] or g["used"] >= self.r.limits["task_grant_steps"]:
            return False
        text = " ".join(str(params.get("task_text") or "").split())[:300]
        if text and sha256(text) != g["digest"]:
            return False  # the grant was for another request
        if risk.kind in ("task", "step") or self.level(risk) != "tap" or risk.kind not in g["kinds"]:
            return False
        app = package if package and (screen is None or any(
            w.package == package and self._app_window(w) for w in screen.windows)) else self.last_app
        if risk.reason.startswith("uninstall:"):
            app = risk.grant or ""  # an uninstall counts as the app it removes
        label = tokens((screen.labels.get(app) if screen else "") or "")
        if app != g["first"] and not (label and has_phrase(g["words"], " ".join(label))):
            return False
        g["used"] += 1
        if package:
            g["apps"].add(package)  # the apps the task actually used
        return True

    def _hidden(self, screen: Screen) -> str:
        """Why reads must not see this screen ("" if they may): a blocked app in any window, or an app window the
        phone can't identify yet (it may be a blocked app still loading)."""
        if any(self.r.blocked(w.package) for w in screen.windows):
            return "builtin:sensitive_apps"
        # A system dialog Android hides from accessibility (a permission dialog: its window has no readable root) is
        # identified from its window-state event: not sensitive, so reads show it; acts on it stay refused.
        dialog = self._ocr_window(screen)
        if any(not w.package and w.type == "application" and (dialog is None or w.id != dialog.id)
               for w in screen.windows):
            return "builtin:unknown_window"
        return ""

    def owner_dialog(self, screen: Screen) -> str | None:
        """A system dialog the phone can't read is up (Android 14+ hides permission dialogs, Safety Center and
        default-apps screens from accessibility): only the owner can answer it. Returns what to tell them."""
        if self._ocr_window(screen) is None:
            return None
        text = str(screen.event.get("text") or "")
        m = re.match(r"(?i)^allow\s+(.+?)\s+to\s", text)
        if not m:
            return "A system dialog needs your answer: choose on your phone, then the task continues."
        kind = next((k for k, rx in self.r.d["permission_dialog"]["kinds"] if re.search(rx, text)), "")
        what = kind.replace("_", " ") if kind else "a permission"
        return f"{m.group(1)} is asking for {what}: choose on your phone, then the task continues."

    def _refuse(self, code, rule, message, method, params, package="") -> Admission:
        return Admission("refuse", code=code, rule=rule, message=message, method=method, params=params, package=package)

    def _track_followup(self, method: str, params: dict, j: Judgement) -> None:
        """A purchase flow's one terminal follow-up ends at an app switch, at BACK/HOME/recents, a launch or a link,
        a tap on a cart or checkout word, and after 3 other actions."""
        f = self.followup
        if f is None:
            return
        name = str(params.get("name", "")).upper()
        if (method in ("launch_app", "open_url", "open_settings") or (method == "key" and name in ("BACK", "HOME", "APP_SWITCH"))
                or (j.app and j.package and j.package != f["package"])
                or (j.label and any_phrase(j.label, self.r.d["cart_words"]))):
            self.followup = None
            return
        f["acts"] += 1
        if f["acts"] > 3:
            self.followup = None

    def _take_token(self, now: int) -> bool:
        lim = self.r.limits
        self.tokens = min(lim["act_burst"], self.tokens + (now - self.last_refill) * lim["act_per_second"] / 1000)
        self.last_refill = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    def _covered(self, risk: Risk, package: str, params: dict) -> str | None:
        now = self.now()
        if risk.money == "terminal" and self.followup and self.followup["package"] == package \
                and self.followup["task"] == str(params.get("task") or ""):  # acts <= 3 (_track_followup)
            self.followup = None
            return "permit:purchase_flow"
        if risk.kind in ("send", "post", "delete") and not risk.money:
            for c in self.credits:
                if c["kind"] == risk.kind:
                    self.credits.remove(c)
                    return "requested"
        if risk.grant is not None:
            wanted = {risk.grant}
            if risk.kind == "permission" and risk.grant.endswith("|once"):
                wanted.add(risk.grant[:-len("once")] + "while_using")  # "while using" covers "only this time"
            for g in self.grants:
                if g.kind == risk.kind and g.binding in wanted and g.issued + g.duration_ms > now:
                    return "grant"
        return None

    def _offer(self, risk: Risk, mode: str) -> dict | None:
        if risk.grant is None or mode == "ask":
            return None
        lim = self.r.limits
        ms = lim["pay_grant_default_ms"] if risk.kind == "pay" else lim["grant_default_ms"]
        return {"kind": risk.kind, "binding": risk.grant, "duration_ms": ms, "confirm_twice": risk.kind == "pay"}

    # --- approvals --------------------------------------------------------------------

    def approval_message(self, approval_id: str, grant: bool = False) -> str:
        p = self.pending[approval_id]
        msg = (f"gk1|tap|{self.device}|{approval_id}|{p.binding}" if p.level == "tap"
               else f"gk1|{self.device}|{approval_id}|{p.binding}")
        if grant and p.grant_offer:
            msg += "|grant:" + policy_canon({"type": "grant", **{k: p.grant_offer[k] for k in
                                                                 ("kind", "binding", "duration_ms")}})
        return msg

    def approve(self, approval_id: str, message: str, signature: str) -> bool:
        """The owner passed the screen lock on the phone's own prompt: mint a one-shot permit for exactly
        that action (and, if they ticked it, the "Always allow" grant)."""
        now = self.now()
        self._expire(now)
        p = self.pending.get(approval_id)
        if p is None:
            return False
        self.mark_shown(approval_id)  # answered, so it was seen
        plain = self.approval_message(approval_id)
        grant = None
        if message != plain:
            if not (p.grant_offer and message == self.approval_message(approval_id, grant=True)):
                return False
            grant = p.grant_offer
        # A tap-level prompt is signed with the tap key, a fingerprint one with the screen-lock key: neither
        # signature can approve the other kind (the messages are domain-separated too).
        if not (self.tap_verifier if p.level == "tap" else self.verifier).verify(message, signature):
            return False
        del self.pending[approval_id]
        self.prompt_closed = now
        if p.risk.kind == "task":  # the owner approved the task: a grant (and the step that asked)
            self._mint_task(p.task, p.digest, p.text)
            if p.step:
                self.permits[p.binding] = now + self.r.limits["permit_ms"]
                if p.package:
                    self.task_grants[p.task]["first"] = self.task_grants[p.task]["first"] or p.package
            self.events.append({"state": "approved", "approval_id": approval_id})
            return True
        self.permits[p.binding] = now + self.r.limits["permit_ms"]
        if p.risk.money == "start":  # the follow-up starts only when this very tap runs (reverify)
            self.start_permits[p.binding] = {"package": p.package, "task": p.task}
        if grant:
            record = {"type": "grant", **{k: grant[k] for k in ("kind", "binding", "duration_ms")}}
            self._apply_policy(record, policy_canon(record))
        self.events.append({"state": "approved", "approval_id": approval_id})
        return True

    def mark_shown(self, approval_id: str) -> None:
        """The prompt is on the owner's screen: now it counts toward the prompt limit."""
        p = self.pending.get(approval_id)
        if p is not None and not p.counted:
            p.counted = True
            self.asks.append(self.now())

    def cancel(self, approval_id: str) -> bool:
        """The prompt ended unanswered (dismissed, or the connection closed): no permit, and not a denial."""
        if self.pending.pop(approval_id, None) is None:
            return False
        self.prompt_closed = self.now()
        self.events.append({"state": "expired", "approval_id": approval_id})
        return True

    def deny(self, approval_id: str) -> bool:
        now = self.now()
        self.mark_shown(approval_id)
        if self.pending.pop(approval_id, None) is None:
            return False
        self.prompt_closed = now
        self.denials = [t for t in self.denials if now - t < self.r.limits["denial_window_ms"]] + [now]
        if len(self.denials) >= self.r.limits["denials_to_suspend"]:
            self.suspended = True
        self.events.append({"state": "denied", "approval_id": approval_id})
        return True

    # --- reverify (right before the side effect, under the gesture lock) ------------------

    def reverify(self, adm: Admission, screen: Screen) -> Admission:
        """Right before the side effect. A Precise/Approximate tap on an OCR'd dialog is only armed here: the phone
        records it once the gesture completed (completed()); a refused one makes it assume Precise again (B1)."""
        self.armed = None
        out = self._reverify(adm, screen)
        if adm.decision == "allow" and adm.method == "tap":
            card = self._ocr_card(adm, self.with_ocr(screen))
            if out.decision != "allow":
                if card is not None:
                    self.accuracy_choice = None
            elif card is not None:
                self.armed = (adm.binding, card)
        return out

    def completed(self, adm: Admission) -> None:
        """The gesture for an admitted action completed on the screen."""
        if self.armed is not None and self.armed[0] == adm.binding:
            self.accuracy_choice = self.armed[1]
        self.armed = None

    def _reverify(self, adm: Admission, screen: Screen) -> Admission:
        if adm.decision != "allow" or adm.method not in self.r.act:
            return adm
        screen = self.with_ocr(screen)
        # No gesture while a prompt is up, or just after one closed: an action admitted a moment before could land on
        # its Allow (B1). The approved action itself (its permit) goes ahead.
        if adm.method != "wake_unlock" and not adm.permit and (
                self.pending or self.now() - self.prompt_closed < self.r.limits["prompt_cooldown_ms"]):
            return self._refuse("gate_prompt_open", "builtin:prompt_cooldown",
                                "An approval prompt is (or was just) on screen. Try again in a moment.",
                                adm.method, adm.params, adm.package)
        j = self.judge(adm.method, adm.params, screen)
        if j.refuse:
            self.permits.pop(adm.permit or "", None)
            return self._refuse(*j.refuse, adm.method, adm.params, j.package)
        if j.field_ref != adm.field_ref:  # focus moved to another field since the check (review M2, L3)
            self.permits.pop(adm.permit or "", None)
            return self._refuse("gate_screen_changed", "builtin:field_changed",
                                "The focused field changed before typing. Look again.", adm.method, adm.params,
                                j.package)
        if adm.strict:
            if binding(adm.method, adm.params, j.money_fp if adm.money else j.strict_fp) != adm.binding:
                self.permits.pop(adm.permit or "", None)
                if adm.money and binding(adm.method, adm.params, j.strict_fp) == adm.plain:
                    return self._refuse("gate_screen_changed", "builtin:price_changed",
                                        "The amount or price changed since it was approved. Look again.",
                                        adm.method, adm.params, j.package)
                return self._refuse("gate_screen_changed", "builtin:toctou",
                                    "The screen changed before the action ran. Look again.", adm.method, adm.params,
                                    j.package)
            if screen.quiet_ms < self.r.limits["quiet_ms"]:
                return self._refuse("gate_screen_unstable", "builtin:toctou",
                                    "The screen is still changing. Try again in a moment.", adm.method, adm.params,
                                    j.package)
        elif j.risk is not None or j.payment:
            return self._refuse("gate_screen_changed", "builtin:toctou",
                                "The screen changed before the action ran. Look again.", adm.method, adm.params,
                                j.package)
        if adm.permit:
            if self.permits.pop(adm.permit, None) is None:
                return self._refuse("gate_screen_changed", "builtin:permit", "That approval was already used.",
                                    adm.method, adm.params, j.package)
            start = self.start_permits.pop(adm.permit, None)
            if start is not None:
                self.followup = {**start, "acts": 0,
                                 "expires": self.now() + self.r.limits["purchase_followup_ms"]}
        return adm

    # --- permission dialogs Android hides from accessibility (Android 14+): the phone's own OCR -------------

    def _ocr_window(self, screen: Screen, need_title: bool = True) -> Window | None:
        """The permission-controller window with no readable tree: identified by its own package, or (when Android
        hides even that, and sends no event either: Samsung, Android 15) by the phone's OCR of its own screenshot:
        an "Allow X to ..." title AND either a fresh permission-controller event or a "Don't allow" button (the
        dialog's shape). A stale event over an app still loading is not a dialog. `need_title=False`: whether the
        phone should OCR the screen to find out (any window with no package and no readable tree)."""
        perm = re.compile(self.r.s["permission_packages"])
        event_pkg = str(screen.event.get("package") or "")
        event_at = int(screen.event.get("at", 0) or 0)
        for w in screen.ordered():
            if w.type != "application":
                continue
            unread = w.root is None or not any(n.text or n.desc for n in walk(w.root, []))
            if not unread:
                continue
            if perm.search(w.package or ""):
                return w
            if w.package:
                continue
            if not need_title:
                return w
            slack = self.r.d["permission_dialog"]["ocr"]["event_slack_ms"]  # the event may land just before the window
            fresh = bool(perm.search(event_pkg)) and event_at >= w.since - slack
            if self._ocr_title(screen) and (fresh or self._ocr_deny(screen)):
                return w
            return None  # only the top unreadable window can be the dialog
        return None

    def _ocr_lines(self, screen: Screen) -> list:
        o = self.r.d["permission_dialog"]["ocr"]
        return [" ".join(str(b.get("text", "")).split()) for b in (screen.ocr or [])
                if float(b.get("conf", 1.0)) >= o["min_conf"]]

    def _ocr_deny(self, screen: Screen) -> bool:
        """The OCR shows a permission dialog's "Don't allow" (or its translation)."""
        return any(self.text_role(t) in ("deny", "deny_forever") for t in self._ocr_lines(screen))

    def ocr_candidate(self, screen: Screen) -> Window | None:
        return self._ocr_window(screen, need_title=False)

    def _ocr_title(self, screen: Screen) -> bool:
        """The phone's OCR shows a permission dialog's title ("Allow Kalshi to ...") with confidence."""
        lines = self._ocr_lines(screen)
        # One line, or two in a row (a title wrapped or split where the app name is bold).
        return any(self.perm_app(t) for t in lines + [a + " " + b for a, b in zip(lines, lines[1:])])

    def text_role(self, text: str) -> str | None:
        toks = tokens(text)
        for role, label in self.r.d["permission_dialog"]["button_texts"]:
            if toks and toks == tokens(label):
                return role
        return None

    def perm_app(self, message: str) -> str | None:
        """The requesting app's name, from the dialog's title ("Allow Kalshi to ...")."""
        for rx in self.r.d["permission_dialog"]["titles"]:
            m = re.search(rx, message)
            if m:
                return m.group(1).strip()
        return None

    GRANT_ROLES = ("allow", "deny", "deny_forever", "one_time", "foreground", "always")

    def with_ocr(self, screen: Screen) -> Screen:
        """The screen with the hidden permission dialog rebuilt from the phone's OCR. Only the dialog card counts
        (review H1, M3): the title is the permission-style title nearest above the Allow/Deny buttons, and lines
        outside the card (that title down to the last button of the button stack) are dropped. Precise/Approximate
        count only between the title and the buttons. Any doubt (no title, more than one permission-style title on
        screen, no buttons under it) marks the dialog `ocr_unsure`: every grant on it asks. Each button line is a
        clickable node (its box padded), the rest text; the root is clickable, so any other tap in it asks."""
        w = self._ocr_window(screen)
        if w is None:
            self.accuracy_choice = None  # the dialog is gone: its card choice with it
            return screen
        if screen.ocr is None:
            return screen
        o = self.r.d["permission_dialog"]["ocr"]
        lines = sorted((b for b in screen.ocr if str(b.get("text", "")).strip()
                        and float(b.get("conf", 1.0)) >= o["min_conf"]),
                       key=lambda b: (b["bounds"][1], b["bounds"][0]))
        pad = o["pad"]
        start = re.compile(o["title_start"])
        texts = [" ".join(str(b["text"]).split()) for b in lines]
        roles = [self.text_role(t) for t in texts]
        bounds = [tuple(int(v) for v in b["bounds"]) for b in lines]
        # Every permission-style title on screen: a line starting like one, plus its wrapped lines up to the '?'.
        blocks, used = [], set()
        for k in range(len(lines)):
            if k in used or roles[k] is not None or not start.search(texts[k]):
                continue
            idx, j = [k], k
            while not texts[j].endswith("?") and len(idx) < o["title_lines"] and j + 1 < len(lines) \
                    and roles[j + 1] is None:
                j += 1
                idx.append(j)
            text = " ".join(texts[i] for i in idx)
            if self.perm_app(text) is None:
                continue
            used.update(idx)
            blocks.append({"idx": idx, "text": text, "top": min(bounds[i][1] for i in idx),
                           "bottom": max(bounds[i][3] for i in idx)})
        buttons = [i for i in range(len(lines)) if roles[i] in self.GRANT_ROLES]

        def below(b):
            return sorted((i for i in buttons if bounds[i][1] >= b["bottom"]), key=lambda i: bounds[i][1])

        with_buttons = [b for b in blocks if below(b)]
        chosen = max(with_buttons, key=lambda b: b["top"]) if with_buttons else None
        unsure = len(blocks) != 1 or chosen is None
        keep = None
        card_top, first_button_top = None, None
        if chosen is not None:
            stack = []
            for b in below(chosen):
                if not stack:
                    stack.append(b)
                    continue
                last = bounds[stack[-1]]
                height = max(1, last[3] - last[1])
                if bounds[b][1] - last[3] <= o["button_gap_lines"] * height:
                    stack.append(b)
                else:
                    break
            box = [bounds[i] for i in chosen["idx"] + stack]
            left, right = min(b[0] for b in box) - pad, max(b[2] for b in box) + pad
            top, bottom = chosen["top"] - pad, max(b[3] for b in box) + pad
            card_top, first_button_top = chosen["bottom"], bounds[stack[0]][1]
            if not unsure:  # in doubt every grant asks anyway: keep every line, so the prompt names the button
                keep = (left, top, right, bottom)
        message = chosen["text"] if chosen else (" ".join(texts[i] for i in range(len(lines)) if roles[i] is None)
                                                 or "?")
        key = f"{w.id}|{message}"
        if self.accuracy_choice and self.accuracy_choice[0] != key:
            self.accuracy_choice = None  # another dialog: assume Precise again
        choice = None if unsure else self.accuracy_choice
        nodes = []
        for i in range(len(lines)):
            l, t, r, bt = bounds[i]
            if keep is not None and not (l >= keep[0] and r <= keep[2] and t >= keep[1] and bt <= keep[3]):
                continue
            text, role = texts[i], roles[i]
            card_role = role in ("precise", "approximate")
            in_card = chosen is not None and t >= card_top and bt <= first_button_top
            if role is not None and (not card_role or in_card or chosen is None):
                checked = None
                if card_role:  # OCR can't see the radio: Android pre-selects Precise
                    checked = role == (choice[1] if choice else "precise")
                nodes.append(Node(text=text, id="ocr", cls="OCRText", bounds=(l - pad, t - pad, r + pad, bt + pad),
                                  clickable=True, checked=checked))
                continue
            nodes.append(Node(text=text, cls="OCRText", bounds=(l, t, r, bt)))
        nodes.insert(0, Node(text=message, id="permission_message", cls="OCRText"))
        if unsure:
            nodes.insert(1, Node(id="ocr_unsure", cls="OCRText"))
        root = Node(cls="OCRDialog", bounds=w.bounds, clickable=True, children=nodes)
        event_pkg = str(screen.event.get("package") or "")
        pkg = w.package or (event_pkg if re.search(self.r.s["permission_packages"], event_pkg)
                            else self.r.d["permission_dialog"]["ocr"]["package"])
        rebuilt = Window(w.id, w.type, w.layer, pkg, w.title, w.bounds, root)
        return dc_replace(screen, windows=[rebuilt if x is w else x for x in screen.windows])

    def _ocr_card(self, adm: Admission, screen: Screen) -> tuple | None:
        """(window id, precise|approximate) when this tap is on an OCR'd dialog's location card."""
        t = hit_test(self.r, screen, float(adm.params.get("x", -1)), float(adm.params.get("y", -1)))
        if t is None or t.window.root is None or t.window.root.cls != "OCRDialog":
            return None
        if any(c.id == "ocr_unsure" for c in t.window.root.children):
            return None
        message = next((c.text for c in t.window.root.children if c.id == "permission_message"), "")
        role = self._perm_role(t)
        # Keyed by the window and its title: the same window with another question starts from Precise again (M1).
        return (f"{t.window.id}|{message}", role) if role in ("precise", "approximate") else None

    # --- the judge: what the action touches and how risky it is ---------------------------

    def label(self, screen: Screen, package: str) -> str:
        return screen.labels.get(package) or package

    def _wording(self, method, params, j: Judgement, screen: Screen) -> str:
        app = self.label(screen, j.package) if j.package else "this phone"
        return {"type_text": f"Allow typing into a field in {app}?", "key": f"Allow pressing Enter in {app}?",
                "launch_app": f"Allow opening {app}?",
                "open_url": f"Allow opening {params.get('url', '')!s:.80} in {app}?",
                "open_settings": j.wording or "Allow opening a Settings page?"}.get(
            method, f"Allow this action in {app}?")

    def judge(self, method: str, params: dict, screen: Screen) -> Judgement:
        r = self.r
        if method in ("tap", "long_press"):
            return self._judge_point(method, float(params.get("x", -1)), float(params.get("y", -1)), screen)
        if method == "swipe":
            x1, y1, x2, y2 = (float(params.get(k, -1)) for k in ("x1", "y1", "x2", "y2"))
            duration = float(params.get("duration_ms", 300))
            dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            if dist <= 2 * r.d["swipe"]["slop_px"] or duration < r.d["swipe"]["tap_ms"]:
                return self._judge_point("swipe", x1, y1, screen)
            return self._judge_swipe(x1, y1, x2, y2, screen)
        if method == "type_text":
            return self._judge_typing(str(params.get("text", "")), screen)
        if method == "key":
            return self._judge_key(str(params.get("name", "")).upper(), screen)
        if method == "launch_app":
            return self._judge_launch(str(params.get("package", "")), screen)
        if method == "open_url":
            return self._judge_url(str(params.get("url", "")), screen, method)
        if method == "open_settings":
            return self._judge_open_settings(str(params.get("page", "")), str(params.get("package", "")), screen)
        return Judgement()  # wake_unlock: wakes the screen, never touches a secure lock

    def _judge_open_settings(self, key: str, package: str, screen: Screen) -> Judgement:
        """A Settings deep link (docs/design/deep-links.md), judged like opening that page (D2): the key must be in
        the phone's fixed table; a page the hard-deny list names (by action, like an activity, or by title) is
        refused whatever the table says; an app's own pages are refused for blocked apps and OpenMob; an ask-tier
        page (manual date and time) asks with the screen lock; everyday pages open."""
        r = self.r
        fp = f"settings|{key}|{package}"
        j = Judgement(fp=fp, strict_fp=fp, changing=True)
        page = self.pages(key)
        if page is None:
            j.refuse = ("gate_denied_page", "builtin:settings_unknown_page",
                        f"No Settings page '{key}' on this phone's list. Navigate there instead.")
            return j
        if re.compile(r.s["refuse_activities"]).search(page.action) or any_phrase(tokens(page.title), r.s["refuse"]):
            j.refuse = ("gate_denied_page", "builtin:settings_security",
                        "This Settings page is security-critical; agents can't open it.")
            return j
        if page.needs_package:
            j.package = package
            if not package:
                j.refuse = ("gate_denied_page", "builtin:settings_package", f"The {page.title} page needs an app's package.")
            elif package in r.own:
                j.refuse = ("gate_blocked_app", "builtin:own_app", "OpenMob's own Settings pages are off-limits to agents.")
            elif r.blocked(package):
                j.refuse = ("gate_blocked_app", "builtin:sensitive_apps",
                            f"Blocked on the phone: {package} is a sensitive app, so its Settings pages are off-limits too. "
                            "Call done.")
            if j.refuse:
                return j
        j.wording = f"Allow opening the {page.title} Settings page" + (
            f" of {self.label(screen, package)}" if package else "") + "?"
        if page.tier == "ask":
            j.risk = Risk("settings", "settings_page", always=True, wording=j.wording)
        return j

    def _uninstall_dialog(self, w: Window) -> bool:
        rx = re.compile(self.r.d["uninstall"]["message"])
        return any(rx.search(n.text or "") for n in walk(w.root, []))

    def _uninstall_target(self, w: Window, screen: Screen) -> str:
        """The app an uninstall dialog or app-info page is about: the installed app whose label it shows (the
        longest match), "protected" for OpenMob or a blocked app, "" if the phone can't tell."""
        texts = [tokens(n.text) for n in walk(w.root, []) if n.text]
        if any(has_phrase(tx, own) for tx in texts for own in self.r.d["uninstall"]["own_labels"]):
            return "protected"
        best, size = "", 0
        for pkg, lbl in screen.labels.items():
            lt = tokens(lbl)
            if pkg in self.r.installers or pkg == w.package or not lt:
                continue
            if any(has_phrase(tx, " ".join(lt)) for tx in texts):
                if self.r.blocked(pkg) or pkg in self.r.own:
                    return "protected"
                if len(lt) > size:
                    best, size = pkg, len(lt)
        return best

    def _surface_refusal(self, w: Window, t: Target | None) -> tuple | None:
        r = self.r
        if not w.package:
            return ("gate_screen_unstable", "builtin:unknown_window",
                    "That part of the screen can't be read yet (an app may still be loading). Look again.")
        if w.package in r.own:
            return ("gate_denied_page", "builtin:own_window", "That's OpenMob's own screen; agents can't touch it.")
        if w.package == r.surf["systemui"]:
            title = re.compile(r.surf["prompt_titles"])
            ids = re.compile(r.surf["prompt_ids"])
            if title.search(w.title or "") or any(ids.search(n.id or "") for n in walk(w.root, [])):
                return ("gate_denied_page", "builtin:prompt_surface",
                        "That's the lock screen or a system approval prompt; agents can't touch it.")
        if w.package in r.installers and not self._uninstall_dialog(w):
            return ("gate_denied_page", "builtin:installer", "Installing apps is off-limits to agents.")
        if r.blocked(w.package):
            return ("gate_blocked_app", "builtin:sensitive_apps",
                    f"Blocked on the phone: {w.package} is a sensitive app. Don't open it another way; call done.")
        return None

    def _app_window(self, w: Window) -> bool:
        """An app's own window (what "the task's app" and a purchase flow's app are), not a system surface."""
        r = self.r
        return (w.type == "application" and w.package != r.surf["systemui"] and not self._is_settings(w)
                and w.package not in r.surf["share_packages"] and w.package not in r.surf["credential_packages"]
                and not re.search(r.s["permission_packages"], w.package or "") and w.package not in r.installers)

    def _is_settings(self, w: Window) -> bool:
        if self.r.settings_pkg.search(w.package or ""):
            return True
        return w.package == self.r.s["gms_package"] and bool(
            re.compile(self.r.s["gms_settings_titles"]).search(w.title or ""))

    def _settings_tier(self, w: Window, t: Target | None, screen: Screen, label: list[str]) -> tuple:
        """("refuse", rule) | ("blocked", rule) | ("ask", page) | ("allow", "") | ("none", "")."""
        s = self.r.s
        title = page_title(w)
        activity = screen.activity if w.type == "application" else ""
        if w.package in s["ask_packages"]:  # e.g. the VPN consent dialog
            return ("ask", w.package)
        if any_phrase(title, s["refuse"]) or any_phrase(label, s["refuse"]) or (
                activity and re.compile(s["refuse_activities"]).search(activity)):
            return ("refuse", "builtin:settings_security")
        page = "/".join([w.package, activity, " ".join(title)])
        screen_toks = tokens(" ".join(f"{n.text} {n.desc}" for n in walk(w.root, [])))
        if any_phrase(title, s["app_info_markers"]) or (has_phrase(screen_toks, "force stop") and (
                has_phrase(screen_toks, "uninstall") or has_phrase(screen_toks, "disable"))):
            blocked_labels = [tokens(lbl) for pkg, lbl in screen.labels.items() if self.r.blocked(pkg)]
            texts = [tokens(n.text) for n in walk(w.root, []) if n.text]
            if any(bl and bl in texts for bl in blocked_labels):
                return ("blocked", "builtin:app_info_blocked")
            if any_phrase(label, s["app_info_ask"]):
                return ("ask", page)
            if any_phrase(label, s["app_info_delete"]):
                return ("delete", page)
            if any_phrase(label, s["app_info_allowed"]):
                return ("allow", "")
            return ("none", "")
        if any_phrase(title, s["ask_pages"]) or any_phrase(label, s["ask_pages"]):
            if label and any_phrase(label, self.r.w["navigation"]) == " ".join(label):
                return ("allow", "")
            return ("ask", page)
        if re.compile(s["permission_packages"]).search(w.package or "") or has_phrase(title, "permissions") \
                or has_phrase(title, "permission"):
            if self._session_app_asks(w, screen, title):
                return ("allow", "")
            return ("ask", page) if self._changing_tap(t, label) else ("none", "")
        if (has_phrase(title, "notifications") or has_phrase(title, "notification")) and self._changing_tap(t, label):
            # Notification switches for a blocked app (a bank's alerts) or for OpenMob itself ask (review L1).
            protected = [tokens(v) for k, v in screen.labels.items() if self.r.blocked(k) or k in self.r.own] + \
                [tokens(x) for x in self.r.d["uninstall"]["own_labels"]]
            if any(p and (has_phrase(label, " ".join(p)) or has_phrase(title, " ".join(p))) for p in protected):
                return ("ask", page)
        if any_phrase(title, s["everyday"]) or any_phrase(label, s["everyday"]):
            return ("allow", "")
        return ("ask", page) if self._changing_tap(t, label) else ("none", "")

    def _session_app_asks(self, w: Window, screen: Screen, title: list[str]) -> bool:
        """Is this permission request from the app the task is using? The dialog names the app in one place
        ("Allow <app> to ..."); without that line, the requesting app's window must be right underneath; on a
        Settings permission page, its header must name the app."""
        session = tokens(screen.labels.get(self.last_app, "")) if self.last_app else []
        if not session:
            return False
        message = next((n.text for n in walk(w.root, []) if n.id.endswith("permission_message") and n.text), "")
        if message:
            app = tokens(self.perm_app(message) or "")
            if not app:
                return False
            if app == session:
                return True
            # The dialog may name the app shorter or longer than its label ("Chess" for "Chess.com"): then the
            # requesting app's window must also be right underneath.
            below = [x for x in screen.ordered() if x.layer < w.layer and x.type == "application"]
            return (app == session[:len(app)] or session == app[:len(session)]) and bool(below) \
                and below[0].package == self.last_app
        if re.compile(self.r.s["permission_packages"]).search(w.package or ""):
            below = [x for x in screen.ordered() if x.layer < w.layer and x.type == "application"]
            return bool(below) and below[0].package == self.last_app
        return has_phrase(title, " ".join(session))

    def _perm_role(self, t: Target) -> str | None:
        pd = self.r.d["permission_dialog"]
        for n in (t.pressed, t.hit):
            if n is not None and n.id:
                for role, rx in pd["button_ids"]:
                    if re.search(rx, n.id):
                        return role
        label = t.label_tokens()
        for role, text in pd["button_texts"]:
            if label and " ".join(label) == " ".join(tokens(text)):
                return role
        return None

    def _permission_dialog(self, w: Window, t: Target, screen: Screen):
        """A tap in a runtime permission dialog: a Risk, None, or a refusal (code, rule, message). Denying (and
        "Approximate") is always fine. "Allow all the time" never. The owner's settings: `deny` lets only "Don't
        allow" through; the task's own app may then grant what they allow without a prompt (signed per-app grants
        first; built in: while using the app, notifications allowed, location approximate); another app's request
        and a button the phone doesn't recognize ask (a tap-level prompt)."""
        pd = self.r.d["permission_dialog"]
        message = next((n.text for n in walk(w.root, []) if n.id.endswith("permission_message") and n.text), "")
        kind = next((k for k, rx in pd["kinds"] if re.search(rx, message)), "other")
        role = self._perm_role(t)
        name = self.perm_app(message) or "this app"
        if role is None:
            # A button the phone doesn't recognize (other ids, another language, OCR it couldn't read) may grant: ask.
            changing = t.pressed is not None and (t.pressed.clickable or t.pressed.long_clickable
                                                  or t.pressed.checked is not None)
            return Risk("permission", "permission:unknown", always=True,
                        wording=f"Allow {name}'s permission request?") if changing else None
        if role in ("deny", "deny_forever", "approximate"):
            return None
        if role == "always":
            return ("gate_permission_choice", "permission:always",
                    "Never \"Allow all the time\": tap \"While using the app\" (or \"Only this time\") instead.")
        if any(n.id == "ocr_unsure" for n in walk(w.root, [])):
            # The OCR couldn't pin down which question the dialog asks (no title, or more than one permission-style
            # title on screen, e.g. one an app drew above the real dialog): any grant asks (review H1).
            return Risk("permission", "permission:ocr_unsure", always=True,
                        wording=f"Allow {name}'s permission request? The phone couldn't tell for sure which "
                                "permission it is: check the dialog.")
        what = "precise location" if role == "precise" else kind.replace("_", " ")
        wording = f"Allow {name} to use your {what}?"
        if role == "precise":
            value, setting_kind = "precise", "location_accuracy"
        elif kind == "notifications":
            value, setting_kind = "allow", "notifications"
        else:
            value, setting_kind = ("once" if role == "one_time" else "while_using"), kind
        setting = self.perm_setting(setting_kind)
        own = self._session_app_asks(w, screen, page_title(w))
        grant = f"{self.last_app}|{setting_kind}|{value}"
        if setting == "deny" and not (own and self._perm_granted(grant)):
            return ("gate_permission_choice", f"permission:{setting_kind}:deny",
                    f"The owner's setting for {setting_kind.replace('_', ' ')} is Don't allow: tap \"Don't allow\".")
        if not own:
            return Risk("permission", "permission:other_app", always=True, wording=wording)
        if role == "precise":
            ok = setting == "precise"
        elif kind == "notifications":
            ok = setting == "allow"
        else:
            # "Only this time" by default; where the dialog doesn't offer it, the next narrowest grant.
            only_once = any(self._perm_role(Target(w, [n])) == "one_time" for n in walk(w.root, []) if n.clickable)
            # ...except a lasting grant of a dangerous kind (contacts, SMS…): it asks unless the owner signed
            # persistent_grants: allow.
            lasting = kind in self.r.d["permission_dialog"]["persistent_ask"] and \
                self.perm_setting("persistent_grants") != "allow"
            ok = setting == "while_using" or (setting == "once" and (role == "one_time" or (not only_once and not lasting)))
        ok = ok or self._perm_granted(grant)
        if ok and kind == "location" and self.perm_setting("location_accuracy") != "precise" and \
                self._precise_selected(w) and not self._perm_granted(f"{self.last_app}|location_accuracy|precise"):
            # Android pre-selects Precise: granting now would give precise location against the owner's setting.
            return ("gate_permission_choice", "permission:location_accuracy",
                    "The owner allows approximate location only: tap \"Approximate\" first, then grant.")
        if ok:
            return None
        return Risk("permission", f"permission:{setting_kind}", always=True, grant=grant, wording=wording)

    def _perm_granted(self, grant: str) -> bool:
        """A signed per-app permission grant ("Always allow" on an earlier prompt)."""
        wanted = {grant}
        if grant.endswith("|once"):
            wanted.add(grant[:-len("once")] + "while_using")
        now = self.now()
        return any(g.kind == "permission" and g.binding in wanted and g.issued + g.duration_ms > now
                   for g in self.grants)

    def _precise_selected(self, w: Window) -> bool:
        for n in walk(w.root, []):
            if n.checked is True and self._perm_role(Target(w, [n])) == "precise":
                return True
        return False

    def _changing_tap(self, t: Target | None, label: list[str]) -> bool:
        if t is not None and any(n.checked is not None for n in (t.pressed, t.hit) if n is not None):
            return True
        return any_phrase(label, self.r.s["change_words"]) is not None

    def _word_risk(self, label: list[str], ids: list[str], package: str, payment: bool,
                   priced: bool = True) -> Risk | None:
        r = self.r
        words = strip_phrases(label, r.w["pre_commit"] + r.w["reversible"])
        id_words = []
        for rid in ids:
            toks = id_tokens(rid)
            for w in r.d["resource_id_risky"]:
                if w not in r.d["resource_id_pre_commit"] and (has_phrase(toks, w) or (" " not in w and w in toks)):
                    id_words.append(w)
        money_ctx = payment or bool(r.money_app.search(package or ""))
        flow = any_phrase(words, r.w["flow_start"]) or next((w for w in id_words if w in ("buy", "purchase",
                                                                                          "transfer")), None)
        if flow in ("subscribe", "join") and not (priced or money_ctx):
            # YouTube's free Subscribe, a free group's Join: reversible, and money only with a price nearby or a
            # billing sheet.
            flow = None
        if flow:
            return Risk("pay", f"words:{flow}", money="start", grant=package)
        terminal = any_phrase(words, r.w["terminal"]) or next(
            (w for w in id_words if w in ("pay", "place order", "placeorder", "confirm")), None)
        if terminal:
            if terminal in ("confirm", "submit") and not money_ctx:
                return Risk("other", f"words:{terminal}")
            return Risk("pay", f"words:{terminal}", money="terminal", grant=package)
        for kind in ("send", "post", "delete"):
            w = any_phrase(words, r.w[kind])
            if w:
                return Risk(kind, f"words:{w}", grant=package)
        for w in id_words:
            if w == "send":
                return Risk("send", "id:send", grant=package)
            if w in ("delete", "remove"):
                return Risk("delete", f"id:{w}", grant=package)
            if w == "submit":
                return Risk("other", "id:submit")
        return None

    # --- phone calls (review H2) ----------------------------------------------------------------

    def call_app(self, package: str) -> bool:
        """A dialer, contacts app or in-call screen: its call buttons place calls."""
        return package in self.r.d["calls"]["packages"]

    @staticmethod
    def mmi(text: str) -> bool:
        """An MMI/USSD code (call forwarding `**21*…#`, settings codes): any * or #, plain or percent-encoded."""
        return "*" in text or "#" in text or "%23" in text or "%2a" in text.lower()

    def _dialed_number(self, w: Window) -> str:
        rx = re.compile(self.r.d["calls"]["number_ids"])
        nodes = walk(w.root, [])
        n = next((n for n in nodes if rx.search(n.id or "") and n.text), None) or \
            next((n for n in nodes if (n.editable or "EditText" in (n.cls or "")) and n.text), None)
        return n.text.strip() if n else ""

    @staticmethod
    def _call_code_risk(number: str, app: str) -> Risk:
        return Risk("call_code", "call:mmi", always=True,
                    wording=f"Allow dialing the code “{number[:40]}” in {app}? Codes with * or # can change phone "
                            "settings, such as forwarding your calls.")

    def _call_risk(self, t: Target, label: list[str], ids: list[str], screen: Screen) -> Risk | None:
        """A tap that places a call: in a dialer, contacts app or in-call screen, a label or id that says call or
        dial (not end call, call history…), or the dialpad's * and # keys; in any app, a button labelled exactly
        Call, or voice/video call. A number with * or # asks with the screen lock."""
        c = self.r.d["calls"]
        w = t.window
        app = self.label(screen, w.package)
        if any_phrase(label, c["safe_words"]) or any(re.search(c["safe_ids"], i) for i in ids):
            return None
        if self.call_app(w.package):
            raw = [f"{n.text}{n.desc}".strip() for n in (t.pressed, t.hit) if n is not None]
            code_key = any(x in ("*", "#") for x in raw) or any(re.search(c["code_keys"], i) for i in ids) or \
                (len(label) == 1 and label[0] in ("pound", "star"))
            if code_key:
                return self._call_code_risk(self._dialed_number(w) + next((x for x in raw if x in ("*", "#")), ""),
                                            app)
            if not (any_phrase(label, c["words"]) or any(re.search(c["ids"], i) for i in ids)):
                return None
            number = self._dialed_number(w) or t.shown()
            if self.mmi(number):
                return self._call_code_risk(number, app)
            return Risk("call", "call:dial", wording="Allow placing a call" +
                        (f" to “{number[:40]}”" if number else "") + f" from {app}?")
        if label == ["call"] or any_phrase(label, c["any_app_words"]) or any(re.search(c["any_app_ids"], i)
                                                                            for i in ids):
            return Risk("call", "call:button", wording=f"Allow starting a call (“{t.shown()[:40]}”) in {app}?")
        return None

    def _judge_point(self, method: str, x: float, y: float, screen: Screen) -> Judgement:
        r = self.r
        t = hit_test(r, screen, x, y)
        if t is None:  # no window there that the phone knows of: it can't judge the touch
            return Judgement(changing=True, refuse=("gate_screen_unstable", "builtin:no_window",
                                                    "The phone can't see what's there. Look again."))
        w = t.window
        j = Judgement(package=w.package, fp=t.fp(False), strict_fp=t.fp(True), changing=True,
                      app=self._app_window(w))
        j.money_fp = j.strict_fp + "|$" + ",".join(money_amounts(w, t))
        if refusal := self._surface_refusal(w, t):
            j.refuse = refusal
            return j
        pressed = t.pressed
        if pressed is not None and (pressed.editable or "EditText" in pressed.cls):
            j.changing = False  # focusing a field changes nothing
        label = t.label_tokens()
        j.label = label
        ids = t.ids()
        j.payment = payment_context(r, screen)
        shown = t.shown()
        app_label = self.label(screen, w.package)
        source = self.last_app or w.package
        in_browser = r.is_cat("browser", source) or t.in_webview()

        def wording(verb="Allow tapping"):
            return f"{verb} “{shown}” in {app_label}?" if shown else f"{verb} an unlabeled spot in {app_label}?"

        if re.compile(r.s["permission_packages"]).search(w.package or "") and any(
                n.id.endswith("permission_message") and n.text for n in walk(w.root, [])):
            # The question is part of what an approval binds: the same button under another question (content
            # swapped in place) needs a new approval (review M1).
            msg = next(n.text for n in walk(w.root, []) if n.id.endswith("permission_message") and n.text)
            msg = "|msg:" + " ".join(tokens(msg))
            j.fp, j.strict_fp, j.money_fp = j.fp + msg, j.strict_fp + msg, j.money_fp + msg
            res = self._permission_dialog(w, t, screen)
            if isinstance(res, tuple):
                j.refuse = res
            else:
                j.risk = res
            return j
        # Settings
        tier = ("none", "")
        if self._is_settings(w):
            tier = self._settings_tier(w, t, screen, label)
            if tier[0] == "refuse":
                j.refuse = ("gate_denied_page", tier[1], "This Settings page is security-critical; agents can't "
                                                         "change it.")
                return j
            if tier[0] == "blocked":
                j.refuse = ("gate_blocked_app", tier[1], "That's a blocked app's settings; agents can't touch it.")
                return j
            if tier[0] == "delete" or (tier[0] == "allow" and any_phrase(label, ["uninstall"])):
                target = self._uninstall_target(w, screen)
                if target == "protected":
                    j.refuse = ("gate_denied_page", "builtin:uninstall_protected",
                                "OpenMob and blocked apps can't be uninstalled or changed by agents.")
                    return j
                if tier[0] == "delete":  # Disable, Clear storage: a delete-kind step on that app (tap level)
                    j.risk = Risk("delete", "uninstall:app_info", grant=target or None, wording=wording()) \
                        if target else Risk("uninstall", "uninstall:unknown", wording=wording())
                    return j
                return j  # Uninstall: opens the installer's confirmation, which asks
        # The package installer's uninstall confirmation (installing is refused above): a delete-kind step.
        if w.package in r.installers:
            target = self._uninstall_target(w, screen)
            if target == "protected":
                j.refuse = ("gate_denied_page", "builtin:uninstall_protected",
                            "OpenMob and blocked apps can't be uninstalled by agents.")
                return j
            if label and any(" ".join(label) == " ".join(tokens(c)) for c in r.d["uninstall"]["confirm"]):
                name = self.label(screen, target) if target else "an app"
                j.risk = Risk("delete", "uninstall:installer", grant=target, wording=f"Allow uninstalling {name}?") \
                    if target else Risk("uninstall", "uninstall:unknown", wording="Allow uninstalling an app?")
            return j
        # Autofill and credential surfaces: always ask.
        cred_title = re.compile(r.surf["credential_titles"])
        surface = (w.type == "input_method" or w.package in r.surf["credential_packages"]
                   or (screen.autofill_service and w.package == screen.autofill_service)
                   or (w.package == r.s["gms_package"] and cred_title.search(w.title or "")))
        if surface or any_phrase(label, r.w["credential"]):
            # What gets filled is the focused field: grants bind to its app, never to a browser or WebView page.
            field_ = focused_field(screen)
            filled = field_[0].package if field_ else ""
            web = bool(field_) and (r.is_cat("browser", filled) or any("WebView" in n.cls for n in field_[2]))
            grant = None if (not filled or web or r.blocked(screen.autofill_service)) else filled
            j.risk = Risk("fill", "autofill", always=True, grant=grant,
                          wording=f"Fill your saved card or password into {self.label(screen, filled or source)}?")
            return j
        # Share sheets: what is shared, to whom. A public link to yourself, or copying it, is low risk.
        if w.package in r.surf["share_packages"] and (re.compile(r.surf["share_titles"]).search(w.title or "")
                                                       or w.package != "android"):
            link = shares_link(r, w)
            if any_phrase(label, r.w["copy"]) or (link and is_self(r, label, screen)):
                return j
            j.risk = Risk("share", "share:" + ("link" if link else "content"), grant=f"{source}>{' '.join(label)}",
                          wording=f"Share to “{shown or 'this target'}”?")
            return j
        # Notification action buttons
        if w.package == r.surf["systemui"] and pressed is not None and pressed is not w.root and (
                re.compile(r.surf["notification_action_ids"]).search(pressed.id or "") or "Button" in pressed.cls):
            j.risk = Risk("other", "notification_action", wording=wording())
            return j
        call = self._call_risk(t, label, ids, screen)
        if call is not None:
            j.risk = call
            return j
        risk = self._word_risk(label, ids, w.package, j.payment, priced=priced(screen, w))
        if risk is not None and risk.kind == "send" and not risk.money and shares_link(r, w) and \
                is_self(r, row_label(t), screen):
            risk = None  # an in-app share list's "Send" next to your own name, while a public link is shared
        if risk is None:
            slider = any(re.compile(r.d["slider_class"]).search(n.cls or "") for n in (pressed, t.hit) if n)
            if slider or any_phrase(label, r.w["slide"]):
                risk = Risk("pay" if j.payment else "other", "slider", money="terminal" if j.payment else None,
                            grant=w.package if j.payment else None)
        if risk is None and t.path and t.path[-1].truncated:
            risk = Risk("other", "truncated")  # the phone couldn't read what's under the point: ask
        money_app = bool(r.money_app.search(w.package or "")) or j.payment
        if risk is None and money_app and not t.labeled():
            risk = Risk("pay" if j.payment else "other", "unlabeled", money="terminal" if j.payment else None)
        if risk is None and money_app and j.changing and not language_covered(r, screen) and not (
                label and any_phrase(label, r.w["navigation"])):
            risk = Risk("other", "language")
        if risk is None and tier[0] == "ask":
            risk = Risk("settings", "settings_page", always=True, grant=tier[1])
        if risk is not None:
            if not risk.wording:
                risk.wording = wording()
            if risk.grant is not None and risk.kind == "pay" and in_browser:
                risk.grant = w.package
        j.risk = risk
        return j

    def _judge_swipe(self, x1, y1, x2, y2, screen: Screen) -> Judgement:
        r = self.r
        start = hit_test(r, screen, x1, y1)
        end = hit_test(r, screen, x2, y2)
        pkg = start.window.package if start else ""
        j = Judgement(package=pkg, changing=False,
                      fp="#".join(t.fp(False) if t else "" for t in (start, end)),
                      strict_fp="#".join(t.fp(True) if t else "" for t in (start, end)),
                      money_fp="#".join(t.fp(True) + "|$" + ",".join(money_amounts(t.window, t)) if t else ""
                                        for t in (start, end)),
                      app=bool(start and self._app_window(start.window)))
        for t in (start, end):
            if t is None:
                continue
            if refusal := self._surface_refusal(t.window, t):
                j.refuse = refusal
                return j
        j.payment = payment_context(r, screen)
        for t in (start, end):
            if t is None:
                continue
            label = t.label_tokens()
            slider = any(re.compile(r.d["slider_class"]).search(n.cls or "") for n in (t.pressed, t.hit) if n) \
                or any_phrase(label, r.w["slide"])
            risk = self._word_risk(label, t.ids(), t.window.package, j.payment)
            if risk is None and slider:
                money = j.payment or any_phrase(strip_phrases(label, r.w["pre_commit"]),
                                                r.w["flow_start"] + r.w["terminal"])
                risk = Risk("pay" if money else "other", "slider", money="terminal" if money else None,
                            grant=t.window.package if money else None)
            if risk is not None:
                shown = t.shown()
                risk.wording = f"Allow sliding “{shown}” in {self.label(screen, t.window.package)}?"
                j.risk = risk
                j.changing = True
                return j
        return j

    @staticmethod
    def _focus_unread(screen: Screen) -> tuple:
        """No field the gate can judge has focus: typing, ENTER and DELETE are refused by rule (review M2). A focus it
        couldn't read (R1) says so; no focus at all says to tap the field."""
        if screen.focus_expected:
            return ("gate_screen_unstable", "builtin:focus_unread",
                    "The field that has focus couldn't be read. Look again.")
        return ("gate_screen_unstable", "builtin:no_field",
                "No text field the phone can judge has focus. Tap the field first, then look again.")

    def _judge_typing(self, text: str, screen: Screen) -> Judgement:
        r = self.r
        scheme = url_scheme(text)
        if scheme in r.d["schemes"]["typed_refused"]:
            return Judgement(changing=True, refuse=("gate_denied", "builtin:typed_scheme",
                                                    f"{scheme}: text is never typed by agents."))
        found = focused_field(screen)
        if found is None:
            return Judgement(changing=True, refuse=self._focus_unread(screen))
        w, node, path = found
        fp = f"{w.id}|{w.package}|{node.id}/{node.hint}/{node.cls}/{int(node.password)}"
        j = Judgement(package=w.package, fp=fp, strict_fp=fp + "/" + ",".join(map(str, node.bounds)), changing=True,
                      money_fp=fp + "/" + ",".join(map(str, node.bounds)) + "|$" + ",".join(money_amounts(w, None)),
                      app=self._app_window(w), field_ref=field_ref(w, node))
        if refusal := self._surface_refusal(w, None):
            j.refuse = refusal
            return j
        if node.password:
            j.refuse = ("gate_password_field", "builtin:password_field",
                        "That's a password field. Agents never type passwords; ask the owner to type it.")
            return j
        if self._is_settings(w) and self._settings_tier(w, None, screen, [])[0] == "refuse":
            j.refuse = ("gate_denied_page", "builtin:settings_security",
                        "This Settings page is security-critical; agents can't change it.")
            return j
        j.payment = payment_context(r, screen)
        about = f"{node.id} {node.hint}"
        app = self.label(screen, w.package)
        if self.call_app(w.package) and self.mmi(text):
            j.risk = self._call_code_risk(node.text + text, app)  # some dialers run a code once its # is typed (H2)
        elif re.compile(r.d["payment_field"]).search(about):
            j.risk = Risk("fill", "card_field", always=True, wording=f"Allow typing into the card field in {app}?")
        elif re.compile(r.d["otp_field"]).search(about):
            j.risk = Risk("fill", "otp_field", always=True, wording=f"Allow typing a code into {app}?")
        return j

    def _judge_key(self, name: str, screen: Screen) -> Judgement:
        r = self.r
        if name not in ("ENTER", "DELETE"):
            return Judgement(changing=False)
        found = focused_field(screen)
        if found is None:
            return Judgement(changing=name == "ENTER", refuse=self._focus_unread(screen))
        w, node, path = found
        fp = f"{w.id}|{w.package}|{node.id}/{node.hint}/{node.cls}"
        j = Judgement(package=w.package, fp=fp, strict_fp=fp + "/" + ",".join(map(str, node.bounds)),
                      money_fp=fp + "/" + ",".join(map(str, node.bounds)) + "|$" + ",".join(money_amounts(w, None)),
                      changing=name == "ENTER", app=self._app_window(w), field_ref=field_ref(w, node))
        if refusal := self._surface_refusal(w, None):
            j.refuse = refusal
            return j
        if name == "DELETE":
            return j
        browser = r.is_cat("browser", w.package)
        webview = any("WebView" in n.cls for n in path)
        if browser and (re.compile(r.d["url_bar"]).search(node.id or "") or looks_like_url(node.text)):
            url = node.text.strip()
            if url_scheme(url) is None and looks_like_url(url):
                url = "https://" + url
            uj = self._judge_url(url, screen, "key")
            uj.fp, uj.strict_fp, uj.changing = fp + "|" + uj.fp, j.strict_fp + "|" + uj.strict_fp, True
            uj.field_ref = j.field_ref
            return uj
        j.payment = payment_context(r, screen)
        app = self.label(screen, w.package)
        if self.call_app(w.package):  # ENTER in a dialer's number field places the call (review H2)
            number = node.text.strip()
            j.risk = self._call_code_risk(number, app) if self.mmi(number) else Risk(
                "call", "call:enter",
                wording="Allow placing a call" + (f" to “{number[:40]}”" if number else "") + f" from {app}?")
            return j
        search = any(re.compile(r.d["search_field"]).search(v or "") for v in (node.id, node.cls, node.hint, node.desc))
        if j.payment:
            j.risk = Risk("pay", "enter:payment", money="terminal", grant=w.package,
                          wording=f"Allow pressing Enter on a checkout in {app}?")
        elif any(r.is_cat(c, w.package) for c in ("messaging", "email", "social")):
            j.risk = Risk("send", "enter:messaging", grant=w.package, wording=f"Allow sending with Enter in {app}?")
        elif (browser or webview) and not search:
            j.risk = Risk("other", "enter:form", wording=f"Allow submitting a form with Enter in {app}?")
        return j

    def _judge_launch(self, package: str, screen: Screen) -> Judgement:
        r = self.r
        j = Judgement(package=package, fp=f"launch|{package}", strict_fp=f"launch|{package}", changing=True,
                      app=True)
        if package in r.own:
            j.refuse = ("gate_blocked_app", "builtin:own_app", "OpenMob can't be opened by agents.")
        elif package in r.installers:
            j.refuse = ("gate_denied_page", "builtin:installer", "Installing apps is off-limits to agents.")
        elif r.blocked(package):
            j.refuse = ("gate_blocked_app", "builtin:sensitive_apps",
                        f"Blocked on the phone: {package} is a sensitive app. Don't open it another way; call done.")
        return j

    def _judge_url(self, url: str, screen: Screen, method: str) -> Judgement:
        r = self.r
        scheme = url_scheme(url)
        resolved = screen.resolve.get(url) or {}
        handler = resolved.get("package") or ""
        candidates = resolved.get("candidates") or ([handler] if handler else [])
        fp = f"url|{scheme}|{handler}"
        j = Judgement(package=handler, fp=fp, strict_fp=fp, changing=True, app=bool(handler))
        if scheme is None or scheme not in r.d["schemes"]["allowed"]:
            j.refuse = ("gate_denied", "builtin:url_scheme",
                        f"{scheme or 'that'}: links aren't opened by agents (only https, http, tel, geo, mailto, "
                        "sms and market).")
            return j
        targets = [handler] if handler else candidates
        for pkg in targets:
            if pkg in r.own or r.blocked(pkg):
                j.refuse = ("gate_blocked_app", "builtin:sensitive_apps",
                            f"Blocked on the phone: that link opens {pkg}, a sensitive app. Call done.")
                return j
            if pkg in r.installers:
                j.refuse = ("gate_denied_page", "builtin:installer", "Installing apps is off-limits to agents.")
                return j
        if scheme == "tel" and handler and not r.is_cat("dialer", handler):
            j.refuse = ("gate_denied", "builtin:tel_handler", "tel: links only open the dialer.")
        elif scheme == "tel" and self.mmi(url.split(":", 1)[-1]):
            # A code with * or # (call forwarding…): some dialers run codes they're handed, so it asks first (H2).
            j.risk = self._call_code_risk(url.split(":", 1)[-1], self.label(screen, handler or "the dialer"))
        return j


# --- running the shared fixtures ------------------------------------------------------------------

FIXTURES = ROOT / "tests/fixtures/gatekeeper"


def load_fixtures(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


def load_screens() -> dict:
    return json.loads((FIXTURES / "screens.json").read_text())


def check(expect: dict, adm: Admission) -> list[str]:
    problems = []
    for key in ("decision", "code", "kind", "money", "level"):
        if key in expect and getattr(adm, key) != expect[key]:
            problems.append(f"{key}: expected {expect[key]!r}, got {getattr(adm, key)!r} ({adm.rule})")
    if "rule" in expect and not adm.rule.startswith(expect["rule"]):
        problems.append(f"rule: expected {expect['rule']!r}, got {adm.rule!r}")
    return problems


def run_step(gk: Gatekeeper, s: dict, screens: dict, state: dict) -> list[str]:
    """One fixture step. `state` carries the last approval id and the saved remaining times."""
    if "call" in s:
        gk.clock.elapsed += state.get("call_ms", 1000)  # calls take time (an agent step is never instant)
        screen = Screen.of(screens[s["screen"]])
        adm = gk.admit(s["call"]["method"], s["call"].get("params") or {}, screen)
        if adm.decision == "ask":
            state["last"] = adm.approval_id
        if adm.decision == "allow":
            again = Screen.of(screens[s.get("reverify_screen", s["screen"])])
            adm = gk.reverify(adm, again)
            if adm.decision == "allow":
                gk.completed(adm)  # the gesture ran
        state["persist"] = gk.persist()
        return check(s["expect"], adm)
    if "approve" in s:
        aid = state.get("last", "")
        accepted = False
        if aid in gk.pending:
            message = gk.approval_message(aid, grant=bool(s.get("grant")))
            if s.get("binding") == "other":
                message = message.replace(gk.pending[aid].binding, sha256("another action"))
            own, other = (TapVerifier, TestVerifier) if gk.pending[aid].level == "tap" else (TestVerifier, TapVerifier)
            # valid: the level's own key; other_key: the other level's key (a tap can't approve a fingerprint prompt)
            sig = {"valid": own.sign(message), "other_key": other.sign(message)}.get(s.get("sig"), "00")
            accepted = gk.approve(aid, message, sig)
        if accepted != s.get("expect_accepted", True):
            return [f"approve: expected accepted={s.get('expect_accepted', True)}, got {accepted}"]
        return []
    if "deny" in s:
        gk.deny(state.get("last", ""))
        return []
    if "cancel" in s:
        gk.cancel(state.get("last", ""))
        return []
    if "shown" in s:
        gk.mark_shown(state.get("last", ""))
        return []
    if "advance_ms" in s:
        gk.clock.elapsed += int(s["advance_ms"])
        gk.clock.wall += int(s["advance_ms"])
        state["persist"] = gk.persist()  # the phone saves remaining times every minute
        return []
    if "set_wall_ms" in s:
        gk.clock.wall = int(s["set_wall_ms"])
        return []
    if "reboot" in s:
        r = s["reboot"]
        gk.reboot(state.get("persist") if r.get("saved") else None, int(r["elapsed_after"]))
        return []
    if "policy" in s:
        record = s["policy"]
        message = f"gk1|policy|{policy_canon(record)}"
        sig = {"valid": TestVerifier.sign(message), "bad": TestVerifier.sign(message + "x")}.get(s.get("sig"), "")
        accepted = gk.add_policy(record, message, sig)
        if accepted != s.get("expect_accepted", True):
            return [f"policy: expected accepted={s.get('expect_accepted', True)}, got {accepted}"]
        return []
    if "instruction" in s:
        gk.note_instruction(s["instruction"])
        return []
    if "stop" in s:
        gk.revoke_all()
        return []
    if "resume" in s:
        gk.resume()
        return []
    return [f"unknown step {s}"]


def run_scenario(scenario: dict, screens: dict, gk: Gatekeeper | None = None) -> list[str]:
    gk = gk or Gatekeeper()
    state: dict = {"call_ms": scenario.get("call_ms", 1000)}
    out = []
    for i, s in enumerate(scenario["steps"]):
        out += [f"step {i}: {p}" for p in run_step(gk, s, screens, state)]
    return out
