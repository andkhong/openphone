from openmob.config import load_config
from openmob.agent.models import model_listing
import pytest


def test_models_allowlist_always_includes_the_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model: claude-fable-5-1\nmodels:\n  - claude-haiku-5-5\n  - {id: claude-opus-5-5, label: Opus}\n"
                    "hotkey: alt_r\nwhisper_model: base.en\n")  # removed settings are ignored
    cfg = load_config(path)
    assert [m["id"] for m in cfg.models] == ["claude-fable-5-1", "claude-haiku-5-5", "claude-opus-5-5"]
    assert cfg.models[2]["label"] == "Opus"


def test_default_models(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("max_steps: 10\n")
    cfg = load_config(path)
    assert cfg.models[0]["id"] == cfg.model == "claude-sonnet-5-5"


def test_default_picker_offers_gemini_and_openai_with_key_availability(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("max_steps: 10\n")
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                 "QWEN_API_KEY", "DASHSCOPE_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-google-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-qwen-key")
    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")
    cfg = load_config(path)
    listing = {m["id"]: m for m in model_listing(cfg)["models"]}
    for model in ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gemini-3.8-flash", "qwen3.8-flash", "grok-4.7"):
        assert listing[model]["available"] is True
    assert listing["gpt-6.1-sol"]["provider"] == "openai"
    assert listing["gemini-3.8-flash"]["provider"] == "google"
    assert listing["qwen3.8-flash"]["provider"] == "qwen"
    assert listing["grok-4.7"]["provider"] == "xai"
    assert listing["claude-sonnet-5-5"]["available"] is False
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    monkeypatch.delenv("XAI_API_KEY")
    assert all(not m["available"] for m in model_listing(load_config(path))["models"])


def test_qwen_and_xai_config_keys_endpoints_and_model_inference(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("models: [qwen3.8-flash, grok-4.7]\nqwen_api_key: ignored\nxai_api_key: ignored\n")
    with pytest.raises(ValueError, match="Unknown config keys"):
        load_config(path)
    path.write_text("model: qwen3.8-flash\nmodels: [qwen3.8-flash, grok-4.7]\n")
    for name in ("QWEN_API_KEY", "DASHSCOPE_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    assert all(not m["available"] for m in model_listing(load_config(path))["models"])
    monkeypatch.setenv("QWEN_API_KEY", "test-qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope")
    monkeypatch.setenv("XAI_API_KEY", "test-xai")
    monkeypatch.setenv("QWEN_BASE_URL", "https://workspace.example/compatible-mode/v1")
    monkeypatch.setenv("XAI_BASE_URL", "https://us.api.x.ai/v1")
    cfg = load_config(path)
    assert cfg.qwen_api_key == "test-qwen" and cfg.xai_api_key == "test-xai"
    assert cfg.qwen_base_url == "https://workspace.example/compatible-mode/v1"
    assert cfg.xai_base_url == "https://us.api.x.ai/v1"
    assert [m["provider"] for m in cfg.models] == ["qwen", "xai"]
    assert all(m["available"] for m in model_listing(cfg)["models"] if m["provider"] in ("qwen", "xai"))
