"""Command execution and result classification."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path

from ..core.schema import BehaviorTarget, ExecutionResult


def _pythonpath_export(cwd: str) -> str:
    root = Path(cwd)
    entries = [str(root)]
    for relative in ("src", "lib"):
        path = root / relative
        if path.is_dir():
            entries.append(str(path))
    joined = ":".join(shlex.quote(entry) for entry in entries)
    return f"export PYTHONPATH={joined}:${{PYTHONPATH:-}}"


def classify_execution(returncode: int, stdout: str, stderr: str, timeout: bool, behavior: BehaviorTarget | None = None) -> str:
    text = f"{stdout}\n{stderr}"
    low = text.lower()
    if timeout:
        return "TIMEOUT"
    if returncode == 0:
        return "PASS"
    if "syntaxerror" in low or "indentationerror" in low:
        return "SYNTAX_ERROR"
    dependency_setup_markers = [
        "module 'numpy' has no attribute 'int'",
        "module 'numpy' has no attribute 'float'",
        "module 'numpy' has no attribute 'complex'",
        "failed to import the compiled extension",
        "cannot import name",
    ]
    issue_terms: list[str] = []
    symptom = ""
    if behavior:
        for obj in behavior.target_apis:
            name = str(obj.get("name") or "")
            if name:
                issue_terms.append(name.split(".")[-1])
        symptom = (
            str(behavior.error_symptom.get("text") or "")
            if isinstance(behavior.error_symptom, dict)
            else ""
        )
        issue_terms += [
            x for x in re.split(r"[^A-Za-z0-9_]+", symptom) if len(x) >= 4
        ]
    symptom_low = symptom.lower()
    if "nameerror:" in low and "nameerror" not in symptom_low:
        return "SETUP_ERROR"
    if (
        re.search(
            r"attributeerror:\s+['\"](?:test|test_)[^'\"]*['\"]"
            r"\s+object has no attribute",
            low,
        )
        and "attributeerror" not in symptom_low
    ):
        return "SETUP_ERROR"
    django_harness_markers = [
        "doesn't declare an explicit app_label",
        "isn't in an application in installed_apps",
        "apps aren't loaded yet",
        "appregistrynotready",
    ]
    if any(marker in low for marker in django_harness_markers):
        return "SETUP_ERROR"
    if (
        "django/core/management/__init__.py" in low
        and "fetch_command" in low
        and ("keyerror:" in low or "unknown command:" in low)
    ):
        return "SETUP_ERROR"
    if "noreversematch" in low and "noreversematch" not in symptom_low:
        return "SETUP_ERROR"
    if (
        ("sqlite3.operationalerror" in low or "django.db.utils.operationalerror" in low)
        and 'near "[]": syntax error' in low
        and not any(
            marker in symptom_low
            for marker in ("sqlite", "syntax error", "operationalerror")
        )
    ):
        return "SETUP_ERROR"
    if (
        "systemcheckerror" in low
        and "system check identified" in low
        and not any(
            marker in symptom_low
            for marker in ("system check", "systemcheckerror", "fields.e", "models.e")
        )
    ):
        return "SETUP_ERROR"
    setup_markers = [
        "importerror",
        "modulenotfounderror",
        "fixture",
        "settings are not configured",
        "improperlyconfigured",
    ]
    if any(s in low for s in dependency_setup_markers):
        return "SETUP_ERROR"
    if any(s in low for s in setup_markers):
        issue_describes_import_failure = any(
            marker in symptom_low
            for marker in ("importerror", "module not found", "cannot import")
        )
        if issue_describes_import_failure and any(
            term.lower() in low for term in issue_terms[:20]
        ):
            return "ISSUE_ALIGNED_FAIL"
        return "SETUP_ERROR"
    if any(
        s in low
        for s in [
            "collection error",
            "error collecting",
            "collected 0 items",
            "no tests ran",
            "not found:",
        ]
    ):
        return "COLLECT_ERROR"
    if issue_terms and any(term.lower() in low for term in issue_terms[:20]):
        return "ISSUE_ALIGNED_FAIL"
    if "assertionerror" in low or re.search(r"\bassert\b", low):
        return "ASSERTION_FAIL"
    return "UNRELATED_FAIL"


def run_command_in_conda(
    command: str,
    cwd: str,
    conda_env: str = "",
    timeout: int = 120,
    no_conda: bool = False,
    behavior: BehaviorTarget | None = None,
    instance_id: str = "",
) -> ExecutionResult:
    started = time.time()
    pythonpath = _pythonpath_export(cwd)
    if no_conda or not conda_env:
        shell_cmd = f"bash -lc {shlex.quote(pythonpath + ' && ' + command)}"
    else:
        activated = (
            "source /root/miniconda3/etc/profile.d/conda.sh && "
            f"conda activate {shlex.quote(conda_env)} && "
            f"{pythonpath} && "
            f"{command}"
        )
        shell_cmd = f"bash -lc {shlex.quote(activated)}"
    timed_out = False
    try:
        proc = subprocess.run(
            shell_cmd,
            shell=True,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        return ExecutionResult(
            instance_id=instance_id,
            command=shell_cmd,
            cwd=cwd,
            returncode=124,
            stdout=stdout,
            stderr=stderr,
            duration=time.time() - started,
            timeout=True,
            status="TIMEOUT",
            error_reason="command timed out",
        )
    except FileNotFoundError:
        fallback = (
            f"{pythonpath} && {command}"
            if no_conda or not conda_env
            else f"source ~/miniconda3/etc/profile.d/conda.sh && conda activate {shlex.quote(conda_env)} && {pythonpath} && {command}"
        )
        proc = subprocess.run(
            ["bash", "-lc", fallback],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        shell_cmd = fallback
    status = classify_execution(proc.returncode, proc.stdout, proc.stderr, timed_out, behavior)
    return ExecutionResult(
        instance_id=instance_id,
        command=shell_cmd,
        cwd=cwd,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration=time.time() - started,
        timeout=timed_out,
        status=status,
        error_reason=(
            ""
            if proc.returncode == 0
            else (proc.stdout + "\n" + proc.stderr)[-4000:]
        ),
    )
