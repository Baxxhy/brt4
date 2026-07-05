"""Load stage prompts from markdown files."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


PROMPTS_ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load_prompt(stage: str, name: str) -> str:
    path = PROMPTS_ROOT / stage / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt not found: {stage}/{name}.md")
    return path.read_text(encoding="utf-8").rstrip("\n")
