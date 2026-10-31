"""Open WebUI validation-only tool for Dynamic Task Planner v0.5a.

This file is intended to be installed as a custom Open WebUI Tool. It imports
only the validation/preview module and cannot reach the executor, adapters,
approval records, Docker, or production services.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_planner() -> Any:
    candidates = (
        Path("/workspace/scripts/task_planner_v05a.py"),
        Path(__file__).resolve().parent.parent / "scripts" / "task_planner_v05a.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("task_planner_v05a", candidate)
            if spec is None or spec.loader is None:
                break
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("validation planner is unavailable")


class Tools:
    """Validation-only Open WebUI tool surface."""

    def validate_dynamic_plan(self, proposed_plan: str, correction: str = "") -> str:
        """Validate and preview a proposed v0.5a JSON plan without execution.

        Provide one optional correction JSON document only after using the
        returned structured errors. The tool never rewrites plans silently.
        """
        try:
            planner = _load_planner()
            result = planner.preview_plan(proposed_plan, correction or None)
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except Exception:
            return json.dumps({
                "valid": False,
                "version": "0.5a",
                "errors": [{"code": "validation_unavailable", "message": "validation service unavailable"}],
            })
