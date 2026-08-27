# ForgeMind

ForgeMind is a deterministic, Python-owned orchestration engine for running a
software-engineering task through six specialized AI roles — **Analyst**,
**Architect-Planner**, **Implementer**, **Tester**, **Reviewer**, and
**Finalizer** — with governance approval gates and bounded retries. Python
owns every state transition; each AI role only ever reports a fact
(ok / error / timeout / pending) about one bounded stage.

## How it works

A task moves through the state machine one stage at a time:

```
ANALYZING → DESIGNING → (plan approval) → IMPLEMENTING → TESTING
  → REVIEWING → (final approval) → FINALIZING → COMPLETED
```

- **Governance gates**: the plan and the final result each pause the task
  for human approval (`forgemind approve` / `forgemind reject`), unless the
  rule-based governance config in `config/governance.yaml` auto-clears them.
- **Retries**: a `TESTS_FAILED` result sends the task back to `IMPLEMENTING`
  (up to `max_implementation_retries`); a review requesting changes does the
  same (up to `max_revision_retries`). Limits are set in
  `config/forgemind.yaml`.
- **Artifacts**: every role writes one Markdown file with a YAML frontmatter
  `status` field into `artifacts/<task_id>/` (`01_analysis.md`,
  `02_design_plan.md`, `03_implementation.md`, `04_test_report.md`,
  `05_review.md`, `06_final_report.md`).

## Install

Requires Python >= 3.11.

```bash
pip install -e ./engine
```

This installs the `forgemind` console command (`forgemind.cli:main`).

## Configuration

- `config/forgemind.yaml` — which `AgentRunner` to use (`manual` or
  `claude_cli`), retry limits, and stage timeout. `runner: manual` (the
  default) requires no AI access at all and drives the state machine purely
  through human-submitted artifacts. `runner: claude_cli` invokes a real,
  authenticated Claude Code CLI (`claude` must be on `PATH`) for every role.
- `config/governance.yaml` — regex patterns that mark plan/implementation
  text as sensitive, and which stages (`plan`, `final`) require approval.

## CLI usage

```bash
# Create a new task from a request; prints the new task_id
forgemind new "Add input validation to the signup form"

# Inspect a task's current state
forgemind status <task_id>

# Run the automatic pipeline (requires runner: claude_cli, or manual
# artifacts already submitted for the current stage) until it completes
# or stops for human input
forgemind run <task_id> [--workspace <path>]

# Manually submit an artifact for the task's current pending stage
# (used with runner: manual)
forgemind submit-artifact <task_id> <stage> <path-to-artifact.md>
# <stage> is one of: analyst, architect_planner, finalizer, implementer,
# reviewer, tester

# Approve or reject a task paused at a governance checkpoint
forgemind approve <task_id>
forgemind reject <task_id> --reason "<reason>"
```

`forgemind run` exits `0` if the task reached `COMPLETED` or is safely
paused awaiting human action (approval, or a manual artifact submission),
and `1` on any other stop reason (e.g. a genuine agent/tooling failure).

## Complete walkthrough (manual runner)

`runner: manual` needs no AI access, so this sequence works right after
`pip install -e ./engine`. Only one task may be active at a time.

```bash
# 1. Create the task
TASK_ID=$(forgemind new "Add input validation to the signup form")

# 2. Run the pipeline. With no artifacts yet, it stops after the analyst
#    stage, waiting for a human to produce 01_analysis.md.
forgemind run "$TASK_ID"
#   -> {"stopped_state": "BLOCKED", "reason": "awaiting_human_action", ...}

# 3. Write the artifact yourself (or via an interactive Claude Code session
#    using agents/analyst.md), then submit it:
forgemind submit-artifact "$TASK_ID" analyst ./01_analysis.md

# 4. Run again to advance to the next stage. Repeat run/submit-artifact for
#    architect_planner, implementer, tester, reviewer, finalizer in turn --
#    forgemind status "$TASK_ID" always shows which stage you're on.
forgemind run "$TASK_ID"
forgemind submit-artifact "$TASK_ID" architect_planner ./02_design_plan.md

# 5. Governance: if the plan or final-report text matches a sensitive
#    pattern in config/governance.yaml (e.g. contains "git push"), the task
#    stops at AWAITING_PLAN_APPROVAL / AWAITING_FINAL_APPROVAL instead of
#    continuing -- `run` will not proceed past it on its own:
forgemind approve "$TASK_ID"      # or: forgemind reject "$TASK_ID" --reason "..."

# 6. Keep alternating run / submit-artifact through tester, reviewer, and
#    finalizer. The task ends at COMPLETED, with all six artifacts in
#    artifacts/$TASK_ID/.
forgemind status "$TASK_ID"
#   -> {"state": "COMPLETED", ...}
```

With `runner: claude_cli` instead, step 2 onward collapses to just
`forgemind run "$TASK_ID"` repeated after each approval -- Claude performs
each stage itself and writes the artifact.

## Tests

```bash
pytest
```

Runs the full suite under `tests/engine` (see `pytest.ini`). This same
command runs automatically in CI (`.github/workflows/tests.yml`) on every
push, pull request, and manual trigger.

## Scope

ForgeMind V1 is the orchestration engine and CLI only. It does not include
a website/UI, public API, database, authentication, payments, cost/usage
tracking, notifications, or deployment automation.
