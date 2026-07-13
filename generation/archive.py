"""Online duplicate-aware candidate archive for ATS-BRT."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from ..core.schema import CandidateArchiveEntry, CandidateTest, TestSegments
from ..core.utils import safe_json_dump


def code_hash(code: str) -> str:
    normalized = "\n".join(line.rstrip() for line in code.strip().splitlines()) + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalized_ast_hash(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    payload = ast.dump(tree, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _oracle_type(code: str) -> str:
    for line in code.splitlines()[:8]:
        marker = "BRT_ORACLE_TYPE:"
        if marker in line:
            return line.split(marker, 1)[1].strip().upper()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "UNKNOWN"
    if any(
        "raise" in ast.unparse(node.items[0].context_expr).lower()
        for node in ast.walk(tree)
        if isinstance(node, (ast.With, ast.AsyncWith)) and node.items
    ):
        return "EXCEPTION_TYPE"
    if any(isinstance(node, ast.Assert) for node in ast.walk(tree)):
        return "ASSERTION"
    return "NO_EXCEPTION"


def behavior_signature(
    segments: TestSegments,
    code: str,
    execution: dict[str, Any] | None = None,
) -> str:
    execution = execution or {}
    target_calls = sorted(
        str(item.get("name") or "") for item in segments.target_call_locations
    )
    observations = execution.get("public_observations")
    observation_keys = sorted(observations) if isinstance(observations, dict) else []
    payload = {
        "target_calls": target_calls,
        "outcome": str(execution.get("outcome") or execution.get("status") or "UNKNOWN"),
        "failure_signature": str(execution.get("normalized_failure_signature") or ""),
        "oracle_type": _oracle_type(code),
        "observation_keys": observation_keys,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


class CandidateArchive:
    """Persist unique candidates and redirect exact duplicates before execution."""

    def __init__(
        self,
        instance_id: str,
        output_dir: str,
        shared_archive_path: str = "",
    ) -> None:
        self.instance_id = instance_id
        self.path = (
            Path(shared_archive_path)
            if shared_archive_path
            else Path(output_dir) / "candidate_archive.json"
        )
        self.summary_path = self.path.with_name("unique_candidate_summary.json")
        self.entries: list[CandidateArchiveEntry] = []
        self.duplicate_redirects: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        items = raw.get("entries") if isinstance(raw, dict) else []
        if not isinstance(items, list):
            return
        fields = CandidateArchiveEntry.__dataclass_fields__
        for item in items:
            if isinstance(item, dict):
                self.entries.append(
                    CandidateArchiveEntry(
                        **{key: value for key, value in item.items() if key in fields}
                    )
                )
        redirects = raw.get("duplicate_redirects") if isinstance(raw, dict) else []
        if isinstance(redirects, list):
            self.duplicate_redirects = [item for item in redirects if isinstance(item, dict)]

    @property
    def unique_count(self) -> int:
        return sum(1 for item in self.entries if item.duplicate_status == "UNIQUE")

    def unique_entries(self) -> list[CandidateArchiveEntry]:
        return [item for item in self.entries if item.duplicate_status == "UNIQUE"]

    def _find_duplicate(self, ast_hash: str) -> CandidateArchiveEntry | None:
        if not ast_hash:
            return None
        return next(
            (
                item
                for item in self.entries
                if item.normalized_ast_hash == ast_hash
                and item.duplicate_status == "UNIQUE"
            ),
            None,
        )

    def register(
        self,
        candidate: CandidateTest,
        segments: TestSegments,
        code_path: str = "",
    ) -> CandidateArchiveEntry:
        ast_hash = normalized_ast_hash(candidate.code)
        duplicate = self._find_duplicate(ast_hash)
        lineage = candidate.lineage or {}
        origin = str(lineage.get("origin") or "UNKNOWN")
        digest = code_hash(candidate.code)
        candidate_id = str(lineage.get("candidate_id") or f"{origin}-r{candidate.round_id}-{digest[:10]}")
        candidate.lineage["candidate_id"] = candidate_id
        entry = CandidateArchiveEntry(
            candidate_id=candidate_id,
            code_hash=digest,
            normalized_ast_hash=ast_hash,
            behavior_signature=behavior_signature(segments, candidate.code),
            segment_hashes={
                "scaffold": segments.scaffold_hash,
                "trigger": segments.trigger_hash,
                "oracle": segments.oracle_hash,
            },
            origin=origin,
            parent_candidate_id=str(lineage.get("parent_candidate_id") or ""),
            seed_id=str(lineage.get("seed_id") or ""),
            round=int(candidate.round_id),
            search_action=str(lineage.get("search_action") or ""),
            code_path=code_path or candidate.candidate_file_path,
            novelty=0.0 if duplicate else 1.0,
            duplicate_status="CODE_DUPLICATE" if duplicate else "UNIQUE",
            duplicate_of=duplicate.candidate_id if duplicate else "",
            duplicate_redirected_from=str(
                lineage.get("duplicate_redirected_from") or ""
            ),
            observation_id=str(lineage.get("observation_id") or ""),
        )
        self.entries.append(entry)
        self.save()
        return entry

    def finalize(
        self,
        candidate_id: str,
        code: str,
        segments: TestSegments,
        execution: dict[str, Any],
        verifier: dict[str, Any],
        target_evidence: dict[str, Any],
        oracle_risk: dict[str, Any],
        surrogate: dict[str, Any] | None,
    ) -> CandidateArchiveEntry | None:
        entry = next((item for item in reversed(self.entries) if item.candidate_id == candidate_id), None)
        if entry is None:
            return None
        entry.executed = True
        entry.behavior_signature = behavior_signature(segments, code, execution)
        entry.buggy_execution = execution
        entry.verifier_decision = verifier
        entry.target_evidence = target_evidence
        entry.oracle_risk = oracle_risk
        entry.surrogate_result = surrogate or {}
        if entry.duplicate_status == "UNIQUE":
            behavior_duplicate = next(
                (
                    item
                    for item in self.entries
                    if item.candidate_id != entry.candidate_id
                    and item.executed
                    and item.duplicate_status == "UNIQUE"
                    and item.behavior_signature == entry.behavior_signature
                ),
                None,
            )
            if behavior_duplicate is not None:
                entry.duplicate_status = "BEHAVIOR_DUPLICATE"
                entry.duplicate_of = behavior_duplicate.candidate_id
                entry.novelty = 0.0
        self.save()
        return entry

    def record_redirect(
        self,
        duplicate_entry: CandidateArchiveEntry,
        redirected_action: str,
        reason: str,
    ) -> None:
        self.duplicate_redirects.append(
            {
                "candidate_id": duplicate_entry.candidate_id,
                "duplicate_of": duplicate_entry.duplicate_of,
                "redirected_action": redirected_action,
                "reason": reason,
            }
        )
        self.save()

    def save(self) -> None:
        payload = {
            "instance_id": self.instance_id,
            "entries": [item.to_dict() for item in self.entries],
            "duplicate_redirects": self.duplicate_redirects,
            "summary": self.summary(),
        }
        safe_json_dump(payload, str(self.path))
        safe_json_dump(self.summary(), str(self.summary_path))

    def summary(self) -> dict[str, Any]:
        by_origin: dict[str, int] = {}
        for item in self.entries:
            if item.duplicate_status != "UNIQUE":
                continue
            by_origin[item.origin] = by_origin.get(item.origin, 0) + 1
        return {
            "instance_id": self.instance_id,
            "raw_candidates": len(self.entries),
            "unique_candidates": self.unique_count,
            "code_duplicates": sum(
                item.duplicate_status == "CODE_DUPLICATE" for item in self.entries
            ),
            "behavior_duplicates": sum(
                item.duplicate_status == "BEHAVIOR_DUPLICATE" for item in self.entries
            ),
            "duplicate_redirects": len(self.duplicate_redirects),
            "unique_by_origin": dict(sorted(by_origin.items())),
        }
