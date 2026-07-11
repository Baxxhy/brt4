"""Direct same-directory evaluator for BRT3 generated tests.

This evaluator intentionally does not use iCoRe/Libro's insertion pipeline.
It copies each generated final_test.py into a new test_brt_*.py file under the
same directory as the primary related test, then executes only that file/test.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..io.io_utils import load_issue_data
from ..core.utils import ensure_dir, safe_json_dump, sanitize_instance_id
from ..runtime.conda_env_manager import (
    classify_env_error,
    conda_activate_cmd,
    preflight_system,
    resolve_eval_env,
)


def repo_path(repo_root_base: str, issue: dict[str, Any]) -> str:
    return str(Path(repo_root_base) / issue["repo"].split("/")[-1])


def run_shell(cmd: str, cwd: str, timeout: int | None = None) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "command": cmd,
            "cwd": cwd,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "duration": time.time() - started,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd,
            "cwd": cwd,
            "returncode": 124,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace"),
            "stderr": exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace"),
            "duration": time.time() - started,
            "timeout": True,
        }


@contextlib.contextmanager
def file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _git_lock_paths(repo_dir: str) -> list[Path]:
    repo = Path(repo_dir)
    paths = [repo / ".git" / "index.lock"]
    git_file = repo / ".git"
    if git_file.is_file():
        try:
            text = git_file.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        prefix = "gitdir:"
        if text.startswith(prefix):
            git_dir = (repo / text[len(prefix):].strip()).resolve()
            paths.append(git_dir / "index.lock")
    return paths


def wait_for_git_locks(repo_dir: str, timeout: int = 600) -> dict[str, Any]:
    started = time.time()
    observed: list[str] = []
    while True:
        existing = [path for path in _git_lock_paths(repo_dir) if path.exists()]
        if not existing:
            return {
                "waited": time.time() - started,
                "observed_locks": observed,
                "timeout": False,
            }
        observed = sorted({*observed, *(str(path) for path in existing)})
        if time.time() - started >= timeout:
            return {
                "waited": time.time() - started,
                "observed_locks": observed,
                "timeout": True,
            }
        time.sleep(2)


def git_reset_to(repo_dir: str, commit: str, clean: bool = True) -> dict[str, Any]:
    pre_wait = wait_for_git_locks(repo_dir)
    if pre_wait.get("timeout"):
        raise RuntimeError(
            f"git locks did not clear in {repo_dir}: {pre_wait.get('observed_locks')}"
        )
    reset = run_shell(f"git reset --hard {shlex.quote(commit)}", repo_dir, 120)
    if reset["returncode"] != 0:
        retry_wait = wait_for_git_locks(repo_dir)
        retry = run_shell(f"git reset --hard {shlex.quote(commit)}", repo_dir, 120)
        reset = {"first": reset, "retry_wait": retry_wait, "retry": retry, **retry}
    if reset["returncode"] != 0:
        raise RuntimeError(f"git reset failed in {repo_dir}: {reset.get('stderr') or reset.get('stdout')}")
    clean_result = None
    if clean:
        clean_result = run_shell("git clean -fdxq", repo_dir, 120)
        if clean_result["returncode"] != 0:
            retry_wait = wait_for_git_locks(repo_dir)
            retry = run_shell("git clean -fdxq", repo_dir, 120)
            clean_result = {"first": clean_result, "retry_wait": retry_wait, "retry": retry, **retry}
        if clean_result["returncode"] != 0:
            raise RuntimeError(f"git clean failed in {repo_dir}: {clean_result.get('stderr') or clean_result.get('stdout')}")
    return {"pre_wait": pre_wait, "reset": reset, "clean": clean_result}


def git_reset_clean(repo_dir: str) -> None:
    run_shell("git reset --hard", repo_dir, 120)
    run_shell("git clean -fdxq", repo_dir, 120)


def apply_patch_text(repo_dir: str, patch_text: str) -> dict[str, Any]:
    if not patch_text.strip():
        return {
            "command": "git apply <empty patch>",
            "cwd": repo_dir,
            "returncode": 2,
            "stdout": "",
            "stderr": "empty patch text",
            "duration": 0.0,
            "timeout": False,
        }
    handle = tempfile.NamedTemporaryFile("w", suffix=".brt3.patch", encoding="utf-8", delete=False)
    patch_file = Path(handle.name)
    try:
        with handle:
            handle.write(patch_text)
        return run_shell(f"git apply {shlex.quote(str(patch_file))}", repo_dir, 120)
    finally:
        try:
            patch_file.unlink()
        except OSError:
            pass


def _remove_eval_worktree(base_repo_dir: str, worktree_dir: Path) -> dict[str, Any]:
    result = run_shell(
        f"git worktree remove --force {shlex.quote(str(worktree_dir))}",
        base_repo_dir,
        300,
    )
    if result["returncode"] != 0 and worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)
        result["fallback_rmtree"] = True
    return result


def prepare_eval_worktree(
    issue: dict[str, Any],
    repo_root_base: str,
    eval_worktree_root: str,
) -> tuple[str, dict[str, Any]]:
    base_repo_dir = repo_path(repo_root_base, issue)
    root = Path(eval_worktree_root)
    root.mkdir(parents=True, exist_ok=True)
    instance_id = str(issue["instance_id"])
    worktree_dir = root / sanitize_instance_id(instance_id)
    lock_name = sanitize_instance_id(issue["repo"].replace("/", "__"))
    lock_path = root / "_locks" / f"{lock_name}.lock"
    metadata: dict[str, Any] = {
        "base_repo_dir": base_repo_dir,
        "worktree_dir": str(worktree_dir),
        "lock_path": str(lock_path),
        "base_commit": issue["base_commit"],
    }
    with file_lock(lock_path):
        if worktree_dir.exists():
            metadata["preexisting_remove"] = _remove_eval_worktree(base_repo_dir, worktree_dir)
            if worktree_dir.exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)
                metadata["preexisting_rmtree"] = True
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        add = run_shell(
            (
                "git worktree add --force --detach "
                f"{shlex.quote(str(worktree_dir))} {shlex.quote(str(issue['base_commit']))}"
            ),
            base_repo_dir,
            600,
        )
        metadata["worktree_add"] = add
        if add["returncode"] != 0:
            raise RuntimeError(
                f"git worktree add failed for {instance_id}: {add.get('stderr') or add.get('stdout')}"
            )
    return str(worktree_dir), metadata


def cleanup_eval_worktree(
    issue: dict[str, Any],
    repo_root_base: str,
    eval_worktree_root: str,
    worktree_dir: str,
) -> dict[str, Any]:
    root = Path(eval_worktree_root)
    base_repo_dir = repo_path(repo_root_base, issue)
    lock_name = sanitize_instance_id(issue["repo"].replace("/", "__"))
    lock_path = root / "_locks" / f"{lock_name}.lock"
    with file_lock(lock_path):
        return _remove_eval_worktree(base_repo_dir, Path(worktree_dir))


def prepare_eval_clone(
    issue: dict[str, Any],
    repo_root_base: str,
    eval_clone_root: str,
) -> tuple[str, dict[str, Any]]:
    base_repo_dir = repo_path(repo_root_base, issue)
    root = Path(eval_clone_root)
    root.mkdir(parents=True, exist_ok=True)
    instance_id = str(issue["instance_id"])
    clone_dir = root / sanitize_instance_id(instance_id)
    metadata: dict[str, Any] = {
        "base_repo_dir": base_repo_dir,
        "clone_dir": str(clone_dir),
        "base_commit": issue["base_commit"],
    }
    if clone_dir.exists():
        shutil.rmtree(clone_dir, ignore_errors=True)
        metadata["preexisting_rmtree"] = True
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    clone = run_shell(
        f"git clone --shared --no-checkout {shlex.quote(base_repo_dir)} {shlex.quote(str(clone_dir))}",
        str(root),
        600,
    )
    metadata["clone"] = clone
    if clone["returncode"] != 0:
        raise RuntimeError(
            f"git clone failed for {instance_id}: {clone.get('stderr') or clone.get('stdout')}"
        )
    checkout = run_shell(f"git checkout --force {shlex.quote(str(issue['base_commit']))}", str(clone_dir), 600)
    metadata["checkout"] = checkout
    if checkout["returncode"] != 0:
        raise RuntimeError(
            f"git checkout failed for {instance_id}: {checkout.get('stderr') or checkout.get('stdout')}"
        )
    return str(clone_dir), metadata


def direct_test_relpath(instance_id: str, generated_dir: str) -> str:
    host_path = Path(generated_dir) / instance_id / "host_context.json"
    test_dir = "tests"
    if host_path.exists():
        host = json.loads(host_path.read_text(encoding="utf-8"))
        host_file = host.get("host_file") or ""
        if host_file:
            test_dir = os.path.dirname(host_file) or "."
    return os.path.join(test_dir, f"test_brt_{sanitize_instance_id(instance_id)}.py")


def runner_parity_info(
    instance_id: str,
    generated_dir: str,
    formal_rel_file: str,
    formal_selector: str,
    formal_command: str,
) -> dict[str, Any]:
    instance_dir = Path(generated_dir) / instance_id
    summary_path = instance_dir / "summary.json"
    host_path = instance_dir / "host_context.json"
    summary: dict[str, Any] = {}
    host: dict[str, Any] = {}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = {}
    try:
        host = json.loads(host_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        host = {}
    generation_path = str(
        summary.get("candidate_repo_path")
        or summary.get("direct_test_repo_path_hint")
        or ""
    )
    generation_command = str(
        summary.get("command") or (summary.get("buggy_execution") or {}).get("command") or ""
    )
    generation_selector = str(summary.get("selector") or "")
    if not generation_selector and "::" in generation_command:
        generation_selector = generation_command.rsplit("::", 1)[-1].split()[0]
    warnings: list[str] = []
    same_dir = (
        bool(generation_path)
        and os.path.dirname(generation_path) == os.path.dirname(formal_rel_file)
    )
    if generation_path and not same_dir:
        warnings.append("formal eval writes BRT to a different directory than generation candidate")
    same_selector = not generation_selector or generation_selector == formal_selector
    if generation_selector and not same_selector:
        warnings.append("formal eval selector differs from generation selector")
    return {
        "generation_candidate_repo_path": generation_path,
        "formal_direct_test_repo_path": formal_rel_file,
        "generation_command": generation_command,
        "formal_command": formal_command,
        "generation_selector": generation_selector,
        "formal_selector": formal_selector,
        "host_file": host.get("host_file", ""),
        "same_dir": same_dir,
        "same_selector": same_selector,
        "warnings": warnings,
    }


def first_test_selector(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                    return f"{node.name}::{child.name}"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            return node.name
    return ""


def test_command(repo: str, version: str, rel_file: str, selector: str) -> str:
    project = repo.split("/")[-1]
    nodeid = rel_file if not selector else f"{rel_file}::{selector}"
    if project in {"astropy", "matplotlib", "flask", "xarray", "pylint", "scikit-learn", "sphinx", "requests"}:
        return f"python -m pytest --no-header --tb=short --show-capture=no --disable-warnings -p no:cacheprovider {nodeid}"
    if project == "seaborn":
        return f"pytest --no-header --show-capture=no --disable-warnings {nodeid}"
    if project == "pytest":
        return f"pytest --disable-warnings --show-capture=no {nodeid} -v"
    if project == "django":
        label = nodeid.replace(".py", "").replace("/", ".").replace("::", ".")
        if label.startswith("tests."):
            label = label[len("tests.") :]
        return f"./tests/runtests.py --settings=test_sqlite {label}"
    if project == "sympy":
        test_name = selector.split("::")[-1] if selector else ""
        if test_name:
            return f"PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C {rel_file} -k {test_name}"
        return f"PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C {rel_file}"
    raise ValueError(f"unsupported project: {repo} version={version}")


def setup_command(repo: str, version: str) -> str:
    project = repo.split("/")[-1]
    # In this workspace the environments were already created by the author-style
    # run. We only need an editable install refresh after resetting/applying.
    if project == "django":
        return "python -m pip install --ignore-installed --no-deps -e ."
    if project == "sympy":
        return "python -m pip install --ignore-installed --no-deps -e ."
    if project == "astropy":
        return "if [ -f pyproject.toml ]; then sed -i 's/requires = \\[\"setuptools\",/requires = [\"setuptools==68.0.0\",/' pyproject.toml; fi && python -m pip install --ignore-installed --no-deps -e .\"[test]\" --verbose"
    if project == "matplotlib":
        if version in {"3.0", "3.1", "3.2", "3.3", "3.4"}:
            return "python setup.py build_ext --inplace"
        return "python -m pip install --ignore-installed --no-deps -e ."
    if project == "scikit-learn":
        return "python -m pip install --ignore-installed --no-deps -e ."
    if project in {"pytest", "sphinx", "xarray", "flask", "seaborn", "requests", "pylint"}:
        return "python -m pip install --ignore-installed --no-deps -e ."
    return "python -m pip install --ignore-installed --no-deps -e ."


def run_setup_with_fallback(command: str, repo_dir: str, timeout: int) -> dict[str, Any]:
    def snapshot(item: dict[str, Any]) -> dict[str, Any]:
        return dict(item)

    result = run_shell(command, repo_dir, timeout)
    log = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    attempts = [{"name": "primary", "command": command, "result": snapshot(result)}]
    if result["returncode"] != 0 and "missing the 'build_editable' hook" in log and " -e ." in command:
        fallback_command = command.replace(" -e .", " .")
        fallback = run_shell(fallback_command, repo_dir, timeout)
        attempts.append({"name": "non_editable_fallback", "command": fallback_command, "result": snapshot(fallback)})
        if fallback["returncode"] == 0:
            fallback["fallback_attempts"] = attempts
            return fallback
        result = fallback
        log = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    if (
        result["returncode"] != 0
        and (
            "uninstall-no-record-file" in log
            or ("egg-link" in log.lower() and "does not match installed location" in log.lower())
        )
        and "python -m pip install" in command
        and " -e ." in command
        and "--ignore-installed" not in command
    ):
        fallback_command = re.sub(
            r"python -m pip install(?![^&]*--ignore-installed)([^&]*\s-e\s+\.)",
            r"python -m pip install --ignore-installed --no-deps\1",
            command,
            count=1,
        )
        fallback = run_shell(fallback_command, repo_dir, timeout)
        attempts.append({"name": "ignore_installed_fallback", "command": fallback_command, "result": snapshot(fallback)})
        if fallback["returncode"] == 0:
            fallback["fallback_attempts"] = attempts
            return fallback
        result = fallback
    result["fallback_attempts"] = attempts
    return result


def classify_run(result: dict[str, Any]) -> dict[str, Any]:
    text = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    low = text.lower()
    if result.get("timeout"):
        status = "TIMEOUT"
    elif result.get("returncode") == 0:
        status = "PASS"
    elif "syntaxerror" in low or "indentationerror" in low:
        status = "SYNTAX_ERROR"
    elif any(x in low for x in ["importerror", "modulenotfounderror", "fixture", "improperlyconfigured", "settings are not configured"]):
        status = "SETUP_ERROR"
    elif "no tests ran" in low or "not found" in low or "collected 0 items" in low:
        status = "COLLECT_ERROR"
    else:
        status = "FAIL"
    return {
        "status": status,
        "failed": status not in {"PASS"},
        "error_excerpt": "\n".join(text.splitlines()[-80:]),
    }


def write_generated_test(repo_dir: str, rel_file: str, code: str) -> str:
    target = Path(repo_dir) / rel_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code.rstrip() + "\n", encoding="utf-8")
    return str(target)


def patch_requires_rebuild(patch_text: str) -> bool:
    build_suffixes = {
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".pyx", ".pxd", ".pxi",
    }
    for line in patch_text.splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = line[len("+++ b/"):].strip()
        if Path(path).suffix.lower() in build_suffixes:
            return True
        if Path(path).name in {"setup.py", "pyproject.toml", "meson.build", "CMakeLists.txt"}:
            return True
    return False


def evaluate_one(
    issue: dict[str, Any],
    generated_dir: str,
    repo_root_base: str,
    timeout: int,
    setup: bool,
    use_generated_worktree: bool = False,
    eval_worktree_root: str = "",
    eval_clone_root: str = "",
    cleanup_isolated_worktree: bool = True,
) -> dict[str, Any]:
    instance_id = issue["instance_id"]
    final_path = Path(generated_dir) / instance_id / "final_test.py"
    if not final_path.exists():
        return {"instance_id": instance_id, "status": "MISSING_GENERATED_TEST", "success": False}
    generated_worktree = Path(generated_dir) / instance_id / "worktree"
    eval_worktree_metadata: dict[str, Any] = {}
    isolated_eval_worktree = False
    isolated_eval_clone = False
    try:
        if use_generated_worktree and generated_worktree.is_dir():
            repo_dir = str(generated_worktree)
            preserve_build_artifacts = True
        elif eval_clone_root:
            repo_dir, eval_worktree_metadata = prepare_eval_clone(issue, repo_root_base, eval_clone_root)
            preserve_build_artifacts = False
            isolated_eval_clone = True
        elif eval_worktree_root:
            repo_dir, eval_worktree_metadata = prepare_eval_worktree(issue, repo_root_base, eval_worktree_root)
            preserve_build_artifacts = False
            isolated_eval_worktree = True
        else:
            repo_dir = repo_path(repo_root_base, issue)
            preserve_build_artifacts = False
    except Exception as exc:  # noqa: BLE001
        return {
            "instance_id": instance_id,
            "status": "ERROR",
            "success": False,
            "error": repr(exc),
            "worktree_mode": "isolated_eval_clone" if eval_clone_root else "isolated_eval_worktree",
        }
    env_resolution = resolve_eval_env(issue, generated_dir)
    env_name = env_resolution.resolved_env
    code = final_path.read_text(encoding="utf-8")
    rel_file = direct_test_relpath(instance_id, generated_dir)
    selector = first_test_selector(code)
    command = test_command(issue["repo"], issue["version"], rel_file, selector)
    runner_parity = runner_parity_info(
        instance_id, generated_dir, rel_file, selector, command
    )
    pythonpath = f"{repo_dir}:{repo_dir}/src:{repo_dir}/lib"
    full_command = f"{conda_activate_cmd(env_name)} && export PYTHONPATH={pythonpath}:$PYTHONPATH && {command}"
    setup_full_command = f"{conda_activate_cmd(env_name)} && {setup_command(issue['repo'], issue['version'])}"
    result: dict[str, Any] = {
        "instance_id": instance_id,
        "repo": issue["repo"],
        "version": issue["version"],
        "env_name": env_name,
        "env_resolution": env_resolution.to_dict(),
        "requested_env": env_resolution.requested_env,
        "recorded_env": env_resolution.recorded_env,
        "resolved_env": env_resolution.resolved_env,
        "resolution_source": env_resolution.resolution_source,
        "env_exists": env_resolution.env_exists,
        "env_health": env_resolution.env_health,
        "repo_dir": repo_dir,
        "generated_final_test": str(final_path),
        "direct_test_repo_path": rel_file,
        "selector": selector,
        "test_command": command,
        "runner_parity": runner_parity,
        "worktree_mode": (
            "generated_instance_worktree"
            if use_generated_worktree and generated_worktree.is_dir()
            else "isolated_eval_clone"
            if isolated_eval_clone
            else "isolated_eval_worktree"
            if isolated_eval_worktree
            else "shared_repo"
        ),
        "eval_isolation": eval_worktree_metadata,
    }
    if not env_resolution.env_exists or not env_resolution.env_health.get("ok"):
        category = str(env_resolution.env_health.get("category") or "ENV_INCOMPLETE")
        result["success"] = False
        result["status"] = category
        result["env_error_category"] = category
        result["error"] = "; ".join(env_resolution.errors) or env_resolution.env_health.get("reason", "")
        return result
    try:
        result["buggy_reset"] = git_reset_to(repo_dir, issue["base_commit"], clean=not preserve_build_artifacts)
        write_generated_test(repo_dir, rel_file, code)
        # Refresh editable-install and namespace-package paths for every
        # instance. Reused conda environments otherwise retain another
        # instance's worktree in .pth/egg-link metadata.
        if setup:
            result["buggy_setup"] = run_setup_with_fallback(setup_full_command, repo_dir, timeout)
            if result["buggy_setup"]["returncode"] != 0:
                result["buggy"] = classify_run(result["buggy_setup"])
                result["fixed"] = {}
                result["success"] = False
                result["status"] = "BUGGY_SETUP_ERROR"
                result["env_error_category"] = classify_env_error(
                    (result["buggy_setup"].get("stdout") or "") + "\n" + (result["buggy_setup"].get("stderr") or "")
                )
                return result
        buggy_run = run_shell(full_command, repo_dir, timeout)
        result["buggy_run"] = buggy_run
        result["buggy"] = classify_run(buggy_run)

        result["pre_patch_reset"] = git_reset_to(repo_dir, issue["base_commit"], clean=not preserve_build_artifacts)
        patch_result = apply_patch_text(repo_dir, issue.get("patch", ""))
        if patch_result["returncode"] != 0:
            result["patch_apply_initial"] = patch_result
            result["patch_retry_reset"] = git_reset_to(repo_dir, issue["base_commit"], clean=not preserve_build_artifacts)
            patch_result = apply_patch_text(repo_dir, issue.get("patch", ""))
        result["patch_apply"] = patch_result
        if patch_result["returncode"] != 0:
            result["fixed"] = {}
            result["success"] = False
            result["status"] = "PATCH_APPLY_ERROR"
            return result
        write_generated_test(repo_dir, rel_file, code)
        if setup and (
            not preserve_build_artifacts
            or patch_requires_rebuild(str(issue.get("patch") or ""))
        ):
            result["fixed_setup"] = run_setup_with_fallback(setup_full_command, repo_dir, timeout)
            if result["fixed_setup"]["returncode"] != 0:
                result["fixed"] = classify_run(result["fixed_setup"])
                result["success"] = False
                result["status"] = "FIXED_SETUP_ERROR"
                result["env_error_category"] = classify_env_error(
                    (result["fixed_setup"].get("stdout") or "") + "\n" + (result["fixed_setup"].get("stderr") or "")
                )
                return result
        fixed_run = run_shell(full_command, repo_dir, timeout)
        result["fixed_run"] = fixed_run
        result["fixed"] = classify_run(fixed_run)
        result["success"] = bool(result["buggy"]["failed"] and not result["fixed"]["failed"])
        if result["success"]:
            result["status"] = "F2P_SUCCESS"
        elif not result["buggy"]["failed"]:
            result["status"] = "BUGGY_PASS"
        elif result["fixed"]["failed"]:
            result["status"] = "FIXED_FAIL"
        else:
            result["status"] = "UNKNOWN"
        return result
    except Exception as exc:  # noqa: BLE001
        result["success"] = False
        result["status"] = "ERROR"
        result["error"] = repr(exc)
        return result
    finally:
        try:
            git_reset_to(repo_dir, issue["base_commit"], clean=not preserve_build_artifacts)
        except Exception:
            pass
        if isolated_eval_worktree and cleanup_isolated_worktree:
            try:
                result["eval_worktree_cleanup"] = cleanup_eval_worktree(
                    issue, repo_root_base, eval_worktree_root, repo_dir
                )
            except Exception as exc:  # noqa: BLE001
                result["eval_worktree_cleanup_error"] = repr(exc)
        if isolated_eval_clone and cleanup_isolated_worktree:
            try:
                shutil.rmtree(repo_dir, ignore_errors=True)
                result["eval_clone_cleanup"] = {"removed": True}
            except Exception as exc:  # noqa: BLE001
                result["eval_clone_cleanup_error"] = repr(exc)


def group_ids_by_repo(issues: dict[str, dict[str, Any]], ids: list[str], workers: int) -> list[list[str]]:
    repo_groups: dict[str, list[str]] = {}
    for iid in ids:
        repo_groups.setdefault(issues[iid]["repo"], []).append(iid)
    buckets = [[] for _ in range(workers)]
    sizes = [0 for _ in range(workers)]
    for _, group in sorted(repo_groups.items(), key=lambda kv: len(kv[1]), reverse=True):
        idx = min(range(workers), key=lambda i: sizes[i])
        buckets[idx].extend(group)
        sizes[idx] += len(group)
    return buckets


ENV_ERROR_CATEGORIES = {
    "ENV_NOT_FOUND",
    "ENV_INCOMPLETE",
    "CONDA_LOCK",
    "DISK_FULL",
    "INSTALL_FAILURE",
    "COMMAND_RESOLUTION_FAILURE",
}


def result_env_category(result: dict[str, Any]) -> str:
    category = str(result.get("env_error_category") or "")
    if category:
        return category
    status = str(result.get("status") or "")
    if status in ENV_ERROR_CATEGORIES:
        return status
    return ""


def update_failfast_state(
    result: dict[str, Any],
    state: dict[str, Any],
    stop_event: threading.Event,
    min_count: int,
    ratio: float,
) -> None:
    with state["lock"]:
        state["total"] += 1
        category = result_env_category(result)
        if category:
            state["env_errors"] += 1
            state["by_env_error_category"][category] = state["by_env_error_category"].get(category, 0) + 1
        if (
            state["total"] >= max(1, min_count)
            and state["env_errors"] / max(1, state["total"]) >= ratio
        ):
            state["invalid_environment"] = True
            stop_event.set()


def run_bucket(
    worker_id: int,
    ids: list[str],
    issues: dict[str, dict[str, Any]],
    args: argparse.Namespace,
    stop_event: threading.Event,
    failfast_state: dict[str, Any],
) -> dict[str, Any]:
    worker_dir = Path(args.output_dir) / f"worker_{worker_id}"
    ensure_dir(worker_dir)
    out_path = worker_dir / "results.json"
    results = {}
    if args.resume and out_path.exists():
        results = json.loads(out_path.read_text(encoding="utf-8"))
    for iid in ids:
        if stop_event.is_set():
            break
        if iid in results:
            continue
        res = evaluate_one(
            issues[iid],
            args.generated_dir,
            args.repo_root_base,
            args.timeout,
            not args.no_setup,
            args.use_generated_worktrees,
            args.eval_worktree_root,
            args.eval_clone_root or str(Path(args.output_dir) / "eval_clones"),
            not args.keep_eval_worktrees,
        )
        results[iid] = res
        update_failfast_state(
            res,
            failfast_state,
            stop_event,
            args.env_failfast_min_count,
            args.env_failfast_ratio,
        )
        safe_json_dump(results, str(out_path))
        print(f"worker_{worker_id} {iid} {res.get('status')} success={res.get('success')}", flush=True)
    return {"worker_id": worker_id, "count": len(results), "path": str(out_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Direct same-directory F2P evaluator for BRT3 outputs.")
    parser.add_argument("--instances_path", required=True)
    parser.add_argument("--generated_dir", required=True)
    parser.add_argument("--repo_root_base", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--instance_id", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max_workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no_setup", action="store_true", help="Skip editable install refresh before each side.")
    parser.add_argument("--env_failfast_min_count", type=int, default=20)
    parser.add_argument("--env_failfast_ratio", type=float, default=0.30)
    parser.add_argument("--use_swebench_lite", action="store_true", help="Load SWE-bench/SWE-bench_Lite to fill patch fields for F2P validation.")
    parser.add_argument(
        "--use_generated_worktrees",
        action="store_true",
        help="Reuse each generated instance's prepared worktree and preserve its build artifacts.",
    )
    parser.add_argument(
        "--eval_worktree_root",
        default="",
        help=(
            "Directory for isolated per-instance formal-eval worktrees. "
            "This is an opt-in mode; the default uses isolated local clones."
        ),
    )
    parser.add_argument(
        "--eval_clone_root",
        default="",
        help=(
            "Directory for isolated per-instance formal-eval local clones. "
            "Defaults to <output_dir>/eval_clones when --use_generated_worktrees and --eval_worktree_root are not set."
        ),
    )
    parser.add_argument(
        "--keep_eval_worktrees",
        action="store_true",
        help="Keep isolated formal-eval clones/worktrees for debugging instead of removing them after each instance.",
    )
    return parser


def fill_patches_from_swebench_lite(issues: dict[str, dict[str, Any]]) -> None:
    try:
        from datasets import Dataset, load_dataset
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("datasets is required for --use_swebench_lite") from exc
    try:
        ds = load_dataset("SWE-bench/SWE-bench_Lite")["test"]
    except OSError as exc:
        # Managed/offline runs may have a readable Arrow cache but no write
        # permission for the adjacent datasets lock file.
        cache_root = Path(
            os.environ.get(
                "HF_DATASETS_CACHE",
                str(Path.home() / ".cache" / "huggingface" / "datasets"),
            )
        )
        candidates = sorted(
            cache_root.glob(
                "SWE-bench___swe-bench_lite/default/*/*/swe-bench_lite-test.arrow"
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise RuntimeError(
                "SWE-bench Lite cache is unavailable and the dataset could not be loaded"
            ) from exc
        ds = Dataset.from_file(str(candidates[0]))
    by_id = {row["instance_id"]: dict(row) for row in ds}
    for iid, row in issues.items():
        src = by_id.get(iid)
        if not src:
            continue
        for key in ["patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "environment_setup_commit"]:
            if key in src and not row.get(key):
                row[key] = src[key]


def main() -> None:
    args = build_parser().parse_args()
    ensure_dir(args.output_dir)
    tmp_root = os.environ.get("TMPDIR") or str(Path(args.output_dir) / "tmp")
    Path(tmp_root).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = tmp_root
    preflight = preflight_system(
        [
            args.output_dir,
            args.generated_dir,
            args.repo_root_base,
            tmp_root,
            str(Path.home()),
        ]
    )
    safe_json_dump(preflight, str(Path(args.output_dir) / "environment_preflight.json"))
    if not preflight.get("ok"):
        metrics = {
            "total_instances": 0,
            "f2p_success": 0,
            "f2p_fail": 0,
            "f2p_at_1": 0,
            "f2p_at_1_percent": 0,
            "by_status": {},
            "by_env_error_category": {"DISK_FULL": 1} if any(not item.get("ok") for item in preflight.get("checks", [])) else {"COMMAND_RESOLUTION_FAILURE": 1},
            "invalid_environment": True,
            "invalid_reason": "environment preflight failed",
            "environment_preflight": preflight,
            "generated_dir": args.generated_dir,
        }
        safe_json_dump({}, str(Path(args.output_dir) / "merged_results.json"))
        safe_json_dump(metrics, str(Path(args.output_dir) / "metrics.json"))
        print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)
        raise SystemExit(3)
    issues = load_issue_data(args.instances_path)
    if args.use_swebench_lite:
        fill_patches_from_swebench_lite(issues)
    ids = [args.instance_id] if args.instance_id else list(issues)
    ids = [iid for iid in ids if iid in issues]
    if args.limit:
        ids = ids[: args.limit]
    buckets = group_ids_by_repo(issues, ids, max(1, args.max_workers))
    summaries = []
    stop_event = threading.Event()
    failfast_state: dict[str, Any] = {
        "lock": threading.Lock(),
        "total": 0,
        "env_errors": 0,
        "by_env_error_category": {},
        "invalid_environment": False,
    }
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as pool:
        futures = [
            pool.submit(run_bucket, i, bucket, issues, args, stop_event, failfast_state)
            for i, bucket in enumerate(buckets)
            if bucket
        ]
        for future in as_completed(futures):
            summaries.append(future.result())
    merged = {}
    for item in summaries:
        path = item["path"]
        if os.path.exists(path):
            merged.update(json.loads(Path(path).read_text(encoding="utf-8")))
    total = len(merged)
    success = sum(1 for r in merged.values() if r.get("success"))
    by_status: dict[str, int] = {}
    by_env_error_category: dict[str, int] = {}
    for r in merged.values():
        by_status[r.get("status", "UNKNOWN")] = by_status.get(r.get("status", "UNKNOWN"), 0) + 1
        category = result_env_category(r)
        if category:
            by_env_error_category[category] = by_env_error_category.get(category, 0) + 1
    metrics = {
        "total_instances": total,
        "f2p_success": success,
        "f2p_fail": total - success,
        "f2p_at_1": success / total if total else 0,
        "f2p_at_1_percent": round(success / total * 100, 4) if total else 0,
        "by_status": by_status,
        "by_env_error_category": by_env_error_category,
        "invalid_environment": bool(failfast_state.get("invalid_environment")),
        "environment_preflight": preflight,
        "mode": (
            "generated_worktree_same_dir_new_file_no_author_injection"
            if args.use_generated_worktrees
            else "isolated_eval_worktree_same_dir_new_file_no_author_injection"
            if args.eval_worktree_root
            else "isolated_eval_clone_same_dir_new_file_no_author_injection"
        ),
        "generated_dir": args.generated_dir,
    }
    safe_json_dump(merged, str(Path(args.output_dir) / "merged_results.json"))
    safe_json_dump(metrics, str(Path(args.output_dir) / "metrics.json"))
    print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
