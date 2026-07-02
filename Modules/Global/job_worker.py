#!/usr/bin/env python3
"""Fusion Flow V3 QAS - background job worker (Pattern B).

Polls EXC.Job_Queue for PENDING requests and runs the matching runner for ONE
movement, using per-run scope overrides (never mutating the shared
CFG.Application_Parameters). This decouples long / batched work from the web dyno:
the portal calls POST /api/enqueue/<verb> (returns 202 immediately) instead of
POST /api/action/<verb> (runs in-process), and this worker picks the row up.

The claim is atomic - UPDATE ... FROM (SELECT TOP (1) ... WITH (READPAST, UPDLOCK)) -
so several workers can run without handing the same job to two of them.

Run:   python Modules/Global/job_worker.py         (loops forever; Ctrl-C to stop)
Env:   WORKER_POLL_SECONDS  poll interval, default 10
       WORKER_ONCE=1        drain the queue once then exit (useful as a cron)
       DB_* / FUSION_FLOW_INI  connection (same resolution as every other module)
"""

from __future__ import annotations

import importlib
import os
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _d in ("Modules/Submission", "Modules/Processing"):
    sys.path.insert(0, str(REPO / _d))

from submission_db import DEFAULT_INI, _conn_str, load_db_config  # noqa: E402

# verb -> (runner module, movement-scope param, extra per-run overrides).
# Mirrors liveWeb/app.py:VERB so a queued run behaves identically to a portal click.
VERB = {
    "promote":   ("promote_ens",      "SUBMISSION_MOVEMENT_KEY", {"SUBMISSION_MAX_ROWS": "1"}),
    "submit":    ("submit_ens",       "SUBMISSION_MOVEMENT_KEY", {"SUBMISSION_MAX_ROWS": "1"}),
    "mirror":    ("mirror_ens",       "SUBMISSION_MOVEMENT_KEY", {"SUBMISSION_MAX_ROWS": "1"}),
    "update":    ("update_ens",       "SUBMISSION_MOVEMENT_KEY", {"SUBMISSION_MAX_ROWS": "1"}),
    "cancel":    ("cancel_ens",       "SUBMISSION_MOVEMENT_KEY", {"SUBMISSION_MAX_ROWS": "1"}),
    "reprocess": ("reprocess_engine", "PROCESSING_MOVEMENT_KEY", {"PROCESSING_MODE": "REPROCESS"}),
}

CLAIM_SQL = """
WITH next_job AS (
    SELECT TOP (1) * FROM EXC.Job_Queue WITH (ROWLOCK, READPAST, UPDLOCK)
    WHERE Status = 'PENDING' ORDER BY QueueID
)
UPDATE next_job
   SET Status = 'RUNNING', StartedAt = SYSUTCDATETIME(), Attempts = Attempts + 1
OUTPUT INSERTED.QueueID, INSERTED.Verb, INSERTED.MovementKey;
"""


def _connect(ini_path: Path):
    import pyodbc
    return pyodbc.connect(_conn_str(load_db_config(ini_path)), autocommit=True)


def _run_job(verb: str, mk: str) -> tuple[int, str]:
    """Import + run the runner for one movement. Returns (exit_code, message)."""
    if verb not in VERB:
        return 2, f"unknown verb '{verb}'"
    module_name, mk_param, extra = VERB[verb]
    overrides = {mk_param: mk, **extra}
    mod = importlib.import_module(module_name)
    code = int(mod.run(overrides=overrides))
    return code, ("ok" if code == 0 else f"exit={code}")


def process_next(conn) -> bool:
    """Claim and run one queued job. Returns False when the queue is empty."""
    cur = conn.cursor()
    row = cur.execute(CLAIM_SQL).fetchone()
    if not row:
        return False
    qid, verb, mk = int(row[0]), row[1], row[2]
    print(f"[WORKER] claim #{qid} {verb} mk={mk}")
    try:
        code, msg = _run_job(verb, mk)
        conn.cursor().execute(
            "UPDATE EXC.Job_Queue SET Status = ?, FinishedAt = SYSUTCDATETIME(), "
            "ExitCode = ?, ResultMessage = ? WHERE QueueID = ?",
            ("DONE" if code == 0 else "FAILED"), code, msg[:2000], qid)
        print(f"[WORKER] #{qid} {'DONE' if code == 0 else 'FAILED'} ({msg})")
    except Exception as e:  # noqa: BLE001 - never let one bad job kill the worker
        conn.cursor().execute(
            "UPDATE EXC.Job_Queue SET Status = 'FAILED', FinishedAt = SYSUTCDATETIME(), "
            "ExitCode = 1, ResultMessage = ? WHERE QueueID = ?",
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"[:2000], qid)
        print(f"[WORKER] #{qid} FAILED: {e}")
    return True


def main() -> int:
    ini = Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI)))
    poll = int(os.environ.get("WORKER_POLL_SECONDS", "10"))
    once = os.environ.get("WORKER_ONCE", "").lower() in ("1", "true", "yes", "on")
    print(f"[WORKER] started; {'drain-once' if once else f'polling every {poll}s'}")
    conn = _connect(ini)
    try:
        while True:
            try:
                worked = process_next(conn)
            except Exception as e:  # transient DB error -> reconnect and keep going
                print(f"[WORKER] loop error, reconnecting: {e}")
                try:
                    conn.close()
                except Exception:
                    pass
                time.sleep(poll)
                conn = _connect(ini)
                continue
            if once and not worked:
                break
            if not worked:
                time.sleep(poll)
    except KeyboardInterrupt:
        print("[WORKER] stopping")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
