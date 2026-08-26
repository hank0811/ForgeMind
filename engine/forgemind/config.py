"""Loads config/forgemind.yaml into a small, explicit config object.

This is pure data loading -- no AI calls, no workflow logic. Retry limits
loaded here are enforced by forgemind.orchestrator, never by an agent.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class ForgeMindConfig:
    runner: str = "manual"
    max_implementation_retries: int = 2
    max_revision_retries: int = 2
    stage_timeout_seconds: int = 600
    claude_cli_executable: str = "claude"


def load_config(path: Path) -> ForgeMindConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    retry_limits = data.get("retry_limits", {}) or {}
    timeouts = data.get("timeouts", {}) or {}
    claude_cli = data.get("claude_cli", {}) or {}
    return ForgeMindConfig(
        runner=data.get("runner", "manual"),
        max_implementation_retries=retry_limits.get("max_implementation_retries", 2),
        max_revision_retries=retry_limits.get("max_revision_retries", 2),
        stage_timeout_seconds=timeouts.get("stage_timeout_seconds", 600),
        claude_cli_executable=claude_cli.get("executable", "claude"),
    )
