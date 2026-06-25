#!/usr/bin/env python3
"""01 - Graph email fetch, classification and inbound trace.

QAS-native entrypoint. It runs the Graph downloader from the Integration Layer.
The downstream V3 design treats this as the first traceable event:
mailbox message -> tenant classification -> original attachment saved -> ING row
when database persistence is enabled.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-mode", choices=["daily", "historic", "custom"], default="daily")
    parser.add_argument("--received-from")
    parser.add_argument("--received-to")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-database", action="store_true")
    args = parser.parse_args()

    command = [sys.executable, str(ROOT / "Integration_Layer" / "Graph" / "graph_mail_customer_downloader.py")]
    if args.run_mode != "daily":
        command += ["--run-mode", args.run_mode]
    if args.received_from:
        command += ["--received-from", args.received_from]
    if args.received_to:
        command += ["--received-to", args.received_to]
    if args.dry_run:
        command.append("--dry-run")
    if args.max_messages:
        command += ["--max-messages", str(args.max_messages)]
    if args.overwrite:
        command.append("--overwrite")
    if args.no_database:
        command.append("--no-database")

    print("FLOW V3 01 -> Graph email fetch + classification + ING trace")
    print(" ".join(command))
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
