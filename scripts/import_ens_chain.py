#!/usr/bin/env python3
"""
NOT FOR PRD: reads/writes legacy BKD.Staging* tables removed by migration 078. Do not run against Fusion_TSS_Automation_PRD.

Import full ENS chain from TSS: ENS header → DEC consignments + goods → SFD → SDI stubs.

Usage:
    python scripts/import_ens_chain.py ENS000000002033455 ENS000000002034925
    python scripts/import_ens_chain.py --file path/to/ens_refs.txt
    python scripts/import_ens_chain.py ENS000000002033455 --dry-run
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(): return False

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string
from app.tenant import get_tenant
from app.tss_api import build_cfg_client

import pyodbc

RATE_LIMIT = 0.3   # seconds between TSS calls

S = get_tenant()["schema"]

# ── Field lists ──────────────────────────────────────────────────────────────

ENS_FIELDS = [
    'status', 'error_message', 'movement_type', 'type_of_passive_transport',
    'identity_no_of_transport', 'identity_no_transport',
    'nationality_of_transport', 'conveyance_ref', 'arrival_date_time',
    'arrival_port', 'place_of_loading', 'place_of_unloading',
    'place_of_acceptance_same_as_loading', 'place_of_acceptance',
    'place_of_delivery_same_as_unloading', 'place_of_delivery',
    'seal_number', 'transport_charges', 'route', 'carrier_eori',
    'carrier_name', 'carrier_street_number', 'carrier_city',
    'carrier_postcode', 'carrier_country', 'haulier_eori',
    'vehicle_registration', 'trailer_registration',
]

CONS_READ_FIELDS = [
    # v2.9.5 Postman read contract for GET /consignments.
    'status', 'transport_document_number', 'consignor_eori',
    'importer_eori', 'controlled_goods', 'holder_of_authorisation',
    'movement_reference_number', 'error_message', 'control_status',
    'eori_for_eidr', 'error_code', 'total_packages', 'gross_mass_kg',
    'declaration_number', 'consignment_number', 'reference',
]

CONS_FIELDS = [
    # Best-effort extended read. TSS may return some of these for portal records
    # even though the minimal read contract is smaller.
    'status', 'error_message', 'goods_description', 'trader_reference',
    'transport_document_number', 'controlled_goods', 'goods_domestic_status',
    'destination_country', 'country_of_destination', 'no_sfd_reason',
    'container_indicator', 'consignor_eori', 'consignor_name',
    'consignor_street_number', 'consignor_city', 'consignor_postcode',
    'consignor_country', 'consignee_eori', 'consignee_name',
    'consignee_street_number', 'consignee_city', 'consignee_postcode',
    'consignee_country', 'importer_eori', 'importer_name',
    'importer_street_number', 'importer_city', 'importer_postcode',
    'importer_country', 'exporter_eori', 'exporter_name',
    'exporter_street_number', 'exporter_city', 'exporter_postcode',
    'exporter_country', 'buyer_same_as_importer', 'seller_same_as_exporter',
    'buyer_eori', 'buyer_name', 'buyer_street_and_number', 'buyer_city',
    'buyer_postcode', 'buyer_country', 'seller_eori', 'seller_name',
    'seller_street_and_number', 'seller_city', 'seller_postcode',
    'seller_country', 'movement_reference_number', 'total_packages',
    'gross_mass_kg', 'control_status', 'goods_item_count', 'ducr',
    'eori_for_eidr', 'declaration_choice', 'use_importer_sde',
    'align_ukims', 'supervising_customs_office',
    'customs_warehouse_identifier', 'ens_number', 'ens_lrn',
    'ens_header_reference', 'declaration_number', 'consignment_number',
    'reference', 'mrn', 'eidr', 'sfd_reference', 'sfd_number',
    'submitted_at', 'submitted_date', 'submitted_date_time',
]

GOODS_FIELDS = [
    'status', 'error_message', 'goods_description', 'commodity_code',
    'type_of_packages', 'number_of_packages', 'package_marks',
    'gross_mass_kg', 'net_mass_kg', 'equipment_number',
    'controlled_goods', 'controlled_goods_type', 'country_of_origin',
    'item_invoice_amount', 'item_invoice_currency', 'procedure_code',
    'additional_procedure_code', 'taric_code', 'cus_code',
    'national_additional_code', 'country_of_preferential_origin',
    'preference', 'valuation_method', 'valuation_indicator',
    'invoice_number', 'nature_of_transaction', 'quota_order_number',
    'supplementary_units', 'statistical_value', 'customs_value',
    'ni_additional_information_codes', 'goods_id', 'reference',
]


# ── DB helpers ───────────────────────────────────────────────────────────────

def _table_columns(cur, table_name):
    cur.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        [S, table_name],
    )
    return {row[0].lower() for row in cur.fetchall()}


def _as_items(result):
    resp = result.get('response') if isinstance(result, dict) else result
    if resp is None:
        return []
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for key in ('items', 'results', 'data'):
            val = resp.get(key)
            if isinstance(val, list):
                return val
        return [resp]
    return []


def _ref_from(item, *keys):
    if not isinstance(item, dict):
        return None
    for k in keys:
        v = (item.get(k) or '').strip().upper()
        if v:
            return v
    return None


def _detail_payload(payload):
    if not isinstance(payload, dict):
        return {}

    container_keys = {
        'result', 'data', 'item', 'record', 'payload',
        'header', 'consignment', 'goods',
    }
    merged = {}
    for key, value in payload.items():
        if key in container_keys:
            continue
        if isinstance(value, (dict, list)):
            continue
        if value not in (None, ''):
            merged[key] = value

    for key in container_keys:
        value = payload.get(key)
        if isinstance(value, dict):
            merged.update(_detail_payload(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    merged.update(_detail_payload(item))
                    break
    return merged


def _merge_payloads(*payloads):
    merged = {}
    for payload in payloads:
        for key, value in _detail_payload(payload).items():
            if value not in (None, ''):
                merged[key] = value
    return merged


def _tss_datetime_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(normalized).replace(tzinfo=None)
    except ValueError:
        pass

    for fmt in (
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ── ENS header upsert ────────────────────────────────────────────────────────

def _upsert_ens_header(cur, ens_ref, data, dry_run):
    cols = _table_columns(cur, 'StagingEnsHeaders')
    field_map = {
        'label': f'Imported {ens_ref}',
        'ens_reference': ens_ref,
        'status': 'CREATED',
        'tss_status': data.get('status') or 'PENDING_SYNC',
        'source': 'TSS_CHAIN_IMPORT',
    }
    for f in ENS_FIELDS:
        if f not in ('status',) and f in cols:
            field_map[f] = data.get(f)
    if 'raw_json' in cols:
        field_map['raw_json'] = json.dumps(data, default=str)

    existing = cur.execute(
        f"SELECT TOP 1 staging_id FROM {S}.StagingEnsHeaders WHERE ens_reference = ?",
        [ens_ref],
    ).fetchone()

    if dry_run:
        action = 'UPDATE' if existing else 'INSERT'
        print(f"    [DRY] {action} StagingEnsHeaders {ens_ref}")
        return existing[0] if existing else -1, not bool(existing)

    if existing:
        sid = existing[0]
        sets = ', '.join(f"[{k}] = ?" for k in field_map if k not in ('label', 'ens_reference', 'source', 'created_at'))
        vals = [v for k, v in field_map.items() if k not in ('label', 'ens_reference', 'source', 'created_at')]
        if sets:
            cur.execute(f"UPDATE {S}.StagingEnsHeaders SET {sets}, updated_at = SYSUTCDATETIME() WHERE staging_id = ?",
                        vals + [sid])
        return sid, False

    cols_insert = [k for k in field_map if k in cols]
    placeholders = ', '.join(['?'] * len(cols_insert))
    col_names = ', '.join(f'[{c}]' for c in cols_insert)
    vals = [field_map[k] for k in cols_insert]
    cur.execute(
        f"INSERT INTO {S}.StagingEnsHeaders ({col_names}) OUTPUT INSERTED.staging_id VALUES ({placeholders})",
        vals,
    )
    row = cur.fetchone()
    return row[0] if row else None, True


# ── Consignment upsert ───────────────────────────────────────────────────────

def _upsert_consignment(cur, staging_ens_id, dec_ref, data, dry_run):
    cols = _table_columns(cur, 'StagingConsignments')
    field_map = {
        'staging_ens_id': staging_ens_id,
        'label': (data.get('trader_reference') or data.get('goods_description') or dec_ref)[:200],
        'dec_reference': dec_ref,
        'declaration_number': dec_ref,
        'status': 'CREATED',
        'tss_status': data.get('status') or 'PENDING_SYNC',
        'source': 'TSS_CHAIN_IMPORT',
    }
    for f in CONS_FIELDS:
        if f not in ('status', 'declaration_number', 'reference') and f in cols:
            if f == 'submitted_at':
                field_map[f] = _tss_datetime_value(
                    data.get('submitted_at') or data.get('submitted_date_time') or data.get('submitted_date')
                )
            else:
                field_map[f] = data.get(f)
    if 'raw_json' in cols:
        field_map['raw_json'] = json.dumps(data, default=str)

    existing = cur.execute(
        f"SELECT TOP 1 staging_id FROM {S}.StagingConsignments WHERE dec_reference = ?",
        [dec_ref],
    ).fetchone()

    if dry_run:
        action = 'UPDATE' if existing else 'INSERT'
        print(f"      [DRY] {action} StagingConsignments {dec_ref}")
        return existing[0] if existing else -1, not bool(existing)

    if existing:
        sid = existing[0]
        skip = {'staging_ens_id', 'label', 'dec_reference', 'declaration_number', 'source', 'created_at', 'status'}
        sets = ', '.join(f"[{k}] = ?" for k in field_map if k not in skip)
        vals = [v for k, v in field_map.items() if k not in skip]
        if sets:
            cur.execute(f"UPDATE {S}.StagingConsignments SET {sets}, updated_at = SYSUTCDATETIME() WHERE staging_id = ?",
                        vals + [sid])
        return sid, False

    cols_insert = [k for k in field_map if k in cols]
    placeholders = ', '.join(['?'] * len(cols_insert))
    col_names = ', '.join(f'[{c}]' for c in cols_insert)
    vals = [field_map[k] for k in cols_insert]
    cur.execute(
        f"INSERT INTO {S}.StagingConsignments ({col_names}) OUTPUT INSERTED.staging_id VALUES ({placeholders})",
        vals,
    )
    row = cur.fetchone()
    return row[0] if row else None, True


# ── Goods upsert ─────────────────────────────────────────────────────────────

def _upsert_goods(cur, staging_cons_id, goods_ref, data, item_number, dry_run):
    cols = _table_columns(cur, 'StagingGoodsItems')
    label = (data.get('goods_description') or goods_ref or f'Item {item_number}')[:200]
    field_map = {
        'staging_cons_id': staging_cons_id,
        'item_number': item_number,
        'label': label,
        'goods_id': goods_ref,
        'status': 'CREATED',
        'tss_status': data.get('status') or 'CREATED',
        'source': 'TSS_CHAIN_IMPORT',
    }
    for f in GOODS_FIELDS:
        if f not in ('status', 'goods_id', 'reference') and f in cols:
            field_map[f] = data.get(f)
    if 'raw_json' in cols:
        field_map['raw_json'] = json.dumps(data, default=str)

    existing = cur.execute(
        f"SELECT TOP 1 staging_id FROM {S}.StagingGoodsItems WHERE goods_id = ?",
        [goods_ref],
    ).fetchone()

    if dry_run:
        action = 'UPDATE' if existing else 'INSERT'
        print(f"        [DRY] {action} StagingGoodsItems {goods_ref}")
        return existing[0] if existing else -1, not bool(existing)

    if existing:
        sid = existing[0]
        skip = {'staging_cons_id', 'goods_id', 'source', 'created_at', 'status'}
        sets = ', '.join(f"[{k}] = ?" for k in field_map if k not in skip)
        vals = [v for k, v in field_map.items() if k not in skip]
        if sets:
            cur.execute(f"UPDATE {S}.StagingGoodsItems SET {sets}, updated_at = SYSUTCDATETIME() WHERE staging_id = ?",
                        vals + [sid])
        return sid, False

    cols_insert = [k for k in field_map if k in cols]
    placeholders = ', '.join(['?'] * len(cols_insert))
    col_names = ', '.join(f'[{c}]' for c in cols_insert)
    vals = [field_map[k] for k in cols_insert]
    cur.execute(
        f"INSERT INTO {S}.StagingGoodsItems ({col_names}) OUTPUT INSERTED.staging_id VALUES ({placeholders})",
        vals,
    )
    row = cur.fetchone()
    return row[0] if row else None, True


# ── SFD upsert ───────────────────────────────────────────────────────────────

def _upsert_sfd(cur, sfd_ref, dec_ref, ens_ref, data, dry_run):
    cols = _table_columns(cur, 'Sfds')
    if not cols:
        return None, False  # table not present

    if dry_run:
        print(f"        [DRY] UPSERT Sfds {sfd_ref}")
        return -1, True

    lookup_cols = [c for c in ('sfd_reference', 'sfd_number', 'reference') if c in cols]
    existing = None
    if lookup_cols:
        where_sql = ' OR '.join(f'[{c}] = ?' for c in lookup_cols)
        existing = cur.execute(
            f"SELECT TOP 1 id FROM {S}.Sfds WHERE {where_sql}",
            [sfd_ref] * len(lookup_cols),
        ).fetchone()

    mrn = data.get('mrn') or data.get('movement_reference_number')
    tss_status = data.get('status') or data.get('tss_status') or 'PENDING_SYNC'
    raw = json.dumps(data, default=str)

    if existing:
        cur.execute(
            f"UPDATE {S}.Sfds SET tss_status=?, mrn=?, raw_json=?, updated_at=SYSUTCDATETIME() WHERE id=?",
            [tss_status, mrn, raw, existing[0]],
        )
        return existing[0], False

    field_map = {
        'sfd_reference': sfd_ref,
        'sfd_number': sfd_ref,
        'declaration_number': dec_ref,
        'consignment_number': dec_ref,
        'ens_reference': ens_ref,
        'mrn': mrn,
        'movement_reference_number': mrn,
        'tss_status': tss_status,
        'status': tss_status,
        'raw_json': raw,
    }
    cols_insert = [k for k in field_map if k in cols]
    placeholders = ', '.join(['?'] * len(cols_insert))
    col_names = ', '.join(f'[{c}]' for c in cols_insert)
    vals = [field_map[k] for k in cols_insert]
    cur.execute(
        f"INSERT INTO {S}.Sfds ({col_names}) OUTPUT INSERTED.id VALUES ({placeholders})",
        vals,
    )
    row = cur.fetchone()
    return row[0] if row else None, True


# ── SDI stub upsert ──────────────────────────────────────────────────────────

def _upsert_sdi_stub(cur, sup_ref, sfd_ref, dec_ref, ens_ref, dry_run):
    cols = _table_columns(cur, 'StagingSupDecHeaders')
    if not cols:
        return None, False

    # column name varies by migration version
    sfd_col = 'sfd_reference' if 'sfd_reference' in cols else 'sfd_number' if 'sfd_number' in cols else None
    id_col = 'staging_id' if 'staging_id' in cols else 'id'

    if dry_run:
        print(f"        [DRY] UPSERT StagingSupDecHeaders {sup_ref}")
        return -1, True

    existing = cur.execute(
        f"SELECT TOP 1 [{id_col}] FROM {S}.StagingSupDecHeaders WHERE sup_dec_number = ?",
        [sup_ref],
    ).fetchone()

    if existing:
        return existing[0], False

    field_map = {
        'sup_dec_number': sup_ref,
        'ens_header_reference': ens_ref,
        'ens_consignment_reference': dec_ref,
        'status': 'CREATED',
        'tss_status': 'PENDING_SYNC',
        'source': 'TSS_CHAIN_IMPORT',
    }
    if sfd_col:
        field_map[sfd_col] = sfd_ref
    if 'consignment_number' in cols:
        field_map['consignment_number'] = dec_ref

    cols_insert = [k for k in field_map if k in cols]
    placeholders = ', '.join(['?'] * len(cols_insert))
    col_names = ', '.join(f'[{c}]' for c in cols_insert)
    vals = [field_map[k] for k in cols_insert]
    cur.execute(
        f"INSERT INTO {S}.StagingSupDecHeaders ({col_names}) OUTPUT INSERTED.[{id_col}] VALUES ({placeholders})",
        vals,
    )
    row = cur.fetchone()
    return row[0] if row else None, True


# ── Per-ENS import ────────────────────────────────────────────────────────────

def import_ens(client, conn, cur, ens_ref, dry_run, counters):
    print(f"\n[{ens_ref}]")

    # 1. ENS header
    result = client.read_header(ens_ref, ENS_FIELDS)
    time.sleep(RATE_LIMIT)
    if not result.get('success'):
        # fallback with shorter field list
        result = client.read_header(ens_ref, [
            'status', 'error_message', 'movement_type', 'conveyance_ref',
            'arrival_date_time', 'arrival_port', 'vehicle_registration',
            'trailer_registration', 'carrier_name', 'carrier_eori', 'route',
        ])
        time.sleep(RATE_LIMIT)
    header_data = _detail_payload(result.get('response') or {})
    if not result.get('success'):
        print(f"  WARN: header read failed — {result.get('message')} — creating stub")

    staging_ens_id, ens_inserted = _upsert_ens_header(cur, ens_ref, header_data, dry_run)
    if not dry_run:
        conn.commit()
    counters['ens_created' if ens_inserted else 'ens_updated'] += 1
    print(f"  ENS {'created' if ens_inserted else 'updated'} staging_id={staging_ens_id}")

    # 2. Discover DEC consignments
    discovered_cons = []
    seen_cons = set()
    for parent_type in ('ens_number', 'declaration_number', 'ens_lrn', 'ens_header_reference'):
        if discovered_cons:
            break
        r = client.lookup_consignments_for_header(ens_ref, parent_type=parent_type)
        time.sleep(RATE_LIMIT)
        if r.get('success'):
            for item in client.as_items(r.get('response')):
                ref = _ref_from(item, 'reference', 'consignment_number', 'declaration_number', 'dec_reference')
                if ref and ref not in seen_cons:
                    discovered_cons.append(item)
                    seen_cons.add(ref)

    if not discovered_cons:
        print(f"  no consignments found in TSS")
        return

    print(f"  {len(discovered_cons)} consignment(s) found")

    for cons_item in discovered_cons:
        dec_ref = _ref_from(cons_item, 'reference', 'consignment_number', 'declaration_number', 'dec_reference')
        if not dec_ref:
            continue

        # read full consignment data
        cr = client.read_consignment(dec_ref, CONS_READ_FIELDS)
        time.sleep(RATE_LIMIT)
        cons_data = _detail_payload(cr.get('response')) if cr.get('success') else {}
        cr_ext = client.read_consignment(dec_ref, CONS_FIELDS)
        time.sleep(RATE_LIMIT)
        if cr_ext.get('success') and isinstance(cr_ext.get('response'), dict):
            cons_data = _merge_payloads(cons_data, cr_ext.get('response'))
        if not cons_data and isinstance(cons_item, dict):
            cons_data = dict(cons_item)
        elif isinstance(cons_item, dict):
            cons_data = _merge_payloads(cons_item, cons_data)
        cons_data.setdefault('reference', dec_ref)

        staging_cons_id, cons_inserted = _upsert_consignment(cur, staging_ens_id, dec_ref, cons_data, dry_run)
        if not dry_run:
            conn.commit()
        counters['cons_created' if cons_inserted else 'cons_updated'] += 1
        print(f"  DEC {dec_ref} {'created' if cons_inserted else 'updated'} staging_cons_id={staging_cons_id}")

        # 3. Goods items
        gr = client.lookup_ens_goods(dec_ref)
        time.sleep(RATE_LIMIT)
        goods_items = client.as_items(gr.get('response')) if gr.get('success') else []
        if not goods_items:
            gr2 = client.lookup_goods(dec_ref, parent_type='consignment_number')
            time.sleep(RATE_LIMIT)
            if gr2.get('success'):
                goods_items = client.as_items(gr2.get('response'))

        for idx, g_item in enumerate(goods_items, start=1):
            goods_ref = _ref_from(g_item, 'goods_id', 'reference')
            if not goods_ref:
                continue
            grd = client.read_goods(goods_ref, GOODS_FIELDS)
            time.sleep(RATE_LIMIT)
            goods_data = _detail_payload(grd.get('response')) if grd.get('success') else {}
            if not goods_data and isinstance(g_item, dict):
                goods_data = dict(g_item)
            elif isinstance(g_item, dict):
                goods_data = _merge_payloads(g_item, goods_data)
            goods_data.setdefault('goods_id', goods_ref)
            _, g_inserted = _upsert_goods(cur, staging_cons_id, goods_ref, goods_data, idx, dry_run)
            if not dry_run:
                conn.commit()
            counters['goods_created' if g_inserted else 'goods_updated'] += 1

        if goods_items:
            print(f"    {len(goods_items)} goods item(s) imported")

        # 4. SFD lookup
        sfdr = client.lookup_sfd(dec_ref)
        time.sleep(RATE_LIMIT)
        sfd_items = client.as_items(sfdr.get('response')) if sfdr.get('success') else []

        for sfd_item in sfd_items:
            sfd_ref = _ref_from(sfd_item, 'sfd_reference', 'reference', 'sfd_number', 'declaration_number')
            if not sfd_ref:
                continue

            _, sfd_inserted = _upsert_sfd(cur, sfd_ref, dec_ref, ens_ref, sfd_item if isinstance(sfd_item, dict) else {}, dry_run)
            if not dry_run:
                # also update sfd_reference on the consignment
                cur.execute(
                    f"UPDATE {S}.StagingConsignments SET sfd_reference=?, updated_at=SYSUTCDATETIME() WHERE staging_id=?",
                    [sfd_ref, staging_cons_id],
                )
                conn.commit()
            counters['sfd_created' if sfd_inserted else 'sfd_updated'] += 1
            print(f"    SFD {sfd_ref} {'created' if sfd_inserted else 'updated'}")

            # 5. SDI lookup
            sdir = client.lookup_sdi(sfd_ref)
            time.sleep(RATE_LIMIT)
            sdi_items = client.as_sdi_lookup_items(sdir.get('response')) if sdir.get('success') else []

            for sdi_item in sdi_items:
                sup_ref = _ref_from(sdi_item, 'sup_dec_number', 'reference', 'supplementary_declaration_number', 'number')
                if not sup_ref:
                    continue
                _, sdi_inserted = _upsert_sdi_stub(cur, sup_ref, sfd_ref, dec_ref, ens_ref, dry_run)
                if not dry_run:
                    conn.commit()
                counters['sdi_created' if sdi_inserted else 'sdi_updated'] += 1
                print(f"      SDI {sup_ref} {'created' if sdi_inserted else 'updated'}")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='Import full ENS chain from TSS.')
    ap.add_argument('refs', nargs='*', help='ENS references')
    ap.add_argument('--file', help='Text file with one ENS reference per line')
    ap.add_argument('--dry-run', action='store_true', help='Parse without writing to DB')
    args = ap.parse_args()

    load_dotenv()

    refs = list(args.refs)
    if args.file:
        with open(args.file) as fh:
            refs.extend(ln.strip() for ln in fh if ln.strip())
    refs = sorted({r.strip().upper() for r in refs if r.strip().upper().startswith('ENS')})

    if not refs:
        print('No ENS references supplied.')
        sys.exit(2)

    print(f'Importing {len(refs)} ENS chain(s){" [DRY RUN]" if args.dry_run else ""}...')

    client = build_cfg_client()
    conn = pyodbc.connect(build_connection_string(timeout=30), autocommit=False) if not args.dry_run else None
    cur = conn.cursor() if conn else None

    counters = {k: 0 for k in (
        'ens_created', 'ens_updated',
        'cons_created', 'cons_updated',
        'goods_created', 'goods_updated',
        'sfd_created', 'sfd_updated',
        'sdi_created', 'sdi_updated',
    )}
    errors = 0

    for ens_ref in refs:
        try:
            import_ens(client, conn, cur, ens_ref, args.dry_run, counters)
        except Exception as exc:
            print(f'  ERROR {ens_ref}: {exc}')
            errors += 1
            if conn:
                conn.rollback()

    if cur:
        cur.close()
    if conn:
        conn.close()

    print('\n── Summary ──────────────────────────────────────')
    for k, v in counters.items():
        if v:
            print(f'  {k}: {v}')
    if errors:
        print(f'  errors: {errors}')
    print('Done.')


if __name__ == '__main__':
    main()
