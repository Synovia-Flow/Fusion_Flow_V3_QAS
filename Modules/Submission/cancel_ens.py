#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Module 3: CANCEL a live ENS header (TSS layer).

Cancels a declaration already in TSS: POST /headers with {op_type:"cancel",
declaration_number}. On success the STG + tracking rows go to Fusion_Status
CANCELLED and the TSS mirror is marked not-live (IsLive=0, CancelledAt). Every call
is logged to API.Call; EXC advances to CANCELLED.

SAFE BY DEFAULT: SUBMISSION_DRY_RUN=1 builds + logs but sends nothing. Cancel is
destructive, so it REQUIRES SUBMISSION_MOVEMENT_KEY (one movement) unless
SUBMISSION_MAX_ROWS caps the batch. No CLI.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

try:
    from .submission_db import SubmissionDb, load_db_config, DEFAULT_INI
    from .tss_client import TssClient
except Exception:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from submission_db import SubmissionDb, load_db_config, DEFAULT_INI  # type: ignore
    from tss_client import TssClient  # type: ignore

ENDPOINT = "headers"


def _truthy(s: str) -> bool:
    return (s or "").strip().lower() in ("1", "true", "yes", "on")


def run(ini_path: Path = DEFAULT_INI) -> int:
    db = SubmissionDb.connect(load_db_config(ini_path))
    client = (db.param("SUBMISSION_CLIENT", "BKD") or "BKD").strip().upper()
    env = (db.param("SUBMISSION_ENV", "TST") or "TST").strip().upper()
    dry_run = _truthy(db.param("SUBMISSION_DRY_RUN", "1"))
    base_path = (db.param("SUBMISSION_API_BASE_PATH", "/x_fhmrc_tss_api/v1/tss_api") or "").strip()
    target_mk = (db.param("SUBMISSION_MOVEMENT_KEY", "") or "").strip()
    try:
        max_rows = int((db.param("SUBMISSION_MAX_ROWS", "0") or "0").strip())
    except ValueError:
        max_rows = 0
    db.dry_run = dry_run

    if not target_mk and max_rows <= 0:
        print("[GUARD] Cancel needs SUBMISSION_MOVEMENT_KEY (one movement) or SUBMISSION_MAX_ROWS. Aborting.")
        db.close(); return 2

    found = done = failed = cancelled = 0
    try:
        db.open_execution("CANCELLING", client, env, "dry-run" if dry_run else "scheduled")
        api = TssClient.from_cfg(db, env, client, base_path, dry_run)
        db.log("START", f"Cancel {client} ENS on {env} dry_run={dry_run}" + (f" MK={target_mk}" if target_mk else ""))

        top = f"TOP ({max_rows}) " if max_rows > 0 else ""
        sql = (f"SELECT {top}MovementKey, declaration_number FROM STG.BKD_ENS_Header WHERE ClientCode = ? "
               f"AND declaration_number IS NOT NULL AND Fusion_Status <> 'CANCELLED'")
        params = [client]
        if target_mk:
            sql += " AND MovementKey = ?"; params.append(target_mk)
        sql += " ORDER BY StgID"
        rows = db.q(sql, *params)
        found = len(rows)
        db.log("SOURCE", f"{found} live movement(s) to cancel.")

        for r in rows:
            mk = (r.get("MovementKey") or "").strip()
            decl = (r.get("declaration_number") or "").strip()
            try:
                payload = {"op_type": "cancel", "declaration_number": decl}
                db.transition("ENS_HEADER", f"MK={mk}", "CANCELLING", "CANCELLING")
                result = api.call("POST", ENDPOINT, payload)
                parsed = TssClient.parse_json(result) if not dry_run else None
                res = parsed.get("result") if isinstance(parsed, dict) else None
                res = res if isinstance(res, dict) else (parsed if isinstance(parsed, dict) else {})
                status = str(res.get("status") or "").lower()
                ok = bool(result.get("ok")) and status not in ("error", "failure")
                db.log_call(process="CANCELLING", resource="Declaration Header", op_type="cancel",
                            movement_key=mk, declaration_number=decl, result=result)
                if dry_run:
                    db.log("DRY_RUN", f"MK={mk}: cancel request built + logged; not sent."); done += 1; db.commit(); continue
                if ok:
                    db.exec("UPDATE STG.BKD_ENS_Header SET Fusion_Status='CANCELLED', Tss_Status='Cancelled', "
                            "UpdatedAt=SYSUTCDATETIME() WHERE ClientCode=? AND MovementKey=?", client, mk)
                    db.exec("UPDATE PRS.BKD_ENS_Header_Tracking SET Fusion_Status='CANCELLED', Tss_Status='Cancelled', "
                            "LastExecutionID=?, UpdatedAt=SYSUTCDATETIME() WHERE ClientCode=? AND MovementKey=?", db.execution_id, client, mk)
                    db.exec("UPDATE TSS.BKD_ENS_Header SET IsLive=0, Tss_Status='Cancelled', CancelledAt=SYSUTCDATETIME(), "
                            "UpdatedAt=SYSUTCDATETIME() WHERE Declaration_Number=?", decl)
                    db.transition("ENS_HEADER", f"MK={mk}", "CANCELLING", "CANCELLED")
                    cancelled += 1; done += 1
                else:
                    msg = res.get("process_message") or result.get("error") or "cancel failed"
                    db.log_error("CANCEL", f"MK={mk}: {msg}", "TSS"); failed += 1
                db.commit()
            except Exception as e:  # noqa: BLE001
                failed += 1; db.conn.rollback()
                db.log_error("CANCEL_ROW", f"MK={mk}: {e}", type(e).__name__, traceback.format_exc())

        db.finish("COMPLETED" if failed == 0 else "COMPLETED_WITH_WARNINGS", found, done, failed)
        db.log("FINISH", f"found={found} cancelled={cancelled} dry_run={dry_run} failed={failed}", "OK")
        print(f"Cancel {client} ENS ({env}): found={found} cancelled={cancelled} dry_run={dry_run} failed={failed}")
        return 0 if failed == 0 else 1
    except Exception as e:  # noqa: BLE001
        db.log_error("RUN", str(e), type(e).__name__, traceback.format_exc())
        db.finish("ERROR", found, done, max(failed, 1), str(e)); raise
    finally:
        db.close()


def main() -> int:
    return run(Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI))))


if __name__ == "__main__":
    raise SystemExit(main())
