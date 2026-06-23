"""
NOT FOR PRD: reads/writes legacy BKD.Staging* tables removed by migration 078. Do not run against Fusion_TSS_Automation_PRD.

Synovia Flow — GMR Status Sync + Arrival Detection
Polls TSS/GVMS for status of all SUBMITTED/ACTIVE GMRs.

When a GMR reaches 'Arrived' or 'Closed':
  - Updates GMR status to CLOSED
  - Updates linked ENS consignments' tss_status
  - Flags StagingSupDecHeaders rows as PENDING for any consignments
    that don't yet have a Supplementary Declaration staged.

This implements the core Route A → SFD trigger:
  ENS submitted → AUTHORISED_FOR_MOVEMENT → GMR → Arrived → SFD due

Usage:
    python scripts/sync_gmr.py
"""
import os, sys, time
from datetime import datetime, timezone
import pyodbc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tss_api import build_cfg_client, resolve_tss_settings
from app.tenant import get_tenant, tenant_aware_cursor
from config.db_connection import build_connection_string

S = get_tenant()["schema"]


def configure_console_output():
    for stream in (getattr(sys, 'stdout', None), getattr(sys, 'stderr', None)):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')


configure_console_output()


# GVMS statuses that mean the vehicle has crossed / arrived
ARRIVED_STATUSES = {'Arrived', 'Closed', 'arrived', 'closed', 'ARRIVED', 'CLOSED'}


def table_columns(cur, table_name):
    cur.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
    """, [S, table_name])
    return {row[0].lower() for row in cur.fetchall()}


def get_connection():
    from dotenv import load_dotenv
    load_dotenv()
    return pyodbc.connect(build_connection_string(timeout=30), autocommit=False)


def add_insert_value(columns, placeholders, values, available_columns, column_name, value):
    if column_name.lower() not in available_columns:
        return
    columns.append(column_name)
    placeholders.append('?')
    values.append(value)


def _gvms_status_from_response(response):
    resp = response or {}
    return resp.get('gvms_status') or resp.get('status') or ''


def flag_sfd_required(cur, conn, staging_ens_id, ens_reference, default_act_as=None):
    """
    For every consignment under this ENS that doesn't already have a
    StagingSupDecHeaders row, create a PENDING stub so the SFD workflow
    can pick it up.
    """
    if not staging_ens_id:
        return 0

    cons_columns = table_columns(cur, 'StagingConsignments')
    no_sfd_filter = ""
    if 'no_sfd_reason' in cons_columns:
        no_sfd_filter = "AND NULLIF(LTRIM(RTRIM(COALESCE(CAST(c.no_sfd_reason AS NVARCHAR(20)), ''))), '') IS NULL"

    cur.execute(f"""
        SELECT c.staging_id, c.dec_reference, c.sfd_reference,
               c.goods_description, c.importer_eori, c.importer_name
        FROM {S}.StagingConsignments c
        WHERE c.staging_ens_id = ?
          AND c.dec_reference IS NOT NULL
          {no_sfd_filter}
    """, [staging_ens_id])
    consignments = cur.fetchall()

    created = 0
    supdec_columns = table_columns(cur, 'StagingSupDecHeaders')
    has_act_as = 'act_as' in supdec_columns
    existing_ref_columns = [
        col for col in ('ens_consignment_ref', 'ens_consignment_reference', 'consignment_number')
        if col in supdec_columns
    ]

    if not existing_ref_columns:
        raise RuntimeError('StagingSupDecHeaders is missing a consignment reference column')

    for csid, dec_ref, sfd_ref, goods_desc, imp_eori, imp_name in consignments:
        # Check if SFD stub already exists for this consignment
        existing_where = ' OR '.join(f"{col} = ?" for col in existing_ref_columns)
        cur.execute(
            f"""
            SELECT COUNT(*) FROM {S}.StagingSupDecHeaders
            WHERE {existing_where}
            """,
            [dec_ref] * len(existing_ref_columns),
        )
        existing = cur.fetchone()[0]
        if existing:
            if sfd_ref and 'sfd_reference' in supdec_columns:
                cur.execute(
                    f"""
                    UPDATE {S}.StagingSupDecHeaders
                    SET sfd_reference = ?, updated_at = SYSUTCDATETIME()
                    WHERE ({existing_where})
                      AND (sfd_reference IS NULL OR sfd_reference = '')
                    """,
                    [sfd_ref] + [dec_ref] * len(existing_ref_columns),
                )
                conn.commit()
            continue

        # Create a PENDING stub so the SFD team can complete and submit it
        columns = []
        placeholders = []
        values = []

        add_insert_value(columns, placeholders, values, supdec_columns, 'label', f'GMR arrival stub for {dec_ref}')
        add_insert_value(columns, placeholders, values, supdec_columns, 'staging_cons_id', csid)
        add_insert_value(columns, placeholders, values, supdec_columns, 'ens_consignment_ref', dec_ref)
        add_insert_value(columns, placeholders, values, supdec_columns, 'ens_consignment_reference', dec_ref)
        add_insert_value(columns, placeholders, values, supdec_columns, 'ens_header_ref', ens_reference)
        add_insert_value(columns, placeholders, values, supdec_columns, 'ens_header_reference', ens_reference)
        add_insert_value(columns, placeholders, values, supdec_columns, 'consignment_number', dec_ref)
        add_insert_value(columns, placeholders, values, supdec_columns, 'goods_description', goods_desc or '')
        add_insert_value(columns, placeholders, values, supdec_columns, 'importer_eori', imp_eori or '')
        add_insert_value(columns, placeholders, values, supdec_columns, 'importer_name', imp_name or '')
        add_insert_value(columns, placeholders, values, supdec_columns, 'sfd_reference', sfd_ref or None)

        if has_act_as:
            add_insert_value(
                columns,
                placeholders,
                values,
                supdec_columns,
                'act_as',
                (default_act_as or '').strip() or None,
            )

        add_insert_value(columns, placeholders, values, supdec_columns, 'status', 'PENDING')
        add_insert_value(columns, placeholders, values, supdec_columns, 'tss_status', 'DRAFT')
        add_insert_value(columns, placeholders, values, supdec_columns, 'source', 'GMR_Status_Sync')

        if 'created_at' in supdec_columns:
            columns.append('created_at')
            placeholders.append('SYSUTCDATETIME()')
        if 'updated_at' in supdec_columns:
            columns.append('updated_at')
            placeholders.append('SYSUTCDATETIME()')

        cur.execute(
            f"""
            INSERT INTO {S}.StagingSupDecHeaders (
                {', '.join(columns)}
            ) VALUES ({', '.join(placeholders)})
            """,
            values,
        )
        conn.commit()
        created += 1
        print(f"      -> SFD stub created for consignment {dec_ref}")

    return created


def main():
    print("Synovia Flow — GMR Status Sync")
    print("=" * 55)

    conn = get_connection()
    cur = tenant_aware_cursor(conn.cursor())
    client = build_cfg_client()
    resolved_settings = resolve_tss_settings()

    cur.execute(f"""
        SELECT staging_id, ens_reference, gmr_id, gvms_status, staging_ens_id, label
        FROM {S}.StagingGmrs
        WHERE status IN ('SUBMITTED', 'ACTIVE')
        ORDER BY staging_id
    """)
    gmrs = cur.fetchall()
    print(f"GMRs to poll: {len(gmrs)}")

    changes = 0
    arrivals = 0
    sfd_stubs = 0

    for sid, ens_ref, gmr_id, prev_gvms, staging_ens_id, label in gmrs:
        ref = gmr_id or ens_ref
        if not ref:
            print(f"  #{sid} - SKIP (no reference)")
            continue

        result = client.read_gmr(ref)

        if not result.get('success'):
            error = (result.get('message') or '')[:60]
            print(f"  #{sid} {ref}: ERROR {error}")
            continue

        new_status = _gvms_status_from_response(result.get('response'))
        prev_gvms = prev_gvms or ''

        duration_ms = result.get('duration_ms') or 0
        if new_status == prev_gvms:
            print(f"  #{sid} {ref}: no change ({new_status}) ({duration_ms}ms)")
            continue

        print(f"  #{sid} {ref}: {prev_gvms} -> {new_status} ({duration_ms}ms)")
        changes += 1

        # Determine portal status from GVMS status
        if new_status in ARRIVED_STATUSES:
            portal_status = 'CLOSED'
        elif new_status in ('Open', 'Pending'):
            portal_status = 'ACTIVE'
        else:
            portal_status = 'ACTIVE'  # keep polling

        cur.execute(f"""
            UPDATE {S}.StagingGmrs SET
                gvms_status = ?,
                status = ?,
                updated_at = SYSUTCDATETIME()
            WHERE staging_id = ?
        """, [new_status, portal_status, sid])
        conn.commit()

        # ── Arrival detected: flag SFDs required ──────────────
        if new_status in ARRIVED_STATUSES:
            arrivals += 1
            print(f"    *** ARRIVED - flagging SFDs required for ENS {ens_ref} ***")

            # Update consignment TSS status to ARRIVED and align mutable local workflow status.
            if staging_ens_id:
                cur.execute(f"""
                    UPDATE {S}.StagingConsignments
                    SET tss_status = 'ARRIVED',
                        status = CASE
                            WHEN UPPER(REPLACE(COALESCE(CAST(status AS NVARCHAR(100)), ''), '_', ' ')) NOT IN ('IMPORTED', 'INGESTED')
                                THEN 'SUBMITTED'
                            ELSE status
                        END,
                        updated_at = SYSUTCDATETIME()
                    WHERE staging_ens_id = ?
                      AND UPPER(REPLACE(COALESCE(CAST(tss_status AS NVARCHAR(100)), ''), '_', ' ')) IN (
                          'AUTHORISED FOR MOVEMENT',
                          'AUTHORIZED FOR MOVEMENT'
                      )
                """, [staging_ens_id])
                conn.commit()

            # Create SFD stubs for consignments that don't have one yet
            n = flag_sfd_required(
                cur,
                conn,
                staging_ens_id,
                ens_ref,
                default_act_as=(resolved_settings.get('act_as') or '').strip() or None,
            )
            sfd_stubs += n
            if n:
                print(f"    Created {n} SFD stub(s)")

    print(f"\n{'=' * 55}")
    print(f"Status changes: {changes}  |  Arrivals: {arrivals}  |  SFD stubs created: {sfd_stubs}")
    conn.close()
    if arrivals:
        try:
            from app.ingestion.sdi_autosubmit import run_sdi_autosubmit
            tenant_code = os.environ.get("TENANT_CODE") or os.environ.get("CLIENT_CODE") or "BKD"
            print("\nChaining PRD-safe SDI autosubmit dry-run after arrival sync...")
            result = run_sdi_autosubmit(tenant_code=tenant_code, dry_run=True, submit=False)
            print(
                "  SDI autosubmit dry-run: "
                f"candidates={result.candidates}, discovered={result.discovered}, "
                f"ready={result.ready}, blocked={result.blocked}, submitted={result.submitted}"
            )
        except Exception as exc:
            print(f"  [warn] SDI autosubmit chain failed: {exc}")
    sys.exit(0)  # sync-only: API poll errors are non-fatal


if __name__ == '__main__':
    main()
