"""
NOT FOR PRD: reads/writes BKD.Staging* tables removed by migration 078.
             Use STG.BKD_* or ING.BKD_* for new pipeline work.

Backfill ENS headers from TSS into BKD.StagingEnsHeaders.

Ported from the older Birkdale repo and adapted to the current schema.
This version focuses on ENS header recovery because that is the safest,
highest-value additive import for the current codebase.

Usage:
    python scripts/backfill_from_tss.py ENS000123456 ENS000123457
    python scripts/backfill_from_tss.py --from-db
"""
import argparse
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_standalone_connection
from app.job_logger import JobRun
from app.tenant import tenant_aware_cursor
from app.tss_api import build_cfg_client

FIELDS = [
    'status',
    'movement_type',
    'arrival_date_time',
    'arrival_port',
    'carrier_name',
    'carrier_eori',
    'vehicle_registration',
    'trailer_registration',
    'error_message',
]


def _load_refs_from_db(cursor):
    rows = cursor.execute(
        """
        SELECT DISTINCT ens_reference
        FROM BKD.StagingEnsHeaders
        WHERE ens_reference IS NOT NULL AND ens_reference <> ''
        ORDER BY ens_reference
        """
    ).fetchall()
    return [row[0] for row in rows]


def _upsert_header(cursor, ens_ref, payload):
    remote_status = payload.get('status') or 'DRAFT'
    error_message = payload.get('error_message')

    exists = cursor.execute(
        "SELECT COUNT(*) FROM BKD.StagingEnsHeaders WHERE ens_reference = ?",
        ens_ref,
    ).fetchone()[0]

    if exists:
        cursor.execute(
            """
            UPDATE BKD.StagingEnsHeaders
            SET tss_status            = ?,
                movement_type         = ?,
                arrival_date_time     = ?,
                arrival_port          = ?,
                carrier_name          = ?,
                carrier_eori          = ?,
                vehicle_registration  = ?,
                trailer_registration  = ?,
                error_message         = ?,
                updated_at            = SYSUTCDATETIME(),
                source                = COALESCE(source, 'Birkdale_Backfill')
            WHERE ens_reference = ?
            """,
            remote_status,
            payload.get('movement_type'),
            payload.get('arrival_date_time'),
            payload.get('arrival_port'),
            payload.get('carrier_name'),
            payload.get('carrier_eori'),
            payload.get('vehicle_registration'),
            payload.get('trailer_registration'),
            error_message,
            ens_ref,
        )
        return 'updated'

    cursor.execute(
        """
        INSERT INTO BKD.StagingEnsHeaders (
            label, ens_reference, status, tss_status,
            movement_type, arrival_date_time, arrival_port,
            carrier_name, carrier_eori,
            vehicle_registration, trailer_registration,
            error_message, source, created_at, updated_at
        ) VALUES (
            ?, ?, 'CREATED', ?,
            ?, ?, ?,
            ?, ?,
            ?, ?,
            ?, 'Birkdale_Backfill', SYSUTCDATETIME(), SYSUTCDATETIME()
        )
        """,
        f'Imported {ens_ref}',
        ens_ref,
        remote_status,
        payload.get('movement_type'),
        payload.get('arrival_date_time'),
        payload.get('arrival_port'),
        payload.get('carrier_name'),
        payload.get('carrier_eori'),
        payload.get('vehicle_registration'),
        payload.get('trailer_registration'),
        error_message,
    )
    return 'inserted'


def main():
    parser = argparse.ArgumentParser(description='Backfill ENS headers from TSS.')
    parser.add_argument('refs', nargs='*', help='ENS references to import')
    parser.add_argument('--from-db', action='store_true', help='Also import ENS refs already present in StagingEnsHeaders')
    args = parser.parse_args()

    load_dotenv()
    client = build_cfg_client()

    with JobRun('backfill_from_tss', triggered_by='manual') as jr:
        conn = get_standalone_connection()
        cursor = tenant_aware_cursor(conn.cursor())

        refs = []
        if args.from_db:
            refs.extend(_load_refs_from_db(cursor))
        refs.extend(args.refs)
        refs = sorted({(ref or '').strip().upper() for ref in refs if (ref or '').strip()})

        if not refs:
            print('No ENS references supplied. Pass ENS refs or use --from-db.')
            jr.log_lines = ['No ENS references supplied.']
            jr.rows_processed = 0
            conn.close()
            return

        print(f'Backfilling {len(refs)} ENS header(s) from TSS...')
        lines = [f'Backfilling {len(refs)} ENS header(s) from TSS.']
        inserted = 0
        updated = 0
        failed = 0

        for ens_ref in refs:
            result = client.read_header(ens_ref, FIELDS)
            if not result.get('success') or not result.get('response'):
                failed += 1
                msg = f'FAILED {ens_ref}: {result.get("message") or "empty response"}'
                lines.append(msg)
                print(msg)
                continue

            action = _upsert_header(cursor, ens_ref, result['response'])
            conn.commit()
            if action == 'inserted':
                inserted += 1
            else:
                updated += 1

            msg = f'{action.upper()} {ens_ref}: {result["response"].get("status", "unknown")}'
            lines.append(msg)
            print(msg)

        summary = f'Done. Inserted={inserted} Updated={updated} Failed={failed}'
        lines.append(summary)
        print(summary)

        jr.rows_processed = inserted + updated
        jr.log_lines = lines
        conn.close()


if __name__ == '__main__':
    main()
