"""openmob command-line entry point."""

from __future__ import annotations

import argparse
import importlib.util
import secrets
import shutil
import sys
import threading
import time

from .config import Config, load_config
from .phone.adb import AdbError, PhoneDriver


def make_anthropic_client(cfg: Config):
    import anthropic

    if not cfg.api_key:
        sys.exit("ANTHROPIC_API_KEY is not set.")
    headers = {"anthropic-workspace-id": cfg.workspace_id} if cfg.workspace_id else None
    return anthropic.Anthropic(api_key=cfg.api_key, default_headers=headers)


def build_agent(cfg: Config, io, driver: PhoneDriver | None = None, label_cache=None):
    from .agent import Agent
    from .agent.models import ModelRouter
    from .jev import JevClient, JevJudge
    from .phone.apps import AppResolver, chain_pickers, make_jev_picker, make_llm_picker

    router = ModelRouter(cfg)
    if not router.available(cfg.model):
        usable = [m["id"] for m in cfg.models if router.available(m["id"])]
        if not usable:
            sys.exit("No model provider is set up. Set ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY "
                     "(or a base_url for an OpenAI-compatible endpoint).")
        print(f"⚠️  {cfg.model} isn't available here (no key for its provider); using {usable[0]}.", flush=True)
        cfg.model = usable[0]
    # App names are picked by a small model: the resolver model if its provider is set up, else the default.
    picker_model = cfg.resolver_model if router.available(cfg.resolver_model) else cfg.model
    driver = driver or PhoneDriver(cfg.adb_serial, cfg.adb_path, cfg.screenshot_max_side)
    judge = None
    if cfg.jev_api_key:
        judge = JevJudge(JevClient(cfg.jev_api_key, cfg.jev_model, cfg.jev_base_url))
    picker = chain_pickers(
        make_jev_picker(judge, cfg.jev_min_confidence) if judge else None,
        make_llm_picker(router, picker_model),
    )
    extra = {"label_cache": label_cache} if label_cache else {}
    resolver = AppResolver(driver, cfg.aliases, llm_picker=picker, **extra)
    return Agent(router, driver, resolver, cfg, io, judge=judge)


def cmd_do(cfg: Config, args) -> None:
    from .runner import ConsoleIO
    from .voice.tts import Speaker

    speaker = Speaker(cfg.speak and not args.quiet, cfg.voice)
    io = ConsoleIO(speaker)
    agent = build_agent(cfg, io)
    cancel = threading.Event()
    try:
        result = agent.run(" ".join(args.text), cancel, go_home=args.home)
    except KeyboardInterrupt:
        cancel.set()
        sys.exit("\nStopped.")
    io.event("result" if result.success else "error", f"{result.summary} ({result.steps} steps)")
    speaker.say(result.summary, block=True)
    sys.exit(0 if result.success else 1)


def cmd_run(cfg: Config, args) -> None:
    from .devices import DeviceManager
    from .voice.tts import Speaker

    speaker = Speaker(cfg.speak and not args.quiet, cfg.voice)
    from .history import History

    from .memory import Memory

    history = History()  # saved chats: the data agents will learn from
    manager = DeviceManager(cfg, build_agent, speaker, adb_discovery=not args.no_adb, history=history,
                            memory=Memory())  # pinned notes: what the agent remembers about its user
    sessions = manager.refresh()
    for s in sessions:
        print(f"📱 {s.info.model} ({s.info.connection}, {s.info.transport})")
    if args.no_adb:
        print("Hosted mode: waiting for phones to connect with the OpenMob app or `openmob bridge`.")
    elif not sessions:
        print("No phone connected yet. Plug one in or run `openmob connect`, then rescan from the dashboard.")

    def active():
        session = manager.first_online()
        if session is None:
            print("No phone online.")
        return session

    transcriber = None
    if importlib.util.find_spec("faster_whisper"):
        from .voice.stt import Transcriber

        transcriber = Transcriber(cfg.whisper_model)
        threading.Thread(target=transcriber._load, daemon=True).start()  # load the model before the first use
        print(f"🎙  Mic button: Whisper ({cfg.whisper_model}) transcribes on this server.")
    else:
        print("🎙  Mic button disabled: install Whisper with  pip install -e '.[voice]'")

    from .server import create_app, lan_ip, serve_in_background

    store = None
    if args.accounts:
        from .accounts import AccountStore

        store = AccountStore()
        print(f"🔐 Accounts on: users sign in, and each phone pairs only to its owner's account ({store.path}).")
    token = cfg.server_token or secrets.token_urlsafe(8)
    serve_in_background(create_app(manager, token, store=store, transcriber=transcriber), cfg.server_host, cfg.server_port)
    # With accounts the dashboard uses a sign-in session, so links carry no secret.
    suffix = "/login" if store else f"/?t={token}"
    print(f"🖥  Dashboard: http://localhost:{cfg.server_port}{suffix}")
    print(f"   On your network: http://{lan_ip()}:{cfg.server_port}{suffix}")
    if args.public:
        from .tunnel import open_tunnel

        url = open_tunnel(cfg.server_port)
        print(f"🌍 Public (HTTPS): {url}{suffix}" if url else "⚠️  Could not open a public tunnel (is cloudflared installed?)")

    print("⌨️  You can also type commands here. Ctrl+C to quit.\n")
    try:
        for line in sys.stdin:
            if session := active():
                session.coordinator.submit(line, "terminal")
        threading.Event().wait()  # no terminal attached (e.g. run as a service): keep serving
    except KeyboardInterrupt:
        pass
    for session in manager.sessions.values():
        session.coordinator.stop()


def cmd_connect(cfg: Config, args) -> None:
    """Switch phones to Wi-Fi control, or reconnect to one by address."""
    probe = PhoneDriver(adb_path=cfg.adb_path)
    try:
        if args.address:
            address = args.address if ":" in args.address else f"{args.address}:5555"
            print(probe.connect(address))
            targets = [PhoneDriver(address, cfg.adb_path)]
        else:
            usb = [serial for serial, state in probe.devices() if state == "device" and ":" not in serial]
            if not usb:
                sys.exit("No phone on USB. Plug it in once (USB debugging on), or pass its IP: openmob connect 192.168.1.23")
            targets = []
            for serial in usb:
                driver = PhoneDriver(serial, cfg.adb_path)
                ip = driver.wifi_ip()
                if not ip:
                    print(f"✗ {serial}: not on Wi-Fi")
                    continue
                driver.enable_tcpip()
                time.sleep(2)
                print(f"✓ {probe.connect(f'{ip}:5555')}")
                targets.append(PhoneDriver(f"{ip}:5555", cfg.adb_path))
            if targets:
                print("You can unplug the cable now. Re-run this after the phone restarts.")
        if args.stay_awake:
            for driver in targets:
                driver.stay_awake(True)
            print("Screen will stay on (undo: adb shell svc power stayon false).")
    except AdbError as exc:
        sys.exit(f"✗ {exc}")


def cmd_apps(cfg: Config, args) -> None:
    from .phone.apps import AppResolver, package_words

    driver = PhoneDriver(cfg.adb_serial, cfg.adb_path, cfg.screenshot_max_side)
    resolver = AppResolver(driver, cfg.aliases)
    resolver.packages(refresh=True)
    if args.refresh:
        resolver.labels.clear()
    if missing := resolver.missing_labels():
        print(f"Reading names of {len(missing)} apps from the phone (one-time, ~1 min for 150 apps)…", file=sys.stderr)
        resolver.ensure_labels()
    for package in resolver.packages():
        label = resolver.labels.get(package) or f"~ {package_words(package)}"
        print(f"{package:55} {label}")
    if args.resolve:
        found = resolver.resolve(args.resolve)
        print(f"\n{args.resolve!r} -> {found.package + ' via ' + found.method if found else 'no match'}")


def cmd_doctor(cfg: Config, args) -> None:
    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"{'✓' if passed else '✗'} {name}{': ' + detail if detail else ''}")

    def optional(name: str, passed: bool, detail: str = "") -> None:
        print(f"{'✓' if passed else '–'} {name}{': ' + detail if detail else ''}")

    print(f"config: {cfg.source or '(defaults)'}")
    check("adb installed", shutil.which(cfg.adb_path) is not None, "brew install android-platform-tools")
    driver = PhoneDriver(cfg.adb_serial, cfg.adb_path, cfg.screenshot_max_side)
    try:
        devices = driver.devices()
        ready = [s for s, state in devices if state == "device"]
        check("phone connected", bool(ready), ", ".join(f"{s} ({st})" for s, st in devices) or "no devices")
        if len(ready) > 1 and not cfg.adb_serial:
            check("single device", False, "several phones connected; set adb_serial or ANDROID_SERIAL")
        if ready:
            w, h = driver.screen_size()
            check("screen size", True, f"{w}x{h}")
            shot = driver.screenshot()
            check("screenshot", True, f"{shot.width}x{shot.height} sent to the model")
            check("foreground app", True, driver.current_app() or "unknown")
    except AdbError as exc:
        check("adb", False, str(exc))
    check("ANTHROPIC_API_KEY", bool(cfg.api_key))
    if cfg.api_key:
        import anthropic

        try:
            make_anthropic_client(cfg).messages.create(
                model=cfg.model, max_tokens=1, messages=[{"role": "user", "content": "hi"}]
            )
            check("Claude reachable", True, cfg.model)
        except anthropic.APIError as exc:
            hint = ""
            if "workspace" in str(exc):
                hint = " (set ANTHROPIC_WORKSPACE_ID, or use a workspace-scoped key)"
            check("Claude reachable", False, f"{getattr(exc, 'message', exc)}{hint}")
    optional("TYPESAFE_API_KEY (Jev classification)", bool(cfg.jev_api_key), "" if cfg.jev_api_key else "not set; Jev disabled")
    if cfg.jev_api_key:
        from .jev import JevClient, JevError, JevJudge

        try:
            judge = JevJudge(JevClient(cfg.jev_api_key, cfg.jev_model, cfg.jev_base_url))
            a = judge.assess_screen("open the menu", {"foreground_app": "launcher", "text_on_screen": ["Loading 42%"]})
            check("Jev reachable", True, a.summary())
        except JevError as exc:
            check("Jev reachable", False, str(exc))

    from .vision import ocr

    optional("Apple Vision OCR", ocr.available(), "" if ocr.available() else "pip install -e '.[mac]'")
    sys.exit(0 if ok else 1)


def cmd_bridge(cfg: Config, args) -> None:
    """Connect a local phone (Android over adb, or an iPhone through WebDriverAgent) to an OpenMob server."""
    import os

    from .bridge import BridgeAuthError, device_info_from_adb, load_key, pair_with_account, run_bridge, save_key
    from .phone.base import DeviceError

    token = args.token or os.environ.get("OPENMOB_TOKEN") or ""
    serial = args.serial or cfg.adb_serial
    try:
        if args.ios:
            from .phone.ios import IOSDriver

            driver = IOSDriver(args.wda_url, udid=args.udid, max_side=cfg.screenshot_max_side)
            driver.screenshot(attempts=1)  # fail now, with a clear message, if WebDriverAgent isn't running
            info = driver.device_info()
        else:
            if not serial:
                ready = [s for s, state in PhoneDriver(adb_path=cfg.adb_path).devices() if state == "device"]
                if len(ready) != 1:
                    sys.exit("No phone connected." if not ready else f"Several phones connected ({', '.join(ready)}); pass --serial.")
                serial = ready[0]
            driver = PhoneDriver(serial, cfg.adb_path, cfg.screenshot_max_side)
            info = device_info_from_adb(driver)
    except (AdbError, DeviceError) as exc:
        hint = "  Is WebDriverAgent running? Start it from Xcode (see docs/ios.md)." if args.ios else ""
        sys.exit(f"✗ {exc}{hint}")

    # Account mode: pair once with --email, then reconnect with the saved per-phone key.
    key = None
    if args.email:
        password = _read_password(args.password_file, prompt=f"OpenMob password for {args.email}: ")
        model = " ".join(filter(None, [info.get("manufacturer", "").title(), info.get("model")]))
        try:
            key = pair_with_account(args.server_url, args.email, password, info["id"], model)
        except BridgeAuthError as exc:
            sys.exit(f"✗ Pairing failed: {exc}")
        save_key(args.server_url, info["id"], key)
        print(f"🔐 Paired this phone to {args.email}.")
    else:
        key = load_key(args.server_url, info["id"])
    if not key and not token:
        sys.exit("Pair with your account: --email YOU@EXAMPLE.COM (or, for token-mode servers, --token TOKEN).")
    run_bridge(driver, args.server_url, token, info, key=key,
               on_rejected=lambda: save_key(args.server_url, info["id"], None))


def _read_password(password_file: str | None, prompt: str) -> str:
    import getpass
    import os

    if os.environ.get("OPENMOB_PASSWORD"):
        return os.environ["OPENMOB_PASSWORD"]
    if password_file:
        try:
            return open(os.path.expanduser(password_file)).read().strip()
        except OSError as exc:
            sys.exit(f"✗ Can't read {password_file}: {exc}")
    return getpass.getpass(prompt)


def cmd_account(cfg: Config, args) -> None:
    """Manage accounts on this server (there's no web sign-up page yet)."""
    import os
    import secrets as _secrets

    from .accounts import AccountStore, AuthError

    store = AccountStore()
    if args.action == "list":
        accounts = store.admin_list_accounts()
        print("\n".join(a["email"] + ("  (admin)" if a["is_admin"] else "") for a in accounts) or "No accounts yet.")
        return
    if not args.email:
        sys.exit(f"openmob account {args.action} needs an EMAIL.")

    if args.action == "add":
        if store.account_id_for(args.email):
            print(f"✓ {args.email} already exists.")
            return
        path = os.path.expanduser(args.password_file) if args.password_file else None
        if path and not os.path.exists(path):
            # Generate a strong password and keep it in an owner-only file, not on screen.
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                f.write(_secrets.token_urlsafe(18))
            print(f"🔑 Generated a password in {path} (owner-only).")
        password = _read_password(path, prompt=f"New password for {args.email}: ")
        try:
            store.create_account(args.email, password)
        except AuthError as exc:
            sys.exit(f"✗ {exc}")
        print(f"✓ Created {args.email}.")
        return

    if args.action in ("admin", "unadmin"):
        # The only way to grant admin: shell access to the server, never a web request.
        if not store.set_admin(args.email, args.action == "admin"):
            sys.exit(f"✗ No account {args.email}. Create it: openmob account add {args.email}")
        if args.action == "admin":
            print(f"✓ {args.email} is now an admin. Sign in and open /admin.")
        else:
            print(f"✓ {args.email} is no longer an admin.")
        return

    if args.action == "link":
        account_id = store.account_id_for(args.email)
        if not account_id:
            sys.exit(f"✗ No account {args.email}. Create it: openmob account add {args.email}")
        code = store.create_login_link(account_id)
        base = args.server or _public_url(cfg.server_port) or f"http://localhost:{cfg.server_port}"
        print(f"One-time sign-in link for {args.email} (works once, for 10 minutes):")
        print(f"{base.rstrip('/')}/auth/link?code={code}")


def cmd_key(cfg: Config, args) -> None:
    """Developer API keys for an account (the same keys /developer makes)."""
    from .accounts import DEFAULT_SCOPES, AccountStore, AuthError
    from .ledger import Ledger

    store = AccountStore()
    account_id = store.account_id_for(args.email)
    if not account_id:
        sys.exit(f"✗ No account {args.email}. Create it: openmob account add {args.email}")
    if args.action == "list":
        keys = store.list_api_keys(account_id)
        for k in keys:
            print(f"{k['prefix']}_…  {k['name']}  [{', '.join(k['scopes'])}]{'  (turned off)' if k['disabled'] else ''}")
        if not keys:
            print("No keys.")
        return
    ledger = Ledger(store.path.with_name("ledger.sqlite"))
    if args.action == "create":
        scopes = [s.strip() for s in args.scopes.split(",")] if args.scopes else list(DEFAULT_SCOPES)
        try:
            key_id, key = store.create_api_key(account_id, args.name or "CLI key", scopes)
        except AuthError as exc:
            sys.exit(f"✗ {exc}")
        ledger.audit("key.create", account_id, key_id, outcome=",".join(sorted(set(scopes))))
        print(f"✓ Created key {key_id}. Copy it now; it won't be shown again:")
        print(key)
        return
    if not args.key_id:
        sys.exit("openmob key revoke needs a KEY_ID (see: openmob key list EMAIL).")
    key_id = args.key_id.removeprefix("om_live_").split("_")[0]
    if not store.revoke_api_key(account_id, key_id):
        sys.exit(f"✗ {args.email} has no key {key_id}.")
    ledger.audit("key.revoke", account_id, key_id)
    # The server re-reads the store, so the key fails on its next request. Revoking from /developer
    # also cancels the key's running tasks; from here they run to the end.
    print(f"✓ Revoked {key_id}. It stops working on its next request.")


def _public_url(port: int) -> str | None:
    """The running tunnel's URL, if `openmob run --public` started one."""
    import json as _json

    from .tunnel import _state_file

    try:
        return _json.loads(_state_file(port).read_text())["url"]
    except (OSError, ValueError, KeyError):
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="openmob", description="Voice-controlled agent for your Android phone.")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="listen for commands from the Mac mic, the phone page, and the terminal")
    p_run.add_argument("--public", action="store_true", help="also publish the dashboard over HTTPS (cloudflared)")
    p_run.add_argument("--no-adb", action="store_true", help="hosted mode: don't use phones attached to this computer")
    p_run.add_argument("--accounts", action="store_true",
                       help="require sign-in; phones pair with one-time codes bound to their owner's account")
    p_run.add_argument("--quiet", action="store_true", help="don't speak replies")

    p_do = sub.add_parser("do", help="run one command and exit")
    p_do.add_argument("text", nargs="+")
    p_do.add_argument("--home", action="store_true", help="press HOME before starting")
    p_do.add_argument("--quiet", action="store_true")

    p_apps = sub.add_parser("apps", help="list launchable apps (to set aliases)")
    p_apps.add_argument("--refresh", action="store_true", help="re-read every app's display name")
    p_apps.add_argument("--resolve", metavar="NAME", help="test how a spoken name resolves")

    sub.add_parser("doctor", help="check the setup")

    p_connect = sub.add_parser("connect", help="control phones over Wi-Fi instead of USB")
    p_connect.add_argument("address", nargs="?", help="phone IP to reconnect to (default: switch USB phones to Wi-Fi)")
    p_connect.add_argument("--stay-awake", action="store_true", help="keep the screen on (for demos)")

    p_bridge = sub.add_parser("bridge", help="connect this computer's phone to a hosted OpenMob server")
    p_bridge.add_argument("server_url", help="server address, e.g. https://openmob.example.com")
    p_bridge.add_argument("--token", help="phone token (default: $OPENMOB_TOKEN)")
    p_bridge.add_argument("--serial", help="adb serial of the phone (default: adb_serial or the only connected phone)")
    p_bridge.add_argument("--ios", action="store_true", help="bridge an iPhone through WebDriverAgent instead of adb")
    p_bridge.add_argument("--wda-url", default="http://localhost:8100", help="WebDriverAgent address (default: http://localhost:8100)")
    p_bridge.add_argument("--udid", help="iPhone UDID, for listing its apps over USB (default: the only connected iPhone)")
    p_bridge.add_argument("--email", help="account mode: pair this phone to your account (prompts for the password)")
    p_bridge.add_argument("--password-file", help="read the account password from this file instead of prompting")

    p_account = sub.add_parser("account", help="manage accounts on this server (add, list, sign-in link, admin)")
    p_account.add_argument("action", choices=["add", "list", "link", "admin", "unadmin"])
    p_account.add_argument("email", nargs="?")
    p_account.add_argument("--password-file", help="add: read (or, if missing, generate) the password in this file")
    p_account.add_argument("--server", help="link: the server's public URL (default: the running tunnel)")

    p_key = sub.add_parser("key", help="developer API keys for an account (create, list, revoke)")
    p_key.add_argument("action", choices=["create", "list", "revoke"])
    p_key.add_argument("email")
    p_key.add_argument("key_id", nargs="?", help="revoke: the key's id or prefix")
    p_key.add_argument("--name", help="create: a name to recognise the key by")
    p_key.add_argument("--scopes", help="create: comma-separated, e.g. devices:read,tasks:run,tasks:read (default)")

    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # show progress promptly even when logged to a file
    cfg = load_config(args.config)
    {"run": cmd_run, "do": cmd_do, "apps": cmd_apps, "doctor": cmd_doctor, "connect": cmd_connect, "account": cmd_account, "bridge": cmd_bridge,
     "key": cmd_key}[args.command](cfg, args)


if __name__ == "__main__":
    main()
