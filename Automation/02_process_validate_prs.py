#!/usr/bin/env python3
"""02 - Process ING evidence into validated canonical records.

Target V3 path:

    ING raw evidence -> Modules/Processing -> PRS canonical records

Current BKD PRD still writes operational records into STG directly in parts of
the V2 runtime. This script points at the V3 Processing engine so the new build
keeps transformation, enrichment and validation out of the portal.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROCESS = ROOT / "Modules" / "Processing" / "PRS_01_engine.py"
REPROCESS = ROOT / "Modules" / "Processing" / "PRS_02_reprocess.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reprocess", action="store_true", help="Run the rejection/reprocess entrypoint.")
    parser.add_argument("--explain", action="store_true", help="Print the target module and exit.")
    args = parser.parse_args()

    target = REPROCESS if args.reprocess else PROCESS
    print("Automation 02: ING -> canonical processing/enrichment/validation")
    print(f"Target module: {target.relative_to(ROOT)}")
    if args.explain:
        print("Validation ownership: Modules/Processing and CFG field maps/choice values.")
        print("Current PRD equivalent: app/ingestion/sales_orders_stage.py + app/pipeline_validation.py")
        return 0

    return subprocess.run([sys.executable, str(target)], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
