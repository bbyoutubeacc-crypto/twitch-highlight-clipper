from clipper.config import load_config


def test_load_repo_config():
    cfg = load_config()
    assert cfg.clips.pre_seconds == 30
    assert cfg.clips.post_seconds == 15
    assert "KEKW" in cfg.detection.hype_terms
    assert cfg.recording.buffer_seconds >= cfg.clips.pre_seconds + cfg.clips.post_seconds


def test_partial_config_and_validation(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("clips:\n  pre_seconds: 100\n  format: quer\nunknown_key: 1\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.clips.format == "16:9"
    assert cfg.recording.buffer_seconds >= 100 + 15
    assert cfg.detection.threshold == 3.0
