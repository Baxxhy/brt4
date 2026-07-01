"""Optional dual-version validation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .executor import run_command_in_conda
from ..core.schema import CandidateTest, DualVersionResult, ExecutionResult
from ..core.utils import safe_json_dump, write_text


def _copy_test_to_repo(candidate: CandidateTest, repo: str, code: str) -> str:
    target = Path(repo) / candidate.candidate_repo_path
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text(str(target), code)
    return str(target)


def run_dual_version_validation(
    instance_id: str,
    mode: str,
    candidate: CandidateTest,
    final_code: str,
    buggy_repo: str,
    buggy_execution: ExecutionResult,
    conda_env: str = "",
    timeout: int = 120,
    no_conda: bool = False,
    patched_repo_base: str = "",
    patch_file: str = "",
    patch_text: str = "",
    output_path: str | None = None,
) -> DualVersionResult:
    if mode == "buggy_only":
        result = DualVersionResult(instance_id, mode, buggy_execution.to_dict(), {}, "BUGGY_ONLY")
    elif mode == "patched_repo":
        if not patched_repo_base:
            result = DualVersionResult(instance_id, mode, buggy_execution.to_dict(), {}, "SETUP_ERROR", "missing patched_repo_base")
        else:
            _copy_test_to_repo(candidate, patched_repo_base, final_code)
            patched = run_command_in_conda(candidate.command, patched_repo_base, conda_env, timeout, no_conda, None, instance_id)
            status = "F2P_SUCCESS" if buggy_execution.returncode != 0 and patched.returncode == 0 else ("BUGGY_PASS" if buggy_execution.returncode == 0 else "PATCHED_FAIL")
            result = DualVersionResult(instance_id, mode, buggy_execution.to_dict(), patched.to_dict(), status)
    elif mode == "patch_file":
        if not patch_file:
            result = DualVersionResult(instance_id, mode, buggy_execution.to_dict(), {}, "SETUP_ERROR", "missing patch_file")
        else:
            with tempfile.TemporaryDirectory(prefix="brt3_patch_") as td:
                repo_copy = os.path.join(td, "repo")
                shutil.copytree(buggy_repo, repo_copy, symlinks=True)
                subprocess.run(["bash", "-lc", f"git apply {patch_file}"], cwd=repo_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                _copy_test_to_repo(candidate, repo_copy, final_code)
                patched = run_command_in_conda(candidate.command, repo_copy, conda_env, timeout, no_conda, None, instance_id)
                status = "F2P_SUCCESS" if buggy_execution.returncode != 0 and patched.returncode == 0 else ("BUGGY_PASS" if buggy_execution.returncode == 0 else "PATCHED_FAIL")
                result = DualVersionResult(instance_id, mode, buggy_execution.to_dict(), patched.to_dict(), status)
    elif mode == "patch_text":
        if not patch_text.strip():
            result = DualVersionResult(instance_id, mode, buggy_execution.to_dict(), {}, "SETUP_ERROR", "missing patch_text")
        else:
            with tempfile.TemporaryDirectory(prefix="brt3_patch_text_") as td:
                repo_copy = os.path.join(td, "repo")
                shutil.copytree(buggy_repo, repo_copy, symlinks=True)
                patch_path = os.path.join(td, "patch.diff")
                write_text(patch_path, patch_text)
                applied = subprocess.run(["bash", "-lc", f"git apply {patch_path}"], cwd=repo_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if applied.returncode != 0:
                    result = DualVersionResult(instance_id, mode, buggy_execution.to_dict(), {"apply_patch": applied.stderr}, "PATCH_APPLY_ERROR")
                else:
                    _copy_test_to_repo(candidate, repo_copy, final_code)
                    patched = run_command_in_conda(candidate.command, repo_copy, conda_env, timeout, no_conda, None, instance_id)
                    status = "F2P_SUCCESS" if buggy_execution.returncode != 0 and patched.returncode == 0 else ("BUGGY_PASS" if buggy_execution.returncode == 0 else "PATCHED_FAIL")
                    result = DualVersionResult(instance_id, mode, buggy_execution.to_dict(), patched.to_dict(), status)
    else:
        result = DualVersionResult(instance_id, mode, buggy_execution.to_dict(), {}, "NOT_IMPLEMENTED", "surrogate_patch is a stub")
    if output_path:
        safe_json_dump(result.to_dict(), output_path)
    return result
