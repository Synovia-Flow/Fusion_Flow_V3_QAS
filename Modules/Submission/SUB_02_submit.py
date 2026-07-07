#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Module 3 step 2: submit ENS header (create).

For each READY STG.BKD_ENS_Header row, builds the TSS Declaration Header payload
and POSTs it to /headers (create). EVERY call is logged in full to API.Call; the
EXC spine advances SUBMITTING -> SUBMITTED (or ERROR). On success the returned ENS
Declaration_Number is captured onto STG, PRS tracking/submission, and the TSS mirror.

SAFE BY DEFAULT: SUBMISSION_DRY_RUN=1 (default) builds + logs the exact request but
does NOT contact TSS. Set 0 to submit for real; SUBMISSION_ENV (default TST) picks
the environment. No CLI; controls SUBMISSION_*.
"""

from __future__ import annotations

import json
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

ENDPOINT = "headers"  # Rule 1: /headers, never /declaration_headers


def _truthy(s: str) -> bool:
    return (s or "").strip().lower() in ("1", "true", "yes", "on")


def unwrap_response_record(payload):
    """Return the single response dict from a TSS payload, tolerant of shape."""
    rec = payload
    if isinstance(payload, dict):
        rec = payload.get("result") or payload.get("data") or payload
    if isinstance(rec, list):
        rec = rec[0] if rec else {}
    return rec if isinstance(rec, dict) else {}


def extract_declaration_number(payload) -> str | None:
    """Pull the TSS declaration reference from a create response.

    Only the authoritative ENS reference fields are trusted. We deliberately do NOT
    fall back to generic id/number/header_id/headerId: those can be ServiceNow internal
    identifiers that GET /?reference= cannot resolve.
    """
    rec = unwrap_response_record(payload)
    for key in ("declaration_number", "declarationNumber", "reference", "ens_number"):
        value = rec.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None


def extract_tss_status(payload, default: str = "Submitted") -> str:
    """Pull the official TSS status from a response without mixing it with Fusion_Status."""
    rec = unwrap_response_record(payload)
    for key in ("status", "tss_status", "state", "process_status"):
        value = rec.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return default


def _is_live_env(env_code: str) -> int:
    return 1 if (env_code or "").strip().upper() in {"PRD", "PROD", "PRODUCTION", "LIVE"} else 0


def sync_create_response(
    db: SubmissionDb,
    row: dict,
    payload: dict,
    response_payload: dict,
    client: str,
    movement_key: str,
    declaration_number: str,
    tss_status: str,
) -> None:
    """Synchronise the official create response into PRS/STG/TSS immediately.

    API.Call remains the immutable request/response audit. The later mirror job can
    still GET the full record and mark the row RECONCILED.
    """
    db.exec(
        "UPDATE STG.BKD_ENS_Header SET Fusion_Status = 'SUBMITTED', "
        "declaration_number = ?, Tss_Status = ?, SubmitExecutionID = ?, "
        "SubmittedAt = SYSUTCDATETIME(), UpdatedAt = SYSUTCDATETIME() "
        "WHERE ClientCode = ? AND MovementKey = ?",
        declaration_number,
        tss_status,
        db.execution_id,
        client,
        movement_key,
    )
    db.exec(
        "UPDATE PRS.BKD_ENS_Header_Submission SET Fusion_Status = 'SUBMITTED', "
        "declaration_number = ?, Tss_Status = ?, SubmittedAt = SYSUTCDATETIME(), "
        "UpdatedAt = SYSUTCDATETIME() WHERE ClientCode = ? AND MovementKey = ?",
        declaration_number,
        tss_status,
        client,
        movement_key,
    )
    db.exec(
        "UPDATE PRS.BKD_ENS_Header_Tracking SET Fusion_Status = 'SUBMITTED', "
        "Declaration_Number = ?, Tss_Status = ?, SubmittedAt = SYSUTCDATETIME(), "
        "LastExecutionID = ?, UpdatedAt = SYSUTCDATETIME() WHERE ClientCode = ? AND MovementKey = ?",
        declaration_number,
        tss_status,
        db.execution_id,
        client,
        movement_key,
    )

    mirror_cols = set(db.introspect("TSS", "BKD_ENS_Header"))
    mirror = {
        field: payload.get(field)
        for field in ENS_PAYLOAD_FIELDS
        if field in mirror_cols and payload.get(field) is not None
    }
    for key in ("StgID", "SubmissionID"):
        if key in mirror_cols:
            mirror[key] = row.get(key)
    mirror.update(
        {
            "Declaration_Number": declaration_number,
            "ClientCode": client,
            "MovementKey": movement_key,
            "Tss_Status": tss_status,
            "RawJson": json.dumps(response_payload, ensure_ascii=False, default=str)[:1_000_000],
            "IsLive": _is_live_env(db.env_code),
            "FetchExecutionID": db.execution_id,
            "FetchedAt": now_utc(),
        }
    )
    db.upsert("TSS", "BKD_ENS_Header", mirror, ["Declaration_Number"], "MirrorID")
    db.log(
        "TSS_RESPONSE_SYNC",
        f"MK={movement_key}: synced TSS create response status={tss_status} declaration={declaration_number}.",
        "OK",
    )


def run(ini_path: Path = DEFAULT_INI, overrides: dict[str, str] | None = None) -> int:
    db = SubmissionDb.connect(load_db_config(ini_path), overrides=overrides)
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
        db.log(
            "START",
            f"Submit(create) {client} ENS to {env} dry_run={dry_run} url~={client_api.url_for(ENDPOINT)}"
            + (f" (MK={target_mk})" if target_mk else ""),
        )

        top = f"TOP ({max_rows}) " if max_rows > 0 else ""
        sql = f"SELECT {top}* FROM STG.BKD_ENS_Header WHERE ClientCode = ? AND Fusion_Status = 'READY'"
        params = [client]
        if target_mk:
            sql += " AND MovementKey = ?"
            params.append(target_mk)
        sql += " ORDER BY StgID"
        rows = db.q(sql, *params)
        found = len(rows)
        db.log(
            "SOURCE",
            f"{found} READY row(s) to submit"
            + (f" (capped at SUBMISSION_MAX_ROWS={max_rows})" if max_rows > 0 else "")
            + ".",
        )

        for row in rows:
            mk = (row.get("MovementKey") or "").strip()
            try:
                payload = {
                    field: row.get(field)
                    for field in ENS_PAYLOAD_FIELDS
                    if row.get(field) is not None and str(row.get(field)).strip() != ""
                }
                payload.setdefault("op_type", "create")
                db.transition("ENS_HEADER", f"MK={mk}", "SUBMITTING", "SUBMITTING")

                result = client_api.call("POST", ENDPOINT, payload)
                parsed = TssClient.parse_json(result) if not dry_run else None
                response_payload = parsed if isinstance(parsed, dict) else {}
                res_obj = unwrap_response_record(parsed)
                status_str = str(res_obj.get("status") or "").lower()
                process_message = res_obj.get("process_message")
                # TSS can report logical failure inside result.status even on 2xx.
                logical_ok = bool(result.get("ok")) and status_str not in ("error", "failure")
                declaration_number = extract_declaration_number(parsed) if logical_ok else None
                tss_status = extract_tss_status(parsed) if logical_ok else "Submitted"

                db.log_call(
                    process="SUBMITTING",
                    resource="Declaration Header",
                    op_type="create",
                    movement_key=mk,
                    declaration_number=declaration_number,
                    result=result,
                )

                if dry_run:
                    db.log("DRY_RUN", f"MK={mk}: request built + logged; not sent. Row stays READY.")
                    done += 1
                    db.commit()
                    continue

                if logical_ok and declaration_number:
                    sync_create_response(
                        db,
                        row,
                        payload,
                        response_payload,
                        client,
                        mk,
                        declaration_number,
                        tss_status,
                    )
                    db.transition("ENS_HEADER", f"MK={mk}", "SUBMITTING", "SUBMITTED")
                    submitted += 1
                    done += 1
                else:
                    err = process_message or result.get("error") or (
                        "submit succeeded but no declaration number was returned" if result.get("ok") else "submit failed"
                    )
                    err_text = (process_message or result.get("response_text") or err)[:4000]
                    db.exec(
                        "UPDATE STG.BKD_ENS_Header SET Fusion_Status = 'ERROR', "
                        "Tss_Error_Message = ?, SubmitExecutionID = ?, UpdatedAt = SYSUTCDATETIME() "
                        "WHERE ClientCode = ? AND MovementKey = ?",
                        err_text,
                        db.execution_id,
                        client,
                        mk,
                    )
                    db.exec(
                        "UPDATE PRS.BKD_ENS_Header_Submission SET Fusion_Status = 'ERROR', "
                        "Tss_Error_Message = ?, UpdatedAt = SYSUTCDATETIME() "
                        "WHERE ClientCode = ? AND MovementKey = ?",
                        err_text,
                        client,
                        mk,
                    )
                    db.exec(
                        "UPDATE PRS.BKD_ENS_Header_Tracking SET Fusion_Status = 'ERROR', "
                        "RejectReason = ?, LastExecutionID = ?, UpdatedAt = SYSUTCDATETIME() "
                        "WHERE ClientCode = ? AND MovementKey = ?",
                        err_text,
                        db.execution_id,
                        client,
                        mk,
                    )
                    db.transition("ENS_HEADER", f"MK={mk}", "SUBMITTING", "ERROR")
                    db.log_error("SUBMIT", f"MK={mk}: {err}", "TSS")
                    failed += 1
                db.commit()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                db.conn.rollback()
                db.log_error("SUBMIT_ROW", f"MK={mk}: {exc}", type(exc).__name__, traceback.format_exc())

        db.finish("COMPLETED" if failed == 0 else "COMPLETED_WITH_WARNINGS", found, done, failed)
        db.log("FINISH", f"found={found} submitted={submitted} dry_run={dry_run} failed={failed}", "OK")
        print(f"Submit {client} ENS ({env}): found={found} submitted={submitted} dry_run={dry_run} failed={failed}")
        return 0 if failed == 0 else 1
    except Exception as exc:  # noqa: BLE001
        db.log_error("RUN", str(exc), type(exc).__name__, traceback.format_exc())
        db.finish("ERROR", found, done, max(failed, 1), str(exc))
        raise
    finally:
        db.close()


def main() -> int:
    return run(Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI))))


if __name__ == "__main__":
    raise SystemExit(main())