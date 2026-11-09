"""Transactional synchronization tests for the canonical standard snapshot."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "ai-sdlc-orchestrator"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.errors import ValidationError
from ai_sdlc.standard_sync import check_standard_sync, recover_standard_sync, sync_standard


def copied_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "skills").mkdir(parents=True)
    shutil.copy2(REPOSITORY_ROOT / "AI_ASSISTED_DEVELOPMENT_STANDARD.md", root)
    shutil.copy2(REPOSITORY_ROOT / "standard-manifest.yaml", root)
    shutil.copytree(SKILL_ROOT, root / "skills" / "ai-sdlc-orchestrator")
    return root


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_clean_check_is_read_only(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    status = check_standard_sync(root)

    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert status.clean is True
    assert status.state == "clean"
    assert before == after


def test_check_reports_stale_snapshot_without_writing(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    snapshot = root / "skills/ai-sdlc-orchestrator/references/standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    snapshot.write_bytes(snapshot.read_bytes() + b"stale")

    status = check_standard_sync(root)

    assert status.clean is False
    assert status.state == "snapshot-stale"


def test_root_check_cli_is_read_only_and_machine_readable() -> None:
    tracked = [
        REPOSITORY_ROOT / "standard-manifest.yaml",
        SKILL_ROOT / "references/standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md",
        SKILL_ROOT / "references/standard/manifest.yaml",
        SKILL_ROOT / "references/standard/compatibility.yaml",
    ]
    before = {path: path.read_bytes() for path in tracked}

    result = subprocess.run(
        [sys.executable, "scripts/sync_standard.py", "--check", "--json"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "clean"
    assert {path: path.read_bytes() for path in tracked} == before


def test_sync_rejects_changed_bytes_without_version_bump(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    source = root / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    source.write_bytes(source.read_bytes() + b"editorial change")

    with pytest.raises(ValidationError, match="version bump"):
        sync_standard(root)


def test_sync_updates_snapshot_and_manifests_but_not_compatibility(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    source = root / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    root_manifest = root / "standard-manifest.yaml"
    compatibility = root / "skills/ai-sdlc-orchestrator/references/standard/compatibility.yaml"
    source.write_text(source.read_text(encoding="utf-8").replace("**Версия:** 4.0.0", "**Версия:** 4.0.1"), encoding="utf-8", newline="\n")
    root_manifest.write_text(root_manifest.read_text(encoding="utf-8").replace('version: "4.0.0"', 'version: "4.0.1"'), encoding="utf-8", newline="\n")
    compatibility_before = compatibility.read_bytes()

    status = sync_standard(root)

    snapshot = root / "skills/ai-sdlc-orchestrator/references/standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    snapshot_manifest = root / "skills/ai-sdlc-orchestrator/references/standard/manifest.yaml"
    assert status.clean is True
    assert snapshot.read_bytes() == source.read_bytes()
    assert digest(snapshot) in snapshot_manifest.read_text(encoding="utf-8")
    assert digest(source) in root_manifest.read_text(encoding="utf-8")
    assert compatibility.read_bytes() == compatibility_before


def test_normal_error_rolls_back_every_target(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    source = root / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    manifest = root / "standard-manifest.yaml"
    source.write_text(source.read_text(encoding="utf-8").replace("**Версия:** 4.0.0", "**Версия:** 4.0.1"), encoding="utf-8", newline="\n")
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('version: "4.0.0"', 'version: "4.0.1"'), encoding="utf-8", newline="\n")
    tracked = [manifest, root / "skills/ai-sdlc-orchestrator/references/standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md", root / "skills/ai-sdlc-orchestrator/references/standard/manifest.yaml"]
    before = {path: path.read_bytes() for path in tracked}
    calls = 0

    def fail_second(source_path: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replacement failure")
        source_path.replace(target_path)

    with pytest.raises(OSError, match="injected"):
        sync_standard(root, replace_file=fail_second)

    assert {path: path.read_bytes() for path in tracked} == before
    assert not (root / ".ai-sdlc-standard-sync").exists()


def test_preparation_failure_removes_incomplete_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = copied_repository(tmp_path)
    source = root / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    manifest = root / "standard-manifest.yaml"
    source.write_text(source.read_text(encoding="utf-8").replace("**Версия:** 4.0.0", "**Версия:** 4.0.1"), encoding="utf-8", newline="\n")
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('version: "4.0.0"', 'version: "4.0.1"'), encoding="utf-8", newline="\n")
    original_copy = shutil.copy2
    calls = 0

    def fail_backup(source_path: Path, target_path: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("backup failed")
        return original_copy(source_path, target_path)

    monkeypatch.setattr(shutil, "copy2", fail_backup)

    with pytest.raises(OSError, match="backup failed"):
        sync_standard(root)

    assert not (root / ".ai-sdlc-standard-sync").exists()
    assert not (root / ".ai-sdlc-standard-sync.preparing").exists()


def test_recovery_discards_unexposed_preparation_directory(tmp_path: Path) -> None:
    root = copied_repository(tmp_path)
    preparing = root / ".ai-sdlc-standard-sync.preparing"
    preparing.mkdir()
    (preparing / "partial").write_text("incomplete", encoding="utf-8")

    recovery = recover_standard_sync(root)

    assert recovery.recovered is True
    assert recovery.action == "discarded-preparation"
    assert not preparing.exists()


@pytest.mark.parametrize("crash_after", (1, 2, 3))
def test_next_check_recovers_an_interrupted_replacement(tmp_path: Path, crash_after: int) -> None:
    root = copied_repository(tmp_path)
    source = root / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    manifest = root / "standard-manifest.yaml"
    source.write_text(source.read_text(encoding="utf-8").replace("**Версия:** 4.0.0", "**Версия:** 4.0.1"), encoding="utf-8", newline="\n")
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('version: "4.0.0"', 'version: "4.0.1"'), encoding="utf-8", newline="\n")
    calls = 0

    def crash(source_path: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        source_path.replace(target_path)
        if calls == crash_after:
            raise SystemExit("simulated crash")

    with pytest.raises(SystemExit, match="simulated"):
        sync_standard(root, replace_file=crash)
    assert (root / ".ai-sdlc-standard-sync/transaction.yaml").is_file()

    recovery = recover_standard_sync(root)

    assert recovery.recovered is True
    assert not (root / ".ai-sdlc-standard-sync").exists()
    assert check_standard_sync(root).state in {"clean", "snapshot-stale"}
