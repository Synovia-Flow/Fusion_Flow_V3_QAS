#!/usr/bin/env python3
"""
NOT FOR PRD: reads/writes legacy BKD.Staging* tables removed by migration 078. Do not run against Fusion_TSS_Automation_PRD.

Cancel one or more TSS supplementary declarations by SUP reference or local staging id.

Examples:
    python scripts/legacy/cancel_sdi.py SUP000000001234567
    python scripts/legacy/cancel_sdi.py --staging-id 42
    python scripts/legacy/cancel_sdi.py SUP000000001234567 SUP000000001234568 --dry-run
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
            SELECT TOP 1 staging_id, sup_dec_number, status
            FROM {S}.StagingSupDecHeaders
            WHERE staging_id = ?
            """,
            [staging_id],
        ).fetchone()
        if not row:
            print(f"SKIP staging #{staging_id}: not found")
            continue
        if not row[1]:
            print(f"SKIP staging #{staging_id}: no SUP reference")
            continue
        if (row[2] or '').upper() in ('CANCELLED', 'CLEARED', 'ACCEPTED'):
            print(f"SKIP staging #{staging_id}: already {row[2]}")
            continue
        refs.append((row[0], _normalise_ref(row[1])))
    return refs


def _lookup_staging_id(cursor, sup_ref):
    row = cursor.execute(
        f"""
        SELECT TOP 1 staging_id
        FROM {S}.StagingSupDecHeaders
        WHERE sup_dec_number = ?
        ORDER BY staging_id DESC
        """,
        [sup_ref],
    ).fetchone()
    return row[0] if row else None


def _log_call(cursor, staging_id, result, payload):
    cursor.execute(
        f"""
        INSERT INTO {S}.ApiCallLog
            (staging_id, call_type, http_method, url, request_payload,
             http_status, response_status, response_message, response_json,
             duration_ms, error_detail)
        VALUES (?, 'CANCEL_SDI', 'POST', ?, ?, ?, ?, ?, ?, ?, ?)
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


def cancel_ref(cursor, client, sup_ref, dry_run=False):
    staging_id = _lookup_staging_id(cursor, sup_ref)
    payload = {"op_type": "cancel", "sup_dec_number": sup_ref}

    if dry_run:
        print(f"DRY RUN {sup_ref}: would POST /supplementary_declarations {json.dumps(payload)}")
        return True

    result = client.cancel_sdi(sup_ref)
    _log_call(cursor, staging_id, result, payload)

    if result.get("success"):
        response = result.get("response") or {}
        tss_status = response.get("status") or result.get("status") or "cancelled"
        if staging_id:
            cursor.execute(
                f"""
                UPDATE {S}.StagingSupDecHeaders
                SET status = 'CANCELLED',
                    tss_status = ?,
                    error_message = NULL,
                    updated_at = SYSUTCDATETIME()
                WHERE staging_id = ?
                """,
                [tss_status, staging_id],
            )
        print(f"OK {sup_ref}: cancelled ({tss_status})")
        return True

    message = result.get("message") or result.get("raw_response") or "unknown error"
    if staging_id:
        cursor.execute(
            f"""
            UPDATE {S}.StagingSupDecHeaders
            SET error_message = ?,
                updated_at = SYSUTCDATETIME()
            WHERE staging_id = ?
            """,
            [f"TSS cancel failed: {message}"[:4000], staging_id],
        )
    print(f"FAIL {sup_ref}: {message}")
    return False


def main():
    parser = argparse.ArgumentParser(description="Cancel TSS supplementary declarations by SUP reference.")
    parser.add_argument("refs", nargs="*", help="SUP references to cancel")
    parser.add_argument("--staging-id", dest="staging_ids", type=int, action="append", default=[],
                        help="Local StagingSupDecHeaders.staging_id to cancel")
    parser.add_argument("--dry-run", action="store_true", help="Print the calls without sending them")
    args = parser.parse_args()

    load_dotenv()
    conn = get_standalone_connection()
    cursor = tenant_aware_cursor(conn.cursor())
    try:
        refs = [(None, _normalise_ref(ref)) for ref in args.refs if _normalise_ref(ref)]
        refs.extend(_lookup_refs(cursor, args.staging_ids))
        refs = [(sid, ref) for sid, ref in refs if ref.startswith("SUP")]

        if not refs:
            print("No SUP references supplied.")
            sys.exit(2)

        client = build_cfg_client()
        ok = 0
        failed = 0
        seen = set()
        for _sid, sup_ref in refs:
            if sup_ref in seen:
                continue
            seen.add(sup_ref)
            if cancel_ref(cursor, client, sup_ref, dry_run=args.dry_run):
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
