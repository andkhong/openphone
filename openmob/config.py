"""Configuration loaded from config.yaml plus environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from .permissions import (DEFAULT_APP_CATEGORIES, DEFAULT_CREDENTIAL_TERMS, DEFAULT_MONEY_KEYWORDS,
                          DEFAULT_SETTINGS_ASK, DEFAULT_SETTINGS_EVERYDAY, DEFAULT_SETTINGS_REFUSE, DEFAULT_SKIP_PHRASES,
                          DEFAULT_START_FLOW_WORDS, DEFAULT_TERMINAL_WORDS, MODES, PERMISSION_DEFAULTS,
                          permission_values)

DEFAULT_CONFIRM_KEYWORDS = [
    "buy", "purchase", "pay", "payment", "checkout", "top up", "recharge",
    "subscribe", "send", "post", "publish", "delete", "remove", "uninstall",
    "transfer", "place order", "confirm order", "share to", "forward to",
]

DEFAULT_MODELS = [
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5 (balanced, recommended)", "provider": "anthropic"},
    {"id": "claude-opus-5-5", "label": "Opus 5.5 (smartest, slower, pricier)", "provider": "anthropic"},
    {"id": "claude-fable-5-1", "label": "Fable 5.1", "provider": "anthropic"},
    # Low effort on labeled screens, canvas_effort on canvas ones (owner's choice, 2026-10-08: measured,
    # low was 15-35% faster per decision on Settings-style steps). Thinking counts toward max_tokens, so
    # the vision step gets the same room instead of the 512-token cap, which would cut it off.
    {"id": "claude-haiku-5-5", "label": "Haiku 5.5 (fastest, cheapest)", "provider": "anthropic",
     "effort": "low", "max_tokens": 8192, "vision_max_tokens": 8192},
    {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "provider": "openai", "context": 1_050_000,
     "reasoning_effort": "low", "max_tokens": 8192, "vision_max_tokens": 8192},
    {"id": "gpt-6-astra", "label": "GPT-6 Astra", "provider": "openai", "context": 1_050_000,
     "reasoning_effort": "low", "max_tokens": 8192, "vision_max_tokens": 8192},
    {"id": "gpt-6-luna", "label": "GPT-6 Luna", "provider": "openai", "context": 1_050_000,
     "reasoning_effort": "low", "max_tokens": 8192, "vision_max_tokens": 8192},
    {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "provider": "google", "context": 1_048_576,
     "thinking_level": "low", "max_tokens": 8192, "vision_max_tokens": 8192},
    {"id": "qwen3.8-flash", "label": "Qwen 3.8 Flash", "provider": "qwen", "context": 1_000_000,
     "max_tokens": 8192, "vision_max_tokens": 8192},
    {"id": "grok-4.7", "label": "Grok 4.7", "provider": "xai", "context": 500_000,
     "reasoning_effort": "low", "max_tokens": 8192, "vision_max_tokens": 8192},
]

# Who serves a model. Keys come from the environment only; `openai_compatible` is any endpoint that
# speaks OpenAI Chat Completions (vLLM, Ollama, LM Studio, OpenRouter…) at a `base_url`.
PROVIDERS = ("anthropic", "openai", "google", "qwen", "xai", "openai_compatible")
# Optional per-model settings in `models:` besides id, label and provider.
MODEL_OPTIONS = ("base_url", "api_key_env", "max_tokens", "vision", "tools", "context",
                 "reasoning_effort", "thinking_level", "vision_max_tokens", "effort", "thinking")
# Claude models: `effort` is output_config.effort; `thinking` is the thinking type. Which pairs a model
# accepts is the API's rule (Haiku 5.5: disabled at effort high or below; Sonnet 5.5: between_tools).
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_THINKING = ("adaptive", "disabled", "between_tools")


def infer_provider(model_id: str) -> str | None:
    """The provider a model id belongs to, from its name (None: say `provider:` in the config)."""
    name = model_id.lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith(("gpt-", "chatgpt", "o1", "o3", "o4", "o5")):
        return "openai"
    if name.startswith(("gemini", "gemma")):
        return "google"
    if name.startswith("qwen"):
        return "qwen"
    if name.startswith("grok"):
        return "xai"
    return None


def normalize_model(entry) -> dict:
    """One `models:` entry as {id, label, provider, ...options}."""
    if not isinstance(entry, dict):
        entry = {"id": entry}
    if "api_key" in entry:
        raise ValueError(f"models: {entry['id']}: keys stay out of config.yaml; name an environment variable with api_key_env")
    model = {"id": str(entry["id"]), "label": str(entry.get("label") or entry["id"])}
    provider = entry.get("provider") or infer_provider(model["id"])
    if provider not in PROVIDERS:
        raise ValueError(f"models: {model['id']} needs a provider: one of {', '.join(PROVIDERS)}")
    model["provider"] = provider
    model.update({k: entry[k] for k in MODEL_OPTIONS if entry.get(k) is not None})
    if model.get("effort") is not None and model["effort"] not in CLAUDE_EFFORTS:
        raise ValueError(f"models: {model['id']}: effort is one of {', '.join(CLAUDE_EFFORTS)}")
    if model.get("thinking") is not None and model["thinking"] not in CLAUDE_THINKING:
        raise ValueError(f"models: {model['id']}: thinking is one of {', '.join(CLAUDE_THINKING)}")
    return model

# Developer API tiers: the public name -> the model that runs it, and what a step costs in credits.
# The mapping is internal; API responses only ever show the tier name.
DEFAULT_API_TIERS = {
    "fast": {"model": "claude-haiku-5-5", "credits_per_step": 1},
    "balanced": {"model": "claude-sonnet-5-5", "credits_per_step": 3},
    "best": {"model": "claude-opus-5-5", "credits_per_step": 10},
}

# Apps that API tasks may not open or operate unless the phone's owner allows it
# (matched against the package name, case-insensitively).
DEFAULT_SENSITIVE_APPS = [
    r"bank", r"banking", r"\bpay\b", r"\.pay", r"wallet", r"paypal", r"venmo", r"squareup\.cash", r"zelle",
    r"chase", r"wellsfargo", r"bofa", r"\\.citi", r"revolut", r"monzo", r"transferwise", r"coinbase", r"binance",
    r"crypto", r"spay", r"walletnfcrel", r"1password", r"bitwarden", r"lastpass", r"dashlane", r"keepass",
    r"\\.keeper", r"authenticator", r"authy",
]

FAST_PATH_MODES = ("off", "fast_tier", "on")

REMOVED_KEYS = {"hotkey", "whisper_model"}
SECRET_FIELDS = {"api_key", "openai_api_key", "google_api_key", "qwen_api_key", "xai_api_key", "compatible_api_key", "workspace_id", "jev_api_key", "source"}  # Mac push-to-talk was removed

CONFIG_LOCATIONS = [
    Path("config.yaml"),
    Path.home() / ".config" / "openmob" / "config.yaml",
]


@dataclass
class Config:
    # Agent
    model: str = "claude-sonnet-5-5"
    resolver_model: str = "claude-haiku-5-5"
    # Models the dashboard may switch a phone to (an allowlist; anything else is refused):
    # {id, label, provider} plus optional base_url, api_key_env, max_tokens, vision, tools, context.
    models: list[dict] = field(default_factory=lambda: [dict(m) for m in DEFAULT_MODELS])
    # Default endpoint for `openai_compatible` models without their own base_url (not a secret).
    openai_compatible_base_url: str | None = None
    qwen_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    xai_base_url: str = "https://api.x.ai/v1"
    max_steps: int = 40
    keep_screenshots: int = 2
    # Vision-step speedups (docs/benchmarks.md, "Vision steps"). Each can be switched off to compare.
    # History trimming: "batch" lets screenshots pile up to history_max_screenshots, then drops the
    # oldest down to keep_screenshots at once, so earlier messages stay byte-identical between steps
    # and the provider's prompt cache covers them. "rolling" (before): keep_screenshots every step.
    history_trim: str = "batch"  # batch | rolling
    history_max_screenshots: int = 4
    # Anthropic: mark the end of the conversation for prompt caching (other providers cache on their own).
    history_cache: bool = True
    # A smaller screenshot for vision steps on screens with a rich labeled element list (not OCR, not a
    # game): its long side in px (0: always the full screenshot_max_side).
    vision_max_side_labeled: int = 768
    vision_labeled_min_elements: int = 8  # labeled accessibility elements that make a screen "rich"
    # Ask for the tool call with at most one short sentence of thought.
    vision_brief: bool = True
    # max_tokens for vision steps (0: the model's own); a reply cut off by it is asked again in full.
    vision_max_tokens: int = 512
    # Anthropic: stream the reply and run each tool call as soon as its block is complete. Off: the
    # gain is only the tail of the reply (tens of ms), and it holds the stream open during approvals.
    vision_stream: bool = False
    # Claude models: the effort for a decision on a canvas screen (no readable labels: games, chess, a
    # drawing app), where reading the picture is the hard part; labeled screens keep the model's own effort.
    # Empty: the model's effort everywhere. OPENMOB_CANVAS_EFFORT overrides.
    canvas_effort: str = "high"
    # Settings deep links (open_settings_page) on phones that offer them. Off only for comparisons:
    # OPENMOB_DEEP_LINKS=off.
    deep_links: bool = True
    # The direct Settings step: a request that plainly names one Settings page ("Open Settings and go to Display"),
    # or an app's own page ("open Chrome's notification settings and tell me whether they're allowed"; the app by
    # its exact installed name), opens it as the task's first action, with no classifier or main-model decision,
    # through the tool's every check. Off: the page is opened only when the classifier chooses to launch Settings,
    # or the main model calls the tool. OPENMOB_DEEP_LINKS_FAST=off to compare.
    deep_links_fast: bool = True
    # In-app links (docs/design/app-links.md, agent/app_links.py): the open_link catalog tool (a YouTube search, Maps
    # directions, a Play Store page… from one link), and open_url reporting which app took the link and when the
    # app it belongs to didn't. Off only for comparisons: OPENMOB_APP_LINKS=off.
    app_links: bool = True
    # The fast path for links: a request that plainly names one `view` catalog entry ("search YouTube for lofi")
    # opens it as the task's first action, with no main-model decision; a value the request doesn't give plainly
    # comes from the small text model. OPENMOB_APP_LINKS_FAST=off to compare.
    app_links_fast: bool = True
    # Actions the main model may take from one reply (1: one per turn). Above 1 it may chain a few whose
    # next screen it can predict (a chess piece then its square, type then Enter): each still passes
    # every check, and the rest of the turn is skipped at any error, owner prompt or unexpected screen.
    # Streaming actions (vision_stream) is off while this is above 1. OPENMOB_MAX_ACTIONS overrides.
    max_actions_per_turn: int = 1
    # Take the screenshot while the app and element reads run, instead of after them (phones answer
    # calls concurrently). Off until measured on real phones. OPENMOB_PARALLEL_OBSERVE=on overrides.
    parallel_observe: bool = False
    # Replay UI paths that worked before (agent/replay.py, docs/design/fewer-model-calls.md): on a request seen
    # before, the stored path's taps, scrolls, Backs and app launches run without the main model while each
    # screen matches, through every check an action gets. Only successful navigation-only tasks that typed
    # nothing, asked nothing and tapped nothing risky are stored. Off until measured. OPENMOB_REPLAY=on overrides.
    replay_paths: bool = False
    replay_min_overlap: float = 0.7  # the share of a stored screen's labels that must be on screen to replay its step
    replay_max_goals: int = 100  # requests whose paths are kept (the oldest go first)
    # Also store paths from isolated callers (API keys). Off: the owner's call (2026-10-08); on only to
    # warm the cache in API-driven benchmarks. OPENMOB_REPLAY_STORE_API=on overrides.
    replay_store_api: bool = False
    # Plan with the main model, execute with the classifier (agent/plan.py, docs/design/fewer-model-calls.md): on a
    # cold run one dedicated model call before the step loop (its only tool: plan_steps) lays the request out as
    # sub-goals with a screen cue each; a reply without a plan changes nothing. Then each step asks the classifier
    # about the current sub-goal and runs its tap, scroll, Back or app launch through every check. The model takes over, told the plan and progress, when the classifier isn't sure, a step changes
    # nothing, a cue doesn't appear within plan_max_steps_per_goal steps, a popup / login / loading screen comes up,
    # the next step would type, or the user corrects. Needs Jev. Off until measured. OPENMOB_PLAN_EXECUTE=on overrides.
    plan_execute: bool = False
    plan_max_steps_per_goal: int = 4
    settle_delay: float = 0.8
    # The most to wait after a scroll: a fling keeps the list moving past settle_delay (measured on a
    # Samsung: 0.8 s never sufficed), so the next look read moving positions and its tap was refused.
    # Adaptive settle still returns as soon as the list stops. 1.2 s (measured 2026-10-08, 3 runs per
    # value): no refused taps and 1-2 fewer model calls at the same task time as 0.8 s; 1.6 s was no
    # better and slower on long lists. OPENMOB_SCROLL_SETTLE_S overrides.
    scroll_settle_delay: float = 1.2
    launch_settle_delay: float = 3.0
    # Adaptive settle: after an action, wait only until the screen stops changing (no window or content
    # change for settle_quiet_ms), capped at the delays above. Needs a phone whose app offers wait_idle;
    # others keep the fixed delays (adb: launches end once the app is in front). OPENMOB_ADAPTIVE_SETTLE=off.
    adaptive_settle: bool = True
    settle_quiet_ms: int = 300  # OPENMOB_SETTLE_QUIET_MS sets it too (to compare e.g. 200)
    # "Wait until" instead of blind waits (agent/waiting.py), on phones whose element list is quick to read
    # (the OpenMob app). After launch_app / open_url: while a spinner or "Loading" is on screen, keep
    # waiting, up to this long in all (0: only the settle above).
    launch_wait_seconds: float = 10.0
    # After tapping Install in the Play Store: wait until its button reads Open/Play again (0: don't).
    install_wait_seconds: float = 90.0
    # Re-read the screen's elements just before tap_element, and don't tap one that moved or vanished
    # (only on phones whose element list is quick to read: the OpenMob app).
    verify_taps: bool = True
    ui_tree: str = "auto"  # auto | always | never
    confirm_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_CONFIRM_KEYWORDS))
    # Ask before risky taps (buy/send/delete…): unrequested = only if the request didn't ask for it.
    confirm_actions: str = "unrequested"  # unrequested | always | never
    # Permission modes (docs/safety.md#permission-modes): a phone's mode when it has no setting yet.
    default_permission_mode: str = "auto"  # ask | plan | auto (looser modes need the phone's own approval)
    # Money actions ask even when the request asked for them (once per task and app).
    money_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_MONEY_KEYWORDS))
    # Steps on the way to a commit point ("Add to cart", "Proceed to checkout"): never asked about.
    confirm_skip_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_SKIP_PHRASES))
    # Categories a trusted entry may name: category -> package-name parts.
    app_categories: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_APP_CATEGORIES.items()})
    # Settings tiers: security-critical pages are refused, everyday ones never ask, the rest ask.
    settings_refuse_terms: list[str] = field(default_factory=lambda: list(DEFAULT_SETTINGS_REFUSE))
    settings_everyday_terms: list[str] = field(default_factory=lambda: list(DEFAULT_SETTINGS_EVERYDAY))
    settings_ask_terms: list[str] = field(default_factory=lambda: list(DEFAULT_SETTINGS_ASK))
    # Autofill / saved-credential suggestions: asked about, refused under `never`.
    credential_terms: list[str] = field(default_factory=lambda: list(DEFAULT_CREDENTIAL_TERMS))
    # After an approved payment: the one follow-up commit tap these name is covered (FOLLOWUP_SECONDS).
    money_terminal_words: list[str] = field(default_factory=lambda: list(DEFAULT_TERMINAL_WORDS))
    # Money words that start a new flow: they always ask again.
    money_start_words: list[str] = field(default_factory=lambda: list(DEFAULT_START_FLOW_WORDS))
    ocr: bool = True  # Apple Vision OCR when the app has no accessibility tree (games)
    # Android's runtime permission dialogs are answered by the loop with no model call (agent/permission_dialogs.py),
    # by these owner defaults: notifications allow | deny; location_accuracy approximate | precise; the other types
    # (location, camera, microphone, photos, nearby_devices…) deny | ask | once | while_using. Never "Allow all the
    # time". A request that talks about permissions ("don't allow notifications") is left to the model.
    auto_permission_dialogs: bool = True
    permission_prompts: dict[str, str] = field(default_factory=lambda: dict(PERMISSION_DEFAULTS))

    # Developer API (/v1): tiers, per-key limits, and apps API tasks can't touch by default.
    api_tiers: dict[str, dict] = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_API_TIERS.items()})
    api_default_tier: str = "balanced"
    api_rate_per_minute: int = 60  # requests per key per minute
    api_max_concurrent_tasks: int = 2  # unfinished tasks per key
    api_daily_tasks: int = 200  # tasks per key per UTC day
    api_daily_credits: int = 0  # credits per key per UTC day, counting running tasks (0: unlimited)
    api_max_running_orchestrations: int = 3  # multi-phone runs in progress per key
    # Account-wide daily caps (UTC), across all of an account's keys, so more keys don't mean more quota.
    api_account_daily_tasks: int = 1000
    api_account_daily_credits: int = 0  # 0: unlimited
    api_max_running_orchestrations_per_account: int = 10
    # A task whose question nobody answers ends (failed, question_timeout) after this long. 0: never.
    question_timeout_seconds: int = 300
    api_max_steps: int = 40  # the most steps a developer may ask for
    # MCP drive sessions (docs/design/mcp-drive.md): an outside agent driving step by step. A session with no
    # command for api_drive_idle_seconds ends; so does one older than api_drive_max_seconds.
    api_drive_idle_seconds: int = 120
    api_drive_max_seconds: int = 1800
    sensitive_apps: list[str] = field(default_factory=lambda: list(DEFAULT_SENSITIVE_APPS))
    # Retention: task text (chats, orchestration plans and results, webhook bodies) older than this
    # many days is deleted; billing totals and the hashed audit log stay. 0 keeps everything forever.
    history_retention_days: int = 30

    # TypeSafe Jev (text-only classifier). Enabled when TYPESAFE_API_KEY is set.
    jev_model: str = "jev-latest"
    jev_base_url: str = "https://api.typesafe.ai"
    jev_min_confidence: float = 0.6
    jev_risk_threshold: float = 0.5
    jev_auto_wait_seconds: float = 30.0  # max time to wait on screens Jev calls "loading"
    # false: run without Jev even when TYPESAFE_API_KEY is set (for comparisons). OPENMOB_JEV=off does the same.
    jev_enabled: bool = True
    # Fast path (needs Jev): on screens with a usable element list, Jev picks the next operation and its
    # target in one call and the step runs without the main model (a small text model writes any text).
    # off | fast_tier (only tasks on the `fast` API tier's model) | on. OPENMOB_FAST_PATH sets it too.
    # On by default since it measured 10.8 s -> 6.0 s on a labeled-app task (docs/benchmarks.md);
    # without Jev it does nothing.
    fast_path: str = "on"
    fast_path_min_probability: float = 0.7  # the chosen operation's and target's probability
    fast_path_min_confidence: float = 0.6  # and Jev's confidence in each
    fast_path_min_elements: int = 3  # labeled elements a screen needs (fewer: the vision step)
    fast_path_text_model: str | None = None  # writes text and app names; default: resolver_model
    # Latency features on top of the fast path (docs/benchmarks.md). Each can be switched off to compare.
    # Fast steps decide from the element list alone; the screenshot is taken only if the step goes to vision.
    fast_path_lazy_screenshot: bool = True
    # One Jev request per step: screen state, the fast path's heads and the risky-tap questions together.
    jev_one_request: bool = True
    jev_risk_heads: int = 40  # candidate targets whose tap risk that request asks (others: a separate check)
    # An app the request names exactly (an installed app's label) opens without the small text model.
    fast_path_direct_launch: bool = True
    # Let the classifier finish a navigation-only task without the vision model's confirmation, when its
    # DONE and "request looks complete" both reach fast_path_done_min_probability and the screen's title or
    # selected tab matches what the request asked to open. On by default: in a real-phone test it fired only
    # on the navigation task, never on tasks with side effects or answers. OPENMOB_FAST_DONE=off turns it off.
    fast_path_done: bool = True
    fast_path_done_min_probability: float = 0.85

    # Phone
    adb_path: str = "adb"
    adb_serial: str | None = None
    screenshot_max_side: int = 1280
    aliases: dict[str, str] = field(default_factory=dict)

    # Spoken replies on the Mac (macOS `say`)
    speak: bool = True
    voice: str | None = None

    # Phone-side web page
    server_host: str = "0.0.0.0"
    server_port: int = 8765
    server_token: str | None = None

    # Secrets come from the environment, never from the YAML file.
    api_key: str | None = None  # ANTHROPIC_API_KEY
    openai_api_key: str | None = None
    google_api_key: str | None = None  # GEMINI_API_KEY or GOOGLE_API_KEY
    qwen_api_key: str | None = None  # QWEN_API_KEY or DASHSCOPE_API_KEY
    xai_api_key: str | None = None  # XAI_API_KEY
    compatible_api_key: str | None = None  # OPENAI_COMPATIBLE_API_KEY (often not needed locally)
    workspace_id: str | None = None
    jev_api_key: str | None = None
    source: Path | None = None


def load_config(path: str | Path | None = None) -> Config:
    candidates = [Path(path)] if path else CONFIG_LOCATIONS
    data: dict = {}
    source = None
    for candidate in candidates:
        if candidate.is_file():
            data = yaml.safe_load(candidate.read_text()) or {}
            source = candidate
            break
    else:
        if path:
            raise FileNotFoundError(f"Config file not found: {path}")

    for removed in REMOVED_KEYS:  # settings of features that no longer exist: ignore, don't fail
        data.pop(removed, None)
    known = {f.name for f in fields(Config)} - SECRET_FIELDS
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown config keys in {source}: {', '.join(sorted(unknown))}")

    cfg = Config(**data)
    cfg.aliases = {str(k): str(v) for k, v in (cfg.aliases or {}).items()}
    cfg.api_key = os.environ.get("ANTHROPIC_API_KEY")
    # Needed only for API keys that aren't scoped to a workspace.
    if os.environ.get("OPENMOB_MAX_ACTIONS", "").strip().isdigit():
        cfg.max_actions_per_turn = int(os.environ["OPENMOB_MAX_ACTIONS"])
    if os.environ.get("OPENMOB_PARALLEL_OBSERVE", "").strip().lower() in ("on", "1", "true", "off", "0", "false"):
        cfg.parallel_observe = os.environ["OPENMOB_PARALLEL_OBSERVE"].strip().lower() in ("on", "1", "true")
    if os.environ.get("OPENMOB_REPLAY", "").strip().lower() in ("on", "1", "true", "off", "0", "false"):
        cfg.replay_paths = os.environ["OPENMOB_REPLAY"].strip().lower() in ("on", "1", "true")
    if os.environ.get("OPENMOB_REPLAY_STORE_API", "").strip().lower() in ("on", "1", "true", "off", "0", "false"):
        cfg.replay_store_api = os.environ["OPENMOB_REPLAY_STORE_API"].strip().lower() in ("on", "1", "true")
    if os.environ.get("OPENMOB_PLAN_EXECUTE", "").strip().lower() in ("on", "1", "true", "off", "0", "false"):
        cfg.plan_execute = os.environ["OPENMOB_PLAN_EXECUTE"].strip().lower() in ("on", "1", "true")
    if os.environ.get("OPENMOB_DEEP_LINKS", "").strip().lower() in ("on", "1", "true", "off", "0", "false"):
        cfg.deep_links = os.environ["OPENMOB_DEEP_LINKS"].strip().lower() in ("on", "1", "true")
    if os.environ.get("OPENMOB_DEEP_LINKS_FAST", "").strip().lower() in ("on", "1", "true", "off", "0", "false"):
        cfg.deep_links_fast = os.environ["OPENMOB_DEEP_LINKS_FAST"].strip().lower() in ("on", "1", "true")
    if os.environ.get("OPENMOB_APP_LINKS", "").strip().lower() in ("on", "1", "true", "off", "0", "false"):
        cfg.app_links = os.environ["OPENMOB_APP_LINKS"].strip().lower() in ("on", "1", "true")
    if os.environ.get("OPENMOB_APP_LINKS_FAST", "").strip().lower() in ("on", "1", "true", "off", "0", "false"):
        cfg.app_links_fast = os.environ["OPENMOB_APP_LINKS_FAST"].strip().lower() in ("on", "1", "true")
    if os.environ.get("OPENMOB_SCROLL_SETTLE_S", "").strip():
        cfg.scroll_settle_delay = float(os.environ["OPENMOB_SCROLL_SETTLE_S"])
    if os.environ.get("OPENMOB_CANVAS_EFFORT") is not None:
        cfg.canvas_effort = os.environ["OPENMOB_CANVAS_EFFORT"].strip()
    cfg.workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    cfg.jev_api_key = os.environ.get("TYPESAFE_API_KEY")
    if os.environ.get("OPENMOB_JEV", "").strip().lower() in {"0", "off", "false", "no"}:
        cfg.jev_enabled = False
    if os.environ.get("OPENMOB_ADAPTIVE_SETTLE", "").strip().lower() in {"0", "off", "false", "no"}:
        cfg.adaptive_settle = False
    round_d = os.environ.get("OPENMOB_ROUND_D", "").strip().lower()
    if round_d:  # all four round D features on or off at once (docs/benchmarks.md), for comparisons
        on = round_d in {"1", "on", "true", "yes"}
        cfg.fast_path_lazy_screenshot = cfg.jev_one_request = cfg.fast_path_direct_launch = cfg.fast_path_done = on
    if os.environ.get("OPENMOB_FAST_DONE", "").strip():
        cfg.fast_path_done = os.environ["OPENMOB_FAST_DONE"].strip().lower() in {"1", "on", "true", "yes"}
    if os.environ.get("OPENMOB_SETTLE_QUIET_MS", "").strip():
        cfg.settle_quiet_ms = int(os.environ["OPENMOB_SETTLE_QUIET_MS"])
    if os.environ.get("OPENMOB_FAST_PATH", "").strip():
        cfg.fast_path = os.environ["OPENMOB_FAST_PATH"].strip().lower()
    cfg.openai_api_key = os.environ.get("OPENAI_API_KEY")
    cfg.google_api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    cfg.qwen_api_key = os.environ.get("QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    cfg.xai_api_key = os.environ.get("XAI_API_KEY")
    cfg.qwen_base_url = os.environ.get("QWEN_BASE_URL", cfg.qwen_base_url)
    cfg.xai_base_url = os.environ.get("XAI_BASE_URL", cfg.xai_base_url)
    cfg.compatible_api_key = os.environ.get("OPENAI_COMPATIBLE_API_KEY")
    cfg.openai_compatible_base_url = os.environ.get("OPENAI_COMPATIBLE_BASE_URL", cfg.openai_compatible_base_url)
    cfg.adb_serial = os.environ.get("ANDROID_SERIAL", cfg.adb_serial)
    cfg.source = source
    cfg.models = [normalize_model(m) for m in cfg.models or []]
    if cfg.model not in {m["id"] for m in cfg.models}:  # the default is always selectable
        cfg.models.insert(0, normalize_model(cfg.model))
    if cfg.confirm_actions not in {"unrequested", "always", "never"}:
        raise ValueError("confirm_actions must be one of: unrequested, always, never")
    if cfg.default_permission_mode not in ("ask", "plan", "auto"):
        raise ValueError("default_permission_mode must be one of: ask, plan, auto")
    if cfg.api_default_tier not in cfg.api_tiers:
        raise ValueError(f"api_default_tier must be one of: {', '.join(cfg.api_tiers)}")
    for name, tier in cfg.api_tiers.items():
        if not isinstance(tier, dict) or "model" not in tier:
            raise ValueError(f"api_tiers.{name} needs a model")
        tier.setdefault("credits_per_step", 1)
        # A tier may name any provider's model; one that isn't in `models:` needs a known provider.
        if tier["model"] not in {m["id"] for m in cfg.models}:
            provider = tier.get("provider") or infer_provider(str(tier["model"]))
            if provider not in PROVIDERS:
                raise ValueError(f"api_tiers.{name}: say which provider serves {tier['model']} (provider: ...)")
            tier["provider"] = provider
    if isinstance(cfg.fast_path, bool):  # YAML reads a bare off / on as a boolean
        cfg.fast_path = "on" if cfg.fast_path else "off"
    if cfg.fast_path not in FAST_PATH_MODES:
        raise ValueError(f"fast_path must be one of: {', '.join(FAST_PATH_MODES)}")
    if cfg.history_trim not in {"batch", "rolling"}:
        raise ValueError("history_trim must be one of: batch, rolling")
    if cfg.ui_tree not in {"auto", "always", "never"}:
        raise ValueError("ui_tree must be one of: auto, always, never")
    prompts = dict(PERMISSION_DEFAULTS)
    for kind, value in (cfg.permission_prompts or {}).items():
        if kind not in PERMISSION_DEFAULTS or value not in permission_values(kind):
            raise ValueError(f"permission_prompts.{kind}: one of {', '.join(permission_values(kind))}"
                             if kind in PERMISSION_DEFAULTS else
                             f"permission_prompts: unknown type {kind!r} (one of {', '.join(PERMISSION_DEFAULTS)})")
        prompts[kind] = value
    cfg.permission_prompts = prompts
    return cfg
