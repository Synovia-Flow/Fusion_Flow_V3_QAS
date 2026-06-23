#!/usr/bin/env python3
"""
NOT FOR PRD: reads/writes BKD.Staging* tables removed by migration 078.
             Use STG.BKD_* or ING.BKD_* for new pipeline work.

Bulk import TSS chain data from offline JSON dumps into Fusion staging tables.

Skips TSS API entirely — reads pre-extracted data from one of:

  * Movement folders   (Birkdale_Build/API_Test/movement_*/)
  * Probe folders      (BKD_Data_Model/Probe/ENS<refnum>/)
  * Flat dump folder   (TSS_Launch/json/ — ens_consignments.json, sfds.json,
                        supplementary_declarations.json, agent_relationships.json)

Inserts/upserts into:
  BKD.StagingEnsHeaders, BKD.StagingConsignments, BKD.StagingGoodsItems,
  BKD.Sfds, BKD.StagingSupDecHeaders

All inserted rows get source='BULK_IMPORT', status='IMPORTED'.

Usage:
  python scripts/bulk_import_from_dumps.py movements <path>
  python scripts/bulk_import_from_dumps.py probe <path>
  python scripts/bulk_import_from_dumps.py flat <path>
  python scripts/bulk_import_from_dumps.py --dry-run movements <path>

Examples:
  python scripts/bulk_import_from_dumps.py movements \\
      "//PL-AZ-Fusion-co/FusionProduction/Birkdale_Build/API_Test"
  python scripts/bulk_import_from_dumps.py flat \\
      "//PL-AZ-Fusion-co/FusionProduction/TSS_Launch/json"
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_standalone_connection

S = "BKD"


# ── Helpers ────────────────────────────────────────────────────────────

def _table_columns(cursor, table):
    cursor.execute(
        """
        SELECT LOWER(COLUMN_NAME) FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [S, table],
    )
    return {r[0] for r in cursor.fetchall()}


def _filter_to_existing(values, columns):
    return [(k, v) for k, v in values if k.lower() in columns]


def _upsert(cursor, table, key_col, key_value, values, *, identity_col='staging_id'):
    """Atomic upsert by single key column using SQL Server MERGE. Returns (id, inserted_bool).

    MERGE under SERIALIZABLE + HOLDLOCK is the canonical race-free upsert pattern in
    SQL Server. Avoids the SELECT-then-INSERT race that classic upsert has.
    """
    columns = _table_columns(cursor, table)
    values = _filter_to_existing(values, columns)
    if not values:
        return None, False
    # Always set the key column even if not in values list (needed for INSERT path)
    if not any(k.lower() == key_col.lower() for k, _ in values):
        values = [(key_col, key_value)] + values

    # Separate columns/values for INSERT vs UPDATE clauses
    insert_cols = [k for k, _ in values]
    update_pairs = [(k, v) for k, v in values if k.lower() != key_col.lower()]

    if 'created_at' in columns and 'created_at' not in [c.lower() for c in insert_cols]:
        insert_cols.append('created_at__SYSUTC')
    if 'updated_at' in columns:
        if 'updated_at' not in [c.lower() for c in insert_cols]:
            insert_cols.append('updated_at__SYSUTC')

    insert_col_sql = ", ".join(f"[{c.replace('__SYSUTC', '')}]" for c in insert_cols)
    insert_val_sql = ", ".join(
        'SYSUTCDATETIME()' if c.endswith('__SYSUTC') else '?' for c in insert_cols
    )
    insert_params = [v for _, v in values]

    update_sql = ", ".join(f"[{k}] = ?" for k, _ in update_pairs)
    if 'updated_at' in columns:
        update_sql += (", " if update_sql else "") + "updated_at = SYSUTCDATETIME()"
    update_params = [v for _, v in update_pairs]

    # Use MERGE with HOLDLOCK to make it atomic against concurrent inserts
    sql = f"""
        MERGE {S}.{table} WITH (HOLDLOCK) AS target
        USING (SELECT ? AS k) AS src
        ON target.[{key_col}] = src.k
        WHEN MATCHED THEN
            UPDATE SET {update_sql or f'[{key_col}] = src.k'}
        WHEN NOT MATCHED THEN
            INSERT ({insert_col_sql}) VALUES ({insert_val_sql})
        OUTPUT $action AS act, INSERTED.[{identity_col}] AS id;
    """
    params = [key_value] + update_params + insert_params
    cursor.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None, False
    action, new_id = row[0], row[1]
    return new_id, (action == 'INSERT')


def _acquire_lock(lock_path):
    """Acquire exclusive file lock. Returns file handle (keep open until done)."""
    import time
    import errno
    if os.path.exists(lock_path):
        try:
            with open(lock_path, 'r') as f:
                content = f.read().strip()
            print(f"ERROR: Lock file exists at {lock_path}")
            print(f"       Held by: {content}")
            print(f"       If stale, delete manually: rm {lock_path}")
            sys.exit(3)
        except Exception:
            pass
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError as e:
        if e.errno == errno.EEXIST:
            print(f"ERROR: Concurrent run blocked. Lock file: {lock_path}")
            sys.exit(3)
        raise
    info = f"pid={os.getpid()} time={datetime.utcnow().isoformat()}Z"
    os.write(fd, info.encode('utf-8'))
    os.close(fd)
    return lock_path


def _release_lock(lock_path):
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _result(payload):
    """Extract response.body.result or body.result or just result."""
    if not isinstance(payload, dict):
        return {}
    if 'response' in payload:
        return ((payload.get('response') or {}).get('body') or {}).get('result') or {}
    if 'body' in payload:
        return (payload.get('body') or {}).get('result') or {}
    if 'result' in payload:
        return payload.get('result') or {}
    return payload


def _norm_status(status):
    """Normalize TSS status string."""
    if not status:
        return 'IMPORTED'
    return str(status).strip().upper().replace(' ', '_')


# ── Movement folder import ─────────────────────────────────────────────

def import_movement_folder(cursor, movement_dir, stats):
    """Import one movement_NN folder. Returns dict of counts."""
    summary = _read_json(movement_dir / '_movement_summary.json')
    if not summary:
        print(f"  SKIP {movement_dir.name}: no _movement_summary.json")
        return

    ens_ref = summary.get('ens_000')
    dec_ref = summary.get('dec_ens')
    sfd_ref = summary.get('dec_sfd')
    sup_ref = summary.get('sup_dec')

    if not ens_ref or not dec_ref:
        print(f"  SKIP {movement_dir.name}: missing ENS or DEC ref")
        return

    print(f"  {movement_dir.name}: ENS={ens_ref} DEC={dec_ref} SFD={sfd_ref or '-'} SUP={sup_ref or '-'}")

    # ── ENS Header ──
    ens_data = _result(_read_json(movement_dir / 'call_02_ens_header.json'))
    ens_id, ens_inserted = _upsert(
        cursor, 'StagingEnsHeaders', 'ens_reference', ens_ref,
        [
            ('ens_reference', ens_ref),
            ('status', 'IMPORTED'),
            ('tss_status', _norm_status(ens_data.get('status'))),
            ('source', 'BULK_IMPORT'),
            ('movement_type', ens_data.get('movement_type')),
            ('arrival_port', ens_data.get('arrival_port')),
            ('arrival_date_time', ens_data.get('arrival_date_time')),
            ('carrier_eori', ens_data.get('carrier_eori')),
            ('carrier_name', ens_data.get('carrier_name')),
            ('identity_no_of_transport', ens_data.get('identity_no_of_transport')),
            ('nationality_of_transport', ens_data.get('nationality_of_transport')),
            ('seal_number', ens_data.get('seal_number')),
            ('route', ens_data.get('route')),
            ('place_of_loading', ens_data.get('place_of_loading')),
            ('place_of_unloading', ens_data.get('place_of_unloading')),
            ('error_message', ens_data.get('error_message') or None),
        ],
    )
    stats['ens_inserted' if ens_inserted else 'ens_updated'] += 1

    # ── Consignment ──
    cons_data = _result(_read_json(movement_dir / 'call_01_ens_consignment.json'))
    cons_id, cons_inserted = _upsert(
        cursor, 'StagingConsignments', 'dec_reference', dec_ref,
        [
            ('dec_reference', dec_ref),
            ('staging_ens_id', ens_id),
            ('ens_reference', ens_ref),
            ('sfd_reference', sfd_ref),
            ('status', 'IMPORTED'),
            ('tss_status', _norm_status(cons_data.get('status'))),
            ('source', 'BULK_IMPORT'),
            ('goods_description', cons_data.get('goods_description')),
            ('movement_reference_number', cons_data.get('movement_reference_number')),
            ('total_packages', cons_data.get('total_packages')),
            ('gross_mass_kg', cons_data.get('gross_mass_kg')),
            ('transport_document_number', cons_data.get('transport_document_number')),
            ('controlled_goods', cons_data.get('controlled_goods')),
            ('goods_domestic_status', cons_data.get('goods_domestic_status')),
            ('consignor_eori', cons_data.get('consignor_eori')),
            ('consignor_name', cons_data.get('consignor_name')),
            ('consignor_country', cons_data.get('consignor_country')),
            ('consignee_name', cons_data.get('consignee_name')),
            ('consignee_country', cons_data.get('consignee_country')),
            ('importer_eori', cons_data.get('importer_eori')),
            ('exporter_eori', cons_data.get('exporter_eori')),
            ('destination_country', cons_data.get('destination_country')),
            ('error_message', cons_data.get('error_message') or None),
        ],
    )
    stats['cons_inserted' if cons_inserted else 'cons_updated'] += 1

    # ── Goods Items ──
    for path in sorted(movement_dir.glob('call_04_ens_goods_item_*.json')):
        gi_data = _result(_read_json(path))
        goods_id = gi_data.get('goods_id') or gi_data.get('reference')
        if not goods_id:
            continue
        _, gi_inserted = _upsert(
            cursor, 'StagingGoodsItems', 'goods_id', goods_id,
            [
                ('goods_id', goods_id),
                ('staging_cons_id', cons_id),
                ('consignment_number', dec_ref),
                ('item_number', gi_data.get('item_number')),
                ('status', 'IMPORTED'),
                ('tss_status', _norm_status(gi_data.get('status'))),
                ('source', 'BULK_IMPORT'),
                ('goods_description', gi_data.get('goods_description')),
                ('commodity_code', gi_data.get('commodity_code')),
                ('procedure_code', gi_data.get('procedure_code')),
                ('country_of_origin', gi_data.get('country_of_origin')),
                ('gross_mass_kg', gi_data.get('gross_mass_kg')),
                ('net_mass_kg', gi_data.get('net_mass_kg')),
                ('number_of_packages', gi_data.get('number_of_packages')),
                ('type_of_packages', gi_data.get('type_of_packages')),
                ('package_marks', gi_data.get('package_marks')),
                ('item_invoice_amount', gi_data.get('item_invoice_amount')),
                ('item_invoice_currency', gi_data.get('item_invoice_currency')),
            ],
        )
        stats['goods_inserted' if gi_inserted else 'goods_updated'] += 1

    # ── SFD ──
    if sfd_ref:
        sfd_data = _result(_read_json(movement_dir / 'call_06_sfd_read.json'))
        _, sfd_inserted = _upsert(
            cursor, 'Sfds', 'sfd_reference', sfd_ref,
            [
                ('sfd_reference', sfd_ref),
                ('ens_consignment_reference', dec_ref),
                ('ens_header_reference', ens_ref),
                ('status', _norm_status(sfd_data.get('status'))),
                ('source', 'BULK_IMPORT'),
                ('goods_description', sfd_data.get('goods_description')),
                ('importer_eori', sfd_data.get('importer_eori')),
                ('exporter_eori', sfd_data.get('exporter_eori')),
                ('movement_reference_number', sfd_data.get('movement_reference_number')),
                ('transport_document_number', sfd_data.get('transport_document_number')),
                ('error_message', sfd_data.get('error_message') or None),
            ],
        )
        stats['sfd_inserted' if sfd_inserted else 'sfd_updated'] += 1

    # ── SDI stub ──
    if sup_ref:
        sdi_data = _result(_read_json(movement_dir / 'call_10_sdi_read.json'))
        _, sdi_inserted = _upsert(
            cursor, 'StagingSupDecHeaders', 'sup_dec_number', sup_ref,
            [
                ('sup_dec_number', sup_ref),
                ('staging_cons_id', cons_id),
                ('sfd_reference', sfd_ref),
                ('ens_consignment_ref', dec_ref),
                ('ens_header_ref', ens_ref),
                ('status', 'IMPORTED'),
                ('tss_status', _norm_status(sdi_data.get('status') if sdi_data else None)),
                ('source', 'BULK_IMPORT'),
                ('movement_reference_number', sdi_data.get('movement_reference_number') if sdi_data else None),
                ('importer_eori', sdi_data.get('importer_eori') if sdi_data else None),
                ('declaration_choice', sdi_data.get('declaration_choice') if sdi_data else None),
            ],
        )
        stats['sdi_inserted' if sdi_inserted else 'sdi_updated'] += 1


def import_movements(conn, root_path, dry_run=False):
    root = Path(root_path)
    stats = {k: 0 for k in (
        'ens_inserted', 'ens_updated',
        'cons_inserted', 'cons_updated',
        'goods_inserted', 'goods_updated',
        'sfd_inserted', 'sfd_updated',
        'sdi_inserted', 'sdi_updated',
    )}
    cursor = conn.cursor()
    movements = sorted(p for p in root.glob('movement_*') if p.is_dir())
    print(f"Found {len(movements)} movement folders in {root}")
    for movement_dir in movements:
        try:
            import_movement_folder(cursor, movement_dir, stats)
            if not dry_run:
                conn.commit()
        except Exception as e:
            print(f"  ERROR {movement_dir.name}: {e}")
            conn.rollback()
    cursor.close()
    return stats


# ── Flat dump import (TSS_Launch/json) ─────────────────────────────────

def import_flat_dump(conn, root_path, dry_run=False):
    root = Path(root_path)
    stats = {k: 0 for k in (
        'cons_inserted', 'cons_updated',
        'sfd_inserted', 'sfd_updated',
        'sdi_inserted', 'sdi_updated',
    )}
    cursor = conn.cursor()

    relationships = _read_json(root / 'agent_relationships.json') or []
    customer_to_sysid = {r['customer_account']: r['customer_account_sys_id'] for r in relationships}
    print(f"Loaded {len(customer_to_sysid)} customer→sys_id mappings")

    # Consignments (no parent ENS — import as orphan stubs)
    cons_list = _read_json(root / 'ens_consignments.json') or []
    print(f"Importing {len(cons_list)} consignments...")
    for cons in cons_list:
        dec_ref = cons.get('reference')
        if not dec_ref:
            continue
        customer = cons.get('_customer')
        act_as = customer_to_sysid.get(customer)
        _, inserted = _upsert(
            cursor, 'StagingConsignments', 'dec_reference', dec_ref,
            [
                ('dec_reference', dec_ref),
                ('status', 'IMPORTED'),
                ('tss_status', _norm_status(cons.get('status'))),
                ('source', 'BULK_IMPORT'),
                ('act_as', act_as),
                ('goods_description', cons.get('goods_description')),
                ('movement_reference_number', cons.get('movement_reference_number')),
                ('total_packages', cons.get('total_packages')),
                ('gross_mass_kg', cons.get('gross_mass_kg')),
                ('transport_document_number', cons.get('transport_document_number')),
                ('controlled_goods', cons.get('controlled_goods')),
                ('importer_eori', cons.get('importer_eori')),
                ('error_message', cons.get('error_message') or None),
            ],
        )
        stats['cons_inserted' if inserted else 'cons_updated'] += 1

    # SFDs
    sfd_list = _read_json(root / 'sfds.json') or []
    print(f"Importing {len(sfd_list)} SFDs...")
    for sfd in sfd_list:
        sfd_ref = sfd.get('reference')
        if not sfd_ref:
            continue
        customer = sfd.get('_customer')
        act_as = customer_to_sysid.get(customer)
        _, inserted = _upsert(
            cursor, 'Sfds', 'sfd_reference', sfd_ref,
            [
                ('sfd_reference', sfd_ref),
                ('ens_consignment_reference', sfd.get('ens_consignment_reference')),
                ('status', _norm_status(sfd.get('status'))),
                ('source', 'BULK_IMPORT'),
                ('act_as', act_as),
                ('goods_description', sfd.get('goods_description')),
                ('importer_eori', sfd.get('importer_eori')),
                ('exporter_eori', sfd.get('exporter_eori')),
                ('transport_document_number', sfd.get('transport_document_number')),
                ('ducr', sfd.get('ducr')),
            ],
        )
        stats['sfd_inserted' if inserted else 'sfd_updated'] += 1

        # Link consignment.sfd_reference if the cons exists
        ens_dec = sfd.get('ens_consignment_reference')
        if ens_dec:
            cursor.execute(
                f"UPDATE {S}.StagingConsignments SET sfd_reference = ?, "
                f"updated_at = SYSUTCDATETIME() WHERE dec_reference = ?",
                [sfd_ref, ens_dec],
            )

    # SDIs
    sdi_list = _read_json(root / 'supplementary_declarations.json') or []
    print(f"Importing {len(sdi_list)} SDIs...")
    for sdi in sdi_list:
        sup_ref = sdi.get('reference')
        if not sup_ref:
            continue
        customer = sdi.get('_customer')
        act_as = customer_to_sysid.get(customer)
        _, inserted = _upsert(
            cursor, 'StagingSupDecHeaders', 'sup_dec_number', sup_ref,
            [
                ('sup_dec_number', sup_ref),
                ('status', 'IMPORTED'),
                ('tss_status', _norm_status(sdi.get('status'))),
                ('source', 'BULK_IMPORT'),
                ('act_as', act_as),
                ('movement_reference_number', sdi.get('movement_reference_number')),
                ('importer_eori', sdi.get('importer_eori')),
                ('declaration_choice', sdi.get('declaration_choice')),
                ('total_packages', sdi.get('total_packages')),
                ('arrival_date_time', sdi.get('arrival_date_time')),
                ('submission_due_date', sdi.get('submission_due_date')),
                ('clear_date_time', sdi.get('clear_date_time')),
                ('trader_reference', sdi.get('trader_reference') or None),
                ('error_message', sdi.get('error_message') or None),
            ],
        )
        stats['sdi_inserted' if inserted else 'sdi_updated'] += 1

    if not dry_run:
        conn.commit()
    cursor.close()
    return stats


# ── Probe folder import ────────────────────────────────────────────────

def import_probe(conn, root_path, dry_run=False):
    """Import a single probe folder (e.g. ENS<refnum>/)."""
    root = Path(root_path)
    stats = {k: 0 for k in (
        'ens_inserted', 'ens_updated',
        'cons_inserted', 'cons_updated',
        'goods_inserted', 'goods_updated',
        'sfd_inserted', 'sfd_updated',
        'sdi_inserted', 'sdi_updated',
    )}
    cursor = conn.cursor()

    index = _read_json(root / '00_INDEX.json')
    if not index:
        print(f"No 00_INDEX.json in {root}")
        return stats

    ens_ref = index.get('probe_run', {}).get('ens_reference')
    dec_refs = index.get('probe_run', {}).get('dec_references', [])
    print(f"Probe: ENS={ens_ref}, {len(dec_refs)} DEC refs")

    ens_data = _result(_read_json(root / '01_ENS_Header.json'))
    ens_id, ens_inserted = _upsert(
        cursor, 'StagingEnsHeaders', 'ens_reference', ens_ref,
        [
            ('ens_reference', ens_ref),
            ('status', 'IMPORTED'),
            ('tss_status', _norm_status(ens_data.get('status'))),
            ('source', 'BULK_IMPORT'),
            ('movement_type', ens_data.get('movement_type')),
            ('arrival_port', ens_data.get('arrival_port')),
            ('arrival_date_time', ens_data.get('arrival_date_time')),
            ('carrier_eori', ens_data.get('carrier_eori')),
            ('carrier_name', ens_data.get('carrier_name')),
            ('identity_no_of_transport', ens_data.get('identity_no_of_transport')),
            ('nationality_of_transport', ens_data.get('nationality_of_transport')),
            ('seal_number', ens_data.get('seal_number')),
            ('route', ens_data.get('route')),
        ],
    )
    stats['ens_inserted' if ens_inserted else 'ens_updated'] += 1

    for dec_ref in dec_refs:
        cons_path = root / f'02_Consignment_{dec_ref}.json'
        cons_data = _result(_read_json(cons_path))
        cons_id, cons_inserted = _upsert(
            cursor, 'StagingConsignments', 'dec_reference', dec_ref,
            [
                ('dec_reference', dec_ref),
                ('staging_ens_id', ens_id),
                ('ens_reference', ens_ref),
                ('status', 'IMPORTED'),
                ('tss_status', _norm_status(cons_data.get('status'))),
                ('source', 'BULK_IMPORT'),
                ('goods_description', cons_data.get('goods_description')),
                ('movement_reference_number', cons_data.get('movement_reference_number')),
                ('total_packages', cons_data.get('total_packages')),
                ('gross_mass_kg', cons_data.get('gross_mass_kg')),
                ('importer_eori', cons_data.get('importer_eori')),
            ],
        )
        stats['cons_inserted' if cons_inserted else 'cons_updated'] += 1

    if not dry_run:
        conn.commit()
    cursor.close()
    return stats


# ── Main ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['movements', 'probe', 'flat'])
    parser.add_argument('path', help='Root path to dump folder')
    parser.add_argument('--dry-run', action='store_true', help='Do not commit')
    args = parser.parse_args()

    if not Path(args.path).exists():
        print(f"Path not found: {args.path}")
        sys.exit(2)

    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bulk_import.lock')
    _acquire_lock(lock_path)

    load_dotenv()
    conn = get_standalone_connection()
    try:
        if args.mode == 'movements':
            stats = import_movements(conn, args.path, dry_run=args.dry_run)
        elif args.mode == 'probe':
            stats = import_probe(conn, args.path, dry_run=args.dry_run)
        elif args.mode == 'flat':
            stats = import_flat_dump(conn, args.path, dry_run=args.dry_run)
        if args.dry_run:
            conn.rollback()
            print("\n[DRY RUN] Rolled back.")
        else:
            print("\nCommitted.")
        print("\nStats:")
        for k, v in stats.items():
            if v:
                print(f"  {k}: {v}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        _release_lock(lock_path)


if __name__ == '__main__':
    main()
