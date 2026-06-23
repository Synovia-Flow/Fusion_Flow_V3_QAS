#!/usr/bin/env python3
"""04 - TSS status watcher and notification gate.

Runs the existing ENS status watcher/sync path. It reads live TSS statuses,
updates local mirrors, syncs DEC/SFD/MRN/goods where available, sends TSS status
attention notifications, and sends the final Authorised for Movement email only
when the movement gate passes.

Uses the local QAS scripts/ package copied into this repository.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _app_root() -> Path:
    candidate = Path(__file__).resolve().parents[2]
    if not (candidate / "scripts" / "sync_prd_ens_statuses.py").exists():
        raise SystemExit(
            "Local QAS script root not found. Expected scripts/sync_prd_ens_statuses.py in this repository."
        )
    return candidate


ROOT = _app_root()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-code", default=os.environ.get("TENANT_CODE") or "BKD")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--min-age-minutes", type=int, default=0)
    args = parser.parse_args()

    command = [
        sys.executable,
        str(ROOT / "scripts" / "sync_prd_ens_statuses.py"),
        "--tenant-code",
        args.tenant_code,
        "--limit",
        str(args.limit),
        "--min-age-minutes",
        str(args.min_age_minutes),
    ]

    print("FLOW V3 04 -> Status watcher + notification gate")
    print(" ".join(command))
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
