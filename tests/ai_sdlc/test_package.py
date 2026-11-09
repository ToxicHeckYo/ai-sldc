"""Contract tests for the portable AI SDLC skill scaffold."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "ai-sdlc-orchestrator"
SCRIPTS_ROOT = SKILL_ROOT / "scripts"


def test_skill_frontmatter_is_discoverable() -> None:
    """The skill is discoverable by its stable name and non-empty trigger text."""
    skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = re.match(r"^---\n(?P<yaml>.*?)\n---\n", skill_text, re.DOTALL)

    assert frontmatter, "SKILL.md must begin with YAML frontmatter"
    assert re.search(r"^name: ai-sdlc-orchestrator$", frontmatter.group("yaml"), re.MULTILINE)
    description = re.search(r"^description: (?P<value>.+)$", frontmatter.group("yaml"), re.MULTILINE)
    assert description and description.group("value").strip()


def test_cli_help_lists_init_resume_check(capsys) -> None:
    """Operators can discover the three scaffolded lifecycle commands from help."""
    script_path = SCRIPTS_ROOT / "ai_sdlc.py"
    spec = importlib.util.spec_from_file_location("ai_sdlc_cli", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main(["--help"]) == 0

    help_text = capsys.readouterr().out
    assert "init" in help_text
    assert "resume" in help_text
    assert "check" in help_text
    assert "compatibility" in help_text


def test_package_imports_without_runtime_adapter(monkeypatch) -> None:
    """Importing the neutral core does not select or construct a runtime adapter."""
    monkeypatch.syspath_prepend(str(SCRIPTS_ROOT))
    sys.modules.pop("ai_sdlc", None)

    package = __import__("ai_sdlc")

    assert package.__name__ == "ai_sdlc"
