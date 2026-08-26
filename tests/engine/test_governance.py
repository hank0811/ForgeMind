from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forgemind import governance


@pytest.fixture
def gov_config(tmp_path: Path) -> governance.GovernanceConfig:
    data = {
        "sensitive_patterns": [
            {"name": "git_push", "regex": r"git\s+push", "reason": "Pushes commits to a remote"},
            {"name": "force_delete", "regex": r"rm\s+-rf", "reason": "Recursive force delete"},
        ],
        "approval_required_stages": ["plan", "final"],
    }
    path = tmp_path / "governance.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return governance.load_governance(path)


def test_load_governance_parses_patterns(gov_config):
    names = {p.name for p in gov_config.patterns}
    assert names == {"git_push", "force_delete"}
    assert gov_config.approval_required_stages == ["plan", "final"]


def test_evaluate_detects_sensitive_action(gov_config):
    result = governance.evaluate("Step 3: run `git push origin main`", gov_config)
    assert result.sensitive is True
    assert result.matches[0].name == "git_push"


def test_evaluate_no_match_on_safe_text(gov_config):
    result = governance.evaluate("Step 1: read the file and summarize it.", gov_config)
    assert result.sensitive is False
    assert result.matches == []


def test_evaluate_is_case_insensitive(gov_config):
    result = governance.evaluate("GIT PUSH origin main", gov_config)
    assert result.sensitive is True


def test_evaluate_detects_multiple_matches(gov_config):
    result = governance.evaluate("run rm -rf /workspace then git push", gov_config)
    names = {m.name for m in result.matches}
    assert names == {"force_delete", "git_push"}


def test_load_governance_from_real_config_file():
    """Sanity check against the actual config/governance.yaml shipped with the repo."""
    repo_root = Path(__file__).resolve().parents[2]
    config = governance.load_governance(repo_root / "config" / "governance.yaml")
    assert any(p.name == "git_push" for p in config.patterns)
    result = governance.evaluate("git push origin feature-branch", config)
    assert result.sensitive is True
