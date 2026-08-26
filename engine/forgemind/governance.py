"""Deterministic, rule-based sensitive-action detection.

Python is the sole authority on whether an action is sensitive. This module
never trusts an agent's self-report -- it matches actual plan/diff text
against explicit patterns loaded from config/governance.yaml.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SensitivePattern:
    name: str
    regex: str
    reason: str


@dataclass
class GovernanceConfig:
    patterns: list[SensitivePattern]
    approval_required_stages: list[str]


@dataclass
class GovernanceMatch:
    name: str
    reason: str
    matched_text: str


@dataclass
class GovernanceResult:
    sensitive: bool
    matches: list[GovernanceMatch]


def load_governance(path: Path) -> GovernanceConfig:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    patterns = [
        SensitivePattern(name=p["name"], regex=p["regex"], reason=p.get("reason", ""))
        for p in data.get("sensitive_patterns", []) or []
    ]
    return GovernanceConfig(
        patterns=patterns,
        approval_required_stages=data.get("approval_required_stages", []) or [],
    )


def evaluate(text: str, config: GovernanceConfig) -> GovernanceResult:
    matches: list[GovernanceMatch] = []
    for pattern in config.patterns:
        m = re.search(pattern.regex, text, flags=re.IGNORECASE)
        if m:
            matches.append(
                GovernanceMatch(name=pattern.name, reason=pattern.reason, matched_text=m.group(0))
            )
    return GovernanceResult(sensitive=bool(matches), matches=matches)
