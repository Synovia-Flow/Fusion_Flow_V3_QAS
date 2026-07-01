#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Module 3 step 2: submit ENS header (create).

For each READY STG.BKD_ENS_Header row, builds the TSS Declaration Header payload
and POSTs it to /headers (create). EVERY call is logged in full to API.Call; the
EXC spine advances SUBMITTING -> SUBMITTED (or ERROR). On success the returned ENS
Declaration_Number is captured onto the STG row and the PRS tracking row.

SAFE BY DEFAULT: SUBMISSION_DRY_RUN=1 (default) builds + logs the exact request but
does NOT contact TSS. Set 0 to submit for real; SUBMISSION_ENV (default TST) picks
the environment. No CLI; controls SUBMISSION_*.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

try:
    from .submission_db import SubmissionDb, load_db_config, DEFAULT_INI, now_utc, ENS_PAYLOAD_FIELDS
    from .tss_client import TssClient
except Exception:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from submission_db import SubmissionDb, load_db_config, DEFAULT_INI, now_utc, ENS_PAYLOAD_FIELDS  # type: ignore
    from tss_client import TssClient  # type: ignore

ENDPOINT = "headers"          # Rule 1: /headers, never /declaration_headers


def _truthy(s: str) -> bool:
    return (s or "").strip().lower() in ("1", "true", "yes", "on")


def extract_declaration_number(payload) -> str | None:
    """Pull the ENS number from a TSS create response, tolerant of shape."""
    rec = payload
    if isinstance(payload, dict):
        rec = payload.get("result") or payload.get("data") or payload
        if isinstance(rec, list):
            rec = rec[0] if rec else {}
    if isinstance(rec, dict):
        for k in ("declaration_number", "declarationNumber", "reference", "ens_number",
                  "header_id", "headerId", "id", "number"):
            v = rec.get(k)
            if v:
                return str(v)
    return None


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

    found = done = failed = submitted = 0
    try:
        db.open_execution("SUBMITTING", client, env, "dry-run" if dry_run else "scheduled")
        client_api = TssClient.from_cfg(db, env, client, base_path, dry_run)
        db.log("START", f"Submit(create) {client} ENS to {env} dry_run={dry_run} url~={client_api.url_for(ENDPOINT)}"
               + (f" (MK={target_mk})" if target_mk else ""))

        top = f"TOP ({max_rows}) " if max_rows > 0 else ""
        sql = f"SELECT {top}* FROM STG.BKD_ENS_Header WHERE ClientCode = ? AND Fusion_Status = 'READY'"
        params = [client]
        if target_mk:
            sql += " AND MovementKey = ?"; params.append(target_mk)
        sql += " ORDER BY StgID"
        rows = db.q(sql, *params)
        found = len(rows)
        db.log("SOURCE", f"{found} READY row(s) to submit"
               + (f" (capped at SUBMISSION_MAX_ROWS={max_rows})" if max_rows > 0 else "") + ".")

        for r in rows:
            mk = (r.get("MovementKey") or "").strip()
            try:
                payload = {f: r.get(f) for f in ENS_PAYLOAD_FIELDS
                           if r.get(f) is not None and str(r.get(f)).strip() != ""}
                payload.setdefault("op_type", "create")
                db.transition("ENS_HEADER", f"MK={mk}", "SUBMITTING", "SUBMITTING")

                result = client_api.call("POST", ENDPOINT, payload)
                parsed = TssClient.parse_json(result) if not dry_run else None
                res_obj = parsed.get("result") if isinstance(parsed, dict) else None
                if not isinstance(res_obj, dict):
                    res_obj = parsed if isinstance(parsed, dict) else {}
                status_str = str(res_obj.get("status") or "").lower()
                proc_msg = res_obj.get("process_message")
                # TSS reports logical failure inside result.status even on 2xx - trust both.
                logical_ok = bool(result.get("ok")) and status_str not in ("error", "failure")
                decl = extract_declaration_number(parsed) if logical_ok else None
                db.log_call(process="SUBMITTING", resource="Declaration Header", op_type="create",
                            movement_key=mk, declaration_number=decl, result=result)

                if dry_run:
                    db.log("DRY_RUN", f"MK={mk}: request built + logged; not sent. Row stays READY.")
                    done += 1
                    db.commit()
                    continue

                if logical_ok and decl:
                    db.exec("UPDATE STG.BKD_ENS_Header SET Fusion_Status = 'SUBMITTED', "
                            "declaration_number = ?, Tss_Status = 'Submitted', SubmitExecutionID = ?, "
                            "SubmittedAt = SYSUTCDATETIME(), UpdatedAt = SYSUTCDATETIME() "
                            "WHERE ClientCode = ? AND MovementKey = ?", decl, db.execution_id, client, mk)
                    db.exec("UPDATE PRS.BKD_ENS_Header_Tracking SET Fusion_Status = 'SUBMITTED', "
                            "Declaration_Number = ?, SubmittedAt = SYSUTCDATETIME(), LastExecutionID = ?, "
                            "UpdatedAt = SYSUTCDATETIME() WHERE ClientCode = ? AND MovementKey = ?",
                            decl, db.execution_id, client, mk)
                    db.transition("ENS_HEADER", f"MK={mk}", "SUBMITTING", "SUBMITTED")
                    submitted += 1; done += 1
                else:
                    err = proc_msg or result.get("error") or "submit failed"
                    db.exec("UPDATE STG.BKD_ENS_Header SET Fusion_Status = 'ERROR', "
                            "Tss_Error_Message = ?, SubmitExecutionID = ?, UpdatedAt = SYSUTCDATETIME() "
                            "WHERE ClientCode = ? AND MovementKey = ?",
                            (proc_msg or result.get("response_text") or err)[:4000], db.execution_id, client, mk)
                    db.transition("ENS_HEADER", f"MK={mk}", "SUBMITTING", "ERROR")
                    db.log_error("SUBMIT", f"MK={mk}: {err}", "TSS")
                    failed += 1
                db.commit()
            except Exception as e:  # noqa: BLE001
                failed += 1
                db.conn.rollback()
                db.log_error("SUBMIT_ROW", f"MK={mk}: {e}", type(e).__name__, traceback.format_exc())

        db.finish("COMPLETED" if failed == 0 else "COMPLETED_WITH_WARNINGS", found, done, failed)
        db.log("FINISH", f"found={found} submitted={submitted} dry_run={dry_run} failed={failed}", "OK")
        print(f"Submit {client} ENS ({env}): found={found} submitted={submitted} dry_run={dry_run} failed={failed}")
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
