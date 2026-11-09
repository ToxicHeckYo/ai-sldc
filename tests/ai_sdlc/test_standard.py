"""Contract tests for the pinned AI-assisted development standard."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "ai-sdlc-orchestrator"
STANDARD_HASH = "c2973549d4178ed76eb9275dbc0d6bcd62ff3827360d0c658393cb5b27532314"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from ai_sdlc.errors import ValidationError
from ai_sdlc.io import sha256_file
from ai_sdlc.standard import SemVer, load_standard_manifest, verify_standard


@pytest.mark.parametrize("value", ("4.0", "04.0.0", "4.0.0-rc.1", "4.0.0+build"))
def test_semver_rejects_non_release_or_noncanonical_values(value: str) -> None:
    """Relaxing SemVer syntax would make standard identities ambiguous."""
    with pytest.raises(ValidationError, match="SemVer"):
        SemVer.parse(value)


def test_semver_accepts_canonical_three_part_release() -> None:
    """The identity parser accepts the exact release notation used by manifests."""
    assert str(SemVer.parse("4.0.0")) == "4.0.0"


def test_manifest_rejects_additional_fields(tmp_path: Path) -> None:
    """An obsolete or unknown identity field cannot silently change semantics."""
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        'version: "4.0.0"\nupdated_at: "2026-11-02"\nsha256: "'
        + "a" * 64
        + '"\nsource_revision: "4.0.0"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="exactly"):
        load_standard_manifest(manifest)


def copied_skill_root(tmp_path: Path) -> Path:
    """Create an isolated copy so each integrity failure is real and local."""
    root = tmp_path / "ai-sdlc-orchestrator"
    shutil.copytree(SKILL_ROOT / "references", root / "references")
    return root


def test_sha256_file_matches_the_pinned_standard_bytes() -> None:
    """Changing any canonical source byte changes the recorded digest."""
    standard_path = SKILL_ROOT / "references" / "standard" / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"

    assert sha256_file(standard_path) == STANDARD_HASH


def test_verify_standard_returns_the_pinned_standard_identity() -> None:
    """A complete pinned copy identifies the agreed version 1.0.0 exactly."""
    identity = verify_standard(SKILL_ROOT)

    assert identity.version == "1.0.0"
    assert identity.sha256 == STANDARD_HASH


def test_verify_standard_uses_one_standard_byte_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement after hashing cannot supply an unverified document version."""
    root = copied_skill_root(tmp_path)
    standard_path = root / "references" / "standard" / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    snapshot = standard_path.read_bytes()
    original_read_bytes = Path.read_bytes
    read_count = 0

    def read_standard_once(path: Path, *args: object, **kwargs: object) -> bytes:
        nonlocal read_count
        if path == standard_path:
            read_count += 1
            if read_count > 1:
                raise AssertionError("verification must not read canonical standard bytes twice")
            standard_path.write_bytes(
                snapshot.replace(b"**\xd0\x92\xd0\xb5\xd1\x80\xd1\x81\xd0\xb8\xd1\x8f:** 1.0.0", b"**\xd0\x92\xd0\xb5\xd1\x80\xd1\x81\xd0\xb8\xd1\x8f:** 1.1.0")
            )
            return snapshot
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_standard_once)

    identity = verify_standard(root)

    assert read_count == 1
    assert identity.version == "1.0.0"
    assert identity.sha256 == STANDARD_HASH


def test_verify_standard_rejects_a_manifest_version_that_disagrees_with_the_document(
    tmp_path: Path,
) -> None:
    """A manifest that names a different version cannot authorize the standard."""
    root = copied_skill_root(tmp_path)
    manifest_path = root / "references" / "standard" / "manifest.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace('version: "1.0.0"', 'version: "1.1.0"'),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="version"):
        verify_standard(root)


def test_verify_standard_rejects_a_changed_standard_byte(tmp_path: Path) -> None:
    """A byte-level edit makes the copied canonical standard untrusted."""
    root = copied_skill_root(tmp_path)
    standard_path = root / "references" / "standard" / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    standard_path.write_bytes(standard_path.read_bytes() + b" ")

    with pytest.raises(ValidationError, match="SHA-256"):
        verify_standard(root)


def test_verify_standard_rejects_a_missing_standard_copy(tmp_path: Path) -> None:
    """Verification fails closed when the pinned standard is not present locally."""
    root = copied_skill_root(tmp_path)
    standard_path = root / "references" / "standard" / "AI_ASSISTED_DEVELOPMENT_STANDARD.md"
    standard_path.unlink()

    with pytest.raises(ValidationError, match="standard"):
        verify_standard(root)
