"""The AgentRunner abstraction.

The orchestrator depends only on this interface -- never on subprocess
details, CLI flags, or any specific invocation mechanism. This is what
lets ForgeMind's workflow engine be built and fully tested (ManualRunner)
before any AI backend is wired in, and lets the AI backend be swapped later
without touching orchestration logic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class AgentResultStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    PENDING = "pending"  # awaiting a human (ManualRunner only)


@dataclass
class AgentResult:
    status: AgentResultStatus
    output_artifact_path: Optional[Path] = None
    raw_log_path: Optional[Path] = None
    duration_seconds: float = 0.0
    runner_name: str = ""
    notes: Optional[str] = None


class AgentRunner(ABC):
    @abstractmethod
    def run_agent(
        self,
        role: str,
        task_context: dict,
        input_artifacts: list[Path],
        workspace_path: Optional[Path],
        allowed_capabilities: list[str],
    ) -> AgentResult:
        """Run one bounded stage for one role and return its result.

        Implementations must never decide workflow state -- they only
        report what happened. The orchestrator alone interprets the result.
        """
        raise NotImplementedError
