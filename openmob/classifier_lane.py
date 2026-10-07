"""Run either classifier against the same app code with separate local state."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import shlex

ROOT = Path(__file__).resolve().parents[1]
KEYS = {
    "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "TYPESAFE_API_KEY",
    "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "OPENAI_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_COMPATIBLE_API_KEY",
}


def load_credentials(path: Path) -> None:
    if not path.exists():
        return  # Keys may already be in the launching shell.
    for number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            parts = shlex.split(line, comments=True)
        except ValueError:
            raise ValueError(f"Invalid credentials syntax on line {number}") from None
        if parts[:1] == ["export"]:
            parts = parts[1:]
        if not parts:
            continue
        if len(parts) != 1 or "=" not in parts[0]:
            raise ValueError(f"Expected KEY=value on credentials line {number}")
        key, value = parts[0].split("=", 1)
        if key not in KEYS:
            raise ValueError(f"Unsupported credential name on line {number}")
        if value:
            os.environ.setdefault(key, value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("jev", "clef"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--credentials", type=Path, default=ROOT / ".classifier-data/credentials.env")
    parser.add_argument("--check", action="store_true", help="check configuration without contacting APIs or phones")
    args = parser.parse_args()
    load_credentials(args.credentials)
    data = ROOT / ".classifier-data" / args.provider
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["OPENMOB_DATA_DIR"] = str(data)

    # Import after choosing the data directory: storage modules bind it at import time.
    from .config import load_config
    from .classifiers import make_judge
    from .agent.models import ModelRouter

    cfg = load_config(ROOT / "config.classifier-lane.yaml")
    cfg.classifier_provider = args.provider
    cfg.jev_enabled = True
    cfg.server_port = args.port or {"jev": 8766, "clef": 8767}[args.provider]
    cfg.server_host = args.host
    judge = make_judge(cfg)
    if judge is None:
        raise SystemExit("Jev needs TYPESAFE_API_KEY")
    judge.client.close()
    router = ModelRouter(cfg)
    if not any(router.available(m["id"]) for m in cfg.models):
        raise SystemExit("Set a main-model API key in the shell or credentials file.")
    print(f"{args.provider.upper()} lane: port {cfg.server_port}; data {data}", flush=True)
    print(f"Classifier: {cfg.clef_model if args.provider == 'clef' else cfg.jev_model}; "
          f"images: {cfg.clef_images if args.provider == 'clef' else 'off'}; OCR: {cfg.ocr}", flush=True)
    if args.check:
        return

    import uvicorn
    from .accounts import AccountStore
    from .devices import DeviceManager
    from .history import History
    from .main import build_agent
    from .memory import Memory
    from .permissions import PermissionStore
    from .server import create_app
    from .voice.tts import Speaker

    manager = DeviceManager(cfg, build_agent, Speaker(False, cfg.voice), adb_discovery=False,
                            history=History(), memory=Memory())
    manager.permissions = PermissionStore(data / "permissions.json")
    store = AccountStore()
    # A fresh lane has a local test account; credentials stay in an owner-only file.
    if not store.admin_list_accounts():
        password = secrets.token_urlsafe(24)
        email = "tester@openmob.local"
        with open(os.open(data / "login.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(f"Email: {email}\nPassword: {password}\n")
        store.create_account(email, password)
    print(f"Sign in: http://localhost:{cfg.server_port}/login (credentials: {data / 'login.txt'})", flush=True)
    app = create_app(manager, secrets.token_urlsafe(32), store=store,
                     session_cookie=f"openmob_{args.provider}_session")
    app.title = f"OpenMob — {args.provider.upper()} lane"
    uvicorn.run(app, host=cfg.server_host, port=cfg.server_port, access_log=False)


if __name__ == "__main__":
    main()
