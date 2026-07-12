"""Lightweight model-call audit logging.

The audit log intentionally stores only routing metadata and counts. It must
not persist prompts, responses, or API keys.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterator


_LOCAL = threading.local()


@dataclass
class ModelCallContext:
    instance_id: str = ""
    repo: str = ""


def current_model_call_context() -> ModelCallContext:
    context = getattr(_LOCAL, "context", None)
    if isinstance(context, ModelCallContext):
        return context
    return ModelCallContext()


@contextmanager
def model_call_context(instance_id: str = "", repo: str = "") -> Iterator[None]:
    previous = getattr(_LOCAL, "context", None)
    _LOCAL.context = ModelCallContext(instance_id=instance_id, repo=repo)
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_LOCAL, "context")
            except AttributeError:
                pass
        else:
            _LOCAL.context = previous


def record_model_call(event: dict[str, Any]) -> None:
    path = os.environ.get("BRT4_MODEL_CALL_LOG", "").strip()
    if not path:
        return
    context = current_model_call_context()
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "thread_id": threading.get_ident(),
        "instance_id": context.instance_id,
        "repo": context.repo,
        **event,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(line)
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
