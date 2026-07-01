#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Module 3 step 1: promote VALIDATED ENS headers to STG.

Copies each VALIDATED PRS.BKD_ENS_Header_Submission row into STG.BKD_ENS_Header
(the submission-ready staging copy), sets it READY, and stamps the PRS tracking
row STG_MATERIALISED. Internal DB step - no TSS call. No CLI; controls SUBMISSION_*.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

try:
    from .submission_db import SubmissionDb, load_db_config, DEFAULT_INI, now_utc
except Exception:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from submission_db import SubmissionDb, load_db_config, DEFAULT_INI, now_utc  # type: ignore

CLIENT_DEFAULT, ENTITY_DEFAULT = "BKD", "ENS_HEADER"


def run(ini_path: Path = DEFAULT_INI) -> int:
    db = SubmissionDb.connect(load_db_config(ini_path))
    client = (db.param("SUBMISSION_CLIENT", CLIENT_DEFAULT) or CLIENT_DEFAULT).strip().upper()
    env = (db.param("SUBMISSION_ENV", "TST") or "TST").strip().upper()
    target_mk = (db.param("SUBMISSION_MOVEMENT_KEY", "") or "").strip()
    found = done = failed = 0
    try:
        db.open_execution("STAGING", client, env, "scheduled")
        db.log("START", f"Promote VALIDATED {client} ENS headers -> STG"
               + (f" (MK={target_mk})" if target_mk else ""))

        stg_cols = set(db.introspect("STG", "BKD_ENS_Header"))
        sub_cols = db.introspect("PRS", "BKD_ENS_Header_Submission")
        copy_cols = [c for c in sub_cols if c in stg_cols
                     and c not in ("Fusion_Status", "CreatedAt", "UpdatedAt")]

        sql = ("SELECT s.*, t.TrackingID FROM PRS.BKD_ENS_Header_Submission s "
               "LEFT JOIN PRS.BKD_ENS_Header_Tracking t "
               "  ON t.ClientCode = s.ClientCode AND t.MovementKey = s.MovementKey "
               "WHERE s.ClientCode = ? AND s.Fusion_Status = 'VALIDATED'")
        params = [client]
        if target_mk:
            sql += " AND s.MovementKey = ?"; params.append(target_mk)
        rows = db.q(sql, *params)
        found = len(rows)
        db.log("SOURCE", f"{found} VALIDATED row(s) to promote.")

        for r in rows:
            mk = (r.get("MovementKey") or "").strip()
            try:
                obj = {c: r.get(c) for c in copy_cols}
                obj["ClientCode"] = client
                obj["MovementKey"] = mk
                obj["TrackingID"] = r.get("TrackingID")
                obj["SubmissionID"] = r.get("SubmissionID")
                obj["Fusion_Status"] = "READY"
                obj["PromoteExecutionID"] = db.execution_id
                obj["PromotedAt"] = now_utc()
                db.upsert("STG", "BKD_ENS_Header", obj, ["ClientCode", "MovementKey"], "StgID")
                # PRS tracking -> STG_MATERIALISED
                db.exec("UPDATE PRS.BKD_ENS_Header_Tracking SET Fusion_Status = 'STG_MATERIALISED', "
                        "LastExecutionID = ?, UpdatedAt = SYSUTCDATETIME() "
                        "WHERE ClientCode = ? AND MovementKey = ?", db.execution_id, client, mk)
                db.transition("ENS_HEADER", f"MK={mk}", "STAGING", "STG_MATERIALISED")
                db.commit()
                done += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                db.conn.rollback()
                db.log_error("PROMOTE", f"MK={mk}: {e}", type(e).__name__, traceback.format_exc())

        db.finish("COMPLETED" if failed == 0 else "COMPLETED_WITH_WARNINGS", found, done, failed)
        db.log("FINISH", f"promoted={done} failed={failed}", "OK")
        print(f"Promote {client} ENS: found={found} promoted={done} failed={failed}")
        return 0 if failed == 0 else 1
    except Exception as e:  # noqa: BLE001
        db.log_error("RUN", str(e), type(e).__name__, traceback.format_exc())
        db.finish("ERROR", found, done, max(failed, 1), str(e))
        raise
    finally:
        db.close()


def main() -> int:
    return run(Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI))))


if __name__ == "__main__":
    raise SystemExit(main())
