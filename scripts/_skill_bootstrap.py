"""Load the repository's bundled skill package for maintenance commands."""

from __future__ import annotations

from pathlib import Path
import sys


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_skill_package() -> Path:
    root = repository_root() / "skills" / "ai-sdlc-orchestrator"
    scripts = str(root / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return root
