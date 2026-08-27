# ForgeMind website interface

A thin local website over the real ForgeMind engine (`engine/forgemind`).
It reuses the orchestrator, state machine, governance, and CLI's own
runner-selection logic directly -- it does not reimplement any pipeline
behavior. The CLI (`forgemind ...`) is untouched and keeps working exactly
as before; this is a second, independent way to drive the same engine.

## Run it

```bash
# From the repo root, with the engine already installed (see the root README):
pip install -r webapp/backend/requirements.txt
python webapp/backend/app.py
```

Then open <http://127.0.0.1:5000>. The server binds to `127.0.0.1` only
(never `0.0.0.0`) because it can execute local shell commands and read/write
your filesystem on the pipeline's behalf -- it must never be reachable from
the network.

## What it does and doesn't do

- It drives whichever runner `config/forgemind.yaml` selects, exactly like
  the CLI: `runner: manual` asks you to paste each stage's artifact
  yourself through the website; `runner: claude_cli` runs each stage
  automatically through your authenticated `claude` CLI.
- Governance, retries, and artifact validation are the real
  `forgemind.governance` / `forgemind.orchestrator` logic -- the website
  cannot bypass an approval gate any more than the CLI can.
- Progress in the browser is polling `GET /api/tasks/<id>` every ~2.5s.
  This is genuine, not simulated: `state.json` is written to disk on every
  real state transition, so polling it reflects the pipeline's actual
  progress.
- Only one task may be active at a time (the same `tasks/.active_task`
  lock the CLI uses).

## Files

- `backend/app.py` -- the Flask API bridge (see its module docstring for
  the full route list and the reasoning behind the background-thread
  design).
- `frontend/` -- plain HTML/CSS/JS, no build step, served directly by
  Flask.

## Tests

`tests/webapp/test_app.py` runs as part of the repo's normal `pytest`
(see the root README) and drives the real `forgemind` package end to end
through the API, using the real `ManualRunner` against a throwaway fake
repo -- not a mock of the orchestrator.
