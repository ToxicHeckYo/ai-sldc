"""Synchronize or inspect the autonomous standard snapshot."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json

from _skill_bootstrap import load_skill_package, repository_root

load_skill_package()

from ai_sdlc.errors import ValidationError
from ai_sdlc.standard_sync import check_standard_sync, sync_standard


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        status = check_standard_sync(repository_root()) if args.check else sync_standard(repository_root())
    except (OSError, ValidationError) as error:
        parser.exit(1, f"standard synchronization failed: {error}\n")
    if args.json:
        print(json.dumps(asdict(status), ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"standard sync: {status.state}")
        if status.detail:
            print(status.detail)
    return 0 if status.clean else 2


if __name__ == "__main__":
    raise SystemExit(main())
