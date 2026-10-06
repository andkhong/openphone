"""Configuration loaded from config.yaml plus environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from .permissions import (DEFAULT_APP_CATEGORIES, DEFAULT_CREDENTIAL_TERMS, DEFAULT_MONEY_KEYWORDS,
                          DEFAULT_SETTINGS_ASK, DEFAULT_SETTINGS_EVERYDAY, DEFAULT_SETTINGS_REFUSE, DEFAULT_SKIP_PHRASES,
                          DEFAULT_START_FLOW_WORDS, DEFAULT_TERMINAL_WORDS, MODES)

DEFAULT_CONFIRM_KEYWORDS = [
    "buy", "purchase", "pay", "payment", "checkout", "top up", "recharge",
    "subscribe", "send", "post", "publish", "delete", "remove", "uninstall",
    "transfer", "place order", "confirm order", "share to", "forward to",
]

DEFAULT_MODELS = [
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5 (balanced, recommended)", "provider": "anthropic"},
    {"id": "claude-opus-5-5", "label": "Opus 5.5 (smartest, slower, pricier)", "provider": "anthropic"},
    {"id": "claude-fable-5-1", "label": "Fable 5.1", "provider": "anthropic"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5 (fastest, cheapest)", "provider": "anthropic"},
]

# Who serves a model. Keys come from the environment only; `openai_compatible` is any endpoint that
# speaks OpenAI Chat Completions (vLLM, Ollama, LM Studio, OpenRouter…) at a `base_url`.
PROVIDERS = ("anthropic", "openai", "google", "openai_compatible")
# Optional per-model settings in `models:` besides id, label and provider.
MODEL_OPTIONS = ("base_url", "api_key_env", "max_tokens", "vision", "tools", "context")


def infer_provider(model_id: str) -> str | None:
    """The provider a model id belongs to, from its name (None: say `provider:` in the config)."""
    name = model_id.lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith(("gpt-", "chatgpt", "o1", "o3", "o4", "o5")):
        return "openai"
    if name.startswith(("gemini", "gemma")):
        return "google"
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
    return model

# Developer API tiers: the public name -> the model that runs it, and what a step costs in credits.
# The mapping is internal; API responses only ever show the tier name.
DEFAULT_API_TIERS = {
    "fast": {"model": "claude-haiku-4-5-20251001", "credits_per_step": 1},
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

REMOVED_KEYS = {"hotkey"}
SECRET_FIELDS = {"api_key", "openai_api_key", "google_api_key", "compatible_api_key", "workspace_id", "jev_api_key", "source"}  # Mac push-to-talk was removed

CONFIG_LOCATIONS = [
    Path("config.yaml"),
    Path.home() / ".config" / "openmob" / "config.yaml",
]


@dataclass
class Config:
    # Agent
    model: str = "claude-sonnet-5-5"
    resolver_model: str = "claude-haiku-4-5-20251001"
    # Models the dashboard may switch a phone to (an allowlist; anything else is refused):
    # {id, label, provider} plus optional base_url, api_key_env, max_tokens, vision, tools, context.
    models: list[dict] = field(default_factory=lambda: [dict(m) for m in DEFAULT_MODELS])
    # Default endpoint for `openai_compatible` models without their own base_url (not a secret).
    openai_compatible_base_url: str | None = None
    max_steps: int = 40
    keep_screenshots: int = 2
    settle_delay: float = 0.8
    launch_settle_delay: float = 3.0
    # Adaptive settle: after an action, wait only until the screen stops changing (no window or content
    # change for settle_quiet_ms), capped at the delays above. Needs a phone whose app offers wait_idle;
    # others keep the fixed delays (adb: launches end once the app is in front). OPENMOB_ADAPTIVE_SETTLE=off.
    adaptive_settle: bool = True
    settle_quiet_ms: int = 300  # OPENMOB_SETTLE_QUIET_MS sets it too (to compare e.g. 200)
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
    # selected tab matches what the request asked to open. Off until measured. OPENMOB_FAST_DONE=on.
    fast_path_done: bool = False
    fast_path_done_min_probability: float = 0.85

    # Phone
    adb_path: str = "adb"
    adb_serial: str | None = None
    screenshot_max_side: int = 1280
    aliases: dict[str, str] = field(default_factory=dict)

    # Spoken replies on the Mac (macOS `say`)
    speak: bool = True
    # Whisper model for the dashboard's mic button: tiny.en | base.en | small.en | medium.en
    whisper_model: str = "base.en"
    voice: str | None = None

    # Phone-side web page
    server_host: str = "0.0.0.0"
    server_port: int = 8765
    server_token: str | None = None

    # Secrets come from the environment, never from the YAML file.
    api_key: str | None = None  # ANTHROPIC_API_KEY
    openai_api_key: str | None = None
    google_api_key: str | None = None  # GEMINI_API_KEY or GOOGLE_API_KEY
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
    if cfg.ui_tree not in {"auto", "always", "never"}:
        raise ValueError("ui_tree must be one of: auto, always, never")
    return cfg
