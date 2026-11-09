"""Explicitly declare support for one tested skill/standard tuple."""

from __future__ import annotations

import argparse
import subprocess
import sys

from _skill_bootstrap import load_skill_package, repository_root

load_skill_package()

from ai_sdlc.compatibility import declare_compatibility
from ai_sdlc.errors import ValidationError


def _run(command: tuple[str, ...]) -> int:
    return subprocess.run(command, cwd=repository_root(), check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-version", required=True)
    parser.add_argument("--standard-version", required=True)
    args = parser.parse_args()
    command = (sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "tests/ai_sdlc", "-q")
    print("Declaration is a maintainer statement after the full test suite; it is not automatic proof of quality.")
    try:
        report = declare_compatibility(repository_root(), args.skill_version, args.standard_version, command, _run)
    except (OSError, ValidationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"declared skill {report.skill_version} -> standard {report.implemented_version} ({report.implemented_sha256})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
