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

CONFIG_LOCATIONS = [
    Path("config.yaml"),
    Path.home() / ".config" / "openmob" / "config.yaml",
]


@dataclass
class Config:
    # Agent
    model: str = "claude-sonnet-5-5"
    resolver_model: str = "claude-haiku-4-5-20251001"
    max_steps: int = 40
    keep_screenshots: int = 2
    settle_delay: float = 0.8
    launch_settle_delay: float = 3.0
    ui_tree: str = "auto"  # auto | always | never
    confirm_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_CONFIRM_KEYWORDS))
    # Ask before risky taps (buy/send/delete…): unrequested = only if the request didn't ask for it.
    confirm_actions: str = "unrequested"  # unrequested | always | never
    ocr: bool = True  # Apple Vision OCR when the app has no accessibility tree (games)

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

    # Voice
    hotkey: str = "alt_r"
    whisper_model: str = "base.en"
    speak: bool = True
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
    if cfg.confirm_actions not in {"unrequested", "always", "never"}:
        raise ValueError("confirm_actions must be one of: unrequested, always, never")
    if cfg.ui_tree not in {"auto", "always", "never"}:
        raise ValueError("ui_tree must be one of: auto, always, never")
    return cfg
