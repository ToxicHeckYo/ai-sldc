"""Pin and verify the local canonical development standard."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any

import yaml

from .errors import ValidationError


_STANDARD_RELATIVE_PATH = Path("references/standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md")
_MANIFEST_RELATIVE_PATH = Path("references/standard/manifest.yaml")
_REQUIRED_MANIFEST_FIELDS = ("version", "updated_at", "sha256")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_PATTERN = re.compile(r"^\*\*Версия:\*\*\s*(?P<version>\S+)\s*$", re.MULTILINE)
_SEMVER_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")


@dataclass(frozen=True, order=True)
class SemVer:
    """A canonical three-part release version without aliases or suffixes."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        if not isinstance(value, str) or _SEMVER_PATTERN.fullmatch(value) is None:
            raise ValidationError("version must be strict SemVer MAJOR.MINOR.PATCH")
        major, minor, patch = (int(part) for part in value.split("."))
        return cls(major, minor, patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class StandardManifest:
    """Declared identity of the pinned canonical standard."""

    version: str
    updated_at: str
    sha256: str


@dataclass(frozen=True)
class StandardIdentity:
    """Verified identity that downstream lifecycle checks can retain."""

    version: str
    updated_at: str
    sha256: str


def load_standard_manifest(path: Path) -> StandardManifest:
    """Load one complete local standard manifest or reject it as untrusted."""
    if not path.is_file():
        raise ValidationError(f"standard manifest is missing: {path}")

    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValidationError(f"standard manifest cannot be read: {path}") from error

    if not isinstance(loaded, dict) or set(loaded) != set(_REQUIRED_MANIFEST_FIELDS):
        raise ValidationError("standard manifest must contain exactly the required fields")

    values: dict[str, str] = {}
    for field in _REQUIRED_MANIFEST_FIELDS:
        value = loaded[field]
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"standard manifest field {field!r} must be a non-empty string")
        values[field] = value

    if not _SHA256_PATTERN.fullmatch(values["sha256"]):
        raise ValidationError("standard manifest sha256 must be a lowercase SHA-256 digest")
    SemVer.parse(values["version"])

    return StandardManifest(**values)


def verify_standard(root: Path) -> StandardIdentity:
    """Verify the pinned local standard without network access or mutation."""
    manifest = load_standard_manifest(root / _MANIFEST_RELATIVE_PATH)
    standard_path = root / _STANDARD_RELATIVE_PATH
    if not standard_path.is_file():
        raise ValidationError(f"canonical standard is missing: {standard_path}")

    try:
        standard_bytes = standard_path.read_bytes()
        actual_hash = hashlib.sha256(standard_bytes).hexdigest()
        document = standard_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValidationError(f"canonical standard cannot be read: {standard_path}") from error

    if actual_hash != manifest.sha256:
        raise ValidationError("canonical standard SHA-256 does not match its manifest")

    version_match = _VERSION_PATTERN.search(document)
    if version_match is None:
        raise ValidationError("canonical standard does not declare a version")
    if version_match.group("version") != manifest.version:
        raise ValidationError("canonical standard version does not match its manifest")

    return StandardIdentity(
        version=manifest.version,
        updated_at=manifest.updated_at,
        sha256=actual_hash,
    )
