"""Normalize a recorded provider fixture using a shipped declarative adapter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from ai_sdlc.adapters import adapted_result_document, normalize_provider_output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--base-revision", required=True)
    args = parser.parse_args()
    raw = yaml.safe_load(args.raw.read_text(encoding="utf-8"))
    result = normalize_provider_output(args.adapter, raw, base_revision=args.base_revision)
    print(json.dumps(adapted_result_document(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
