"""Vendored iCoRe-compatible environment and test command helpers."""

from __future__ import annotations

import ast
import fcntl
import json
import os
import re
import shlex
import subprocess
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    tomllib = None
from pathlib import Path
from typing import Any

from .icore_exec_spec import make_exec_spec
from ..runtime.conda_env_manager import (
    conda_env_inventory,
    default_env_name,
)


CONDA_SH = os.environ.get(
    "BRT3_CONDA_SH", "/root/miniconda3/etc/profile.d/conda.sh"
)


def env_name_for(repo: str, version: str) -> str:
    return default_env_name({"repo": repo, "version": version}, prefix=os.environ.get("BRT4_CONDA_ENV_PREFIX"))


def conda_env_names() -> set[str]:
    return set(conda_env_inventory())


def conda_env_exists(env_name: str) -> bool:
    return env_name in conda_env_names()


def _run_script(script: str, cwd: str, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["bash", "-lc", script],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "command": script,
            "cwd": cwd,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": script,
            "cwd": cwd,
            "returncode": 124,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": exc.stderr if isinstance(exc.stderr, str) else "",
            "timeout": True,
        }


def make_instance_spec(
    instance_id: str,
    repo: str,
    version: str,
    base_commit: str,
    environment_setup_commit: str,
) -> Any:
    spec = make_exec_spec(
        {
            "instance_id": instance_id,
            "repo": repo,
            "version": version,
            "base_commit": base_commit,
            "environment_setup_commit": environment_setup_commit or base_commit,
            "test_patch": "",
        }
    )
    spec.test_directives = []
    return spec


def ensure_icore_environment(
    spec: Any,
    env_name: str,
    cwd: str,
    timeout: int,
) -> dict[str, Any]:
    lock_path = Path(os.environ.get("TMPDIR") or "/tmp") / (
        "brt3_env_" + re.sub(r"[^A-Za-z0-9_.-]", "_", env_name) + ".lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if conda_env_exists(env_name):
            return {
                "status": "REUSED",
                "env_name": env_name,
                "created": False,
                "returncode": 0,
            }
        script = spec.env_script.replace(spec.env_name, env_name)
        script = _pin_python_before_conda_env_create(script)
        script = script.replace(
            "source ~/miniconda3/bin/activate", f"source {CONDA_SH}"
        )
        safe_env_name = re.sub(r"[^A-Za-z0-9_.-]", "_", env_name)
        requirements_path = Path(cwd) / (
            f".brt4_requirements_{safe_env_name}.txt"
        )
        for shared_path in (
            "$HOME/requirements.txt",
            "${HOME}/requirements.txt",
            "~/requirements.txt",
            "/root/requirements.txt",
        ):
            script = script.replace(shared_path, str(requirements_path))
        script = script.replace(
            f"rm {requirements_path}", f"rm -f {requirements_path}"
        )
        script_path = Path(cwd) / "brt3_icore_env_setup.sh"
        script_path.write_text(script, encoding="utf-8")
        result = _run_script(f"bash {script_path}", cwd, max(timeout, 1800))
        result.update(
            {
                "status": "CREATED" if result["returncode"] == 0 else "CREATE_ERROR",
                "env_name": env_name,
                "created": result["returncode"] == 0,
                "script_path": str(script_path),
            }
        )
        return result


def _pin_python_before_conda_env_create(script: str) -> str:
    """Move a post-create Python pin into an embedded environment.yml.

    Some iCoRe setup scripts create an unpinned environment first and only then
    install the requested Python version. Current conda can resolve packages for
    a newer Python that cannot later be downgraded. Pinning in the heredoc keeps
    the environment solve internally consistent without changing repository
    source or the requested Python version.
    """

    if "conda env create" not in script:
        return script
    match = re.search(
        r"conda install\s+python=([0-9]+(?:\.[0-9]+)?)\s+-y", script
    )
    if match is None:
        return script
    version = match.group(1)
    dependency_pattern = rf"(?m)^\s*-\s*python(?:\s*=\s*{re.escape(version)})?\s*$"
    if not re.search(dependency_pattern, script):
        script = re.sub(
            r"(?m)^dependencies:\s*$",
            f"dependencies:\n  - python={version}",
            script,
            count=1,
        )
    script = re.sub(
        rf"\s*&&\s*conda install\s+python={re.escape(version)}\s+-y",
        "",
        script,
        count=1,
    )
    script = re.sub(
        rf"(?m)^\s*conda install\s+python={re.escape(version)}\s+-y\s*$",
        ": # Python version pinned in environment.yml",
        script,
        count=1,
    )
    return script


def env_lock_path(env_name: str, suffix: str) -> Path:
    return Path(os.environ.get("TMPDIR") or "/tmp") / (
        "brt3_env_"
        + re.sub(r"[^A-Za-z0-9_.-]", "_", env_name)
        + f"_{suffix}.lock"
    )


def _build_dependency_command(repo_path: str) -> str:
    pyproject = Path(repo_path) / "pyproject.toml"
    if not pyproject.exists() or tomllib is None:
        return ""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return ""
    requirements = data.get("build-system", {}).get("requires", [])
    selected = []
    for requirement in requirements:
        normalized = re.split(r"[<>=!~;\\[]", str(requirement), 1)[0]
        normalized = normalized.strip().lower().replace("_", "-")
        if normalized in {"setuptools", "oldest-supported-numpy", "numpy"}:
            continue
        selected.append(str(requirement))
    if not selected:
        return ""
    quoted = " ".join(shlex.quote(item) for item in selected)
    return f"python -m pip install {quoted}"


def icore_setup_command(spec: Any, repo_path: str = "") -> str:
    install = spec.install
    commands = list(install.get("pre_install", []))
    if (
        getattr(spec, "repo", "") == "matplotlib/matplotlib"
        and str(getattr(spec, "version", "")) in {"3.0", "3.1", "3.2", "3.3", "3.4"}
    ):
        # These releases still use setup.py develop/easy_install internals.
        # Newer setuptools can finish compiling and then fail while saving the
        # editable-install .pth file.
        commands.append(
            "python -m pip install --force-reinstall 'setuptools==75.1.0'"
        )
    if getattr(spec, "repo", "") == "astropy/astropy" and str(getattr(spec, "version", "")).startswith("1.3"):
        commands.append("python -m pip install --force-reinstall 'numpy==1.23.5' 'cython<3'")
    if repo_path and "--no-build-isolation" in str(install.get("install", "")):
        build_deps = _build_dependency_command(repo_path)
        if build_deps:
            commands.append(build_deps)
    if install.get("install"):
        commands.append(install["install"])
    commands.extend(install.get("eval_commands", []))
    command = " && ".join(commands)
    command = command.replace("--no-use-pep517 ", "")
    command = command.replace(
        "sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen",
        "(test ! -f /etc/locale.gen || (sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && (command -v locale-gen >/dev/null 2>&1 && locale-gen || true)))",
    )
    return command


def first_test_selector(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name.startswith("test"):
                        return f"{node.name}::{child.name}"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                return node.name
    return ""


def icore_test_command(
    repo: str,
    version: str,
    test_path: str,
    selector: str = "",
) -> str:
    project = repo.split("/")[-1]
    nodeid = test_path if not selector else f"{test_path}::{selector}"
    if project in {
        "astropy",
        "matplotlib",
        "flask",
        "xarray",
        "pylint",
        "scikit-learn",
        "sphinx",
        "requests",
    }:
        return (
            "python -m pytest --no-header --tb=short --show-capture=no "
            f"--disable-warnings -p no:cacheprovider {nodeid}"
        )
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
        suffix = f" -k {test_name}" if test_name else ""
        return (
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' "
            f"bin/test -C {test_path}{suffix}"
        )
    raise ValueError(f"unsupported iCoRe project: {repo} version={version}")


def dump_spec(spec: Any, path: str) -> None:
    data = {
        "instance_id": getattr(spec, "instance_id", ""),
        "repo": getattr(spec, "repo", ""),
        "version": getattr(spec, "version", ""),
        "base_commit": getattr(spec, "base_commit", ""),
        "environment_setup_commit": getattr(spec, "environment_setup_commit", ""),
        "env_name": getattr(spec, "env_name", ""),
        "install": getattr(spec, "install", {}),
        "test_cmd": getattr(spec, "test_cmd", ""),
    }
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
