"""Strict Git checkpoint adapter used by coordinator-owned transactions."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


class GitCommitAdapter:
    """Commit exactly the requested paths and verify the resulting Git log."""

    def commit(self, root: Path, paths: tuple[str, ...], message: str) -> str:
        expected = tuple(dict.fromkeys(Path(path).as_posix() for path in paths))
        if not expected:
            raise RuntimeError("Git checkpoint has no paths to commit")
        try:
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *expected],
                check=True, capture_output=True,
            ).stdout.decode("utf-8")
            actual = set()
            for record in status.split("\0"):
                if not record:
                    continue
                actual.add(Path(record[3:] if len(record) >= 4 else record).as_posix())
            if not actual:
                raise RuntimeError("Git checkpoint has no changes to commit")
            subprocess.run(["git", "-C", str(root), "add", "--", *sorted(actual)], check=True, capture_output=True)
            staged = set(subprocess.run(
                ["git", "-C", str(root), "diff", "--cached", "--name-only", "--", *sorted(actual)],
                check=True, capture_output=True,
            ).stdout.decode("utf-8").splitlines())
            if staged != actual:
                raise RuntimeError(f"Git checkpoint staged paths differ: staged={sorted(staged)!r}, expected={sorted(actual)!r}")
            subprocess.run(
                ["git", "-C", str(root), "commit", "--only", "-m", message, "--", *sorted(actual)],
                check=True, capture_output=True,
            )
            revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True).stdout.decode().strip()
            if re.fullmatch(r"[0-9a-fA-F]{40}", revision) is None:
                raise RuntimeError("Git checkpoint did not return a resolved commit SHA")
            committed = set(subprocess.run(
                ["git", "-C", str(root), "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", revision, "--"],
                check=True, capture_output=True,
            ).stdout.decode().splitlines())
            if committed != actual:
                raise RuntimeError(f"Git checkpoint changed unexpected paths: {sorted(committed)!r}")
            log = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%H %s"], check=True, capture_output=True).stdout.decode()
            if not log.startswith(revision + " ") or message not in log:
                raise RuntimeError("Git checkpoint log does not confirm the requested commit")
            return revision
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(f"Git checkpoint failed: {error}") from error


__all__ = ["GitCommitAdapter"]
