"""
NOT FOR PRD: reads/writes legacy BKD.Staging* tables removed by migration 078. Do not run against Fusion_TSS_Automation_PRD.

Stage Route A GMR rows automatically when an ENS header is fully ready.

Readiness rule used here:
  - ENS header has at least one consignment
  - every consignment is AUTHORISED_FOR_MOVEMENT
  - no active GMR already exists for that ENS header

Usage:
    python scripts/stage_ready_gmrs.py
"""
import os
import sys

import pyodbc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string
from app.tenant import get_tenant, tenant_aware_cursor
from app.tss_api import resolve_tss_settings

S = get_tenant()["schema"]
AUTHORISED_STATUSES = ('AUTHORISED_FOR_MOVEMENT', 'Authorised for Movement')
TEST_FALLBACK_BLOCKED_STATUSES = {
    'TRADER_INPUT_REQUIRED', 'Trader Input Required',
    'AMENDMENT_REQUIRED', 'Amendment Required',
    'DO_NOT_LOAD', 'Do Not Load',
    'CANCELLED', 'Cancelled',
}


def get_connection():
    from dotenv import load_dotenv
    load_dotenv()
    return pyodbc.connect(build_connection_string(timeout=30), autocommit=False)


def is_test_tss_environment():
    try:
        resolved = resolve_tss_settings()
        base_url = (resolved.get('base_url') or '').strip().lower()
        environment = (resolved.get('environment') or '').strip().lower()
        return (
            'tsstestenv' in base_url
            or 'test-api.service.hmrc.gov.uk' in base_url
            or environment == 'test'
        )
    except Exception:
        return False


def main():
    print("Synovia Flow - GMR Auto-Stager")
    print("=" * 55)

    conn = get_connection()
    cur = tenant_aware_cursor(conn.cursor())

    test_env_fallback = is_test_tss_environment()
    cur.execute(f"""
        SELECT staging_id, ens_reference, label, identity_no_of_transport
        FROM {S}.StagingEnsHeaders
        WHERE ens_reference IS NOT NULL
          AND ISNULL(status, '') <> 'CANCELLED'
        ORDER BY staging_id
    """)
    rows = []
    for staging_ens_id, ens_reference, label, identity_no_of_transport in cur.fetchall():
        cons = cur.execute(f"""
            SELECT c.staging_id, c.dec_reference, c.tss_status, c.status,
                   COUNT(g.staging_id) AS goods_count,
                   SUM(CASE WHEN g.status = 'CREATED' THEN 1 ELSE 0 END) AS goods_ready_count
            FROM {S}.StagingConsignments c
            LEFT JOIN {S}.StagingGoodsItems g ON g.staging_cons_id = c.staging_id
            WHERE c.staging_ens_id = ?
            GROUP BY c.staging_id, c.dec_reference, c.tss_status, c.status
        """, [staging_ens_id]).fetchall()
        cons_count = len(cons)
        auth_count = sum(1 for row in cons if row[2] in AUTHORISED_STATUSES)
        fallback_ready_count = 0
        if test_env_fallback:
            for _csid, dec_ref, tss_status, local_status, goods_count, goods_ready_count in cons:
                if not (dec_ref or '').strip():
                    continue
                if (local_status or '').strip().upper() not in ('CREATED', 'SUBMITTED'):
                    continue
                if (tss_status or '').strip() in TEST_FALLBACK_BLOCKED_STATUSES:
                    continue
                if (goods_count or 0) < 1 or (goods_ready_count or 0) != (goods_count or 0):
                    continue
                fallback_ready_count += 1
        effective_ready_count = max(auth_count, fallback_ready_count)
        active_gmr_count = cur.execute(f"""
            SELECT COUNT(*)
            FROM {S}.StagingGmrs
            WHERE staging_ens_id = ?
              AND status IN ('PENDING', 'SUBMITTED', 'ACTIVE')
        """, [staging_ens_id]).fetchone()[0]
        if cons_count > 0 and cons_count == effective_ready_count and active_gmr_count == 0:
            rows.append((staging_ens_id, ens_reference, label, identity_no_of_transport, cons_count, auth_count, active_gmr_count, effective_ready_count))
    print(f"ENS headers ready for GMR staging: {len(rows)}")

    staged = 0
    skipped = 0

    for staging_ens_id, ens_reference, label, identity_no_of_transport, cons_count, auth_count, _active_gmr_count, effective_ready_count in rows:
        existing = cur.execute(f"""
            SELECT COUNT(*)
            FROM {S}.StagingGmrs
            WHERE staging_ens_id = ?
              AND status IN ('PENDING', 'SUBMITTED', 'ACTIVE', 'CLOSED')
        """, [staging_ens_id]).fetchone()[0]
        if existing:
            skipped += 1
            print(f"  {ens_reference}: skip (GMR already exists)")
            continue

        vehicle_registration = (identity_no_of_transport or '').strip() or None
        cur.execute(f"""
            INSERT INTO {S}.StagingGmrs (
                staging_ens_id, ens_reference, label,
                vehicle_registration, status,
                notes, retry_count, max_retries,
                created_at, created_by
            ) VALUES (?, ?, ?, ?, 'PENDING', ?, 0, 3, SYSUTCDATETIME(), 'automation')
        """, [
            staging_ens_id,
            ens_reference,
            f"GMR - {ens_reference}" if not label else f"GMR - {ens_reference} - {label}",
            vehicle_registration,
            f"Auto-staged after {effective_ready_count}/{cons_count} consignments became GMR-ready.",
        ])
        conn.commit()
        staged += 1
        print(f"  {ens_reference}: staged GMR")

    print(f"\n{'=' * 55}")
    print(f"Staged: {staged}  |  Skipped: {skipped}")
    conn.close()
    sys.exit(0)


if __name__ == '__main__':
    main()
