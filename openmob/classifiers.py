"""Select the experimental classifier without changing the default Jev behavior."""
from .jev import JevClient, JevJudge


def make_judge(cfg):
    if not cfg.jev_enabled:
        return None
    if cfg.classifier_provider == "clef":
        from .clef import ClefClient, ClefJudge
        return ClefJudge(ClefClient(cfg.clef_api_key, cfg.clef_account_id, cfg.clef_model))
    if cfg.classifier_provider != "jev":
        raise ValueError("classifier_provider must be jev or clef")
    return JevJudge(JevClient(cfg.jev_api_key, cfg.jev_model, cfg.jev_base_url)) if cfg.jev_api_key else None
