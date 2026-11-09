"""Check the repository standard against the bundled skill declaration."""

from __future__ import annotations

from dataclasses import asdict
import argparse
import json

from _skill_bootstrap import load_skill_package, repository_root

skill_root = load_skill_package()

from ai_sdlc.compatibility import CompatibilityState, check_compatibility


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = check_compatibility(skill_root, repository_root())
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"compatibility: {report.state.value} ({report.mode})")
        print(f"skill={report.skill_version} source={report.source_version} snapshot={report.snapshot_version} implements={report.implemented_version}")
        print(report.detail)
        print(report.next_action)
    return 0 if report.state is CompatibilityState.COMPATIBLE else 2


if __name__ == "__main__":
    raise SystemExit(main())
