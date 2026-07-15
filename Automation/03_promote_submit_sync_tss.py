#!/usr/bin/env python3
"""03 - Promote validated records, submit to TSS, then sync/mirror responses.

Default sequence:

    PRS -> STG -> TSS submit -> TSS mirror -> response JSON capture

The underlying Submission modules are safe-by-configuration. In V3, live/dry-run
behaviour is controlled by CFG/Application parameters, not by this wrapper.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STEPS = {
    "promote": ROOT / "Modules" / "Submission" / "SUB_01_promote.py",
    "submit": ROOT / "Modules" / "Submission" / "SUB_02_submit.py",
    "mirror": ROOT / "Modules" / "Submission" / "SUB_03_mirror.py",
    "fetch-json": ROOT / "Modules" / "Submission" / "SUB_06_fetch_json.py",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--steps",
        nargs="+",
        choices=sorted(STEPS),
        default=["promote", "submit", "mirror", "fetch-json"],
        help="Submission steps to run in order.",
    )
    parser.add_argument("--explain", action="store_true", help="Print selected modules and exit.")
    args = parser.parse_args()

    print("Automation 03: promote/submit/sync TSS")
    for step in args.steps:
        print(f"- {step}: {STEPS[step].relative_to(ROOT)}")
    if args.explain:
        print("Current PRD equivalent: TSS API client, submit helpers, sync workers and TSS.BKD_API_Exchanges.")
        return 0

    worst = 0
    for step in args.steps:
        result = subprocess.run([sys.executable, str(STEPS[step])], cwd=ROOT)
        worst = max(worst, result.returncode or 0)
        if result.returncode:
            break
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
