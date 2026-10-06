"""`openmob phone add` against a fake adb (no real phone is ever touched).

The fake keeps a phone's state (installed APK, permissions, battery list, accessibility settings,
app ops, the app's prefs), answers the adb commands the CLI runs, and stands in for the owner:
when the app is opened on a pairing link, it "confirms on the phone" by claiming the code and
connecting, unless told not to.
"""

import hashlib
import shlex
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import FakeDriver
from test_easy_connect import srv  # noqa: F401  (fixture)
from test_remote import FakePhone, wait_for

from openmob.phone_add import A11Y_SERVICE, PACKAGE, PhoneAddError, phone_add, pick_device, Adb

OTHER_A11Y = "com.google.android.marvin.talkback/.TalkBackService:com.lastpass.lpandroid/.LPAccessibilityService"


class FakeAdb:
    def __init__(self, server=None, sdk=34, devices="SERIAL1\tdevice", confirm=True, ioreg=""):
        self.server, self.sdk, self.devices, self.confirm, self.ioreg = server, sdk, devices, confirm, ioreg
        self.apk_hash = None  # sha256 of the installed base.apk
        self.granted = set()
        self.whitelist = {"com.android.phone"}
        self.secure = {"enabled_accessibility_services": OTHER_A11Y, "accessibility_enabled": "1"}
        self.appops = "default"
        self.prefs = {}  # the app's shared prefs (device_id, server), readable with run-as
        self.commands = []  # every adb command, as a string
        self.links = []  # app links the app was opened on
        self.phone = None

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        if cmd[0] == "ioreg":
            return subprocess.CompletedProcess(cmd, 0, self.ioreg, "")
        args = cmd[1:]
        if args[:1] == ["-s"]:
            args = args[2:]
        self.commands.append(" ".join(args))
        out, code = self.handle(args)
        return subprocess.CompletedProcess(cmd, code, out, "")

    def handle(self, args):
        if args == ["devices"]:
            return f"List of devices attached\n{self.devices}\n", 0
        if args[0] == "install":
            self.apk_hash = hashlib.sha256(open(args[-1], "rb").read()).hexdigest()
            self.prefs.setdefault("device_id", "uuid-1")
            return "Performing Streamed Install\nSuccess\n", 0
        assert args[0] == "shell" and len(args) == 2, args
        words = shlex.split(args[1])
        match words:
            case ["getprop", "ro.build.version.sdk"]:
                return f"{self.sdk}\n", 0
            case ["getprop", "ro.build.version.release"]:
                return "14\n", 0
            case ["getprop", "ro.product.manufacturer"]:
                return "samsung\n", 0
            case ["getprop", "ro.product.model"]:
                return "SM-S948U1\n", 0
            case ["pm", "path", _]:
                return (f"package:/data/app/~~x/{PACKAGE}/base.apk\n", 0) if self.apk_hash else ("", 1)
            case ["sha256sum", path]:
                return f"{self.apk_hash}  {path}\n", 0
            case ["dumpsys", "package", _]:
                return "".join(f"    {p}: granted=true\n" for p in self.granted), 0
            case ["pm", "grant", _, perm]:
                self.granted.add(perm)
                return "", 0
            case ["dumpsys", "deviceidle", "whitelist"]:
                return "".join(f"user,{p},10001\n" for p in sorted(self.whitelist)), 0
            case ["dumpsys", "deviceidle", "whitelist", plus]:
                self.whitelist.add(plus.removeprefix("+"))
                return f"Added: {plus[1:]}\n", 0
            case ["settings", "get", "secure", name]:
                return self.secure.get(name, "null") + "\n", 0
            case ["settings", "put", "secure", name, value]:
                self.secure[name] = value
                return "", 0
            case ["appops", "set", _, "PROJECT_MEDIA", mode]:
                self.appops = mode
                return "", 0
            case ["appops", "get", _, "PROJECT_MEDIA"]:
                return f"PROJECT_MEDIA: {self.appops}\n", 0
            case ["run-as", _, "cat", _]:
                return "".join(f'<string name="{k}">{v}</string>\n' for k, v in self.prefs.items()), 0
            case ["input", "keyevent", _]:
                return "", 0
            case ["am", "start", "-n", _, "-a", "android.intent.action.VIEW", "-d", link]:
                self.links.append(link)
                if self.confirm:
                    threading.Thread(target=self.owner_confirms, args=(link,), daemon=True).start()
                return "Starting: Intent\n", 0
            case ["am", "start", "-n", _, "-a", "dev.openmob.action.CONNECT"]:
                return "Starting: Intent\n", 0
        raise AssertionError(f"unexpected adb shell command: {words}")

    def owner_confirms(self, link):
        """What the app does after its owner passes the screen-lock prompt: claim, save, connect."""
        q = parse_qs(urlsplit(link).query)
        server = q["server"][0]
        r = httpx.post(f"{self.server}/api/pair/claim",
                       json={"code": q["code"][0], "device_id": self.prefs["device_id"], "model": "SM-S948U1"})
        assert r.status_code == 200, r.text
        self.prefs["server"] = server
        self.phone = FakePhone(self.server.replace("http", "ws"), FakeDriver(), device_id=self.prefs["device_id"],
                               key=r.json()["key"])


def nap(seconds):
    time.sleep(0.05)


@pytest.fixture
def apk(tmp_path):
    path = tmp_path / "app-debug.apk"
    path.write_bytes(b"PK fake apk v1")
    return path


def _key(srv, scopes=("devices:add",)):  # noqa: F811
    srv.user("dev@example.com")
    return srv.store.create_api_key(srv.store.account_id_for("dev@example.com"), "cli", list(scopes))[1]


def test_phone_add_runs_every_step_then_is_idempotent(srv, apk):  # noqa: F811
    key = _key(srv)
    adb = FakeAdb(server=srv.base)
    lines = []
    report = phone_add(srv.base, key, apk=str(apk), run=adb, out=lines.append, sleep=nap)
    outcomes = {step: outcome for step, outcome, _ in report.rows}
    assert outcomes == {"App": "done", "Notifications": "done", "Background": "done", "Accessibility": "done",
                        "Screen sharing": "warn", "Pairing": "done", "Online": "done"}
    assert "android.permission.POST_NOTIFICATIONS" in adb.granted and PACKAGE in adb.whitelist
    # Other accessibility services are kept, ours is added once.
    services = adb.secure["enabled_accessibility_services"].split(":")
    assert services == OTHER_A11Y.split(":") + [A11Y_SERVICE]
    assert adb.appops == "allow"
    assert any("Connect this phone to" in line for line in lines) and "is ready" in lines[-1]
    assert srv.store.is_paired(srv.store.account_id_for("dev@example.com"), "uuid-1")

    # Second run: nothing to install, grant, enable or pair; no second confirmation on the phone.
    before, ran = len(adb.links), len(adb.commands)
    report = phone_add(srv.base, key, apk=str(apk), run=adb, out=lines.append, sleep=nap)
    outcomes = {step: outcome for step, outcome, _ in report.rows}
    assert outcomes["App"] == outcomes["Notifications"] == outcomes["Background"] == "already"
    assert outcomes["Accessibility"] == outcomes["Pairing"] == "already" and outcomes["Online"] == "done"
    assert len(adb.links) == before
    assert adb.secure["enabled_accessibility_services"].split(":").count(A11Y_SERVICE) == 1
    assert not any(c.startswith(("install", "shell 'settings put'", "shell 'pm grant'")) or "settings put" in c
                   for c in adb.commands[ran:])


def test_phone_add_fixes_only_what_is_missing(srv, apk):  # noqa: F811
    key = _key(srv)
    adb = FakeAdb(server=srv.base)
    phone_add(srv.base, key, apk=str(apk), run=adb, out=lambda s: None, sleep=nap)
    # Someone turned accessibility off and took the battery exemption away.
    adb.secure["enabled_accessibility_services"] = OTHER_A11Y
    adb.secure["accessibility_enabled"] = "0"
    adb.whitelist.discard(PACKAGE)
    report = phone_add(srv.base, key, apk=str(apk), run=adb, out=lambda s: None, sleep=nap)
    outcomes = {step: outcome for step, outcome, _ in report.rows}
    assert outcomes["Accessibility"] == outcomes["Background"] == "done" and outcomes["App"] == "already"
    assert adb.secure["accessibility_enabled"] == "1" and A11Y_SERVICE in adb.secure["enabled_accessibility_services"]
    assert OTHER_A11Y.split(":")[0] in adb.secure["enabled_accessibility_services"]


def test_phone_add_needs_the_on_phone_confirmation(srv, apk):  # noqa: F811
    key = _key(srv)
    adb = FakeAdb(server=srv.base, confirm=False)  # the owner never confirms on the phone
    with pytest.raises(PhoneAddError, match="wasn't confirmed"):
        phone_add(srv.base, key, apk=str(apk), run=adb, out=lambda s: None, sleep=nap, wait_paired=0.5)
    assert len(adb.links) == 1 and "code=" in adb.links[0]
    assert srv.store.list_devices(srv.store.account_id_for("dev@example.com")) == []  # not paired


def test_phone_add_needs_a_devices_add_key(srv, apk):  # noqa: F811
    key = _key(srv, scopes=("devices:read",))
    with pytest.raises(PhoneAddError, match="devices:add"):
        phone_add(srv.base, key, apk=str(apk), run=FakeAdb(server=srv.base), out=lambda s: None, sleep=nap)
    with pytest.raises(PhoneAddError, match="API key"):
        phone_add(srv.base, "", apk=str(apk), run=FakeAdb(server=srv.base), out=lambda s: None)


@pytest.mark.parametrize("devices, ioreg, message", [
    ("", "", "No phone found"),
    ("", '"idVendor" = 1256\n', "Auto Blocker"),
    ("", '"idVendor" = 6353\n', "Google phone is plugged in"),
    ("SERIAL1\tunauthorized", "", "Allow USB debugging"),
    ("A\tdevice\nB\tdevice", "", "--serial"),
])
def test_clear_errors_when_the_phone_isnt_usable(devices, ioreg, message, monkeypatch):
    monkeypatch.setattr("openmob.phone_add.sys.platform", "darwin")
    adb = FakeAdb(devices=devices, ioreg=ioreg)
    with pytest.raises(PhoneAddError, match=message):
        pick_device(Adb("adb", None, adb), None)


def test_old_android_is_refused(srv, apk):  # noqa: F811
    with pytest.raises(PhoneAddError, match="Android 11 or newer"):
        phone_add(srv.base, _key(srv), apk=str(apk), run=FakeAdb(server=srv.base, sdk=29), out=lambda s: None)


def test_missing_adb_is_explained():
    def run(*a, **k):
        raise FileNotFoundError("adb")

    with pytest.raises(PhoneAddError, match="isn't installed"):
        pick_device(Adb("adb", None, run), None)


def test_cli_wires_phone_add(monkeypatch):
    from openmob import main

    seen = {}
    monkeypatch.setattr("openmob.phone_add.phone_add", lambda server, key, **kw: seen.update(server=server, key=key, **kw))
    monkeypatch.setenv("OPENMOB_API_KEY", "om_live_x_y")
    main.main(["phone", "add", "--server", "https://openmob.example.com", "--serial", "S1"])
    assert seen["server"] == "https://openmob.example.com" and seen["key"] == "om_live_x_y" and seen["serial"] == "S1"
    assert wait_for(lambda: True)
