"""Conservative delta minimization metadata for NS-GEM."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ..core.schema import NSGEMOperatorPlan
from ..core.utils import safe_json_dump


def conservative_delta_minimize(
    candidate_code: str,
    applied_ops: list[str],
    status: str,
    plan: NSGEMOperatorPlan | None,
    output_dir: str,
) -> tuple[str, dict[str, Any]]:
    result: dict[str, Any] = {
        "attempted": False,
        "minimality_score": 0,
        "removed_ops": [],
        "kept_ops": applied_ops,
        "reason": "not eligible",
    }
    if status not in {"SURROGATE_F2P_SUCCESS", "ISSUE_ALIGNED_FAIL"} or len(applied_ops) <= 1:
        safe_json_dump(result, str(Path(output_dir) / "delta_minimization.json"))
        return candidate_code, result
    result["attempted"] = True
    result["reason"] = "metadata-only conservative minimization"
    result["minimality_score"] = 5
    if plan and plan.risk == "high":
        result["minimality_score"] = 2
    try:
        ast.parse(candidate_code)
    except SyntaxError:
        result["minimality_score"] = 0
        result["reason"] = "candidate not parseable"
    safe_json_dump(result, str(Path(output_dir) / "delta_minimization.json"))
    return candidate_code, result
