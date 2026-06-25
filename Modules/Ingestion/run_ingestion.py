#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Ingestion runner (scheduler entry point).

Runs the full ingestion cycle for a client with NO prompts and NO required
arguments - intended for Windows Task Scheduler / SQL Agent, not interactive use.

Steps (config-driven; everything read from CFG + Configuration/Fusion_Flow_QAS.ini):
  1. Birkdale_Sales_Orders  - download relevant attachments, move mail to
                              Fusion_Processed/<client>, land to ING.
  2. Process_ENS_Headers    - parse the TSS-Details emails into the timestamped
                              ENS CSV in the client ENS_Source folder.

Each step opens its own EXC.Execution and logs to EXC/LOG. Exit code is 0 only
if every step succeeded, so the scheduler can alert on failure.

Schedule (no CLI args needed):
    python D:\...\Fusion_Flow_V3_QAS\Modules\Ingestion\run_ingestion.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ingest import DEFAULT_INI
import birkdale_sales_orders as DL
import ens_headers as ENS
import load_raw as LOAD


def main() -> int:
    p = argparse.ArgumentParser(description="Fusion Flow ingestion runner (scheduler entry point).")
    p.add_argument("--client", default="BKD", help="Client code (default BKD)")
    p.add_argument("--ini", type=Path, default=DEFAULT_INI)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    client = args.client.strip().upper()

    print(f"=== Fusion Flow ingestion: {client} ===")
    rc_download = DL.run(args.ini, args.dry_run)              # 1. download files + move mail
    rc_ens = ENS.run_from_graph(client, args.ini, None, args.dry_run)   # 2. ENS CSV
    rc_load = LOAD.run(args.ini, args.dry_run)               # 3. load raw tables + move files to Processed

    overall = max(rc_download, rc_ens, rc_load)
    print(f"=== done: download={rc_download}, ens={rc_ens}, load={rc_load}, overall={overall} ===")
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
