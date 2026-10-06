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
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "android/app/src/main/resources/gatekeeper/rules.json"
MODES = {"ask": 0, "plan": 1, "auto": 1, "trusted": 2, "never": 3}
ACTION_PARAMS = ("x", "y", "x1", "y1", "x2", "y2", "duration_ms", "text", "name", "package", "url")


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
        parts = [self.id, self.text, self.desc, self.cls]
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

    @classmethod
    def of(cls, d: dict) -> "Window":
        return cls(id=int(d.get("id", 0)), type=d.get("type", "application"), layer=int(d.get("layer", 0)),
                   package=d.get("package", ""), title=d.get("title", ""),
                   bounds=tuple(d.get("bounds", (0, 0, 1080, 2400))),
                   root=Node.of(d["root"]) if d.get("root") else None)


@dataclass
class Screen:
    windows: list
    keyguard_locked: bool = False
    keyguard_secure: bool = True
    quiet_ms: int = 1000
    activity: str = ""
    autofill_service: str = ""
    resolve: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)

    @classmethod
    def of(cls, d: dict) -> "Screen":
        kg = d.get("keyguard") or {}
        return cls(windows=[Window.of(w) for w in d.get("windows", [])], keyguard_locked=bool(kg.get("locked")),
                   keyguard_secure=bool(kg.get("secure", True)), quiet_ms=int(d.get("quiet_ms", 1000)),
                   activity=d.get("activity", ""), autofill_service=d.get("autofill_service", ""),
                   resolve=d.get("resolve", {}), labels=d.get("labels", {}))

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
    return None


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
    words = [t for t in screen_tokens(screen) if len(t) >= 2 and not t.isdigit()]
    if len(words) < rules.d["language"]["min_tokens"]:
        return True
    known = sum(1 for t in words if t in rules.lexicon)
    return known / len(words) >= rules.d["language"]["threshold"]


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


class TestVerifier:
    """Fixtures sign with sha256("test-key:" + message); the phone uses its Keystore key."""

    @staticmethod
    def sign(message: str) -> str:
        return sha256("test-key:" + message)

    def verify(self, message: str, signature: str) -> bool:
        return bool(signature) and signature == self.sign(message)


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
    def __init__(self, rules: Rules | None = None, clock: Clock | None = None, verifier=None, device: str = "dev"):
        self.r = rules or Rules.load()
        self.clock = clock or Clock()
        self.verifier = verifier or TestVerifier()
        self.device = device
        self.permits: dict[str, int] = {}  # binding -> expiry (monotonic ms)
        self.pending: dict[str, Pending] = {}
        self.followup: dict | None = None  # {package, task, expires, acts}
        self.start_permits: dict[str, dict] = {}  # binding -> {package, task}: a flow-starting money approval
        self.credits: list[dict] = []  # {kind, expires}
        self.grants: list[Grant] = []
        self.ceiling = "auto"
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
        self.tokens, self.last_refill = float(self.r.limits["act_burst"]), now
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
        if kind == "grant":
            g_kind = record.get("kind")
            if g_kind not in ("send", "post", "delete", "pay", "share", "settings", "fill") or not record.get("binding"):
                return False
            cap = lim["pay_grant_max_ms"] if g_kind == "pay" else lim["grant_max_ms"]
            duration = min(int(record.get("duration_ms", 0)), cap)
            if duration <= 0:
                return False
            self.grants.append(Grant(g_kind, str(record["binding"]), duration, now, canon))
            return True
        return False

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
        self.never = None
        self.suspended = False

    # --- admit ------------------------------------------------------------------------

    def admit(self, method: str, params: dict, screen: Screen) -> Admission:
        now = self.now()
        self._expire(now)
        params = params or {}
        if method in self.r.meta:
            return Admission("allow", method=method, params=params)
        if method in self.r.read or method.startswith("stream_"):
            hidden = self._hidden(screen)
            if method in ("screenshot", "ui_elements") and hidden:
                return Admission("redact", rule=hidden, method=method, params=params)
            if method == "stream_start" and hidden:
                return self._refuse("gate_blocked_app", hidden, "A protected app is on screen, so the live view "
                                    "can't start now.", method, params)
            return Admission("allow", method=method, params=params)
        if method not in self.r.act:
            return self._refuse("gate_unknown_method", "builtin:methods", f"Unknown method {method}.", method, params)
        if self.pending:
            return self._refuse("gate_prompt_open", "builtin:prompt_open",
                                "Waiting for the owner to answer a prompt on the phone.", method, params)
        if method != "wake_unlock" and screen.keyguard_locked and screen.keyguard_secure:
            return self._refuse("phone_locked", "builtin:keyguard", "The phone is locked. Unlock it to continue.",
                                method, params)
        j = self.judge(method, params, screen)
        if j.refuse:
            return self._refuse(*j.refuse, method, params, package=j.package)
        if self.suspended:
            return self._refuse("gate_rate_limited", "builtin:suspended",
                                "Paused on the phone after repeated denials. The owner can resume.", method, params)
        if not self._take_token(now):
            return self._refuse("gate_rate_limited", "builtin:act_rate", "Too many actions. Slow down.", method, params)
        bind = binding(method, params, j.strict_fp)
        if method != "wake_unlock" and bind not in self.permits:
            # Any other action voids every outstanding approval: what the owner saw may no longer be true (a
            # quantity "+", another item scrolled into place). The honest server re-sends at once.
            self.permits.clear()
            self.start_permits.clear()
        self._track_followup(method, params, j)
        if j.app and j.package:
            self.last_app = j.package
        mode = self.mode(params.get("mode"))
        risk = j.risk
        hint = str(params.get("hint") or "").strip()
        if risk is None and hint and mode != "never":
            risk = Risk("other", "server_hint", wording=self._wording(method, params, j, screen))
        if mode == "ask" and j.changing and risk is None:
            risk = Risk("step", "mode:ask", wording=self._wording(method, params, j, screen))
        base = dict(method=method, params=params, package=j.package)
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
            return Admission("allow", strict=True, binding=bind, kind=risk.kind, **base)
        if bind in self.permits:
            return Admission("allow", strict=True, binding=bind, permit=bind, kind=risk.kind, money=risk.money, **base)
        if mode != "ask" and (why := self._covered(risk, j.package, params)):
            return Admission("allow", strict=True, binding=bind, kind=risk.kind, rule=why, money=risk.money, **base)
        window = self.r.limits["ask_window_ms"]
        self.asks = [t for t in self.asks if now - t < window]
        if len(self.asks) >= self.r.limits["asks_per_window"]:
            return self._refuse("gate_rate_limited", "builtin:prompt_rate", "Too many approval prompts. Try later.",
                                method, params, j.package)
        self.asks.append(now)
        aid = "gk" + uuid.uuid4().hex  # gate prompts are "gk…"; the legacy approval path refuses such ids
        offer = self._offer(risk, mode)
        self.pending[aid] = Pending(aid, bind, now, risk, j.package, str(params.get("task") or ""), offer)
        self.events.append({"state": "pending", "approval_id": aid})
        return Admission("ask", code="gate_needs_approval", rule=f"risk:{risk.reason}", kind=risk.kind,
                         approval_id=aid, wording=risk.wording, grant_offer=offer, binding=bind, money=risk.money,
                         message="Waiting for the owner's approval on the phone.", **base)

    def _hidden(self, screen: Screen) -> str:
        """Why reads must not see this screen ("" if they may): a blocked app in any window, or an app window the
        phone can't identify yet (it may be a blocked app still loading)."""
        if any(self.r.blocked(w.package) for w in screen.windows):
            return "builtin:sensitive_apps"
        if any(not w.package and w.type == "application" for w in screen.windows):
            return "builtin:unknown_window"
        return ""

    def _refuse(self, code, rule, message, method, params, package="") -> Admission:
        return Admission("refuse", code=code, rule=rule, message=message, method=method, params=params, package=package)

    def _track_followup(self, method: str, params: dict, j: Judgement) -> None:
        """A purchase flow's one terminal follow-up ends at an app switch, at BACK/HOME/recents, a launch or a link,
        a tap on a cart or checkout word, and after 3 other actions."""
        f = self.followup
        if f is None:
            return
        name = str(params.get("name", "")).upper()
        if (method in ("launch_app", "open_url") or (method == "key" and name in ("BACK", "HOME", "APP_SWITCH"))
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
            for g in self.grants:
                if g.kind == risk.kind and g.binding == risk.grant and g.issued + g.duration_ms > now:
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
        msg = f"gk1|{self.device}|{approval_id}|{p.binding}"
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
        plain = f"gk1|{self.device}|{approval_id}|{p.binding}"
        grant = None
        if message != plain:
            if not (p.grant_offer and message == self.approval_message(approval_id, grant=True)):
                return False
            grant = p.grant_offer
        if not self.verifier.verify(message, signature):
            return False
        del self.pending[approval_id]
        self.permits[p.binding] = now + self.r.limits["permit_ms"]
        if p.risk.money == "start":  # the follow-up starts only when this very tap runs (reverify)
            self.start_permits[p.binding] = {"package": p.package, "task": p.task}
        if grant:
            record = {"type": "grant", **{k: grant[k] for k in ("kind", "binding", "duration_ms")}}
            self._apply_policy(record, policy_canon(record))
        self.events.append({"state": "approved", "approval_id": approval_id})
        return True

    def cancel(self, approval_id: str) -> bool:
        """The prompt ended unanswered (dismissed, or the connection closed): no permit, and not a denial."""
        if self.pending.pop(approval_id, None) is None:
            return False
        self.events.append({"state": "expired", "approval_id": approval_id})
        return True

    def deny(self, approval_id: str) -> bool:
        now = self.now()
        if self.pending.pop(approval_id, None) is None:
            return False
        self.denials = [t for t in self.denials if now - t < self.r.limits["denial_window_ms"]] + [now]
        if len(self.denials) >= self.r.limits["denials_to_suspend"]:
            self.suspended = True
        self.events.append({"state": "denied", "approval_id": approval_id})
        return True

    # --- reverify (right before the side effect, under the gesture lock) ------------------

    def reverify(self, adm: Admission, screen: Screen) -> Admission:
        if adm.decision != "allow" or adm.method not in self.r.act:
            return adm
        j = self.judge(adm.method, adm.params, screen)
        if j.refuse:
            self.permits.pop(adm.permit or "", None)
            return self._refuse(*j.refuse, adm.method, adm.params, j.package)
        if adm.strict:
            if binding(adm.method, adm.params, j.strict_fp) != adm.binding:
                self.permits.pop(adm.permit or "", None)
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

    # --- the judge: what the action touches and how risky it is ---------------------------

    def label(self, screen: Screen, package: str) -> str:
        return screen.labels.get(package) or package

    def _wording(self, method, params, j: Judgement, screen: Screen) -> str:
        app = self.label(screen, j.package) if j.package else "this phone"
        return {"type_text": f"Allow typing into a field in {app}?", "key": f"Allow pressing Enter in {app}?",
                "launch_app": f"Allow opening {app}?",
                "open_url": f"Allow opening {params.get('url', '')!s:.80} in {app}?"}.get(
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
        return Judgement()  # wake_unlock: wakes the screen, never touches a secure lock

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
        if w.package in r.installers:
            return ("gate_denied_page", "builtin:installer", "Installing apps is off-limits to agents.")
        if r.blocked(w.package):
            return ("gate_blocked_app", "builtin:sensitive_apps",
                    f"Blocked on the phone: {w.package} is a sensitive app. Don't open it another way; call done.")
        return None

    def _app_window(self, w: Window) -> bool:
        """An app's own window (what "the task's app" and a purchase flow's app are), not a system surface."""
        r = self.r
        return (w.type == "application" and w.package != r.surf["systemui"] and not self._is_settings(w)
                and w.package not in r.surf["share_packages"] and w.package not in r.surf["credential_packages"])

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
            m = re.match(r"(?i)^allow\s+(.+?)\s+to\s", message)
            return bool(m) and tokens(m.group(1)) == session
        if re.compile(self.r.s["permission_packages"]).search(w.package or ""):
            below = [x for x in screen.ordered() if x.layer < w.layer and x.type == "application"]
            return bool(below) and below[0].package == self.last_app
        return has_phrase(title, " ".join(session))

    def _changing_tap(self, t: Target | None, label: list[str]) -> bool:
        if t is not None and any(n.checked is not None for n in (t.pressed, t.hit) if n is not None):
            return True
        return any_phrase(label, self.r.s["change_words"]) is not None

    def _word_risk(self, label: list[str], ids: list[str], package: str, payment: bool) -> Risk | None:
        r = self.r
        words = strip_phrases(label, r.w["pre_commit"])
        id_words = []
        for rid in ids:
            toks = id_tokens(rid)
            for w in r.d["resource_id_risky"]:
                if w not in r.d["resource_id_pre_commit"] and (has_phrase(toks, w) or (" " not in w and w in toks)):
                    id_words.append(w)
        money_ctx = payment or bool(r.money_app.search(package or ""))
        flow = any_phrase(words, r.w["flow_start"]) or next((w for w in id_words if w in ("buy", "purchase",
                                                                                          "transfer")), None)
        if flow:
            return Risk("pay", f"words:{flow}", money="start", grant=package)
        terminal = any_phrase(words, r.w["terminal"]) or next(
            (w for w in id_words if w in ("pay", "place order", "placeorder", "confirm")), None)
        if terminal:
            if terminal == "confirm" and not money_ctx:
                return Risk("other", "words:confirm")
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

    def _judge_point(self, method: str, x: float, y: float, screen: Screen) -> Judgement:
        r = self.r
        t = hit_test(r, screen, x, y)
        if t is None:
            return Judgement(changing=True)
        w = t.window
        j = Judgement(package=w.package, fp=t.fp(False), strict_fp=t.fp(True), changing=True,
                      app=self._app_window(w))
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
        # Share sheets
        if w.package in r.surf["share_packages"] and (re.compile(r.surf["share_titles"]).search(w.title or "")
                                                       or w.package != "android"):
            j.risk = Risk("share", "share_sheet", grant=f"{source}>{' '.join(label)}",
                          wording=f"Share to “{shown or 'this target'}”?")
            return j
        # Notification action buttons
        if w.package == r.surf["systemui"] and pressed is not None and pressed is not w.root and (
                re.compile(r.surf["notification_action_ids"]).search(pressed.id or "") or "Button" in pressed.cls):
            j.risk = Risk("other", "notification_action", wording=wording())
            return j
        risk = self._word_risk(label, ids, w.package, j.payment)
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

    def _judge_typing(self, text: str, screen: Screen) -> Judgement:
        r = self.r
        scheme = url_scheme(text)
        if scheme in r.d["schemes"]["typed_refused"]:
            return Judgement(changing=True, refuse=("gate_denied", "builtin:typed_scheme",
                                                    f"{scheme}: text is never typed by agents."))
        found = focused_field(screen)
        if found is None:
            return Judgement(changing=True)
        w, node, path = found
        fp = f"{w.id}|{w.package}|{node.id}/{node.hint}/{node.cls}/{int(node.password)}"
        j = Judgement(package=w.package, fp=fp, strict_fp=fp + "/" + ",".join(map(str, node.bounds)), changing=True,
                      app=self._app_window(w))
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
        if re.compile(r.d["payment_field"]).search(about):
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
            return Judgement(changing=name == "ENTER")
        w, node, path = found
        fp = f"{w.id}|{w.package}|{node.id}/{node.hint}/{node.cls}"
        j = Judgement(package=w.package, fp=fp, strict_fp=fp + "/" + ",".join(map(str, node.bounds)),
                      changing=name == "ENTER", app=self._app_window(w))
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
            return uj
        j.payment = payment_context(r, screen)
        app = self.label(screen, w.package)
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
        return j


# --- running the shared fixtures ------------------------------------------------------------------

FIXTURES = ROOT / "tests/fixtures/gatekeeper"


def load_fixtures(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


def load_screens() -> dict:
    return json.loads((FIXTURES / "screens.json").read_text())


def check(expect: dict, adm: Admission) -> list[str]:
    problems = []
    for key in ("decision", "code", "kind", "money"):
        if key in expect and getattr(adm, key) != expect[key]:
            problems.append(f"{key}: expected {expect[key]!r}, got {getattr(adm, key)!r} ({adm.rule})")
    if "rule" in expect and not adm.rule.startswith(expect["rule"]):
        problems.append(f"rule: expected {expect['rule']!r}, got {adm.rule!r}")
    return problems


def run_step(gk: Gatekeeper, s: dict, screens: dict, state: dict) -> list[str]:
    """One fixture step. `state` carries the last approval id and the saved remaining times."""
    if "call" in s:
        gk.clock.elapsed += state.get("call_ms", 200)  # calls take time (an agent step is never instant)
        screen = Screen.of(screens[s["screen"]])
        adm = gk.admit(s["call"]["method"], s["call"].get("params") or {}, screen)
        if adm.decision == "ask":
            state["last"] = adm.approval_id
        if adm.decision == "allow":
            again = Screen.of(screens[s.get("reverify_screen", s["screen"])])
            adm = gk.reverify(adm, again)
        state["persist"] = gk.persist()
        return check(s["expect"], adm)
    if "approve" in s:
        aid = state.get("last", "")
        accepted = False
        if aid in gk.pending:
            message = gk.approval_message(aid, grant=bool(s.get("grant")))
            if s.get("binding") == "other":
                message = message.replace(gk.pending[aid].binding, sha256("another action"))
            sig = TestVerifier.sign(message) if s.get("sig") == "valid" else "00"
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
    state: dict = {"call_ms": scenario.get("call_ms", 200)}
    out = []
    for i, s in enumerate(scenario["steps"]):
        out += [f"step {i}: {p}" for p in run_step(gk, s, screens, state)]
    return out
