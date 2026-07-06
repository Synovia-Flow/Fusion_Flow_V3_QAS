#!/usr/bin/env python3
"""Run the local Fusion Flow jobs in pipeline order (on-prem full cycle).

Default order: ingest -> process -> promote -> submit -> mirror -> fetch-json.
Each step reads its behaviour from CFG.Application_Parameters (SUBMISSION_ENV,
SUBMISSION_DRY_RUN, SUBMISSION_MAX_ROWS, PROCESSING_MODE, ...) and connects via the
DB_* env vars or Configuration/Fusion_Flow_QAS.ini — exactly as each script does alone.

    python Modules/run_all.py                    # full cycle
    python Modules/run_all.py --only ingest,process
    python Modules/run_all.py --skip submit
    python Modules/run_all.py --stop-on-error
    python Modules/run_all.py --list

Intended for manual on-prem runs; the scheduler (Development/Deploy/scheduler) runs the
same jobs individually on their own cadence.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path

MODULES = Path(__file__).resolve().parent
for _d in ("Ingestion", "Processing", "Submission", "Global"):
    sys.path.insert(0, str(MODULES / _d))

# step name -> (module, human label)
STEPS = {
    "ingest":  ("ING_00_run_cycle", "Ingestion cycle"),
    "process": ("PRS_01_engine",    "Process + validate"),
    "promote": ("SUB_01_promote",   "Promote to STG"),
    "submit":  ("SUB_02_submit",    "Submit to TSS"),
    "mirror":  ("SUB_03_mirror",    "Mirror TSS status"),
    "fetch":   ("SUB_06_fetch_json", "Fetch TSS JSON"),
}
DEFAULT_ORDER = ["ingest", "process", "promote", "submit", "mirror", "fetch"]
# extra jobs not in the default cycle — run explicitly with --only
EXTRA = {
    "reprocess": ("PRS_02_reprocess",      "Reprocess rejected"),
    "reference": ("REF_01_choice_values",  "Refresh TSS choice values"),
    "commodity": ("REF_02_commodity_codes", "Refresh commodity codes"),
}
ALL = {**STEPS, **EXTRA}


def _csv(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def main() -> int:
    p = argparse.ArgumentParser(description="Run local Fusion Flow jobs in order.")
    p.add_argument("--only", help="comma list of steps to run, in the given order")
    p.add_argument("--skip", help="comma list of steps to skip")
    p.add_argument("--stop-on-error", action="store_true", help="halt at the first failing step")
    p.add_argument("--list", action="store_true", help="list the available steps and exit")
    args = p.parse_args()

    if args.list:
        print("Default cycle:", " -> ".join(DEFAULT_ORDER))
        for k, (m, lbl) in ALL.items():
            print(f"  {k:10s} {lbl:28s} {m}")
        return 0

    if args.only:
        order = [s for s in _csv(args.only) if s in ALL]
        for bad in (s for s in _csv(args.only) if s not in ALL):
            print(f"[WARN] unknown step ignored: {bad}")
    else:
        order = list(DEFAULT_ORDER)
    if args.skip:
        skip = set(_csv(args.skip))
        order = [s for s in order if s not in skip]

    if not order:
        print("Nothing to run.")
        return 0

    print(f"=== run_all: {' -> '.join(order)} ===")
    results: list[tuple[str, int, float]] = []
    for name in order:
        module, lbl = ALL[name]
        print(f"\n----- {name}: {lbl} ({module}) -----")
        t0 = time.time()
        try:
            code = int(importlib.import_module(module).run())
        except Exception as e:  # noqa: BLE001 - one bad step shouldn't hide the rest
            code = 1
            print(f"[ERROR] {name} raised: {e}")
        dt = time.time() - t0
        results.append((name, code, dt))
        print(f"----- {name}: {'OK' if code == 0 else f'FAILED({code})'} in {dt:.1f}s -----")
        if code != 0 and args.stop_on_error:
            print("[STOP] --stop-on-error set; halting.")
            break

    print("\n=== summary ===")
    for name, code, dt in results:
        print(f"  {name:10s} {'OK' if code == 0 else 'FAILED':8s} {dt:6.1f}s")
    failed = [n for n, c, _ in results if c != 0]
    print(f"=== {'ALL OK' if not failed else 'FAILED: ' + ', '.join(failed)} ===")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
