"""Command execution and result classification."""

from __future__ import annotations

import re
import os
import shlex
import subprocess
import time
from pathlib import Path

from ..core.schema import BehaviorTarget, ExecutionResult


DEFAULT_CONDA_SH = (
    "/root/conda/ENTER/etc/profile.d/conda.sh"
    if Path("/root/conda/ENTER/etc/profile.d/conda.sh").is_file()
    else "/root/miniconda3/etc/profile.d/conda.sh"
)
CONDA_SH = os.environ.get("BRT3_CONDA_SH", DEFAULT_CONDA_SH)


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


def _normalize_log_fragment(text: str) -> str:
    text = re.sub(r"/tmp/[^\s:]+", "<tmp>", text)
    text = re.sub(r"/var/folders/[^\s:]+", "<tmp>", text)
    text = re.sub(r"brt3_surrogate_[A-Za-z0-9_/-]+", "brt_surrogate", text)
    text = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?\b", "<timestamp>", text)
    text = re.sub(r"line \d+", "line N", text)
    text = re.sub(r":\d+(?::\d+)?", ":N", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _execution_failure_fields(status: str, stdout: str, stderr: str) -> dict[str, str]:
    text = f"{stdout}\n{stderr}"
    exception_type = ""
    exception_message = ""
    failure_location = ""
    top_project_frame = ""
    for raw in reversed(text.splitlines()):
        line = raw.strip()
        match = re.match(r"([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Failure)):\s*(.*)", line)
        if match:
            exception_type = match.group(1).rsplit(".", 1)[-1]
            exception_message = _normalize_log_fragment(match.group(2))
            break
    frame_pattern = re.compile(r'File "([^"]+)", line \d+(?:, in ([A-Za-z_][A-Za-z0-9_]*))?')
    for match in frame_pattern.finditer(text):
        path = match.group(1)
        function = match.group(2) or ""
        normalized = _normalize_log_fragment(f"{path}::{function}")
        if not failure_location:
            failure_location = normalized
        if "site-packages" not in path and "dist-packages" not in path:
            top_project_frame = normalized
            break
    signature_parts = [
        status,
        exception_type,
        exception_message,
        top_project_frame or failure_location,
    ]
    return {
        "exception_type": exception_type,
        "exception_message_normalized": exception_message,
        "failure_location": failure_location,
        "top_project_frame": top_project_frame,
        "normalized_failure_signature": "|".join(
            part for part in signature_parts if part
        )[:1000],
    }


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
            f"source {shlex.quote(CONDA_SH)} && "
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
        failure_fields = _execution_failure_fields("TIMEOUT", stdout, stderr)
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
            outcome="TIMEOUT",
            return_code=124,
            **failure_fields,
        )
    except FileNotFoundError:
        fallback = (
            f"{pythonpath} && {command}"
            if no_conda or not conda_env
            else f"source {shlex.quote(CONDA_SH)} && conda activate {shlex.quote(conda_env)} && {pythonpath} && {command}"
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
    failure_fields = _execution_failure_fields(status, proc.stdout, proc.stderr)
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
        outcome=status,
        return_code=proc.returncode,
        **failure_fields,
    )
