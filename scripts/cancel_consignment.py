#!/usr/bin/env python3
"""
NOT FOR PRD: reads/writes BKD.Staging* tables removed by migration 078.
             Use STG.BKD_* or ING.BKD_* for new pipeline work.

Cancel one or more TSS consignments by DEC reference or local staging id.

Examples:
    python scripts/cancel_consignment.py DEC000000001073037
    python scripts/cancel_consignment.py --staging-id 125
    python scripts/cancel_consignment.py DEC000000001073037 DEC000000001073038 --dry-run
"""
import argparse
import json
import os
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_standalone_connection
from app.tenant import get_tenant, tenant_aware_cursor
from app.tss_api import build_cfg_client

S = get_tenant()["schema"]


def _normalise_ref(value):
    return (value or "").strip().upper()


def _lookup_refs(cursor, staging_ids):
    refs = []
    for staging_id in staging_ids:
        row = cursor.execute(
            f"""
            SELECT staging_id, dec_reference, sfd_reference
            FROM {S}.StagingConsignments
            WHERE staging_id = ?
            """,
            [staging_id],
        ).fetchone()
        if not row:
            print(f"SKIP staging #{staging_id}: not found")
            continue
        if row[2]:
            print(f"SKIP staging #{staging_id}: has SFD reference {row[2]}")
            continue
        if not row[1]:
            print(f"SKIP staging #{staging_id}: no DEC reference")
            continue
        refs.append((row[0], _normalise_ref(row[1])))
    return refs


def _lookup_staging_id(cursor, dec_ref):
    row = cursor.execute(
        f"""
        SELECT TOP 1 staging_id
        FROM {S}.StagingConsignments
        WHERE dec_reference = ?
        ORDER BY staging_id DESC
        """,
        [dec_ref],
    ).fetchone()
    return row[0] if row else None


def _log_call(cursor, staging_id, result, payload):
    cursor.execute(
        f"""
        INSERT INTO {S}.ApiCallLog
            (staging_id, call_type, http_method, url, request_payload,
             http_status, response_status, response_message, response_json,
             duration_ms, error_detail)
        VALUES (?, 'CANCEL_CONSIGNMENT', 'POST', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            staging_id,
            result.get("url", ""),
            json.dumps(payload),
            result.get("http_status", 0),
            result.get("status", ""),
            str(result.get("message") or "")[:500],
            result.get("raw_response", "")[:4000],
            result.get("duration_ms", 0),
            None if result.get("success") else str(result.get("error_message") or result.get("message") or "")[:2000],
        ],
    )


def cancel_ref(cursor, client, dec_ref, dry_run=False):
    staging_id = _lookup_staging_id(cursor, dec_ref)
    payload = {"op_type": "cancel", "consignment_number": dec_ref}

    if dry_run:
        print(f"DRY RUN {dec_ref}: would POST /consignments {json.dumps(payload)}")
        return True

    result = client.cancel_consignment(dec_ref)
    _log_call(cursor, staging_id, result, payload)

    if result.get("success"):
        response = result.get("response") or {}
        tss_status = response.get("status") or result.get("status") or "cancelled"
        if staging_id:
            cursor.execute(
                f"""
                UPDATE {S}.StagingConsignments
                SET status = 'CANCELLED',
                    tss_status = ?,
                    error_message = NULL,
                    updated_at = SYSUTCDATETIME()
                WHERE staging_id = ?
                """,
                [tss_status, staging_id],
            )
        print(f"OK {dec_ref}: cancelled ({tss_status})")
        return True

    message = result.get("message") or result.get("raw_response") or "unknown error"
    if staging_id:
        cursor.execute(
            f"""
            UPDATE {S}.StagingConsignments
            SET error_message = ?,
                updated_at = SYSUTCDATETIME()
            WHERE staging_id = ?
            """,
            [f"TSS cancel failed: {message}"[:4000], staging_id],
        )
    print(f"FAIL {dec_ref}: {message}")
    return False


def main():
    parser = argparse.ArgumentParser(description="Cancel TSS consignments by DEC reference.")
    parser.add_argument("refs", nargs="*", help="DEC references to cancel")
    parser.add_argument("--staging-id", dest="staging_ids", type=int, action="append", default=[],
                        help="Local BKD.StagingConsignments.staging_id to cancel")
    parser.add_argument("--dry-run", action="store_true", help="Print the calls without sending them")
    args = parser.parse_args()

    load_dotenv()
    conn = get_standalone_connection()
    cursor = tenant_aware_cursor(conn.cursor())
    try:
        refs = [(None, _normalise_ref(ref)) for ref in args.refs if _normalise_ref(ref)]
        refs.extend(_lookup_refs(cursor, args.staging_ids))
        refs = [(sid, ref) for sid, ref in refs if ref.startswith("DEC")]

        if not refs:
            print("No DEC references supplied.")
            sys.exit(2)

        client = build_cfg_client()
        ok = 0
        failed = 0
        seen = set()
        for _sid, dec_ref in refs:
            if dec_ref in seen:
                continue
            seen.add(dec_ref)
            if cancel_ref(cursor, client, dec_ref, dry_run=args.dry_run):
                ok += 1
            else:
                failed += 1
            if not args.dry_run:
                conn.commit()

        print(f"Done. Cancelled={ok} Failed={failed}")
        sys.exit(1 if failed else 0)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
