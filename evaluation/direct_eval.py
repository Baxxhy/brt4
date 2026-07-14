"""Direct same-directory evaluator for BRT3 generated tests.

This evaluator intentionally does not use iCoRe/Libro's insertion pipeline.
It copies each generated final_test.py into a new test_brt_*.py file under the
same directory as the primary related test, then executes only that file/test.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pickle
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..io.io_utils import load_issue_data
from ..core.utils import ensure_dir, safe_json_dump, sanitize_instance_id


CONDA_EXE = os.environ.get("CONDA_EXE", "/root/miniconda3/bin/conda")


def conda_activate_cmd(env_name: str) -> str:
    return f'eval "$("{CONDA_EXE}" shell.bash hook)" && conda activate {env_name}'


def env_name_for(issue: dict[str, Any]) -> str:
    repo = issue["repo"]
    version = issue["version"]
    owner, name = repo.split("/")
    return f"setup_{owner}_{name}__{version}"


def resolve_conda_env(env_name: str) -> str:
    if not env_name:
        return ""
    names: list[str] = []
    try:
        proc = subprocess.run(
            ["conda", "env", "list", "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        data = json.loads(proc.stdout or "{}")
        names = [Path(path).name for path in data.get("envs", [])]
    except Exception:
        names = []
    if not names:
        try:
            proc = subprocess.run(
                ["conda", "env", "list"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                names.append(line.split()[0])
        except Exception:
            return env_name
    if env_name in names:
        return env_name
    matches = sorted(name for name in names if name.endswith(env_name))
    for preferred in ("direct_brt_ecg_we1_", "direct_brt_ecg_we0_", "direct_brt_we1_", "direct_brt_we0_"):
        for name in matches:
            if name.startswith(preferred):
                return name
    return matches[-1] if matches else env_name


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


def git_reset_to(repo_dir: str, commit: str, clean: bool = True) -> dict[str, Any]:
    reset = run_shell(f"git reset --hard {shlex.quote(commit)}", repo_dir, 120)
    if reset["returncode"] != 0:
        retry = run_shell(f"git reset --hard {shlex.quote(commit)}", repo_dir, 120)
        reset = {"first": reset, "retry": retry, **retry}
    if reset["returncode"] != 0:
        raise RuntimeError(f"git reset failed in {repo_dir}: {reset.get('stderr') or reset.get('stdout')}")
    clean_result = None
    if clean:
        clean_result = run_shell("git clean -fdxq", repo_dir, 120)
        if clean_result["returncode"] != 0:
            retry = run_shell("git clean -fdxq", repo_dir, 120)
            clean_result = {"first": clean_result, "retry": retry, **retry}
        if clean_result["returncode"] != 0:
            raise RuntimeError(f"git clean failed in {repo_dir}: {clean_result.get('stderr') or clean_result.get('stdout')}")
    return {"reset": reset, "clean": clean_result}


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


def trace_test_command(
    command: str,
    coverage_dir: str,
) -> str:
    """Wrap a repository-native test command with stdlib trace collection."""

    output_dir = Path(coverage_dir)
    counts_file = output_dir / "trace_counts.dat"
    trace_parts = [
        "python",
        "-m",
        "trace",
        "--count",
        "--file",
        str(counts_file),
    ]
    # CPython trace only persists --file counts when it also emits a report.
    # Keep reports under the evaluation artifact directory, never in the repo.
    trace_parts.extend(["--coverdir", str(output_dir / "cover")])
    trace_prefix = " ".join(shlex.quote(part) for part in trace_parts)
    trace_prefix += ' --ignore-dir "${CONDA_PREFIX:-/root/miniconda3}:/root/miniconda3"'
    parts = shlex.split(command)
    env_parts: list[str] = []
    while parts and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]):
        env_parts.append(parts.pop(0))
    env_prefix = " ".join(shlex.quote(part) for part in env_parts)
    if env_prefix:
        env_prefix += " "
    if len(parts) >= 3 and parts[0] in {"python", "python3"} and parts[1] == "-m":
        module = parts[2]
        args = " ".join(shlex.quote(part) for part in parts[3:])
        return f"{env_prefix}{trace_prefix} --module {shlex.quote(module)} {args}".strip()
    if len(parts) >= 2 and parts[0] in {"python", "python3"}:
        args = " ".join(shlex.quote(part) for part in parts[1:])
        return f"{env_prefix}{trace_prefix} {args}".strip()
    if parts and parts[0] in {"pytest", "py.test"}:
        args = " ".join(shlex.quote(part) for part in parts[1:])
        return f"{env_prefix}{trace_prefix} --module pytest {args}".strip()
    if parts and parts[0] == "unittest":
        args = " ".join(shlex.quote(part) for part in parts[1:])
        return f"{env_prefix}{trace_prefix} --module unittest {args}".strip()
    return f"{env_prefix}{trace_prefix} {' '.join(shlex.quote(part) for part in parts)}".strip()


def patch_target_lines(patch_text: str) -> dict[str, list[int]]:
    """Extract post-patch Python line numbers added or replaced by a patch."""

    targets: dict[str, set[int]] = {}
    current_file = ""
    new_line: int | None = None
    for line in patch_text.splitlines():
        if line.startswith("+++ "):
            raw_path = line[4:].strip().split("\t", 1)[0]
            if raw_path == "/dev/null":
                current_file = ""
                new_line = None
                continue
            current_file = raw_path[2:] if raw_path.startswith("b/") else raw_path
            targets.setdefault(current_file, set())
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            new_line = int(match.group(1)) if match else None
            continue
        if not current_file or new_line is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            targets[current_file].add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1
    return {
        path: sorted(lines)
        for path, lines in targets.items()
        if lines and Path(path).suffix == ".py"
    }


def _cover_file_matches_target(cover_file: Path, coverage_dir: Path, target: str) -> bool:
    """Match both regular-package and namespace-package trace report layouts."""

    rel = cover_file.relative_to(coverage_dir).as_posix()
    if rel.startswith("cover/"):
        rel = rel[len("cover/") :]
    if rel.endswith(".cover"):
        rel = rel[: -len(".cover")]
    target_stem = target.replace("\\", "/")
    if target_stem.endswith(".py"):
        target_stem = target_stem[:-3]
    target_variants = {target_stem, target_stem.replace("/", ".")}
    for prefix in ("src/", "lib/"):
        if target_stem.startswith(prefix):
            stripped = target_stem[len(prefix) :]
            target_variants.update({stripped, stripped.replace("/", ".")})
    rel_variants = {rel, rel.replace("/", ".")}
    return any(
        actual == expected or actual.endswith("/" + expected) or actual.endswith("." + expected)
        for actual in rel_variants
        for expected in target_variants
    )


def _read_trace_counts(counts_file: Path) -> tuple[dict[tuple[str, int], int], str]:
    if not counts_file.is_file():
        return {}, "trace counts file was not produced"
    try:
        with counts_file.open("rb") as handle:
            payload = pickle.load(handle)  # noqa: S301 - locally generated trace artifact.
    except Exception as exc:  # noqa: BLE001
        return {}, f"unable to parse trace counts: {exc}"
    counts = payload[0] if isinstance(payload, tuple) and payload else payload
    if not isinstance(counts, dict):
        return {}, f"unexpected trace counts payload: {type(counts).__name__}"
    normalized: dict[tuple[str, int], int] = {}
    for key, value in counts.items():
        if (
            isinstance(key, tuple)
            and len(key) == 2
            and isinstance(key[0], str)
            and isinstance(key[1], int)
        ):
            normalized[(key[0], key[1])] = int(value or 0)
    return normalized, ""


def parse_patch_coverage(
    coverage_dir: Path,
    target_lines: dict[str, list[int]],
    repo_dir: str = "",
) -> dict[str, Any]:
    """Parse trace counts first and annotated .cover files as a fallback."""

    covered: dict[str, list[int]] = {path: [] for path in target_lines}
    matched_sources: dict[str, list[str]] = {path: [] for path in target_lines}
    counts, counts_error = _read_trace_counts(coverage_dir / "trace_counts.dat")
    repo_root = Path(repo_dir).resolve() if repo_dir else None
    for target, lines in target_lines.items():
        expected = (repo_root / target).resolve() if repo_root else None
        hits: set[int] = set()
        sources: set[str] = set()
        for (source, line_number), count in counts.items():
            if source.startswith("<"):
                continue
            source_path = Path(source).resolve()
            path_match = expected is not None and source_path == expected
            if not path_match and repo_root is not None:
                try:
                    path_match = source_path.relative_to(repo_root).as_posix() == target
                except ValueError:
                    path_match = False
            if not path_match:
                continue
            sources.add(str(source_path))
            if count > 0 and line_number in set(lines):
                hits.add(line_number)
        covered[target] = sorted(hits)
        matched_sources[target] = sorted(sources)

    cover_root = coverage_dir / "cover"
    coverage_files = list(cover_root.rglob("*.cover")) if cover_root.is_dir() else []
    for target, lines in target_lines.items():
        if matched_sources[target]:
            continue
        matching_files = [
            path for path in coverage_files if _cover_file_matches_target(path, cover_root, target)
        ]
        hits = set(covered[target])
        for cover_file in matching_files:
            matched_sources[target].append(str(cover_file))
            try:
                rows = cover_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for index, text in enumerate(rows, start=1):
                if index in set(lines) and re.match(r"\s*\d+:", text):
                    hits.add(index)
        covered[target] = sorted(hits)

    target_count = sum(len(lines) for lines in target_lines.values())
    covered_count = sum(len(lines) for lines in covered.values())
    matched_count = sum(bool(paths) for paths in matched_sources.values())
    if not target_lines:
        status = "NO_TARGET_LINES"
        failure_reason = "golden patch has no added or replaced Python target lines"
    elif not counts and not coverage_files:
        status = "PARSE_FAILED"
        failure_reason = counts_error or "no trace artifacts were produced"
    elif matched_count == 0:
        status = "PATH_MATCH_FAILED"
        failure_reason = "trace artifacts exist but no executed source path matches a patch target"
    else:
        status = "SUCCESS"
        failure_reason = ""
    return {
        "coverage_status": status,
        "failure_reason": failure_reason,
        "target_lines_by_file": target_lines,
        "covered_lines_by_file": covered,
        "matched_sources_by_file": matched_sources,
        "target_line_count": target_count,
        "covered_line_count": covered_count,
        "patch_coverage": covered_count / target_count if target_count else 0.0,
        "coverage_files_found": len(coverage_files),
        "trace_count_entries": len(counts),
        "trace_counts_error": counts_error,
    }


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
) -> dict[str, Any]:
    instance_id = issue["instance_id"]
    generated_worktree = Path(generated_dir) / instance_id / "worktree"
    repo_dir = (
        str(generated_worktree)
        if use_generated_worktree and generated_worktree.is_dir()
        else repo_path(repo_root_base, issue)
    )
    preserve_build_artifacts = use_generated_worktree and generated_worktree.is_dir()
    env_name = resolve_conda_env(env_name_for(issue))
    final_path = Path(generated_dir) / instance_id / "final_test.py"
    if not final_path.exists():
        return {"instance_id": instance_id, "status": "MISSING_GENERATED_TEST", "success": False}
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
        "repo_dir": repo_dir,
        "generated_final_test": str(final_path),
        "direct_test_repo_path": rel_file,
        "selector": selector,
        "test_command": command,
        "runner_parity": runner_parity,
        "worktree_mode": "generated_instance_worktree" if preserve_build_artifacts else "shared_repo",
    }
    try:
        result["buggy_reset"] = git_reset_to(repo_dir, issue["base_commit"], clean=not preserve_build_artifacts)
        write_generated_test(repo_dir, rel_file, code)
        # Refresh editable-install and namespace-package paths for every
        # instance. Reused conda environments otherwise retain another
        # instance's worktree in .pth/egg-link metadata.
        if setup:
            result["buggy_setup"] = run_shell(setup_full_command, repo_dir, timeout)
            if result["buggy_setup"]["returncode"] != 0:
                result["buggy"] = classify_run(result["buggy_setup"])
                result["fixed"] = {}
                result["success"] = False
                result["status"] = "BUGGY_SETUP_ERROR"
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
            result["fixed_setup"] = run_shell(setup_full_command, repo_dir, timeout)
            if result["fixed_setup"]["returncode"] != 0:
                result["fixed"] = classify_run(result["fixed_setup"])
                result["success"] = False
                result["status"] = "FIXED_SETUP_ERROR"
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


def run_bucket(worker_id: int, ids: list[str], issues: dict[str, dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    worker_dir = Path(args.output_dir) / f"worker_{worker_id}"
    ensure_dir(worker_dir)
    out_path = worker_dir / "results.json"
    results = {}
    if args.resume and out_path.exists():
        results = json.loads(out_path.read_text(encoding="utf-8"))
    for iid in ids:
        if iid in results:
            continue
        res = evaluate_one(
            issues[iid],
            args.generated_dir,
            args.repo_root_base,
            args.timeout,
            not args.no_setup,
            args.use_generated_worktrees,
        )
        results[iid] = res
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
    parser.add_argument("--use_swebench_lite", action="store_true", help="Load SWE-bench/SWE-bench_Lite to fill patch fields for F2P validation.")
    parser.add_argument(
        "--use_generated_worktrees",
        action="store_true",
        help="Reuse each generated instance's prepared worktree and preserve its build artifacts.",
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
    issues = load_issue_data(args.instances_path)
    if args.use_swebench_lite:
        fill_patches_from_swebench_lite(issues)
    ids = [args.instance_id] if args.instance_id else list(issues)
    ids = [iid for iid in ids if iid in issues]
    if args.limit:
        ids = ids[: args.limit]
    buckets = group_ids_by_repo(issues, ids, max(1, args.max_workers))
    summaries = []
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as pool:
        futures = [pool.submit(run_bucket, i, bucket, issues, args) for i, bucket in enumerate(buckets) if bucket]
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
    for r in merged.values():
        by_status[r.get("status", "UNKNOWN")] = by_status.get(r.get("status", "UNKNOWN"), 0) + 1
    metrics = {
        "total_instances": total,
        "f2p_success": success,
        "f2p_fail": total - success,
        "f2p_at_1": success / total if total else 0,
        "f2p_at_1_percent": round(success / total * 100, 4) if total else 0,
        "by_status": by_status,
        "mode": "direct_same_dir_new_file_no_author_injection",
        "generated_dir": args.generated_dir,
    }
    safe_json_dump(merged, str(Path(args.output_dir) / "merged_results.json"))
    safe_json_dump(metrics, str(Path(args.output_dir) / "metrics.json"))
    print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
