from openmob.config import load_config


def test_models_allowlist_always_includes_the_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model: claude-fable-5-1\nmodels:\n  - claude-haiku-4-5-20251001\n  - {id: claude-opus-5-5, label: Opus}\n"
                    "hotkey: alt_r\nwhisper_model: base.en\n")  # removed settings are ignored
    cfg = load_config(path)
    assert [m["id"] for m in cfg.models] == ["claude-fable-5-1", "claude-haiku-4-5-20251001", "claude-opus-5-5"]
    assert cfg.models[2]["label"] == "Opus"


def test_default_models(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("max_steps: 10\n")
    cfg = load_config(path)
    assert cfg.models[0]["id"] == cfg.model == "claude-sonnet-5-5"
