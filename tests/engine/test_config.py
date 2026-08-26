from __future__ import annotations

from pathlib import Path

from forgemind.config import load_config


def test_load_config_applies_overrides_and_defaults(tmp_path: Path):
    path = tmp_path / "forgemind.yaml"
    path.write_text(
        "runner: manual\n"
        "retry_limits:\n"
        "  max_implementation_retries: 3\n"
        "timeouts:\n"
        "  stage_timeout_seconds: 120\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.runner == "manual"
    assert config.max_implementation_retries == 3
    assert config.max_revision_retries == 2  # default, not overridden
    assert config.stage_timeout_seconds == 120


def test_load_config_from_real_config_file():
    repo_root = Path(__file__).resolve().parents[2]
    config = load_config(repo_root / "config" / "forgemind.yaml")
    assert config.runner == "manual"
    assert config.max_implementation_retries == 2
    assert config.max_revision_retries == 2
