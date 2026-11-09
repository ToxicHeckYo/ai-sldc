"""Command-line scaffold for the portable AI SDLC orchestrator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from collections.abc import Sequence
import subprocess
import re
from typing import Any

import yaml


COMMANDS = ("init", "resume", "advance", "check", "status", "compatibility")


class _CommitConfirmedError(RuntimeError):
    """Post-commit verification failure carrying a durable commit identity."""

    def __init__(self, message: str, identity: str) -> None:
        super().__init__(message)
        self.commit_identity = identity


class GitCommitAdapter:
    """Host adapter that confirms a real Git commit by its resolved SHA."""

    def commit(self, root: Path, paths: tuple[str, ...], message: str) -> str:
        expected = tuple(dict.fromkeys(Path(path).as_posix() for path in paths))
        if not expected:
            raise RuntimeError("Git checkpoint has no paths to commit")
        try:
            actual = tuple(sorted(_changed_paths(root, expected)))
            if not actual:
                raise RuntimeError("Git checkpoint has no changes to commit")
            subprocess.run(["git", "-C", str(root), "add", "--", *actual], check=True, capture_output=True, text=True)
            staged = _staged_paths(root, actual)
            if staged != set(actual):
                raise RuntimeError(f"Git checkpoint precondition changed: staged={sorted(staged)!r}, expected={sorted(actual)!r}")
            subprocess.run(["git", "-C", str(root), "commit", "--only", "-m", message, "--", *actual], check=True, capture_output=True, text=True)
            result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(f"Git checkpoint failed: {error}") from error
        revision = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
            raise RuntimeError("Git checkpoint did not return a resolved commit SHA")
        try:
            changed = subprocess.run(
                ["git", "-C", str(root), "diff-tree", "--no-commit-id", "--name-only", "-r", revision, "--"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
        except (OSError, subprocess.CalledProcessError) as error:
            raise _CommitConfirmedError(f"Git checkpoint cannot verify committed paths: {error}", revision) from error
        if set(changed) != set(actual):
            raise _CommitConfirmedError(f"Git checkpoint changed unexpected paths: {changed!r}", revision)
        return revision


def _git_path_set(root: Path, command: list[str], paths: tuple[str, ...]) -> set[str]:
    result = subprocess.run(["git", "-C", str(root), *command, "--", *paths], check=True, capture_output=True, text=True)
    values: set[str] = set()
    for record in result.stdout.split("\0") if "-z" in command else result.stdout.splitlines():
        if not record:
            continue
        if "-z" in command:
            if len(record) < 4:
                continue
            value = record[3:]
            if " -> " in value:
                value = value.rsplit(" -> ", 1)[-1]
        else:
            value = record
        values.add(Path(value).as_posix())
    return values


def _changed_paths(root: Path, paths: tuple[str, ...]) -> set[str]:
    return _git_path_set(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], paths)


def _staged_paths(root: Path, paths: tuple[str, ...]) -> set[str]:
    return _git_path_set(root, ["diff", "--cached", "--name-only"], paths)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-sdlc",
        description="Coordinate a governed software delivery lifecycle.",
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")
    init = subcommands.add_parser("init", help="Initialize a canonical project skeleton.")
    init.add_argument("root", nargs="?", default=".")
    init.add_argument("--standard", help="verified pinned skill/package root; standalone documents rejected")
    init.add_argument("--lock", help="Provider lock YAML.")
    resume = subcommands.add_parser("resume", help="Resume from canonical project state.")
    resume.add_argument("root", nargs="?", default=".")
    advance = subcommands.add_parser("advance", help="Integrate validated provider results.")
    advance.add_argument("root", nargs="?", default=".")
    advance.add_argument("--results", required=True, help="YAML list of staged provider results.")
    advance.add_argument("--approvals", help="YAML approval mapping.")
    check = subcommands.add_parser("check", help="Compatibility alias for status check.")
    check.add_argument("root", nargs="?", default=".")
    check.add_argument("--root", dest="root_option")
    status = subcommands.add_parser("status", help="Generate or check PROJECT_STATUS.md.")
    status_commands = status.add_subparsers(dest="status_command", required=True)
    for operation in ("generate", "check"):
        operation_parser = status_commands.add_parser(operation)
        operation_parser.add_argument("root", nargs="?", default=".", help="Project root (default: current directory).")
        operation_parser.add_argument("--root", dest="root_option", help="Project root (alternative to the positional argument).")
    compatibility = subcommands.add_parser("compatibility", help="Inspect bundled standard compatibility.")
    compatibility_commands = compatibility.add_subparsers(dest="compatibility_command", required=True)
    compatibility_check = compatibility_commands.add_parser("check")
    compatibility_check.add_argument("--repository")
    compatibility_check.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI scaffold and return a process-compatible exit status."""
    parser = _build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)

    if not arguments or arguments[0] in {"-h", "--help"}:
        parser.print_help()
        return 0 if arguments else 2

    try:
        parsed = parser.parse_args(arguments)
    except SystemExit as error:
        return int(error.code)

    from ai_sdlc.orchestrator import advance as advance_project, initialize_project, propose_next_actions, resume_project
    from ai_sdlc.status_view import check_status, generate_status
    from ai_sdlc.errors import OrchestratorError

    try:
        if parsed.command == "compatibility":
            from ai_sdlc.compatibility import CompatibilityState, check_compatibility

            skill_root = Path(__file__).resolve().parents[1]
            report = check_compatibility(skill_root, Path(parsed.repository) if parsed.repository else None)
            if parsed.json:
                print(json.dumps(report.to_dict(), ensure_ascii=False, separators=(",", ":")))
            else:
                print(f"compatibility: {report.state.value} ({report.mode})")
                print(f"skill={report.skill_version} source={report.source_version} snapshot={report.snapshot_version} implements={report.implemented_version}")
                print(report.detail)
                print(report.next_action)
            return 0 if report.state is CompatibilityState.COMPATIBLE else 2
        if parsed.command == "init":
            if not parsed.lock:
                raise OrchestratorError("--lock is required for init; provider selection must be explicit")
            lock = Path(parsed.lock)
            snapshot = initialize_project(Path(parsed.root), Path(parsed.standard) if parsed.standard else None, lock)
            print(f"initialized {snapshot.plan.project_id}")
            return 0
        if parsed.command == "resume":
            context = resume_project(Path(parsed.root))
            actions = propose_next_actions(context)
            print(f"resumed {context.snapshot.plan.project_id}; actions={len(actions.actions)}")
            return 0
        if parsed.command == "advance":
            values: Any = yaml.safe_load(Path(parsed.results).read_text(encoding="utf-8"))
            if not isinstance(values, list):
                raise OrchestratorError("--results YAML must contain a list")
            approvals = {}
            if parsed.approvals:
                loaded = yaml.safe_load(Path(parsed.approvals).read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    from ai_sdlc.approvals import Approval

                    for work_item, value in loaded.items():
                        if not isinstance(work_item, str) or not isinstance(value, dict):
                            raise OrchestratorError("--approvals YAML must map work items to approval records")
                        try:
                            approvals[work_item] = Approval(**value)
                        except (TypeError, ValueError) as error:
                            raise OrchestratorError(f"invalid approval for {work_item}: {error}") from error
                elif loaded is not None:
                    raise OrchestratorError("--approvals YAML must contain a mapping")
            snapshot = advance_project(Path(parsed.root), values, approvals, adapter=GitCommitAdapter())
            print(f"advanced {snapshot.plan.project_id}")
            return 0
        root = Path(getattr(parsed, "root_option", None) or parsed.root)
        if parsed.command == "check":
            result = check_status(root)
            print(result.message, file=sys.stderr if not result.is_valid else sys.stdout)
            return 0 if result.is_valid else 1
        if parsed.status_command == "generate":
            print(generate_status(root))
            return 0
        result = check_status(root)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, json.JSONDecodeError, TypeError, ValueError, OrchestratorError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(result.message, file=sys.stderr if not result.is_valid else sys.stdout)
    return 0 if result.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
