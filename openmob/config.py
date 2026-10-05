"""Configuration loaded from config.yaml plus environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

DEFAULT_CONFIRM_KEYWORDS = [
    "buy", "purchase", "pay", "payment", "checkout", "top up", "recharge",
    "subscribe", "send", "post", "publish", "delete", "remove", "uninstall",
    "transfer", "place order", "confirm order",
]

DEFAULT_MODELS = [
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5 (balanced, recommended)"},
    {"id": "claude-opus-5-5", "label": "Opus 5.5 (smartest, slower, pricier)"},
    {"id": "claude-fable-5-1", "label": "Fable 5.1"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5 (fastest, cheapest)"},
]

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

REMOVED_KEYS = {"hotkey"}  # Mac push-to-talk was removed

CONFIG_LOCATIONS = [
    Path("config.yaml"),
    Path.home() / ".config" / "openmob" / "config.yaml",
]


@dataclass
class Config:
    # Agent
    model: str = "claude-sonnet-5-5"
    resolver_model: str = "claude-haiku-4-5-20251001"
    # Models the dashboard may switch a phone to (an allowlist; anything else is refused).
    models: list[dict] = field(default_factory=lambda: [dict(m) for m in DEFAULT_MODELS])
    max_steps: int = 40
    keep_screenshots: int = 2
    settle_delay: float = 0.8
    launch_settle_delay: float = 3.0
    ui_tree: str = "auto"  # auto | always | never
    confirm_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_CONFIRM_KEYWORDS))
    # Ask before risky taps (buy/send/delete…): unrequested = only if the request didn't ask for it.
    confirm_actions: str = "unrequested"  # unrequested | always | never
    ocr: bool = True  # Apple Vision OCR when the app has no accessibility tree (games)

    # Developer API (/v1): tiers, per-key limits, and apps API tasks can't touch by default.
    api_tiers: dict[str, dict] = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_API_TIERS.items()})
    api_default_tier: str = "balanced"
    api_rate_per_minute: int = 60  # requests per key per minute
    api_max_concurrent_tasks: int = 2  # unfinished tasks per key
    api_daily_tasks: int = 200  # tasks per key per UTC day
    api_max_steps: int = 40  # the most steps a developer may ask for
    sensitive_apps: list[str] = field(default_factory=lambda: list(DEFAULT_SENSITIVE_APPS))
    # Delete chats (and their turns) older than this many days; 0 keeps them forever.
    history_retention_days: int = 0

    # TypeSafe Jev (text-only classifier). Enabled when TYPESAFE_API_KEY is set.
    jev_model: str = "jev-latest"
    jev_base_url: str = "https://api.typesafe.ai"
    jev_min_confidence: float = 0.6
    jev_risk_threshold: float = 0.5
    jev_auto_wait_seconds: float = 30.0  # max time to wait on screens Jev calls "loading"

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
    api_key: str | None = None
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
    known = {f.name for f in fields(Config)} - {"api_key", "workspace_id", "jev_api_key", "source"}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown config keys in {source}: {', '.join(sorted(unknown))}")

    cfg = Config(**data)
    cfg.aliases = {str(k): str(v) for k, v in (cfg.aliases or {}).items()}
    cfg.api_key = os.environ.get("ANTHROPIC_API_KEY")
    # Needed only for API keys that aren't scoped to a workspace.
    cfg.workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    cfg.jev_api_key = os.environ.get("TYPESAFE_API_KEY")
    cfg.adb_serial = os.environ.get("ANDROID_SERIAL", cfg.adb_serial)
    cfg.source = source
    cfg.models = [
        {"id": str(m["id"]), "label": str(m.get("label") or m["id"])} if isinstance(m, dict) else {"id": str(m), "label": str(m)}
        for m in cfg.models or []
    ]
    if cfg.model not in {m["id"] for m in cfg.models}:  # the default is always selectable
        cfg.models.insert(0, {"id": cfg.model, "label": cfg.model})
    if cfg.confirm_actions not in {"unrequested", "always", "never"}:
        raise ValueError("confirm_actions must be one of: unrequested, always, never")
    if cfg.api_default_tier not in cfg.api_tiers:
        raise ValueError(f"api_default_tier must be one of: {', '.join(cfg.api_tiers)}")
    for name, tier in cfg.api_tiers.items():
        if not isinstance(tier, dict) or "model" not in tier:
            raise ValueError(f"api_tiers.{name} needs a model")
        tier.setdefault("credits_per_step", 1)
    if cfg.ui_tree not in {"auto", "always", "never"}:
        raise ValueError("ui_tree must be one of: auto, always, never")
    return cfg
