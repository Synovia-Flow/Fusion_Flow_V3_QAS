#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Ingestion runner (scheduler entry point).

Runs the Birkdale ingestion cycle with NO prompts and NO arguments - intended
for Windows Task Scheduler / SQL Agent, not interactive use.

No CLI (design decision): the scheduler simply runs

    python Modules/Ingestion/run_ingestion.py

The job list is DATA, not code: the runner reads the active INGESTION steps for
the client from CFG.Job (ordered by StepNo) and dispatches each by its JobCode.
Run controls come from CFG.Application_Parameters:
    INGESTION_CLIENT   client code to run        (script default: BKD)
    INGESTION_DRY_RUN  1/true = report only      (script default: 0)
The .ini connection path may be overridden with the FUSION_FLOW_INI env var.

Each step opens its own EXC.Execution and logs to EXC/LOG. The exit code is the
worst step result, so the scheduler can alert on any failure. If CFG.Job is not
yet deployed, the runner falls back to the built-in BKD step order.
"""

from __future__ import annotations

import os
from pathlib import Path

from ingest import DEFAULT_INI, IngestionDb, load_db_config
import birkdale_sales_orders as DL
import ens_headers as ENS
import load_raw as LOAD

MODULE = "INGESTION"
DEFAULT_CLIENT = "BKD"

# JobCode -> callable(ini_path, client, dry_run) -> int (return code).
DISPATCH = {
    "ING_BKD_ACQUIRE_EMAIL": lambda ini, client, dry: DL.run(ini, dry),
    "ING_BKD_PARSE_ENS":     lambda ini, client, dry: ENS.run_from_graph(client, ini, None, dry),
    "ING_BKD_LOAD_RAW":      lambda ini, client, dry: LOAD.run(ini, dry),
}
# Used only if CFG.Job is unavailable/empty (pre-deploy safety net).
FALLBACK_STEPS = ["ING_BKD_ACQUIRE_EMAIL", "ING_BKD_PARSE_ENS", "ING_BKD_LOAD_RAW"]


def _resolve_controls_and_steps(ini_path: Path) -> tuple[str, bool, list[dict]]:
    """Read INGESTION_CLIENT / INGESTION_DRY_RUN and the ordered active steps
    for that client from CFG.Job. Falls back gracefully if the DB or table is
    unavailable. Returns (client, dry_run, [{JobCode, JobName}, ...])."""
    client, dry_run, steps = DEFAULT_CLIENT, False, []
    try:
        db = IngestionDb.connect(load_db_config(ini_path))
    except Exception as error:  # noqa: BLE001 - no DB: use script defaults + fallback
        print(f"[WARN] Could not connect to read job registry ({error}); using defaults.")
        return client, dry_run, [{"JobCode": c, "JobName": c} for c in FALLBACK_STEPS]
    try:
        client = (db.fetch_parameter("INGESTION_CLIENT", DEFAULT_CLIENT) or DEFAULT_CLIENT).strip().upper()
        dry_run = (db.fetch_parameter("INGESTION_DRY_RUN", "0") or "0").strip().lower() in ("1", "true", "yes", "on")
        try:
            steps = db._query(
                "SELECT JobCode, JobName FROM CFG.Job "
                "WHERE ModuleName = ? AND JobType = 'STEP' AND IsActive = 1 "
                "  AND (ClientCode = ? OR ClientCode IS NULL) AND StepNo IS NOT NULL "
                "ORDER BY StepNo", MODULE, client)
        except Exception as error:  # noqa: BLE001 - table not deployed yet
            print(f"[WARN] CFG.Job not available ({error}); using built-in step order.")
            steps = []
    finally:
        db.close()
    if not steps:
        steps = [{"JobCode": c, "JobName": c} for c in FALLBACK_STEPS]
    return client, dry_run, steps


def run(ini_path: Path = DEFAULT_INI) -> int:
    client, dry_run, steps = _resolve_controls_and_steps(ini_path)
    mode = " [dry-run]" if dry_run else ""
    print(f"=== Fusion Flow ingestion: {client}{mode} ===")
    print("Steps: " + " -> ".join(s["JobCode"] for s in steps))

    worst = 0
    for s in steps:
        code = s["JobCode"]
        fn = DISPATCH.get(code)
        if fn is None:
            print(f"[WARN] No handler for job {code}; skipping.")
            continue
        print(f"--- {code}: {s.get('JobName', code)} ---")
        try:
            rc = fn(ini_path, client, dry_run)
        except Exception as error:  # noqa: BLE001 - keep the cycle going, record worst rc
            print(f"[ERROR] {code} failed: {error}")
            rc = 1
        print(f"--- {code}: rc={rc} ---")
        worst = max(worst, rc or 0)

    print(f"=== done: overall={worst} ===")
    return worst


def main() -> int:
    ini_path = Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI)))
    return run(ini_path)


if __name__ == "__main__":
    raise SystemExit(main())
