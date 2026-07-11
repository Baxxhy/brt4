"""Conda environment identity, resolution, and health checks for BRT4.

The generation path is the source of truth for the environment actually used by
an instance. Formal evaluation must prefer that recorded identity instead of
reconstructing an environment name from the run name.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


CONDA_EXE = os.environ.get("CONDA_EXE", "/root/miniconda3/bin/conda")
ENV_NAMING_SCHEME_VERSION = "brt4-conda-env-v2"

_INVENTORY_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_HEALTH_CACHE: dict[str, dict[str, Any]] = {}


def sanitize_env_component(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:96]


def default_env_name(issue: dict[str, Any], prefix: str | None = None) -> str:
    repo = str(issue.get("repo") or "")
    version = str(issue.get("version") or "")
    if not repo or "/" not in repo or not version:
        return ""
    owner, name = repo.split("/", 1)
    if prefix is None:
        prefix = str(os.environ.get("BRT4_CONDA_ENV_PREFIX") or "")
    return (
        f"{sanitize_env_component(prefix)}"
        f"setup_{sanitize_env_component(owner)}_{sanitize_env_component(name)}__"
        f"{sanitize_env_component(version)}"
    )


def conda_activate_cmd(env_name: str) -> str:
    return f'eval "$({shlex.quote(CONDA_EXE)} shell.bash hook)" && conda activate {shlex.quote(env_name)}'


def _parse_conda_env_list_text(stdout: str) -> dict[str, str]:
    envs: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not parts:
            continue
        if "*" in parts:
            parts = [part for part in parts if part != "*"]
        if len(parts) >= 2 and parts[-1].startswith("/"):
            envs[parts[0]] = parts[-1]
    return envs


def conda_env_inventory(refresh: bool = False, timeout: int = 30) -> dict[str, str]:
    cache_key = CONDA_EXE
    cached = _INVENTORY_CACHE.get(cache_key)
    if cached and not refresh and time.time() - cached[0] < 30:
        return dict(cached[1])
    envs: dict[str, str] = {}
    try:
        proc = subprocess.run(
            [CONDA_EXE, "env", "list", "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        data = json.loads(proc.stdout or "{}")
        for path in data.get("envs", []):
            name = Path(str(path)).name
            if name:
                envs[name] = str(path)
    except Exception:
        envs = {}
    if not envs:
        try:
            proc = subprocess.run(
                [CONDA_EXE, "env", "list"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            envs = _parse_conda_env_list_text(proc.stdout or "")
        except Exception:
            envs = {}
    _INVENTORY_CACHE[cache_key] = (time.time(), dict(envs))
    return envs


def env_exists(env_name: str, inventory: dict[str, str] | None = None) -> bool:
    return bool(env_name) and env_name in (inventory if inventory is not None else conda_env_inventory())


def env_health_check(env_name: str, timeout: int = 60, refresh: bool = False) -> dict[str, Any]:
    if not env_name:
        return {"ok": False, "category": "COMMAND_RESOLUTION_FAILURE", "reason": "empty env name"}
    inventory = conda_env_inventory(refresh=refresh)
    env_path = inventory.get(env_name, "")
    if not env_path:
        return {
            "ok": False,
            "category": "ENV_NOT_FOUND",
            "env_name": env_name,
            "env_path": "",
            "reason": "conda environment not found",
        }
    if env_name in _HEALTH_CACHE and not refresh:
        return dict(_HEALTH_CACHE[env_name])
    proc = subprocess.run(
        [
            CONDA_EXE,
            "run",
            "-n",
            env_name,
            "python",
            "-c",
            (
                "import json,sys,sysconfig; "
                "print(json.dumps({'executable': sys.executable, "
                "'version': sys.version.split()[0], "
                "'purelib': sysconfig.get_paths().get('purelib','')}))"
            ),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    ok = proc.returncode == 0
    category = "" if ok else classify_env_error(proc.stdout + "\n" + proc.stderr)
    info: dict[str, Any] = {
        "ok": ok,
        "category": category,
        "env_name": env_name,
        "env_path": env_path,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }
    if ok:
        try:
            parsed = json.loads((proc.stdout or "").strip().splitlines()[-1])
            info.update(parsed)
        except Exception:
            pass
    _HEALTH_CACHE[env_name] = dict(info)
    return info


def classify_env_error(text: str) -> str:
    low = (text or "").lower()
    if "environmentnamenotfound" in low or "could not find conda environment" in low:
        return "ENV_NOT_FOUND"
    if "no space left on device" in low or "disk quota exceeded" in low:
        return "DISK_FULL"
    if "lockerror" in low or "failed to acquire lock" in low or "index.lock" in low:
        return "CONDA_LOCK"
    if "command not found" in low or "conda:" in low and "not found" in low:
        return "COMMAND_RESOLUTION_FAILURE"
    if "modulenotfounderror" in low or "importerror" in low:
        return "ENV_INCOMPLETE"
    if "egg-link" in low and "does not match installed location" in low:
        return "INSTALL_FAILURE"
    if "failed building wheel" in low or "subprocess-exited-with-error" in low:
        return "INSTALL_FAILURE"
    if "setup.py" in low or "pip install" in low:
        return "INSTALL_FAILURE"
    return "ENV_INCOMPLETE"


def extract_env_records(instance_id: str, generated_dir: str) -> list[dict[str, Any]]:
    instance_dir = Path(generated_dir) / instance_id
    records: list[dict[str, Any]] = []
    for filename in ("repo_prepare.json", "summary.json"):
        path = instance_dir / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data.get("env_name"), str) and data["env_name"]:
            records.append({"env_name": data["env_name"], "source": f"{filename}:env_name"})
        environment = data.get("environment")
        if isinstance(environment, dict) and isinstance(environment.get("env_name"), str):
            records.append(
                {
                    "env_name": environment["env_name"],
                    "source": f"{filename}:environment.env_name",
                    "setup_status": environment.get("status"),
                }
            )
        repo_prepare = data.get("repo_prepare")
        if isinstance(repo_prepare, dict):
            if isinstance(repo_prepare.get("env_name"), str) and repo_prepare["env_name"]:
                records.append({"env_name": repo_prepare["env_name"], "source": f"{filename}:repo_prepare.env_name"})
            nested = repo_prepare.get("environment")
            if isinstance(nested, dict) and isinstance(nested.get("env_name"), str):
                records.append(
                    {
                        "env_name": nested["env_name"],
                        "source": f"{filename}:repo_prepare.environment.env_name",
                        "setup_status": nested.get("status"),
                    }
                )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (record.get("env_name", ""), record.get("source", ""))
        if key not in seen:
            deduped.append(record)
            seen.add(key)
    return deduped


@dataclass
class EnvResolution:
    requested_env: str
    recorded_env: str
    resolved_env: str
    resolution_source: str
    env_exists: bool
    env_path: str = ""
    env_health: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    legacy_fallback_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_generation_env(issue: dict[str, Any], explicit_env: str = "", run_prefix: str | None = None) -> EnvResolution:
    requested = explicit_env or default_env_name(issue, run_prefix)
    health = env_health_check(requested) if requested else {"ok": False, "category": "COMMAND_RESOLUTION_FAILURE"}
    return EnvResolution(
        requested_env=requested,
        recorded_env=requested,
        resolved_env=requested,
        resolution_source="explicit_cli" if explicit_env else "default_generation_name",
        env_exists=bool(health.get("ok") or health.get("env_path")),
        env_path=str(health.get("env_path") or ""),
        env_health=health,
    )


def resolve_eval_env(
    issue: dict[str, Any],
    generated_dir: str,
    run_prefix: str | None = None,
    allow_legacy: bool = True,
    health_timeout: int = 60,
) -> EnvResolution:
    instance_id = str(issue.get("instance_id") or "")
    requested = default_env_name(issue, run_prefix)
    records = extract_env_records(instance_id, generated_dir)
    inventory = conda_env_inventory()
    if records:
        candidates: list[dict[str, Any]] = []
        for record in records:
            env_name = str(record.get("env_name") or "")
            exists = env_name in inventory
            candidate = dict(record)
            candidate.update({"exists": exists, "env_path": inventory.get(env_name, "")})
            candidates.append(candidate)
            if exists:
                health = env_health_check(env_name, timeout=health_timeout)
                return EnvResolution(
                    requested_env=requested,
                    recorded_env=env_name,
                    resolved_env=env_name,
                    resolution_source=str(record.get("source") or "generation_metadata"),
                    env_exists=True,
                    env_path=inventory.get(env_name, ""),
                    env_health=health,
                    candidates=candidates,
                )
        first = str(records[0].get("env_name") or "")
        return EnvResolution(
            requested_env=requested,
            recorded_env=first,
            resolved_env=first,
            resolution_source="generation_metadata_missing_env",
            env_exists=False,
            env_path="",
            env_health={
                "ok": False,
                "category": "ENV_NOT_FOUND",
                "env_name": first,
                "reason": "generation metadata records an environment that does not exist",
            },
            candidates=candidates,
            errors=["recorded environment does not exist; refusing fallback to another run env"],
        )
    candidates = []
    legacy_names = []
    if requested:
        legacy_names.append((requested, "legacy_default_run_prefix"))
    unprefixed = default_env_name(issue, prefix="")
    if allow_legacy and unprefixed and unprefixed != requested:
        legacy_names.append((unprefixed, "legacy_unprefixed_default"))
    for env_name, source in legacy_names:
        candidate = {"env_name": env_name, "source": source, "exists": env_name in inventory, "env_path": inventory.get(env_name, "")}
        candidates.append(candidate)
        if candidate["exists"]:
            health = env_health_check(env_name, timeout=health_timeout)
            return EnvResolution(
                requested_env=requested,
                recorded_env="",
                resolved_env=env_name,
                resolution_source=source,
                env_exists=True,
                env_path=inventory.get(env_name, ""),
                env_health=health,
                candidates=candidates,
                warnings=["generation env metadata missing; legacy fallback used"],
                legacy_fallback_used=True,
            )
    fallback = legacy_names[0][0] if legacy_names else requested
    return EnvResolution(
        requested_env=requested,
        recorded_env="",
        resolved_env=fallback,
        resolution_source="legacy_resolution_failed",
        env_exists=False,
        env_health={"ok": False, "category": "ENV_NOT_FOUND", "env_name": fallback},
        candidates=candidates,
        errors=["no generation env metadata and no legacy candidate exists"],
        legacy_fallback_used=True,
    )


def environment_identity_metadata(
    issue: dict[str, Any],
    env_name: str,
    run_id: str = "",
    setup_status: str = "",
    setup_script_fingerprint: str = "",
    source: str = "generation",
) -> dict[str, Any]:
    health = env_health_check(env_name) if env_name else {"ok": False, "category": "COMMAND_RESOLUTION_FAILURE"}
    return {
        "scheme_version": ENV_NAMING_SCHEME_VERSION,
        "env_name": env_name,
        "env_path": health.get("env_path", ""),
        "repo": issue.get("repo", ""),
        "version": issue.get("version", ""),
        "base_commit": issue.get("base_commit", ""),
        "environment_setup_commit": issue.get("environment_setup_commit") or issue.get("base_commit", ""),
        "run_id": run_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "setup_status": setup_status,
        "python_version": health.get("version", ""),
        "env_health": health,
        "setup_script_fingerprint": setup_script_fingerprint,
        "source": source,
    }


def preflight_system(paths: list[str], min_free_gb: float = 2.0, min_free_inodes: int = 10000) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    ok = True
    for raw in paths:
        path = Path(raw).expanduser()
        probe = path if path.exists() else path.parent
        try:
            usage = shutil.disk_usage(probe)
            statvfs = os.statvfs(probe)
            free_gb = usage.free / (1024**3)
            free_inodes = statvfs.f_favail
            item = {
                "path": str(path),
                "probe": str(probe),
                "free_gb": round(free_gb, 3),
                "free_inodes": int(free_inodes),
                "ok": free_gb >= min_free_gb and free_inodes >= min_free_inodes,
            }
        except OSError as exc:
            item = {"path": str(path), "ok": False, "error": repr(exc)}
        checks.append(item)
        ok = ok and bool(item.get("ok"))
    conda_ok = Path(CONDA_EXE).exists()
    if conda_ok:
        try:
            proc = subprocess.run([CONDA_EXE, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
            conda_ok = proc.returncode == 0
            conda_version = (proc.stdout or proc.stderr).strip()
        except Exception as exc:
            conda_ok = False
            conda_version = repr(exc)
    else:
        conda_version = "missing"
    ok = ok and conda_ok
    return {
        "ok": ok,
        "checks": checks,
        "conda_exe": CONDA_EXE,
        "conda_ok": conda_ok,
        "conda_version": conda_version,
    }
