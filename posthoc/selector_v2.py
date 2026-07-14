"""Conservative, generation-independent Selector V2.

This module consumes only completed P0 generation artifacts. It never imports
the generation pipeline, executes candidates, or reads evaluation results.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


ERROR_STATUSES = {
    "COLLECT_ERROR",
    "ENV_ERROR",
    "ERROR",
    "SETUP_ERROR",
    "SYNTAX_ERROR",
    "TIMEOUT",
}
ISSUE_FAILURE_STATUSES = {"F2P_SUCCESS", "ISSUE_ALIGNED_FAIL", "SURROGATE_F2P_SUCCESS"}
SURROGATE_SUCCESS_STATUSES = {"F2P_SUCCESS", "SURROGATE_F2P_SUCCESS"}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def normalized_code_hash(code: str) -> str:
    """Hash syntax while ignoring formatting and source locations."""
    try:
        normalized = ast.dump(ast.parse(code), annotate_fields=True, include_attributes=False)
    except SyntaxError:
        normalized = "\n".join(line.rstrip() for line in code.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _tri(value: Any) -> int:
    if value is True or str(value).lower() == "true":
        return 2
    if value is False or str(value).lower() == "false":
        return 0
    return 1


def _execution_status(checkpoint: dict[str, Any]) -> str:
    execution = checkpoint.get("execution")
    if not isinstance(execution, dict):
        return "UNKNOWN"
    return str(execution.get("outcome") or execution.get("status") or "UNKNOWN").upper()


def _surrogate_status(checkpoint: dict[str, Any]) -> str:
    surrogate = checkpoint.get("surrogate")
    if not isinstance(surrogate, dict):
        return "UNKNOWN"
    return str(surrogate.get("status") or "UNKNOWN").upper()


def _oracle_level(checkpoint: dict[str, Any]) -> str:
    risk = checkpoint.get("oracle_risk")
    if not isinstance(risk, dict):
        return "UNKNOWN"
    return str(risk.get("risk_level") or risk.get("level") or risk.get("risk") or "UNKNOWN").upper()


def _call_name(node: ast.Call) -> str:
    value: ast.AST = node.func
    parts: list[str] = []
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))


def _assertion_nodes(tree: ast.AST) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            nodes.append(node)
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name.startswith("assert") or ".assert" in name or name.endswith(("raises", "warns")):
                nodes.append(node)
    return nodes


def _code_features(code: str, target_names: set[str]) -> dict[str, Any]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {
            "assertion_count": 0,
            "public_behavior_assertion": False,
            "private_assertion_risk": True,
            "overspecified_oracle": True,
            "target_api_present": False if target_names else None,
            "ast_node_count": 10**9,
            "code_lines": len(code.splitlines()),
            "call_count": 0,
        }

    assertions = _assertion_nodes(tree)
    private_risk = False
    long_literal = False
    full_text_risk = False
    for assertion in assertions:
        for node in ast.walk(assertion):
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                private_risk = True
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                long_literal |= len(text) > 120
                lowered = text.lower()
                full_text_risk |= len(text) > 60 and any(
                    token in lowered
                    for token in ("traceback", "select ", "insert ", "error:", "exception")
                )
            if isinstance(node, ast.Call) and _call_name(node).endswith(("repr", "__repr__")):
                full_text_risk = True

    call_names = [_call_name(node) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    target_present: bool | None = None
    if target_names:
        target_present = any(
            call == target or call.endswith(f".{target}") or target.endswith(f".{call}")
            for call in call_names
            for target in target_names
        )
    overspecified = long_literal or full_text_risk or len(assertions) > 2
    return {
        "assertion_count": len(assertions),
        "public_behavior_assertion": bool(assertions) and not private_risk and not overspecified,
        "private_assertion_risk": private_risk,
        "overspecified_oracle": overspecified,
        "target_api_present": target_present,
        "ast_node_count": sum(1 for _ in ast.walk(tree)),
        "code_lines": len([line for line in code.splitlines() if line.strip()]),
        "call_count": len(call_names),
    }


def _target_names(instance_dir: Path) -> set[str]:
    paths = [instance_dir / "behavior_target.json"]
    paths.extend(sorted((instance_dir / "seed_candidates").glob("seed_*/behavior_target.json")))
    names: set[str] = set()
    for path in paths:
        value = load_json(path)
        apis = value.get("target_apis")
        if not isinstance(apis, list):
            continue
        for api in apis:
            if isinstance(api, str):
                candidate = api
            elif isinstance(api, dict):
                candidate = next(
                    (
                        str(api[key])
                        for key in ("qualified_name", "api", "name", "function_name")
                        if api.get(key)
                    ),
                    "",
                )
            else:
                candidate = ""
            candidate = re.sub(r"\(.*$", "", candidate).strip()
            if candidate:
                names.add(candidate)
                names.add(candidate.rsplit(".", 1)[-1])
    return names


def _candidate_path(ranking_path: Path, checkpoint: dict[str, Any]) -> Path | None:
    raw_path = str(checkpoint.get("code_path") or "")
    direct = Path(raw_path) if raw_path else None
    if direct is not None and direct.is_file():
        return direct
    round_id = checkpoint.get("round_id")
    if round_id is not None:
        for candidate in (
            ranking_path.parent / "checkpoints" / f"candidate_attempt_{round_id}.py",
            ranking_path.parent / f"candidate_attempt_{round_id}.py",
        ):
            if candidate.is_file():
                return candidate
    return None


def _source_label(instance_dir: Path, ranking_path: Path) -> str:
    try:
        relative = ranking_path.relative_to(instance_dir)
    except ValueError:
        return "top"
    if len(relative.parts) >= 3 and relative.parts[0] == "seed_candidates":
        return relative.parts[1]
    return "top"


def _origin(checkpoints: list[dict[str, Any]], index: int) -> str:
    if index == 0:
        return "generation"
    previous = checkpoints[index - 1]
    verifier = previous.get("verifier") if isinstance(previous.get("verifier"), dict) else {}
    action = str(verifier.get("next_action") or "").lower()
    if action in {"repair_setup", "repair_trigger", "repair_oracle"}:
        return action
    return "unknown_repair"


@dataclass
class PosthocCandidate:
    instance_id: str
    candidate_id: str
    code_path: Path
    code_hash: str
    source: str
    round_id: int
    origin: str
    checkpoint: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    legacy_match: bool = False
    features: dict[str, Any] = field(default_factory=dict)
    rank_key: tuple[int, ...] = ()

    def manifest_record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "code_path": str(self.code_path),
            "code_hash": self.code_hash,
            "source": self.source,
            "round_id": self.round_id,
            "origin": self.origin,
            "aliases": self.aliases,
            "legacy_match": self.legacy_match,
            "features": self.features,
            "rank_key": list(self.rank_key),
        }


def _candidate_features(candidate: PosthocCandidate, target_names: set[str]) -> dict[str, Any]:
    checkpoint = candidate.checkpoint
    code = candidate.code_path.read_text(encoding="utf-8", errors="replace")
    status = _execution_status(checkpoint)
    execution = checkpoint.get("execution") if isinstance(checkpoint.get("execution"), dict) else {}
    verifier = checkpoint.get("verifier") if isinstance(checkpoint.get("verifier"), dict) else {}
    returncode = execution.get("returncode", execution.get("return_code"))
    executable = status not in ERROR_STATUSES and (returncode is not None or status != "UNKNOWN")
    if status in ERROR_STATUSES:
        protocol_value: bool | None = False
    elif executable:
        protocol_value = True
    else:
        protocol_value = None
    decision = str(verifier.get("decision") or "UNKNOWN").lower()
    issue_aligned = status in ISSUE_FAILURE_STATUSES or (
        str(verifier.get("failure_class") or "").lower() == "issue_aligned"
    )
    code_features = _code_features(code, target_names)
    return {
        "protocol_valid": protocol_value,
        "buggy_executable": executable,
        "buggy_outcome": status,
        "issue_aligned_fail": issue_aligned,
        "semantic_accept": decision == "accept",
        "semantic_target_hit": verifier.get("target_hit"),
        "target_api_present": code_features["target_api_present"],
        "surrogate_positive_validation": _surrogate_status(checkpoint)
        in SURROGATE_SUCCESS_STATUSES,
        "surrogate_status": _surrogate_status(checkpoint),
        "oracle_risk": _oracle_level(checkpoint),
        **code_features,
    }


def _risk_score(level: str) -> int:
    return {"LOW": 3, "MEDIUM": 2, "UNKNOWN": 1, "HIGH": 0}.get(level, 1)


def _rank_key(candidate: PosthocCandidate) -> tuple[int, ...]:
    features = candidate.features
    assertions = int(features.get("assertion_count") or 0)
    return (
        _tri(features.get("protocol_valid")),
        int(bool(features.get("buggy_executable"))),
        int(bool(features.get("issue_aligned_fail"))),
        _tri(features.get("target_api_present")),
        _tri(features.get("semantic_target_hit")),
        int(bool(features.get("semantic_accept"))),
        int(bool(features.get("surrogate_positive_validation"))),
        _risk_score(str(features.get("oracle_risk") or "UNKNOWN")),
        int(bool(features.get("public_behavior_assertion"))),
        int(1 <= assertions <= 2),
        int(not bool(features.get("overspecified_oracle"))),
        int(not bool(features.get("private_assertion_risk"))),
        -min(int(features.get("ast_node_count") or 10**9), 10**9),
        -min(candidate.round_id, 10**6),
    )


def collect_candidates(instance_dir: Path) -> tuple[list[PosthocCandidate], PosthocCandidate | None]:
    """Collect and de-duplicate all top-level and seed-level checkpoints."""
    final_path = instance_dir / "final_test.py"
    if not final_path.is_file():
        return [], None
    legacy_hash = normalized_code_hash(final_path.read_text(encoding="utf-8", errors="replace"))
    target_names = _target_names(instance_dir)
    ranking_paths = [instance_dir / "candidate_ranking.json"]
    ranking_paths.extend(sorted((instance_dir / "seed_candidates").glob("seed_*/candidate_ranking.json")))
    by_hash: dict[str, PosthocCandidate] = {}

    for ranking_path in ranking_paths:
        ranking = load_json(ranking_path)
        checkpoints = ranking.get("checkpoints")
        if not isinstance(checkpoints, list):
            continue
        typed = [item for item in checkpoints if isinstance(item, dict)]
        source = _source_label(instance_dir, ranking_path)
        for index, checkpoint in enumerate(typed):
            code_path = _candidate_path(ranking_path, checkpoint)
            if code_path is None:
                continue
            round_id = int(checkpoint.get("round_id") or 0)
            strict_result = load_json(ranking_path.parent / f"strict_verifier_round_{round_id}.json")
            if strict_result:
                checkpoint = dict(checkpoint)
                verifier = checkpoint.get("verifier")
                merged_verifier = dict(verifier) if isinstance(verifier, dict) else {}
                merged_verifier.update(strict_result)
                checkpoint["verifier"] = merged_verifier
            code_hash = normalized_code_hash(code_path.read_text(encoding="utf-8", errors="replace"))
            candidate_id = f"{source}:round_{round_id}"
            candidate = PosthocCandidate(
                instance_id=instance_dir.name,
                candidate_id=candidate_id,
                code_path=code_path,
                code_hash=code_hash,
                source=source,
                round_id=round_id,
                origin=_origin(typed, index),
                checkpoint=checkpoint,
                legacy_match=code_hash == legacy_hash,
            )
            candidate.features = _candidate_features(candidate, target_names)
            candidate.rank_key = _rank_key(candidate)
            existing = by_hash.get(code_hash)
            if existing is None:
                by_hash[code_hash] = candidate
            elif candidate.rank_key > existing.rank_key:
                candidate.aliases = existing.aliases + [existing.candidate_id]
                candidate.legacy_match = candidate.legacy_match or existing.legacy_match
                by_hash[code_hash] = candidate
            else:
                existing.aliases.append(candidate_id)
                if candidate.legacy_match:
                    existing.legacy_match = True

    legacy = next((candidate for candidate in by_hash.values() if candidate.legacy_match), None)
    if legacy is None:
        legacy = PosthocCandidate(
            instance_id=instance_dir.name,
            candidate_id="legacy:final",
            code_path=final_path,
            code_hash=legacy_hash,
            source="top",
            round_id=10**6,
            origin="legacy_final",
            legacy_match=True,
        )
        legacy.features = _candidate_features(legacy, target_names)
        legacy.rank_key = _rank_key(legacy)
        by_hash[legacy_hash] = legacy
    return list(by_hash.values()), legacy


def select_candidate(
    candidates: Iterable[PosthocCandidate],
    legacy: PosthocCandidate,
    fallback_to_legacy: bool = True,
) -> tuple[PosthocCandidate, str, bool, list[PosthocCandidate]]:
    """Select only when generation-time evidence is strictly better than Legacy."""
    ranked = sorted(candidates, key=lambda item: (item.rank_key, item.legacy_match), reverse=True)
    if not ranked:
        return legacy, "no readable checkpoint candidate; retained Legacy", True, []
    best = ranked[0]
    if best.code_hash == legacy.code_hash:
        return legacy, "Legacy already has the strongest post-hoc evidence", False, ranked
    trusted_dimensions = 12
    if fallback_to_legacy and best.rank_key[:trusted_dimensions] <= legacy.rank_key[:trusted_dimensions]:
        return legacy, "no candidate has strictly stronger trusted evidence; retained Legacy", True, ranked
    dimensions = [
        "protocol validity",
        "buggy executability",
        "issue-aligned failure",
        "target API evidence",
        "semantic target evidence",
        "semantic acceptance",
        "surrogate positive validation",
        "oracle risk",
        "public minimal oracle",
        "oracle specificity",
        "code complexity",
        "candidate age",
    ]
    improvements = [
        dimensions[index]
        for index, (new, old) in enumerate(zip(best.rank_key, legacy.rank_key))
        if new > old and index < len(dimensions)
    ]
    reason = "strictly stronger generation-time evidence"
    if improvements:
        reason += ": " + ", ".join(improvements[:3])
    return best, reason, False, ranked
