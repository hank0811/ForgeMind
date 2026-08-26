"""Generic stage orchestration mechanics.

This module deliberately contains no agent-specific content, prompts, or
schemas -- the five V1 roles (analyst, architect_planner, implementer,
tester, reviewer) are represented here only as pure workflow metadata
(which state their work is "in progress" under, which state a success
lands in). Wiring real agent execution to these roles is a later phase;
Phase 1 only needs the mechanics to exist and be provably correct via
ManualRunner.

Ownership, per the agreed architecture:
- The orchestrator (this module) decides every state transition, every
  retry, and every governance check.
- The AgentRunner it calls only ever reports a fact (ok/error/timeout/
  pending) about one bounded stage. It never decides what happens next.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from forgemind import artifacts, governance
from forgemind import task as task_module
from forgemind.config import ForgeMindConfig
from forgemind.runners.base import AgentResult, AgentResultStatus, AgentRunner
from forgemind.runners.manual_runner import ManualRunner
from forgemind.state_machine import TERMINAL_STATES, TaskState, TaskStateMachine

# Pure workflow metadata: which state a role's work is "in progress" under,
# and which state a successful result lands in. No agent behavior here.
STAGE_MAP: dict[str, dict[str, TaskState]] = {
    "analyst": {"in_progress": TaskState.ANALYZING, "success": TaskState.ANALYZED},
    "architect_planner": {"in_progress": TaskState.DESIGNING, "success": TaskState.DESIGNED},
    "implementer": {"in_progress": TaskState.IMPLEMENTING, "success": TaskState.IMPLEMENTED},
    "tester": {
        "in_progress": TaskState.TESTING,
        "success": TaskState.TESTS_PASSED,
        "failure": TaskState.TESTS_FAILED,
    },
    "reviewer": {"in_progress": TaskState.REVIEWING, "success": TaskState.REVIEWED},
    "finalizer": {"in_progress": TaskState.FINALIZING, "success": TaskState.COMPLETED},
}

# Minimal, generic artifact contract shared by every role until real agent
# schemas exist. Every submitted artifact must declare its outcome via this
# one frontmatter field -- Python reads it as a fact, never as a decision.
REQUIRED_ARTIFACT_FIELDS: dict[str, list[str]] = {
    "analyst": ["status"],
    "architect_planner": ["status"],
    "implementer": ["status"],
    "tester": ["status"],
    "reviewer": ["status"],
    "finalizer": ["status"],
}

# Canonical location a submitted artifact is copied into (artifacts/<task_id>/...),
# per the filenames already named in the agreed architecture. This gives
# governance checkpoints a stable, known path to read from later, without
# adding any new persisted state.
ARTIFACT_FILENAMES: dict[str, str] = {
    "analyst": "01_analysis.md",
    "architect_planner": "02_design_plan.md",
    "implementer": "03_implementation.md",
    "tester": "04_test_report.md",
    "reviewer": "05_review.md",
    "finalizer": "06_final_report.md",
}


def _build_task_context(tasks_root: Path, task_id: str) -> dict:
    request = task_module.read_task_request(tasks_root, task_id)
    return {"task_id": task_id, "request": request}


def start_stage(
    sm: TaskStateMachine,
    role: str,
    runner: AgentRunner,
    input_artifacts: list[Path],
    workspace_path: Optional[Path] = None,
    allowed_capabilities: Optional[list[str]] = None,
) -> AgentResult:
    """Move a task into a stage's in-progress state and invoke the runner.

    The orchestrator only ever talks to `runner` through the AgentRunner
    interface -- it has no idea whether that's a human or an automated
    adapter, and that is the point.

    If the task is already sitting in the stage's in-progress state, the
    transition is skipped rather than attempted (a self-transition is not
    a legal state change). This is what lets a retry loop-back --
    retry_after_test_failure()/retry_after_review_revision() both land on
    IMPLEMENTING -- re-invoke the same role via this same function instead
    of needing a separate "resume" entry point.
    """
    stage = STAGE_MAP[role]
    if sm.state != stage["in_progress"]:
        sm.transition(stage["in_progress"])
    context = _build_task_context(sm.tasks_root, sm.task_id)
    result = runner.run_agent(
        role=role,
        task_context=context,
        input_artifacts=input_artifacts,
        workspace_path=workspace_path,
        allowed_capabilities=allowed_capabilities or [],
    )
    if result.status == AgentResultStatus.PENDING:
        sm.transition(TaskState.BLOCKED)
    elif result.status in (AgentResultStatus.ERROR, AgentResultStatus.TIMEOUT):
        sm.transition(TaskState.FAILED, reason=f"{role} runner returned {result.status.value}")
    # OK: caller validates the artifact and calls complete_stage() explicitly.
    return result


def complete_stage(
    sm: TaskStateMachine,
    role: str,
    artifact_path: Path,
    required_fields: list[str],
    target_state: Optional[TaskState] = None,
) -> list[str]:
    """Validate a produced artifact and, if valid, transition to its target state.

    `target_state` defaults to the role's success state, but callers may
    override it (e.g. the tester stage landing on TESTS_FAILED instead of
    TESTS_PASSED) based on the artifact's *content* -- a fact the caller
    read, not a decision the agent made.
    """
    errors = artifacts.validate_artifact_file(artifact_path, required_fields)
    if errors:
        return errors
    stage = STAGE_MAP[role]
    sm.transition(target_state or stage["success"])
    return []


def resume_blocked_stage(sm: TaskStateMachine) -> TaskState:
    """Resume a BLOCKED task back into the in-progress state it was blocked from."""
    if sm.state != TaskState.BLOCKED or sm.blocked_from is None:
        raise ValueError(f"Task {sm.task_id} is not blocked")
    resume_state = sm.blocked_from
    sm.transition(resume_state)
    return resume_state


def retry_after_test_failure(sm: TaskStateMachine, config: ForgeMindConfig) -> TaskState:
    """Called when sm.state == TESTS_FAILED. Enforces the retry limit deterministically."""
    if sm.implementation_retry_count >= config.max_implementation_retries:
        sm.transition(TaskState.FAILED, reason="implementation retry limit exceeded")
        return TaskState.FAILED
    sm.increment_implementation_retry()
    sm.transition(TaskState.IMPLEMENTING, reason="retry after test failure")
    return TaskState.IMPLEMENTING


def retry_after_review_revision(sm: TaskStateMachine, config: ForgeMindConfig) -> TaskState:
    """Called when sm.state == REVIEWING and the review requested changes."""
    if sm.revision_retry_count >= config.max_revision_retries:
        sm.transition(TaskState.FAILED, reason="revision retry limit exceeded")
        return TaskState.FAILED
    sm.increment_revision_retry()
    sm.transition(TaskState.IMPLEMENTING, reason="retry after review revision")
    return TaskState.IMPLEMENTING


# ---------------------------------------------------------------------------
# Phase 2: human-driven submission, approval/rejection, and governance
# wiring. Everything below still only ever mutates state through
# TaskStateMachine.transition() and the retry helpers above -- an artifact
# file is read for facts, never trusted to change state.json itself.
# ---------------------------------------------------------------------------


def apply_plan_governance_checkpoint(
    sm: TaskStateMachine,
    plan_artifact_path: Path,
    governance_config: Optional[governance.GovernanceConfig],
) -> Optional[governance.GovernanceResult]:
    """Called once the architect_planner stage has landed on DESIGNED.

    Always enters AWAITING_PLAN_APPROVAL first (the audit trail always shows
    a plan was evaluated). If governance found nothing sensitive, Python
    immediately performs the same PLAN_APPROVED transition a human approval
    would perform -- no bypass edge, no new state, just Python choosing not
    to wait. If it found something sensitive, or governance wasn't supplied
    at all, the task waits at AWAITING_PLAN_APPROVAL for approve_task/reject_task.
    """
    result: Optional[governance.GovernanceResult] = None
    if governance_config is not None:
        _frontmatter, body = artifacts.read_markdown(plan_artifact_path)
        result = governance.evaluate(body, governance_config)

    if result is None:
        sm.transition(
            TaskState.AWAITING_PLAN_APPROVAL,
            reason="governance not evaluated; human approval required",
        )
    elif result.sensitive:
        names = ", ".join(m.name for m in result.matches)
        sm.transition(
            TaskState.AWAITING_PLAN_APPROVAL,
            reason=f"governance: sensitive actions detected ({names}); human approval required",
        )
    else:
        sm.transition(
            TaskState.AWAITING_PLAN_APPROVAL, reason="governance: no sensitive actions detected"
        )
        sm.transition(
            TaskState.PLAN_APPROVED, reason="auto-approved: governance found nothing sensitive"
        )
    return result


def apply_final_governance_checkpoint(
    sm: TaskStateMachine,
    implementation_artifact_path: Path,
    governance_config: Optional[governance.GovernanceConfig],
) -> Optional[governance.GovernanceResult]:
    """Called once the reviewer stage has approved (landed on REVIEWED).

    Evaluates the *implementer's* artifact -- the actual implementation
    content -- not the reviewer's own commentary. Same wait-vs-auto-approve
    policy as the plan checkpoint.
    """
    result: Optional[governance.GovernanceResult] = None
    if governance_config is not None:
        _frontmatter, body = artifacts.read_markdown(implementation_artifact_path)
        result = governance.evaluate(body, governance_config)

    if result is None:
        sm.transition(
            TaskState.AWAITING_FINAL_APPROVAL,
            reason="governance not evaluated; human approval required",
        )
    elif result.sensitive:
        names = ", ".join(m.name for m in result.matches)
        sm.transition(
            TaskState.AWAITING_FINAL_APPROVAL,
            reason=f"governance: sensitive actions detected ({names}); human approval required",
        )
    else:
        sm.transition(
            TaskState.AWAITING_FINAL_APPROVAL, reason="governance: no sensitive actions detected"
        )
        sm.transition(
            TaskState.FINAL_APPROVED, reason="auto-approved: governance found nothing sensitive"
        )
    return result


def submit_artifact(
    sm: TaskStateMachine,
    artifacts_root: Path,
    stage: str,
    submitted_path: Path,
    runner: ManualRunner,
    config: ForgeMindConfig,
    governance_config: Optional[governance.GovernanceConfig] = None,
) -> list[str]:
    """Handle `forgemind submit-artifact <task_id> <stage> <path>`.

    Takes an already-loaded TaskStateMachine, exactly like start_stage(),
    complete_stage(), and approve_task()/reject_task() -- there is exactly
    one way to load task state in this codebase, and it happens once, in
    the caller. Validates everything before touching any state, then
    drives the real transition entirely through the existing
    state-machine-owned helpers. Returns a list of error strings; empty
    means success.
    """
    task_id = sm.task_id
    tasks_root = sm.tasks_root

    if stage not in STAGE_MAP:
        return [f"unknown stage: {stage} (expected one of {sorted(STAGE_MAP)})"]

    if sm.state != TaskState.BLOCKED:
        return [
            f"task {task_id} is not awaiting a manual submission "
            f"(current state: {sm.state.value})"
        ]

    pending_path = tasks_root / task_id / "pending_prompt.md"
    if not pending_path.exists():
        return [f"task {task_id} has no pending prompt to submit against"]

    pending_frontmatter, _pending_body = artifacts.read_markdown(pending_path)
    expected_role = pending_frontmatter.get("role")
    if stage != expected_role:
        return [f"task {task_id} is awaiting a '{expected_role}' submission, not '{stage}'"]

    if not submitted_path.exists():
        return [f"submitted artifact does not exist: {submitted_path}"]

    required_fields = REQUIRED_ARTIFACT_FIELDS.get(stage, ["status"])
    errors = artifacts.validate_artifact_file(submitted_path, required_fields)
    if errors:
        return errors

    # Install into the canonical artifact location. This file is a fact
    # record for humans and for governance to read later -- it never
    # touches state.json directly; only the transitions below do that.
    frontmatter, body = artifacts.read_markdown(submitted_path)
    canonical_path = artifacts_root / task_id / ARTIFACT_FILENAMES[stage]
    artifacts.write_markdown(canonical_path, frontmatter, body)

    runner.complete_pending(task_id, canonical_path)
    resume_blocked_stage(sm)

    status = frontmatter.get("status")

    if stage == "tester":
        if status == "failed":
            complete_stage(
                sm, stage, canonical_path, required_fields, target_state=TaskState.TESTS_FAILED
            )
            retry_after_test_failure(sm, config)
        else:
            complete_stage(sm, stage, canonical_path, required_fields)
    elif stage == "reviewer":
        if status == "changes_requested":
            retry_after_review_revision(sm, config)
        else:
            complete_stage(sm, stage, canonical_path, required_fields)
            impl_path = artifacts_root / task_id / ARTIFACT_FILENAMES["implementer"]
            apply_final_governance_checkpoint(sm, impl_path, governance_config)
    elif stage == "architect_planner":
        complete_stage(sm, stage, canonical_path, required_fields)
        apply_plan_governance_checkpoint(sm, canonical_path, governance_config)
    else:
        complete_stage(sm, stage, canonical_path, required_fields)

    return []


def approve_task(sm: TaskStateMachine) -> list[str]:
    """Handle `forgemind approve <task_id>`.

    Legal only from AWAITING_PLAN_APPROVAL or AWAITING_FINAL_APPROVAL --
    both transitions it performs already exist in state_machine.TRANSITIONS.
    """
    if sm.state == TaskState.AWAITING_PLAN_APPROVAL:
        sm.transition(TaskState.PLAN_APPROVED, reason="approved by human")
        return []
    if sm.state == TaskState.AWAITING_FINAL_APPROVAL:
        sm.transition(TaskState.FINAL_APPROVED, reason="approved by human")
        return []
    return [
        f"task {sm.task_id} is not awaiting approval (current state: {sm.state.value}); "
        f"approval is only valid from AWAITING_PLAN_APPROVAL or AWAITING_FINAL_APPROVAL"
    ]


def reject_task(sm: TaskStateMachine, reason: str) -> list[str]:
    """Handle `forgemind reject <task_id> --reason "<reason>"`.

    PLAN_REJECTED and FINAL_REJECTED are both terminal states (per
    state_machine.TERMINAL_STATES), so the state machine itself releases
    the task lock as soon as this transition lands.
    """
    if not reason or not reason.strip():
        return ["a reason is required to reject a task"]
    if sm.state == TaskState.AWAITING_PLAN_APPROVAL:
        sm.transition(TaskState.PLAN_REJECTED, reason=reason)
        return []
    if sm.state == TaskState.AWAITING_FINAL_APPROVAL:
        sm.transition(TaskState.FINAL_REJECTED, reason=reason)
        return []
    return [
        f"task {sm.task_id} is not awaiting approval (current state: {sm.state.value}); "
        f"rejection is only valid from AWAITING_PLAN_APPROVAL or AWAITING_FINAL_APPROVAL"
    ]


# ---------------------------------------------------------------------------
# Phase 8: automatic end-to-end progression through all five core roles,
# extended in Phase 9 with the Finalizer (FINAL_APPROVED -> FINALIZING ->
# COMPLETED).
#
# This is a pure driver -- it contains no new transitions, no new retry
# semantics, and no new governance logic. Every state change it causes goes
# through start_stage(), complete_stage(), retry_after_test_failure(),
# retry_after_review_revision(), apply_plan_governance_checkpoint(), or
# apply_final_governance_checkpoint(), exactly as a human driving the CLI
# stage by stage already would. It only decides *when* to call each of
# those, based on the current TaskState, and stops the instant a state is
# reached that this codebase does not yet know how to move past on its own
# (an approval checkpoint, or a terminal state -- COMPLETED included).
# ---------------------------------------------------------------------------

#: Default least-privilege tool sets per role, matching the roles' own
#: agents/<role>.md constraints. Callers may override per role.
#: Every role's agents/<role>.md requires it to write exactly one output
#: artifact, regardless of how otherwise-restricted its role is -- so
#: every entry needs "Write", not just the roles that also edit workspace
#: code. Claude Code's tool permission model is not path-scoped (granting
#: "Write" allows writing any file within --add-dir/cwd, not just the one
#: named artifact); the single-file restriction is enforced by prompt
#: instruction plus Python's post-hoc artifact validation, exactly as it
#: already was for Implementer's broader Write access. Discovered missing
#: for 5 of 6 roles in Phase 11 via a real (non-mocked) Claude run, whose
#: Analyst was denied permission to write its own required artifact.
#: Implementer and Tester are the only two roles whose own agents/*.md
#: require running real commands (validation checks; tests/builds/lint).
#: Both "Bash" and "PowerShell" are listed because Claude Code's actual
#: shell-execution tool self-identifies differently per platform -- on
#: this Windows machine it is literally named "PowerShell" (confirmed via
#: a real permission_denials log in Phase 11, and empirically verified in
#: Phase 12: granting "Bash" alone left every real command denied, while
#: adding "PowerShell" let the same command run for real, with zero
#: denials). Listing both is harmless: an inapplicable name in an
#: allowlist is simply never matched, on either platform. No other role
#: gains a shell tool -- each of their own agents/*.md explicitly
#: forbids running commands.
DEFAULT_CAPABILITIES: dict[str, list[str]] = {
    "analyst": ["Read", "Grep", "Glob", "Write"],
    "architect_planner": ["Read", "Grep", "Glob", "Write"],
    "implementer": ["Read", "Edit", "Write", "Bash", "PowerShell"],
    "tester": ["Read", "Bash", "PowerShell", "Write"],
    "reviewer": ["Read", "Grep", "Write"],
    "finalizer": ["Read", "Write"],
}

#: States where a human must act next (an approval gate, or a ManualRunner
#: stage awaiting manual submission). The driver always stops here.
_AWAITING_HUMAN_STATES = frozenset(
    {
        TaskState.BLOCKED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.AWAITING_FINAL_APPROVAL,
    }
)

_DEFAULT_MAX_ITERATIONS = 50


@dataclass
class PipelineOutcome:
    stopped_state: TaskState
    reason: str
    iterations: int


def _artifact_path(artifacts_root: Path, task_id: str, role: str) -> Path:
    return artifacts_root / task_id / ARTIFACT_FILENAMES[role]


def _advance_after_stage_result(
    sm: TaskStateMachine,
    role: str,
    result: AgentResult,
    config: ForgeMindConfig,
) -> None:
    """After start_stage() returns, decide which existing completion/retry
    function applies. PENDING/ERROR/TIMEOUT need nothing further here --
    start_stage() already transitioned to BLOCKED/FAILED itself. For OK,
    this reuses exactly the role-dispatch already established and tested
    for the ClaudeCodeCLIRunner path in Phases 5-7: read the artifact's own
    reported `status`, then call the one existing function that fact maps
    to. This function never writes state.json itself.
    """
    if result.status != AgentResultStatus.OK:
        return

    required_fields = REQUIRED_ARTIFACT_FIELDS.get(role, ["status"])
    errors = artifacts.validate_artifact_file(result.output_artifact_path, required_fields)
    if errors:
        sm.transition(TaskState.FAILED, reason=f"{role} artifact failed validation: {errors}")
        return

    frontmatter, _body = artifacts.read_markdown(result.output_artifact_path)
    status = frontmatter.get("status")

    if role == "tester" and status == "failed":
        complete_stage(sm, role, result.output_artifact_path, required_fields, target_state=TaskState.TESTS_FAILED)
    elif role == "reviewer" and status == "changes_requested":
        retry_after_review_revision(sm, config)
    else:
        complete_stage(sm, role, result.output_artifact_path, required_fields)


def _run_and_advance(
    sm: TaskStateMachine,
    role: str,
    runner: AgentRunner,
    input_artifacts: list[Path],
    workspace_path: Optional[Path],
    allowed_capabilities: list[str],
    config: ForgeMindConfig,
) -> AgentResult:
    result = start_stage(sm, role, runner, input_artifacts, workspace_path, allowed_capabilities)
    _advance_after_stage_result(sm, role, result, config)
    return result


def run_full_pipeline(
    sm: TaskStateMachine,
    runner: AgentRunner,
    artifacts_root: Path,
    workspace_path: Optional[Path],
    config: ForgeMindConfig,
    governance_config: Optional[governance.GovernanceConfig] = None,
    capabilities: Optional[dict[str, list[str]]] = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
) -> PipelineOutcome:
    """Automatically drive one task through Analyst -> Architect-Planner ->
    Implementer -> Tester -> Reviewer -> Finalizer, reusing only the
    existing, already-tested primitives above.

    Takes an already-loaded TaskStateMachine, exactly like every other
    function in this module -- task creation and state loading happen once,
    in the caller. Safe to call again on a task that previously stopped:
    it always acts on whatever the current state actually is, so resuming
    after a human calls approve_task()/reject_task() (or after fixing a
    FAILED task's cause and creating a new attempt) just works. Calling it
    again on an already-COMPLETED task is a safe no-op (0 iterations).

    Stops and returns without raising when:
    - the task reaches a terminal state (COMPLETED, FAILED, CANCELLED,
      PLAN_REJECTED, FINAL_REJECTED) -- COMPLETED is the normal successful
      end of the whole pipeline,
    - the task reaches a state requiring a human (BLOCKED,
      AWAITING_PLAN_APPROVAL, AWAITING_FINAL_APPROVAL),
    - or `max_iterations` is exceeded (a safety net against a runaway
      loop; real retry loops are already bounded by config and will hit
      FAILED long before this triggers under any reasonable configuration).
    """
    caps = dict(DEFAULT_CAPABILITIES)
    if capabilities:
        caps.update(capabilities)

    task_id = sm.task_id

    def artifact(role: str) -> Path:
        return _artifact_path(artifacts_root, task_id, role)

    for iteration in range(1, max_iterations + 1):
        state = sm.state

        if state in TERMINAL_STATES:
            return PipelineOutcome(state, "terminal", iteration - 1)
        if state in _AWAITING_HUMAN_STATES:
            return PipelineOutcome(state, "awaiting_human_action", iteration - 1)

        if state == TaskState.CREATED:
            _run_and_advance(sm, "analyst", runner, [], workspace_path, caps["analyst"], config)

        elif state == TaskState.ANALYZED:
            _run_and_advance(
                sm, "architect_planner", runner, [artifact("analyst")], workspace_path,
                caps["architect_planner"], config,
            )

        elif state == TaskState.DESIGNED:
            apply_plan_governance_checkpoint(sm, artifact("architect_planner"), governance_config)

        elif state in (TaskState.PLAN_APPROVED, TaskState.IMPLEMENTING):
            # IMPLEMENTING is reachable here only via a retry loop-back
            # (retry_after_test_failure / retry_after_review_revision both
            # land on IMPLEMENTING) -- start_stage() resolves a fresh
            # IMPLEMENTING synchronously within one call, so the driver
            # never observes it mid-flight otherwise.
            _run_and_advance(
                sm, "implementer", runner,
                [artifact("analyst"), artifact("architect_planner")],
                workspace_path, caps["implementer"], config,
            )

        elif state == TaskState.IMPLEMENTED:
            _run_and_advance(
                sm, "tester", runner,
                [artifact("analyst"), artifact("architect_planner"), artifact("implementer")],
                workspace_path, caps["tester"], config,
            )

        elif state == TaskState.TESTS_FAILED:
            retry_after_test_failure(sm, config)

        elif state == TaskState.TESTS_PASSED:
            _run_and_advance(
                sm, "reviewer", runner,
                [
                    artifact("analyst"), artifact("architect_planner"),
                    artifact("implementer"), artifact("tester"),
                ],
                workspace_path, caps["reviewer"], config,
            )

        elif state == TaskState.REVIEWED:
            apply_final_governance_checkpoint(sm, artifact("implementer"), governance_config)

        elif state in (TaskState.FINAL_APPROVED, TaskState.FINALIZING):
            # FINALIZING is included for the same reason IMPLEMENTING is:
            # if the finalizer runner ever returns PENDING, start_stage()
            # moves the task to BLOCKED and this driver stops there (its
            # normal human-checkpoint behavior). A later, separate call to
            # this function -- after something outside this driver (e.g.
            # `forgemind submit-artifact`) calls resume_blocked_stage() and
            # moves BLOCKED back to FINALIZING -- will land in this branch
            # and correctly re-invoke the finalizer, thanks to
            # start_stage()'s existing skip-if-already-in-progress check.
            _run_and_advance(
                sm, "finalizer", runner,
                [
                    artifact("analyst"), artifact("architect_planner"),
                    artifact("implementer"), artifact("tester"), artifact("reviewer"),
                ],
                workspace_path, caps["finalizer"], config,
            )

        else:
            # DESIGNING/TESTING/REVIEWING mid-flight (e.g. a ManualRunner
            # left the task BLOCKED before completing a stage) or any other
            # state this driver has no defined next action for. Stop
            # rather than guess at a transition.
            return PipelineOutcome(state, f"no automatic action defined for {state.value}", iteration - 1)

    return PipelineOutcome(sm.state, "max_iterations_reached", max_iterations)
