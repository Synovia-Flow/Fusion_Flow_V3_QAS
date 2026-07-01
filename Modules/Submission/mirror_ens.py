#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Module 3 step 3: mirror the live TSS record.

For each SUBMITTED STG.BKD_ENS_Header row that has a Declaration_Number, GETs the
header back from TSS and upserts the authoritative live record into
TSS.BKD_ENS_Header (the live mirror the update/cancel jobs link to). The STG row is
then marked RECONCILED (complete) and the PRS tracking row RECONCILED. Every call is
logged to API.Call; EXC advances RECONCILING -> RECONCILED.

SAFE BY DEFAULT: SUBMISSION_DRY_RUN=1 logs the GET it WOULD make and stops. No CLI.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

try:
    from .submission_db import SubmissionDb, load_db_config, DEFAULT_INI, now_utc
    from .tss_client import TssClient
except Exception:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from submission_db import SubmissionDb, load_db_config, DEFAULT_INI, now_utc  # type: ignore
    from tss_client import TssClient  # type: ignore

ENDPOINT = "headers"


def _truthy(s: str) -> bool:
    return (s or "").strip().lower() in ("1", "true", "yes", "on")


def unwrap_record(payload):
    """Return the single header dict from a TSS GET response, tolerant of shape."""
    rec = payload
    if isinstance(payload, dict):
        rec = payload.get("result") or payload.get("data") or payload
    if isinstance(rec, list):
        rec = rec[0] if rec else {}
    return rec if isinstance(rec, dict) else {}


def run(ini_path: Path = DEFAULT_INI) -> int:
    db = SubmissionDb.connect(load_db_config(ini_path))
    client = (db.param("SUBMISSION_CLIENT", "BKD") or "BKD").strip().upper()
    env = (db.param("SUBMISSION_ENV", "TST") or "TST").strip().upper()
    dry_run = _truthy(db.param("SUBMISSION_DRY_RUN", "1"))
    base_path = (db.param("SUBMISSION_API_BASE_PATH", "/x_fhmrc_tss_api/v1") or "").strip()
    target_mk = (db.param("SUBMISSION_MOVEMENT_KEY", "") or "").strip()
    try:
        max_rows = int((db.param("SUBMISSION_MAX_ROWS", "0") or "0").strip())
    except ValueError:
        max_rows = 0
    db.dry_run = dry_run

    found = done = failed = 0
    try:
        db.open_execution("RECONCILING", client, env, "dry-run" if dry_run else "scheduled")
        client_api = TssClient.from_cfg(db, env, client, base_path, dry_run)
        db.log("START", f"Mirror {client} ENS from {env} dry_run={dry_run}"
               + (f" (MK={target_mk})" if target_mk else ""))

        mirror_cols = set(db.introspect("TSS", "BKD_ENS_Header"))
        top = f"TOP ({max_rows}) " if max_rows > 0 else ""
        sql = (f"SELECT {top}* FROM STG.BKD_ENS_Header WHERE ClientCode = ? AND Fusion_Status = 'SUBMITTED' "
               "AND declaration_number IS NOT NULL")
        params = [client]
        if target_mk:
            sql += " AND MovementKey = ?"; params.append(target_mk)
        sql += " ORDER BY StgID"
        rows = db.q(sql, *params)
        found = len(rows)
        db.log("SOURCE", f"{found} SUBMITTED row(s) to mirror.")

        for r in rows:
            mk = (r.get("MovementKey") or "").strip()
            decl = (r.get("declaration_number") or "").strip()
            try:
                db.transition("ENS_HEADER", f"MK={mk}", "RECONCILING", "RECONCILING")
                # TSS header GET is by query param: /tss_api/headers?reference=ENS... (not a path segment)
                result = client_api.call("GET", f"{ENDPOINT}?reference={decl}", None)
                db.log_call(process="RECONCILING", resource="Declaration Header", op_type="read",
                            movement_key=mk, declaration_number=decl, result=result)

                if dry_run:
                    db.log("DRY_RUN", f"MK={mk}: GET {decl} logged; not sent. Row stays SUBMITTED.")
                    done += 1
                    db.commit()
                    continue

                if not result.get("ok"):
                    db.log_error("MIRROR", f"MK={mk} decl={decl}: {result.get('error')}", "TSS")
                    failed += 1
                    db.commit()
                    continue

                rec = unwrap_record(TssClient.parse_json(result))
                obj = {c: rec.get(c) for c in mirror_cols if c in rec}
                obj.update({
                    "Declaration_Number": decl, "ClientCode": client, "MovementKey": mk,
                    "StgID": r.get("StgID"), "SubmissionID": r.get("SubmissionID"),
                    "Tss_Status": rec.get("status") or rec.get("tss_status") or "Submitted",
                    "RawJson": json.dumps(rec, default=str)[:1_000_000],
                    "IsLive": 1, "FetchExecutionID": db.execution_id, "FetchedAt": now_utc(),
                })
                db.upsert("TSS", "BKD_ENS_Header", obj, ["Declaration_Number"], "MirrorID")

                db.exec("UPDATE STG.BKD_ENS_Header SET Fusion_Status = 'RECONCILED', "
                        "Tss_Status = ?, MirrorExecutionID = ?, ReconciledAt = SYSUTCDATETIME(), "
                        "UpdatedAt = SYSUTCDATETIME() WHERE ClientCode = ? AND MovementKey = ?",
                        obj["Tss_Status"], db.execution_id, client, mk)
                db.exec("UPDATE PRS.BKD_ENS_Header_Tracking SET Fusion_Status = 'RECONCILED', "
                        "Tss_Status = ?, LastExecutionID = ?, UpdatedAt = SYSUTCDATETIME() "
                        "WHERE ClientCode = ? AND MovementKey = ?",
                        obj["Tss_Status"], db.execution_id, client, mk)
                db.transition("ENS_HEADER", f"MK={mk}", "RECONCILING", "RECONCILED")
                db.commit()
                done += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                db.conn.rollback()
                db.log_error("MIRROR_ROW", f"MK={mk}: {e}", type(e).__name__, traceback.format_exc())

        db.finish("COMPLETED" if failed == 0 else "COMPLETED_WITH_WARNINGS", found, done, failed)
        db.log("FINISH", f"found={found} mirrored={done} dry_run={dry_run} failed={failed}", "OK")
        print(f"Mirror {client} ENS ({env}): found={found} mirrored={done} dry_run={dry_run} failed={failed}")
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
