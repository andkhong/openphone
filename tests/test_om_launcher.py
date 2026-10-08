"""The environment launcher must serve the same cached APK that `om phone` installs."""

import os
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("environment", ["standard", "gatekeeper"])
@pytest.mark.parametrize("explicit_apk", [False, True])
def test_run_passes_the_environment_apk_to_the_server(tmp_path, environment, explicit_apk):
    repo = tmp_path / "checkout"
    interpreter = repo / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nprintf "%s" "$OPENMOB_APK" > "$OPENMOB_TEST_REPORT"\n')
    interpreter.chmod(0o755)
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, body in (("lsof", "exit 1"), ("git", "echo test-build")):
        stub = commands / name
        stub.write_text("#!/bin/sh\n" + body + "\n")
        stub.chmod(0o755)
    cache = tmp_path / "envs/apk"
    cache.mkdir(parents=True)
    apk = cache / f"{environment}.apk"
    apk.write_bytes(b"cached app")
    report = tmp_path / "served-apk"
    env = {**os.environ, "PATH": f"{commands}:{os.environ['PATH']}", "OPENMOB_ENVS_HOME": str(cache.parent),
           "OPENMOB_STANDARD_DIR": str(repo), "OPENMOB_GATEKEEPER_DIR": str(repo),
           "OPENMOB_STANDARD_DATA": str(tmp_path / "standard-data"),
           "OPENMOB_GATEKEEPER_DATA": str(tmp_path / "gatekeeper-data"), "OPENMOB_TEST_REPORT": str(report)}
    env.pop("OPENMOB_APK", None)
    expected = str(apk)
    if explicit_apk:
        expected = str(tmp_path / "custom.apk")
        env["OPENMOB_APK"] = expected
    launcher = Path(__file__).resolve().parents[1] / "scripts/om"
    subprocess.run(["bash", str(launcher), "run", environment], env=env, check=True, capture_output=True)
    assert report.read_text() == expected


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_run_loads_saved_provider_settings_only_from_a_private_file(tmp_path, mode):
    repo = tmp_path / "checkout"
    interpreter = repo / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    package = repo / "openmob"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "config.py").write_text("def load_config(path=None): return None\n")
    settings = {"QWEN_API_KEY": "test-qwen", "DASHSCOPE_API_KEY": "test-dashscope", "XAI_API_KEY": "test-xai",
                "QWEN_BASE_URL": "https://workspace.example/compatible-mode/v1", "XAI_BASE_URL": "https://us.api.x.ai/v1"}
    (package / "main.py").write_text(
        "import json, os\nfrom pathlib import Path\nfrom .config import load_config\n"
        f"def main(): Path(os.environ['OPENMOB_TEST_REPORT']).write_text(json.dumps({{k: os.environ.get(k) for k in {list(settings)!r}}}))\n")
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, body in (("lsof", "exit 1"), ("git", "echo test-build")):
        stub = commands / name
        stub.write_text("#!/bin/sh\n" + body + "\n")
        stub.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir()
    keys = data / "provider-keys.env"
    keys.write_text("\n".join(f"export {k}={v}" for k, v in settings.items()))
    keys.chmod(mode)
    report = tmp_path / "provider-settings"
    env = {**os.environ, "PATH": f"{commands}:{os.environ['PATH']}", "OPENMOB_ENVS_HOME": str(tmp_path / "envs"),
           "OPENMOB_STANDARD_DIR": str(repo), "OPENMOB_STANDARD_DATA": str(data), "OPENMOB_TEST_REPORT": str(report)}
    for name in settings:
        env.pop(name, None)
    launcher = Path(__file__).resolve().parents[1] / "scripts/om"
    run = subprocess.run(["bash", str(launcher), "run", "standard"], env=env, capture_output=True, text=True)
    if mode == 0o600:
        assert run.returncode == 0 and json.loads(report.read_text()) == settings
        assert "ANTHROPIC_API_KEY is set neither" in run.stderr  # no Claude key among these settings
        assert "TYPESAFE_API_KEY is set neither" in run.stderr  # nor Jev's
    else:
        assert run.returncode != 0 and "chmod 600" in run.stderr and not report.exists()
