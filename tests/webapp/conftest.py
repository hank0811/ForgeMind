"""Makes webapp/backend/app.py importable as `app` for the webapp test
suite, without turning it into an installed package -- it is a thin,
directly-run Flask entry point (`python -m webapp.backend.app` /
`python webapp/backend/app.py`), not a library other code imports."""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2] / "webapp" / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
