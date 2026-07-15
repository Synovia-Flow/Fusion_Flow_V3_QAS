#!/usr/bin/env python3
"""01 - Ingest Graph email/files into ING evidence.

This is the compact Automation entrypoint for the first part of the pipeline:

    Graph mailbox -> classification -> source files -> ING evidence

Business logic belongs in Modules/Ingestion. This wrapper exists so an operator
or scheduler has one obvious script to call.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "Modules" / "Ingestion" / "ING_00_run_cycle.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--explain", action="store_true", help="Print the target module and exit.")
    args = parser.parse_args()

    print("Automation 01: Graph/email/file ingestion -> ING raw evidence")
    print(f"Target module: {TARGET.relative_to(ROOT)}")
    if args.explain:
        print("Reads configured mailbox/jobs from CFG/Application parameters where deployed.")
        print("Current PRD equivalent: scripts/pull_inbound_email.py + app/ingestion/graph_mail.py")
        return 0

    return subprocess.run([sys.executable, str(TARGET)], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
