"""
Supplementary Declaration (SDI) CRUD Blueprint — Fusion Flow V2 BKD Portal
Uses app.db module. Links to SFD/Consignment/ENS chain.
API: POST /supplementary_declarations, POST /goods (consignment_number=SUP...)
"""
import logging
import re
import csv
import io
import json
import time
from collections import Counter, namedtuple
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, g, jsonify
from app.db import query_all, query_one, execute, db_cursor
from app.sdi_payloads import (
    SDI_HEADER_NESTED_FIELDS,
    SDI_UPDATE_FIELDS,
    build_sdi_create_payload,
    build_sdi_goods_update_payload_for_api_attempt,
    build_sdi_update_payload,
    build_sdi_update_payload_for_api_attempt,
)
from app.status_utils import (
    TSS_FILTER_STATUS_TABS,
    badge_class_for_status,
    canonical_filter_status,
    consignment_should_discover_sdi,
    effective_tss_filter_status,
    normalize_status_key,
    status_filter_tabs,
)
from app.tss_api import TssApiClient, build_cfg_client
from app.tss_guidance import clean_tss_message, explain_tss_error

CVOption = namedtuple('CVOption', ['value', 'name'])

supdec_bp = Blueprint('supdec', __name__,
    template_folder='../../templates/supdec',
    url_prefix='/supdec')

S = 'BKD'

PRD_SDI_SUBMIT_READ_FIELDS = tuple(dict.fromkeys((
    'status',
    'submission_due_date',
    'error_message',
    'sup_dec_number',
    'sfd_number',
    *SDI_UPDATE_FIELDS,
    *(
        source_name
        for source_names in SDI_HEADER_NESTED_FIELDS.values()
        for source_name in source_names
    ),
)))

PRD_SDI_GOODS_SYNC_READ_FIELDS = (
    'goods_description',
    'commodity_code',
    'country_of_origin',
    'gross_mass_kg',
    'net_mass_kg',
    'number_of_packages',
    'type_of_packages',
    'package_marks',
    'item_invoice_amount',
    'item_invoice_currency',
    'taric_code',
    'national_additional_code',
    'document_references',
    'item_number',
    'goods_item_number',
    'supplementary_units',
)

PRD_SDI_POST_SUBMIT_REVIEW_STATUSES = {
    'DRAFT',
    'TRADER INPUT REQUIRED',
    'AMENDMENT REQUIRED',
    'REJECTED',
    'ERROR',
    'FAILED',
    'FAILURE',
}
PRD_SDI_KNOWN_TIR_STATUSES = {'TRADER INPUT REQUIRED', 'AMENDMENT REQUIRED'}
PRD_SDI_TERMINAL_OR_IN_FLIGHT_STATUSES = {
    'CLOSED',
    'COMPLETED',
    'CLEARED',
    'ACCEPTED',
    'CANCELLED',
    'CANCELED',
    'PROCESSING',
    'PENDING PAYMENT',
    'SUBMITTED',
}
PRD_SDI_TERMINAL_STATUSES = {
    'CLOSED',
    'COMPLETED',
    'CLEARED',
    'ACCEPTED',
    'CANCELLED',
    'CANCELED',
}
PRD_SDI_KNOWN_TARIC_BY_COMMODITY_ORIGIN = {
    # Learned from closed TSS history for steel safeguard goods.
    ('7318129090', 'CN'): '899L',
}
logger = logging.getLogger(__name__)
OFFICIAL_TSS_SUP_RE = re.compile(r'^SUP0{6,}\d+$', re.IGNORECASE)
SUPDEC_TSS_READ_FIELDS = (
    'status',
    'error_message',
    'movement_reference_number',
    'clear_date_time',
    'sfd_number',
    'importer_eori',
    'trader_reference',
    'transport_document_number',
    'arrival_date_time',
    'submission_due_date',
)

def badge_class(status):
    return badge_class_for_status(status)


def _supdec_detail_target(sd):
    ref = (sd or {}).get('sup_dec_number')
    if ref:
        return url_for('supdec.detail_by_ref', sup_ref=ref)
    return url_for('supdec.detail', sid=(sd or {}).get('staging_id', 0))


SUPDEC_HEADER_ALIASES = {
    'staging_id': ('id',),
    'sfd_reference': ('sfd_number',),
    'ens_consignment_ref': ('ens_consignment_reference',),
    'ens_header_ref': ('ens_header_reference',),
    'freight_charge_currency': ('freight_currency',),
    'insurance': ('insurance_amount',),
}

SUPDEC_GOODS_ALIASES = {
    'staging_id': ('id',),
    'staging_supdec_id': ('supdec_header_id',),
    'item_number': ('goods_item_number',),
    'sup_goods_id': ('tss_goods_id_sdi', 'goods_id'),
    'type_of_packages': ('type_of_package',),
    'gross_mass_kg': ('gross_weight_kg',),
    'net_mass_kg': ('net_weight_kg',),
    'ni_additional_information_codes': ('national_additional_codes',),
}

SDI_HEADER_PAYLOAD_FIELDS = tuple(dict.fromkeys((
    'op_type',
    'sup_dec_number',
    *SDI_UPDATE_FIELDS,
    'header_additions_deductions',
    *SDI_HEADER_NESTED_FIELDS.keys(),
)))

SDI_HEADER_PAYLOAD_REQUIREMENTS = {
    'op_type': 'Required',
    'sup_dec_number': 'Required',
    'declaration_choice': 'Required',
    'arrival_date_time': 'Required',
    'representation_type': 'Required',
    'controlled_goods': 'Required',
    'additional_procedure': 'Required',
    'goods_domestic_status': 'Required',
    'movement_type': 'Required',
    'destination_country': 'Required',
    'nationality_of_transport': 'Required',
    'identity_no_of_transport': 'Required',
    'postponed_vat': 'Required',
    'incoterm': 'Required',
    'authorisation_type': 'Required',
    'un_locode': 'Conditional',
    'delivery_location_country': 'Conditional',
    'delivery_location_town': 'Conditional',
    'vat_number': 'Conditional',
    'freight_charge': 'Conditional',
    'freight_charge_currency': 'Conditional',
    'exporter_eori': 'Conditional',
    'exporter_name': 'Conditional',
    'exporter_street_number': 'Conditional',
    'exporter_city': 'Conditional',
    'exporter_postcode': 'Conditional',
    'exporter_country': 'Conditional',
    'header_additions_deductions': 'Conditional',
    'header_previous_document': 'Conditional',
    'holder_of_authorisation': 'Conditional',
}


@lru_cache(maxsize=16)
def _table_columns(table_name):
    try:
        rows = query_all(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
            """,
            [S, table_name],
        )
        return {row['COLUMN_NAME'].lower() for row in rows}
    except Exception:
        logger.exception("Failed to inspect columns for %s.%s", S, table_name)
        return set()


def _first_existing_column(table_name, *candidates):
    columns = _table_columns(table_name)
    for candidate in candidates:
        if candidate and candidate.lower() in columns:
            return candidate
    return None


@lru_cache(maxsize=64)
def _schema_table_columns(schema_name, table_name):
    try:
        rows = query_all(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
            """,
            [schema_name, table_name],
        )
        return {row['COLUMN_NAME'].lower() for row in rows}
    except Exception:
        logger.exception("Failed to inspect columns for %s.%s", schema_name, table_name)
        return set()


def _schema_table_has_column(schema_name, table_name, column_name):
    return (column_name or '').lower() in _schema_table_columns(schema_name, table_name)


def _insert_existing_columns(cursor, table_name, values, identity_col=None):
    columns = _table_columns(table_name)
    insert_items = [
        (name, value)
        for name, value in values
        if name and name.lower() in columns
    ]
    if not insert_items:
        raise RuntimeError(f'No compatible columns found for {S}.{table_name}')

    column_sql = ', '.join(f'[{name}]' for name, _ in insert_items)
    placeholders = ', '.join('?' for _ in insert_items)
    output_sql = ''
    if identity_col and identity_col.lower() in columns:
        output_sql = f' OUTPUT INSERTED.[{identity_col}]'
    cursor.execute(
        f"INSERT INTO {S}.[{table_name}] ({column_sql}){output_sql} VALUES ({placeholders})",
        [value for _, value in insert_items],
    )
    if output_sql:
        row = cursor.fetchone()
        return row[0] if row else None
    return None


def _split_supdec_import_refs(raw):
    return [
        ref.strip().upper()
        for ref in re.split(r'[\s,;]+', raw or '')
        if ref.strip()
    ]


def _import_supdec_stub(cursor, ref, act_as=None):
    if not ref.startswith('SUP'):
        return {
            'ref': ref,
            'status': 'error',
            'msg': 'SDI import only accepts SUP references. Use ENS Import for ENS or DEC references.',
        }

    id_col = _supdec_header_id_column() or _first_existing_column('StagingSupDecHeaders', 'staging_id', 'id')
    existing = query_one(
        f"SELECT TOP 1 [{id_col}] AS staging_id FROM {S}.StagingSupDecHeaders WHERE sup_dec_number = ?",
        [ref],
    )
    if existing:
        return {'ref': ref, 'status': 'skipped', 'msg': 'This SUP already exists locally.'}

    _insert_existing_columns(
        cursor,
        'StagingSupDecHeaders',
        [
            ('label', f'Imported {ref}'),
            ('sup_dec_number', ref),
            ('status', 'IMPORTED'),
            ('tss_status', 'PENDING_SYNC'),
            ('act_as', (act_as or '').strip() or None),
            ('source', 'TSS_IMPORT'),
            ('created_by', 'tss_import'),
        ],
        identity_col=id_col,
    )
    return {'ref': ref, 'status': 'created', 'msg': 'SUP saved as an SDI. Refresh or sync from TSS to pull full data.'}


def _compat_select_expr(table_name, alias, target_name, *source_names, default='NULL'):
    source_name = _first_existing_column(table_name, *(source_names or (target_name,)))
    if source_name:
        expr = f"{alias}.{source_name}"
        if source_name.lower() != target_name.lower():
            expr += f" AS {target_name}"
        return expr
    return f"{default} AS {target_name}"


def _compat_star_projection(table_name, alias, alias_map):
    select_parts = [f"{alias}.*"]
    columns = _table_columns(table_name)
    for target_name, source_names in alias_map.items():
        if target_name.lower() in columns:
            continue
        source_name = _first_existing_column(table_name, *source_names)
        if source_name:
            select_parts.append(f"{alias}.{source_name} AS {target_name}")
    return ", ".join(select_parts)


def _supdec_header_id_column():
    return _first_existing_column('StagingSupDecHeaders', 'staging_id', 'id')


def _supdec_goods_parent_column():
    return _first_existing_column('StagingSupDecGoods', 'staging_supdec_id', 'supdec_header_id')


def _supdec_goods_item_column():
    return _first_existing_column('StagingSupDecGoods', 'item_number', 'goods_item_number')


def _supdec_goods_id_column():
    return _first_existing_column('StagingSupDecGoods', 'staging_id', 'id')


def _supdec_goods_remote_id_column():
    return _first_existing_column('StagingSupDecGoods', 'sup_goods_id', 'tss_goods_id_sdi', 'goods_id')


def _clean_ref(value):
    return str(value or '').strip().upper()


def _first_text(*values):
    for value in values:
        if value not in (None, ''):
            cleaned = str(value).strip()
            if cleaned:
                return cleaned
    return ''


def _sdi_reference_from_tss_item(item):
    item = item or {}
    return _first_text(
        item.get('sup_dec_number'),
        item.get('reference'),
        item.get('supplementary_declaration_number'),
        item.get('number'),
    )


def _sdi_status_from_tss_item(item, default=''):
    item = item or {}
    return _first_text(item.get('status'), item.get('state'), default)


def _sdi_goods_reference_from_tss_item(item):
    item = item or {}
    return _first_text(item.get('goods_id'), item.get('reference'), item.get('number'), item.get('sys_id'))


def _coerce_template_date(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y %H:%M:%S'):
        try:
            return datetime.strptime(raw.split('.')[0], fmt).date()
        except ValueError:
            continue
    return None


def _coerce_template_datetime(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.replace('T', ' ').replace('Z', '')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%d/%m/%Y %H:%M:%S', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(normalized.split('.')[0], fmt)
        except ValueError:
            continue
    return None


def _prepare_supdec_list_rows(rows):
    prepared = []
    for row in rows or []:
        item = dict(row)
        item['submission_due_date'] = _coerce_template_date(item.get('submission_due_date'))
        item['created_at'] = _coerce_template_datetime(item.get('created_at'))
        tss_status = normalize_status_key(item.get('tss_status'))
        item['display_status'] = effective_tss_filter_status(
            item.get('status'),
            item.get('tss_status'),
            pending_sync=bool(
                item.get('sup_dec_number')
                and (not tss_status or tss_status in {'PENDING SYNC', 'IMPORTED', 'SYNC PENDING'})
            ),
        )
        prepared.append(item)
    return prepared


def _supdec_sql_status_key(alias, column):
    return f"UPPER(REPLACE(COALESCE(CAST({alias}.[{column}] AS NVARCHAR(100)), ''), '_', ' '))"


def _supdec_sql_has_value(alias, column):
    return f"NULLIF(LTRIM(RTRIM(COALESCE(CAST({alias}.[{column}] AS NVARCHAR(100)), ''))), '') IS NOT NULL"


def _supdec_effective_status_expr():
    local_col = _first_existing_column('StagingSupDecHeaders', 'status') or 'status'
    remote_col = _first_existing_column('StagingSupDecHeaders', 'tss_status') or local_col
    ref_col = _first_existing_column('StagingSupDecHeaders', 'sup_dec_number', 'sfd_number')
    local = _supdec_sql_status_key('sd', local_col)
    remote = _supdec_sql_status_key('sd', remote_col)
    remote_has_value = _supdec_sql_has_value('sd', remote_col)
    if ref_col:
        pending_sync_when = (
            f"{_supdec_sql_has_value('sd', ref_col)} AND "
            f"(NOT ({remote_has_value}) OR {remote} IN ('PENDING SYNC', 'SYNC PENDING', 'IMPORTED'))"
        )
    else:
        pending_sync_when = f"{remote} IN ('PENDING SYNC', 'SYNC PENDING', 'IMPORTED')"
    return f"""
        CASE
            WHEN {local} IN ('IMPORTED', 'INGESTED') THEN {local}
            WHEN {pending_sync_when} THEN 'PENDING_SYNC'
            WHEN {remote_has_value} THEN {remote}
            WHEN {local} IN ('', 'CREATED', 'DRAFT', 'INSERTED', 'PENDING', 'PENDING REVIEW', 'VALIDATED') THEN 'DRAFT'
            WHEN {local} = 'CANCELED' THEN 'CANCELLED'
            WHEN {local} = 'PENDING SYNC' THEN 'PENDING_SYNC'
            WHEN {local} = 'VALIDATION ERROR' THEN 'VALIDATION_ERROR'
            WHEN {local} = 'SUBMIT ERROR' THEN 'SUBMIT_ERROR'
            ELSE {local}
        END
    """


def _load_sfd_link_for_supdec(sd, linked_cons=None):
    sfd_columns = _table_columns('Sfds')
    if not sfd_columns:
        return None

    reference_col = _first_existing_column('Sfds', 'sfd_reference', 'sfd_number', 'reference')
    cons_ref_col = _first_existing_column('Sfds', 'declaration_number', 'consignment_number', 'ens_consignment_reference')
    if not reference_col and not cons_ref_col:
        return None

    status_col = _first_existing_column('Sfds', 'tss_status', 'status')
    mrn_col = _first_existing_column('Sfds', 'movement_reference_number', 'mrn')
    eidr_col = _first_existing_column('Sfds', 'eori_for_eidr')
    updated_col = _first_existing_column('Sfds', 'updated_at', 'created_at', 'synced_at')
    raw_json_col = _first_existing_column('Sfds', 'raw_json')

    maybe_sfd_refs = {
        _clean_ref((sd or {}).get('sfd_reference')),
        _clean_ref((sd or {}).get('ens_consignment_ref')),
        _clean_ref((linked_cons or {}).get('sfd_reference')),
    } - {''}
    maybe_cons_refs = {
        _clean_ref((sd or {}).get('ens_consignment_ref')),
        _clean_ref((linked_cons or {}).get('dec_reference')),
    } - {''}

    where_parts = []
    params = []
    if reference_col and maybe_sfd_refs:
        placeholders = ', '.join('?' for _ in maybe_sfd_refs)
        where_parts.append(f"UPPER(s.[{reference_col}]) IN ({placeholders})")
        params.extend(sorted(maybe_sfd_refs))
    if cons_ref_col and maybe_cons_refs:
        placeholders = ', '.join('?' for _ in maybe_cons_refs)
        where_parts.append(f"UPPER(s.[{cons_ref_col}]) IN ({placeholders})")
        params.extend(sorted(maybe_cons_refs))
    if not where_parts:
        return None

    join_parts = []
    if cons_ref_col:
        join_parts.append(f"s.[{cons_ref_col}] = c.dec_reference")
    if reference_col:
        join_parts.append(f"c.sfd_reference IS NOT NULL AND c.sfd_reference = s.[{reference_col}]")
    join_on = ' OR '.join(f"({part})" for part in join_parts) or '1 = 0'
    order_by = f"s.[{updated_col}] DESC" if updated_col else (
        f"s.[{reference_col}] DESC" if reference_col else f"s.[{cons_ref_col}] DESC"
    )

    return query_one(
        f"""
        SELECT TOP 1
               {f"s.[{reference_col}] AS sfd_reference," if reference_col else "NULL AS sfd_reference,"}
               {f"s.[{cons_ref_col}] AS dec_reference," if cons_ref_col else "NULL AS dec_reference,"}
               {f"s.[{status_col}] AS tss_status," if status_col else "NULL AS tss_status,"}
               {f"s.[{mrn_col}] AS movement_reference_number," if mrn_col else "NULL AS movement_reference_number,"}
               {f"s.[{eidr_col}] AS eori_for_eidr," if eidr_col else "NULL AS eori_for_eidr,"}
               {f"s.[{updated_col}] AS updated_at," if updated_col else "NULL AS updated_at,"}
               {f"s.[{raw_json_col}] AS raw_json," if raw_json_col else "NULL AS raw_json,"}
               c.staging_id,
               c.dec_reference AS staging_dec_reference,
               c.sfd_reference AS staging_sfd_reference,
               e.ens_reference
        FROM {S}.Sfds s
        LEFT JOIN {S}.StagingConsignments c ON {join_on}
        LEFT JOIN {S}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
        WHERE {' OR '.join(where_parts)}
        ORDER BY {order_by}
        """,
        params,
    )


def _load_consignment_for_supdec(sd, linked_sfd=None):
    if (sd or {}).get('staging_cons_id'):
        linked_cons = query_one(f"""
            SELECT c.*, e.ens_reference
            FROM {S}.StagingConsignments c
            LEFT JOIN {S}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
            WHERE c.staging_id = ?
        """, [sd['staging_cons_id']])
        if linked_cons:
            return linked_cons

    for ref in (
        (linked_sfd or {}).get('staging_dec_reference'),
        (linked_sfd or {}).get('dec_reference'),
        (sd or {}).get('ens_consignment_ref'),
        (sd or {}).get('sfd_reference'),
    ):
        if not ref:
            continue
        linked_cons = query_one(f"""
            SELECT c.*, e.ens_reference
            FROM {S}.StagingConsignments c
            LEFT JOIN {S}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
            WHERE c.dec_reference = ? OR c.sfd_reference = ?
        """, [ref, ref])
        if linked_cons:
            return linked_cons

    return None


def _build_declaration_chain(sd, linked_cons=None, linked_sfd=None, linked_gmr=None):
    cons_ref = _first_text(
        (linked_cons or {}).get('dec_reference'),
        (linked_sfd or {}).get('staging_dec_reference'),
        (linked_sfd or {}).get('dec_reference'),
        (sd or {}).get('ens_consignment_ref'),
    )
    sfd_ref = _first_text(
        (linked_sfd or {}).get('sfd_reference'),
        (linked_sfd or {}).get('staging_sfd_reference'),
        (linked_cons or {}).get('sfd_reference'),
        (sd or {}).get('sfd_reference'),
    )
    if cons_ref and sfd_ref and _clean_ref(cons_ref) == _clean_ref(sfd_ref):
        cons_ref = _first_text((linked_sfd or {}).get('dec_reference'), (linked_cons or {}).get('dec_reference'))

    return {
        'ens_reference': _first_text((sd or {}).get('ens_header_ref'), (linked_cons or {}).get('ens_reference'), (linked_sfd or {}).get('ens_reference')),
        'consignment_reference': cons_ref,
        'sfd_reference': sfd_ref,
        'sfd_customs_reference': _first_text((linked_sfd or {}).get('movement_reference_number'), (linked_sfd or {}).get('eori_for_eidr')),
        'sfd_customs_label': 'EIDR' if (linked_sfd or {}).get('eori_for_eidr') and not (linked_sfd or {}).get('movement_reference_number') else 'MRN',
        'sdi_reference': _first_text((sd or {}).get('sup_dec_number'), 'PENDING'),
        'sdi_status': _first_text((sd or {}).get('status'), 'PENDING'),
        'movement_reference': _first_text((linked_gmr or {}).get('gmr_id'), f"GMR #{(linked_gmr or {}).get('staging_id')}" if (linked_gmr or {}).get('staging_id') else ''),
    }


def _consignment_sdi_block_reason(cons):
    if not cons:
        return 'No linked consignment was found for this SFD.'
    sfd_ref = _first_text(
        (cons or {}).get('sfd_number'),
        (cons or {}).get('sfd_reference'),
        (cons or {}).get('synced_sfd_reference'),
    )
    if (cons.get('no_sfd_reason') or '').strip() and not sfd_ref:
        return 'This consignment is marked as not requiring SFD/SDI.'
    if not consignment_should_discover_sdi(cons):
        return 'No synced SFD is available yet for SDI discovery.'
    return ''


def _load_sfd_parent_record(sfd_id):
    return query_one(f"""
        SELECT TOP 1
               s.id AS sfd_id,
               COALESCE(NULLIF(s.sfd_number, ''), NULLIF(s.sfd_reference, '')) AS sfd_number,
               COALESCE(NULLIF(s.ens_consignment_reference, ''), NULLIF(s.declaration_number, '')) AS consignment_number,
               s.tss_status AS sfd_tss_status,
               s.movement_reference_number AS sfd_mrn,
               c.staging_id,
               c.staging_ens_id,
               c.dec_reference,
               c.goods_description,
               c.transport_document_number,
               c.controlled_goods,
               c.goods_domestic_status,
               c.importer_eori,
               c.importer_name,
               c.exporter_eori,
               c.exporter_name,
               c.no_sfd_reason,
               c.generate_SD,
               c.tss_status AS cons_tss_status,
               e.ens_reference
        FROM {S}.Sfds s
        LEFT JOIN {S}.StagingConsignments c
          ON c.dec_reference = s.ens_consignment_reference
          OR c.dec_reference = s.declaration_number
          OR c.sfd_reference = s.sfd_number
          OR c.sfd_reference = s.sfd_reference
        LEFT JOIN {S}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
        WHERE s.id = ?
    """, [sfd_id])


def _sfd_parent_option(row):
    sfd_number = row.get('sfd_number') or ''
    cons_number = row.get('dec_reference') or row.get('consignment_number') or ''
    description = (row.get('goods_description') or '')[:40]
    return {
        'id': str(row['sfd_id']),
        'sfd_number': sfd_number,
        'cons_number': cons_number,
        'display': f"{sfd_number} - {cons_number} - {description} ({row.get('cons_tss_status') or row.get('sfd_tss_status') or 'SFD'})",
        'staging_cons_id': row.get('staging_id'),
    }


def _build_supdec_prepop_from_sfd_parent(parent, default_act_as=''):
    if not parent:
        return {'act_as': default_act_as}
    return {
        'sfd_parent': str(parent['sfd_id']),
        'sfd_reference': parent.get('sfd_number', ''),
        'ens_consignment_ref': parent.get('dec_reference') or parent.get('consignment_number') or '',
        'ens_header_ref': parent.get('ens_reference') or '',
        'goods_description': parent.get('goods_description', ''),
        'transport_document_number': parent.get('transport_document_number', ''),
        'controlled_goods': parent.get('controlled_goods', ''),
        'goods_domestic_status': parent.get('goods_domestic_status', ''),
        'importer_eori': parent.get('importer_eori', ''),
        'importer_name': parent.get('importer_name', ''),
        'exporter_eori': parent.get('exporter_eori', ''),
        'exporter_name': parent.get('exporter_name', ''),
        'act_as': default_act_as,
    }


def _normalize_tss_status(value):
    text = (value or '').strip()
    if not text:
        return None
    mapping = {
        'draft': 'PENDING',
        'submitted': 'SUBMITTED',
        'accepted': 'ACCEPTED',
        'cleared': 'CLEARED',
        'cancelled': 'CANCELLED',
        'canceled': 'CANCELLED',
        'rejected': 'REJECTED',
        'trader input required': 'TRADER_INPUT_REQUIRED',
        'pending payment': 'PENDING_PAYMENT',
    }
    return mapping.get(text.lower(), text.upper().replace(' ', '_'))


LOCAL_NOT_YET_SYNCED = 'PENDING_SYNC'
SUPDEC_LOCAL_DRAFT_STATUSES = {'PENDING', 'VALIDATED', 'FAILED'}
SUPDEC_REMOTE_DRAFT_STATUSES = {'PENDING'}
SUPDEC_REMOTE_TERMINAL_STATUSES = {
    'ACCEPTED',
    'CANCELLED',
    'CANCELED',
    'CLEARED',
    'CLOSED',
    'COMPLETED',
    'SUBMITTED',
}
SUPDEC_SUBMITTABLE_STATUSES = {
    'PENDING', 'FAILED', 'REJECTED', 'TRADER_INPUT_REQUIRED', 'VALIDATED', 'IMPORTED',
}


def _supdec_effective_tss_status(sd):
    """Prefer live TSS status; fall back to local status when TSS not yet synced."""
    if not sd:
        return None
    tss_status = _normalize_tss_status(sd.get('tss_status'))
    if tss_status and tss_status != LOCAL_NOT_YET_SYNCED:
        return tss_status
    return _normalize_tss_status(sd.get('status'))


def _supdec_can_cancel_in_tss(sd):
    if not sd or not sd.get('sup_dec_number'):
        return False
    tss_status = _normalize_tss_status(sd.get('tss_status'))
    if tss_status and tss_status != LOCAL_NOT_YET_SYNCED:
        return tss_status not in SUPDEC_REMOTE_TERMINAL_STATUSES
    return _normalize_tss_status(sd.get('status')) not in SUPDEC_REMOTE_TERMINAL_STATUSES


def _supdec_can_recall_in_tss(sd):
    if not sd or not sd.get('sup_dec_number'):
        return False
    return _supdec_effective_tss_status(sd) == 'PENDING_PAYMENT'


def _supdec_can_submit_in_tss(sd):
    if not sd or not sd.get('sup_dec_number'):
        return False
    return _normalize_tss_status(sd.get('status')) in SUPDEC_SUBMITTABLE_STATUSES


def _supdec_can_create_in_tss(sd):
    """SDI can be originated in TSS when it has no remote reference yet and is in a local draft state."""
    if not sd:
        return False
    if (sd.get('sup_dec_number') or '').strip():
        return False
    return _normalize_tss_status(sd.get('status')) in SUPDEC_LOCAL_DRAFT_STATUSES


def _redirect_to_detail(sd, sid):
    return redirect(_supdec_detail_target(sd or {'staging_id': sid}))


def _load_supdec_header_record(sid):
    header_id_column = _supdec_header_id_column()
    if not header_id_column:
        return None
    return query_one(
        f"""
        SELECT {_compat_star_projection('StagingSupDecHeaders', 'sd', SUPDEC_HEADER_ALIASES)}
        FROM {S}.StagingSupDecHeaders sd
        WHERE sd.{header_id_column} = ?
        """,
        [sid],
    )


def _load_supdec_goods_records(sid):
    goods_parent_column = _supdec_goods_parent_column()
    if not goods_parent_column:
        return []

    order_columns = []
    goods_item_column = _supdec_goods_item_column()
    goods_id_column = _supdec_goods_id_column()
    if goods_item_column:
        order_columns.append(f"g.{goods_item_column}")
    if goods_id_column:
        order_columns.append(f"g.{goods_id_column}")
    order_clause = f" ORDER BY {', '.join(order_columns)}" if order_columns else ""

    return query_all(
        f"""
        SELECT {_compat_star_projection('StagingSupDecGoods', 'g', SUPDEC_GOODS_ALIASES)}
        FROM {S}.StagingSupDecGoods g
        WHERE g.{goods_parent_column} = ?
        {order_clause}
        """,
        [sid],
    )


def _load_source_goods_records(sd):
    staging_cons_id = sd.get('staging_cons_id')
    if not staging_cons_id:
        return []
    try:
        return query_all(
            f"""
            SELECT *
            FROM {S}.StagingGoodsItems
            WHERE staging_cons_id = ?
            ORDER BY staging_id
            """,
            [staging_cons_id],
        )
    except Exception:
        logger.exception("Failed to load source goods for SDI %s", sd.get('staging_id'))
        return []


def _tss_client():
    return build_cfg_client()


def _explicit_supdec_act_as(sd=None):
    explicit = ((sd or {}).get('act_as') or '').strip()
    if explicit:
        return explicit
    return None


def _default_supdec_act_as():
    return (getattr(_tss_client(), 'default_act_as', '') or '').strip() or None


def _current_supdec_act_as(sd=None):
    return _explicit_supdec_act_as(sd) or _default_supdec_act_as()


def _tss_requires_customs_agent_role(error):
    return 'customs agent role required' in str(error or '').casefold()


def _customs_agent_role_guidance(sd=None):
    effective = _current_supdec_act_as(sd)
    if effective:
        return (
            'TSS rejected the request for the current Act As value. '
            f'Current Act As: {effective}. Open Edit, choose the customer relationship that owns this SUP, then refresh again.'
        )
    return (
        'TSS requires a Customs Agent relationship for this SUP. '
        'Open Edit and set Act As, or configure Admin Settings > TSS API > ACT_AS as the tenant default, then refresh again.'
    )


def _sync_failure_message(exc, sd=None):
    if _tss_requires_customs_agent_role(exc):
        return _customs_agent_role_guidance(sd)
    return clean_tss_message(str(exc)) or str(exc)


def _persist_supdec_sync_failure(sid, exc, sd=None):
    message = _sync_failure_message(exc, sd)
    updates = {
        'error_message': message[:2000],
    }
    current_tss_status = (sd or {}).get('tss_status')
    if not current_tss_status or current_tss_status == 'PENDING_SYNC':
        updates['tss_status'] = 'PENDING_SYNC'
    _update_supdec_row(sid, updates)
    return message


def _try_autofill_single_supdec_act_as(sid, sd=None):
    if _current_supdec_act_as(sd):
        return None
    relationships = load_agent_relationships()
    if len(relationships) != 1:
        return None
    act_as = relationships[0].get('value')
    if not act_as:
        return None
    _update_supdec_row(sid, {'act_as': act_as})
    return act_as


def load_agent_relationships():
    relationships = []
    try:
        result = _tss_client().get_agent_relationships()
        items = TssApiClient.as_items(result.get('response'))
        for item in items:
            sys_id = str(item.get('customer_account_sys_id') or '').strip()
            if not sys_id:
                continue
            customer_account = str(item.get('customer_account') or '').strip()
            agent_account = str(item.get('agent_account') or '').strip()
            label = customer_account or sys_id
            if agent_account:
                label = f"{label} - via {agent_account}"
            relationships.append({
                'value': sys_id,
                'label': label,
                'customer_account': customer_account,
                'agent_account': agent_account,
            })
        relationships.sort(key=lambda rel: (rel['customer_account'] or rel['label'], rel['value']))
    except Exception:
        logger.exception("Failed to load TSS agent relationships")

    if not relationships:
        # Fallback: demo/offline relationships seeded in AppConfiguration.
        # Allows actAs dropdown to work without a live TSS connection.
        try:
            import json
            from app.config_store import get as cfg_get
            raw = cfg_get('DEMO', 'demo_agent_relationships', fallback='')
            if raw:
                relationships = json.loads(raw)
        except Exception:
            pass

    return relationships


def _update_supdec_row(sid, updates):
    if not updates:
        return
    header_id_column = _supdec_header_id_column()
    if not header_id_column:
        raise RuntimeError('Missing SDI header identifier column')

    assignments = []
    values = []
    for column_name, value in updates.items():
        if _first_existing_column('StagingSupDecHeaders', column_name):
            assignments.append(f"{column_name}=?")
            values.append(value)

    if not assignments:
        return

    if _first_existing_column('StagingSupDecHeaders', 'updated_at'):
        assignments.append('updated_at=SYSUTCDATETIME()')
    execute(
        f"UPDATE {S}.StagingSupDecHeaders SET {', '.join(assignments)} WHERE {header_id_column}=?",
        values + [sid],
    )


def _sync_supdec_from_tss(sid, sup_ref, read_result=None, act_as=None):
    result = read_result or _tss_client().read_sdi(
        sup_ref,
        fields=SUPDEC_TSS_READ_FIELDS,
        act_as=act_as,
    )
    if not result.get('success'):
        raise RuntimeError(result.get('message') or 'TSS SDI read failed')

    data = result.get('response') or {}
    normalized_status = _normalize_tss_status(
        _sdi_status_from_tss_item(data, default=result.get('status') or '')
    )
    updates = {
        'sup_dec_number': sup_ref,
        'tss_status': _sdi_status_from_tss_item(data, default=result.get('status') or ''),
        'status': normalized_status,
        'sfd_reference': _first_text(data.get('sfd_reference'), data.get('sfd_number'), data.get('parent'), data.get('u_parent')) or None,
        'movement_reference_number': data.get('movement_reference_number') or data.get('mrn') or None,
        'error_message': data.get('error_message') or data.get('error_details') or None,
        'clear_date_time': data.get('clear_date_time') or None,
    }
    _update_supdec_row(sid, updates)
    return data


def _try_refresh_supdec_with_relationships(sid, sd, sup_ref, current_act_as=None):
    """Find the TSS customer relationship that can read this SUP, then sync it.

    TSS exposes the authoritative relationship list via agent_relationships.
    On an auth-context failure we can safely probe read-only SDI reads against
    those relationships and persist the one that TSS accepts for this SUP.
    """
    relationships = load_agent_relationships()
    if not relationships:
        return None

    current = (current_act_as or '').strip()
    client = _tss_client()
    for rel in relationships:
        act_as = (rel.get('value') or '').strip()
        if not act_as or act_as == current:
            continue

        result = client.read_sdi(
            sup_ref,
            fields=SUPDEC_TSS_READ_FIELDS,
            act_as=act_as,
        )
        if not result.get('success'):
            continue

        _update_supdec_row(sid, {'act_as': act_as})
        sd_with_act_as = dict(sd or {})
        sd_with_act_as['act_as'] = act_as
        _sync_supdec_from_tss(sid, sup_ref, read_result=result, act_as=act_as)
        goods_synced = _sync_supdec_goods_from_tss(sid, sd=sd_with_act_as, act_as=act_as)
        return {
            'act_as': act_as,
            'label': rel.get('label') or rel.get('customer_account') or act_as,
            'goods_synced': goods_synced,
        }

    return None


def _update_supdec_goods_row(gid, updates):
    if not updates:
        return
    goods_id_column = _supdec_goods_id_column()
    if not goods_id_column:
        raise RuntimeError('Missing SDI goods identifier column')

    assignments = []
    values = []
    for column_name, value in updates.items():
        if _first_existing_column('StagingSupDecGoods', column_name):
            assignments.append(f"{column_name}=?")
            values.append(value)

    if not assignments:
        return

    if _first_existing_column('StagingSupDecGoods', 'updated_at'):
        assignments.append('updated_at=SYSUTCDATETIME()')
    execute(
        f"UPDATE {S}.StagingSupDecGoods SET {', '.join(assignments)} WHERE {goods_id_column}=?",
        values + [gid],
    )


def _insert_supdec_goods_row(sid, item_number, remote_goods_id, source_goods=None, tss_goods=None):
    goods_parent_column = _supdec_goods_parent_column()
    if not goods_parent_column:
        raise RuntimeError('Missing SDI goods linkage column')

    source_goods = source_goods or {}
    tss_goods = tss_goods or {}

    insert_values = [
        (goods_parent_column, sid),
        (_supdec_goods_item_column(), item_number),
        (_supdec_goods_remote_id_column(), remote_goods_id),
        ('label', source_goods.get('label') or f'SDI item {item_number}'),
        ('status', _normalize_tss_status(_sdi_status_from_tss_item(tss_goods)) or 'PENDING'),
        ('goods_description', source_goods.get('goods_description') or tss_goods.get('goods_description') or None),
        (_first_existing_column('StagingSupDecGoods', 'type_of_packages', 'type_of_package'),
         source_goods.get('type_of_packages') or source_goods.get('type_of_package') or tss_goods.get('type_of_packages') or None),
        ('number_of_packages', source_goods.get('number_of_packages') or tss_goods.get('number_of_packages') or None),
        ('package_marks', source_goods.get('package_marks') or tss_goods.get('package_marks') or None),
        (_first_existing_column('StagingSupDecGoods', 'gross_mass_kg', 'gross_weight_kg'),
         source_goods.get('gross_mass_kg') or source_goods.get('gross_weight_kg') or tss_goods.get('gross_mass_kg') or None),
        (_first_existing_column('StagingSupDecGoods', 'net_mass_kg', 'net_weight_kg'),
         source_goods.get('net_mass_kg') or source_goods.get('net_weight_kg') or tss_goods.get('net_mass_kg') or None),
        ('commodity_code', source_goods.get('commodity_code') or tss_goods.get('commodity_code') or None),
        ('procedure_code', source_goods.get('procedure_code') or tss_goods.get('procedure_code') or None),
        ('additional_procedure_code', _first_text(
            source_goods.get('additional_procedure_code'),
            tss_goods.get('additional_procedure_code'),
            tss_goods.get('additional_procedure_codes'),
            '000',
        )),
        ('country_of_origin', source_goods.get('country_of_origin') or tss_goods.get('country_of_origin') or None),
    ]

    columns = []
    values = []
    for column_name, value in insert_values:
        if column_name and _first_existing_column('StagingSupDecGoods', column_name):
            columns.append(column_name)
            values.append(value)

    execute(
        f"INSERT INTO {S}.StagingSupDecGoods ({', '.join(columns)}, created_at) "
        f"VALUES ({', '.join(['?'] * len(values))}, SYSUTCDATETIME())",
        values,
    )


def _sync_supdec_goods_from_tss(sid, sd=None, goods_items=None, act_as=None):
    sd = sd or _load_supdec_header_record(sid)
    if not sd:
        raise RuntimeError('SDI not found')

    sup_ref = (sd.get('sup_dec_number') or '').strip()
    if not sup_ref:
        return 0

    remote_id_column = _supdec_goods_remote_id_column()
    goods_item_column = _supdec_goods_item_column()
    if not remote_id_column or not goods_item_column:
        return 0

    client = _tss_client()
    goods_items = goods_items or client.lookup_sdi_goods(sup_ref, act_as=act_as)
    existing_rows = _load_supdec_goods_records(sid)
    source_rows = _load_source_goods_records(sd)
    existing_by_remote_id = {
        row.get('sup_goods_id'): row
        for row in existing_rows
        if row.get('sup_goods_id')
    }
    synced = 0

    for index, item in enumerate(goods_items, start=1):
        remote_goods_id = _sdi_goods_reference_from_tss_item(item)
        if not remote_goods_id:
            continue

        updates = {
            remote_id_column: remote_goods_id,
            goods_item_column: index,
            'status': _normalize_tss_status(_sdi_status_from_tss_item(item)) or 'PENDING',
        }

        existing = existing_by_remote_id.get(remote_goods_id)
        if not existing and index - 1 < len(existing_rows):
            existing = existing_rows[index - 1]

        if existing:
            _update_supdec_goods_row(existing['staging_id'], updates)
        else:
            source_goods = source_rows[index - 1] if index - 1 < len(source_rows) else {}
            _insert_supdec_goods_row(sid, index, remote_goods_id, source_goods=source_goods, tss_goods=item)
        synced += 1

    return synced

def get_cv(table, val_col='value', name_col='name'):
    def sort_key(option):
        if table == 'CV_preference':
            value = str(option.value or '').strip()
            try:
                return (0, int(value), value)
            except ValueError:
                return (1, value, option.name or value)
        if table == 'CV_currency':
            value = str(option.value or '').strip()
            return (value, option.name or value)
        return (option.name or option.value, option.value)

    try:
        rows = query_all(f"SELECT [{val_col}], [{name_col}] FROM TSS.[{table}] ORDER BY [{name_col}]")
        options = [CVOption(r[val_col], r[name_col]) for r in rows]
        if options:
            options.sort(key=sort_key)
            return options
    except Exception:
        pass

    api_field = {
        'CV_addition_deduction_code': 'addition_deduction_code',
        'CV_additional_info_code': 'additional_info_code',
        'CV_additional_procedure_code': 'additional_procedure_code',
        'CV_auth_type_code': 'auth_type_code',
        'CV_controlled_goods_type': 'controlled_goods_type',
        'CV_country': 'country',
        'CV_currency': 'currency',
        'CV_document_code': 'document_code',
        'CV_document_status': 'document_status',
        'CV_goods_domestic_status': 'goods_domestic_status',
        'CV_incoterm': 'incoterm',
        'CV_movement_type': 'movement_type',
        'CV_national_additional_code': 'national_additional_code',
        'CV_nature_of_transaction': 'nature_of_transaction',
        'CV_ni_additional_information_code': 'ni_additional_information_code',
        'CV_preference': 'preference',
        'CV_previous_document_class': 'previous_document_class',
        'CV_previous_document_type': 'previous_document_type',
        'CV_procedure_code': 'procedure_code',
        'CV_representation_type': 'representation_type',
        'CV_sd_declaration_choice': 'sd_declaration_choice',
        'CV_sd_location_of_goods': 'sd_location_of_goods',
        'CV_standalone_sdi_authorisation_type': 'standalone_sdi_authorisation_type',
        'CV_supervising_customs_office': 'supervising_customs_office',
        'CV_type_of_package': 'type_of_package',
        'CV_un_locode': 'un_locode',
        'CV_valuation_indicator': 'valuation_indicator',
        'CV_valuation_method': 'valuation_method',
    }.get(table)
    if api_field:
        try:
            client = _tss_client()
            result = client.get_choice_values(api_field)
            items = TssApiClient.as_items(result.get('response'))
            options = []
            for item in items:
                value = str(item.get(val_col) or item.get('value') or item.get('code') or '').strip()
                name = str(item.get(name_col) or item.get('name') or item.get('description') or value).strip()
                if value:
                    options.append(CVOption(value, name))
            if options:
                options.sort(key=sort_key)
                return options
        except Exception:
            logger.exception("Failed to load choice values for %s via API fallback", table)
    return []

def load_supdec_choices():
    return {
        'declaration_choice': get_cv('CV_sd_declaration_choice'),
        'authorisation_types': get_cv('CV_standalone_sdi_authorisation_type'),
        'representation_types': get_cv('CV_representation_type'),
        'goods_domestic_status': get_cv('CV_goods_domestic_status'),
        'movement_types': get_cv('CV_movement_type'),
        'incoterms': get_cv('CV_incoterm'),
        'countries': get_cv('CV_country'),
        'currencies': get_cv('CV_currency'),
        'sd_location_of_goods': get_cv('CV_sd_location_of_goods'),
        'supervising_offices': get_cv('CV_supervising_customs_office'),
        'un_locodes': get_cv('CV_un_locode'),
        'add_ded_codes': get_cv('CV_addition_deduction_code'),
        'auth_type_codes': get_cv('CV_auth_type_code'),
        'prev_doc_types': get_cv('CV_previous_document_type'),
    }

def load_supdec_goods_choices():
    cached = getattr(g, '_supdec_goods_choices', None)
    if cached is not None:
        return cached
    choices = {
        'valuation_methods': get_cv('CV_valuation_method'),
        'valuation_indicators': get_cv('CV_valuation_indicator'),
        'nature_of_transaction': get_cv('CV_nature_of_transaction'),
        'preferences': get_cv('CV_preference'),
        'ni_addl_info': get_cv('CV_ni_additional_information_code'),
        'national_additional_codes': get_cv('CV_national_additional_code'),
        'procedure_codes': get_cv('CV_procedure_code'),
        'addl_procedure_codes': get_cv('CV_additional_procedure_code'),
        'controlled_goods_type': get_cv('CV_controlled_goods_type'),
        'countries': get_cv('CV_country'),
        'currencies': get_cv('CV_currency'),
        'type_of_packages': get_cv('CV_type_of_package'),
        'document_codes': get_cv('CV_document_code'),
        'document_statuses': get_cv('CV_document_status'),
    }
    g._supdec_goods_choices = choices
    return choices


def _load_source_goods_record(staging_id):
    if not staging_id:
        return None
    try:
        return query_one(
            f"SELECT * FROM {S}.StagingGoodsItems WHERE staging_id = ?",
            [staging_id],
        )
    except Exception:
        logger.exception("Failed to load source goods row %s", staging_id)
        return None


def _count_source_goods(sd):
    """Count goods rows on the linked source consignment, 0 if unavailable."""
    staging_cons_id = (sd or {}).get('staging_cons_id')
    if not staging_cons_id:
        return 0
    try:
        row = query_one(
            f"SELECT COUNT(*) AS n FROM {S}.StagingGoodsItems WHERE staging_cons_id = ?",
            [staging_cons_id],
        )
        return int((row or {}).get('n', 0) or 0)
    except Exception:
        logger.exception("Failed to count source goods for SDI %s", sd.get('staging_id'))
        return 0


def _load_source_goods_options(sd):
    """Return source consignment goods as picker options for SDI goods prefill."""
    if not sd:
        return []
    staging_cons_id = sd.get('staging_cons_id')
    if not staging_cons_id:
        return []
    rows = query_all(
        f"""
        SELECT staging_id, label, goods_description, commodity_code,
               number_of_packages, gross_mass_kg, net_mass_kg
        FROM {S}.StagingGoodsItems
        WHERE staging_cons_id = ?
        ORDER BY staging_id
        """,
        [staging_cons_id],
    )
    options = []
    for idx, r in enumerate(rows, start=1):
        desc = (r.get('goods_description') or r.get('label') or '').strip() or f'Item {idx}'
        options.append({
            'id': r['staging_id'],
            'label': f"#{idx} - {desc[:60]}",
        })
    return options


def _supdec_goods_form_from_source(sd, source_row):
    """Build a prefill form dict from a source goods row, applying SDI header defaults."""
    sd = sd or {}
    src = source_row or {}
    invoice_currency = (
        src.get('item_invoice_currency')
        or sd.get('total_invoice_currency')
        or ''
    )
    invoice_amount = src.get('item_invoice_amount') or src.get('unit_value') or ''
    return {
        'label': src.get('label') or '',
        'goods_description': src.get('goods_description') or '',
        'type_of_packages': src.get('type_of_packages') or src.get('type_of_package') or '',
        'number_of_packages': src.get('number_of_packages') or 1,
        'package_marks': src.get('package_marks') or '',
        'gross_mass_kg': src.get('gross_mass_kg') or src.get('gross_weight_kg') or '',
        'net_mass_kg': src.get('net_mass_kg') or src.get('net_weight_kg') or '',
        'commodity_code': src.get('commodity_code') or '',
        'procedure_code': src.get('procedure_code') or '',
        'additional_procedure_code': src.get('additional_procedure_code') or '',
        'country_of_origin': src.get('country_of_origin') or '',
        'item_invoice_amount': invoice_amount,
        'item_invoice_currency': invoice_currency,
        'valuation_method': src.get('valuation_method') or '',
        'valuation_indicator': src.get('valuation_indicator') or '',
        'invoice_number': src.get('invoice_number') or '',
        'nature_of_transaction': src.get('nature_of_transaction') or '',
        'preference': src.get('preference') or '',
        'ni_additional_information_codes': (
            src.get('ni_additional_information_codes') or src.get('national_additional_codes') or ''
        ),
        'country_of_preferential_origin': src.get('country_of_preferential_origin') or '',
        'statistical_value': src.get('statistical_value') or invoice_amount,
    }


def _supdec_goods_form_defaults(sd):
    """Return a blank prefill dict seeded with sensible SDI header defaults."""
    sd = sd or {}
    return {
        'item_invoice_currency': sd.get('total_invoice_currency') or '',
    }


TRADER_DEFAULT_FIELDS = (
    'valuation_method',
    'valuation_indicator',
    'nature_of_transaction',
    'preference',
    'ni_additional_information_codes',
    'country_of_preferential_origin',
    'item_invoice_currency',
)


def _trader_defaults_table_exists():
    return bool(_table_columns('SupDecTraderDefaults'))


def _load_trader_defaults(importer_eori):
    """Return persisted trader-only defaults for an importer EORI, or {} if none."""
    eori = (importer_eori or '').strip()
    if not eori or not _trader_defaults_table_exists():
        return {}
    try:
        row = query_one(
            f"SELECT * FROM {S}.SupDecTraderDefaults WHERE importer_eori = ?",
            [eori],
        )
    except Exception:
        logger.exception("Failed to load trader defaults for %s", eori)
        return {}
    if not row:
        return {}
    return {field: row.get(field) for field in TRADER_DEFAULT_FIELDS if row.get(field)}


def _upsert_trader_defaults(importer_eori, values):
    """Persist non-empty trader-only fields for an importer EORI."""
    eori = (importer_eori or '').strip()
    if not eori or not _trader_defaults_table_exists():
        return
    columns = _table_columns('SupDecTraderDefaults')
    payload = {
        field: (str(values.get(field) or '').strip())
        for field in TRADER_DEFAULT_FIELDS
        if field in columns
    }
    payload = {k: v for k, v in payload.items() if v}
    if not payload:
        return
    try:
        existing = query_one(
            f"SELECT staging_id FROM {S}.SupDecTraderDefaults WHERE importer_eori = ?",
            [eori],
        )
        if existing:
            set_parts = [f"[{k}] = ?" for k in payload]
            if 'updated_at' in columns:
                set_parts.append('[updated_at] = SYSUTCDATETIME()')
            if 'last_seen_at' in columns:
                set_parts.append('[last_seen_at] = SYSUTCDATETIME()')
            execute(
                f"UPDATE {S}.SupDecTraderDefaults SET {', '.join(set_parts)} WHERE staging_id = ?",
                list(payload.values()) + [existing['staging_id']],
            )
        else:
            cols = ['importer_eori'] + list(payload)
            placeholders = ['?'] * len(cols)
            execute(
                f"INSERT INTO {S}.SupDecTraderDefaults ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
                [eori] + list(payload.values()),
            )
    except Exception:
        logger.exception("Failed to upsert trader defaults for %s", eori)


def _apply_trader_defaults(form, importer_eori):
    """Overlay persisted trader-only defaults onto blank form fields."""
    defaults = _load_trader_defaults(importer_eori)
    if not defaults:
        return form
    merged = dict(form or {})
    for field, value in defaults.items():
        if not merged.get(field):
            merged[field] = value
    return merged


def _supdec_lookup_product(query_text):
    """Lookup a product in DocProductCatalog by SKU, barcode, product_code or description."""
    text = (query_text or '').strip()
    if not text:
        return None
    columns = _table_columns('DocProductCatalog')
    if not columns:
        return None
    searchable = [c for c in ('sku', 'barcode', 'product_code') if c in columns]
    if not searchable:
        return None
    where = " OR ".join(f"[{c}] = ?" for c in searchable)
    params = [text] * len(searchable)
    if 'active' in columns:
        where = f"({where}) AND active = 1"
    select_cols = [
        c for c in (
            'sku', 'barcode', 'product_code', 'description',
            'commodity_code', 'country_of_origin', 'unit_price', 'currency',
            'procedure_code', 'additional_procedure_code', 'valuation_method',
            'valuation_indicator', 'preference_code',
            'ni_additional_information_codes', 'ni_additional_info_code',
            'nature_of_transaction', 'country_of_preferential_origin',
            'taric_code', 'cus_code', 'national_additional_code',
            'quota_order_number', 'controlled_goods', 'controlled_goods_type',
        ) if c in columns
    ]
    if not select_cols:
        return None
    sql = f"SELECT TOP 1 {', '.join(f'[{c}]' for c in select_cols)} FROM {S}.DocProductCatalog WHERE {where}"
    try:
        return query_one(sql, params)
    except Exception:
        logger.exception("Product lookup failed for %s", text)
        return None

def get_sfd_parents():
    """Get SFDs that can spawn SDIs — from synced Sfds table + consignments."""
    results = []
    try:
        rows = query_all(f"""
            SELECT s.id AS sfd_id, s.sfd_reference, s.declaration_number AS consignment_number,
                   s.tss_status, c.goods_description, c.importer_eori
            FROM {S}.Sfds s
            LEFT JOIN {S}.EnsConsignments c ON c.declaration_number = s.declaration_number
            ORDER BY s.id DESC""")
        for r in rows:
            results.append({
                'id': str(r['sfd_id']),
                'sfd_number': r.get('sfd_reference', ''),
                'cons_number': r.get('consignment_number', ''),
                'display': f"{r.get('sfd_reference','')} — {r.get('goods_description','')[:40]} ({r.get('tss_status','')})",
            })
    except: pass

    # Also from staging consignments that are CREATED (no SFD yet but reference captured)
    try:
        rows = query_all(f"""
            SELECT staging_id, dec_reference, goods_description, importer_eori, tss_status
            FROM {S}.StagingConsignments
            WHERE status = 'CREATED' AND dec_reference IS NOT NULL
            ORDER BY staging_id DESC""")
        for r in rows:
            if not any(r['dec_reference'] in x.get('cons_number','') for x in results):
                results.append({
                    'id': f"cons:{r['staging_id']}",
                    'sfd_number': '',
                    'cons_number': r['dec_reference'],
                    'display': f"[Consignment] {r['dec_reference']} — {r.get('goods_description','')[:40]}",
                })
    except: pass
    return results


def _load_supdec_source_consignment(staging_id):
    return query_one(f"""
        SELECT c.*, e.ens_reference
        FROM {S}.StagingConsignments c
        LEFT JOIN {S}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
        WHERE c.staging_id = ?""", [staging_id])


def _build_supdec_prepop_from_consignment(cons, default_act_as=''):
    if not cons:
        return {'act_as': default_act_as}
    return {
        'sfd_parent': f"cons:{cons['staging_id']}",
        'goods_description': cons.get('goods_description', ''),
        'transport_document_number': cons.get('transport_document_number', ''),
        'controlled_goods': cons.get('controlled_goods', ''),
        'importer_eori': cons.get('importer_eori', ''),
        'importer_name': cons.get('importer_name', ''),
        'exporter_eori': cons.get('exporter_eori', ''),
        'exporter_name': cons.get('exporter_name', ''),
        'ens_consignment_ref': cons.get('dec_reference', ''),
        'ens_header_ref': cons.get('ens_reference', ''),
        'act_as': default_act_as,
    }


def get_sfd_parents_for_ens(ens_id):
    results = []
    try:
        rows = query_all(f"""
            SELECT c.staging_id,
                   c.dec_reference,
                   c.sfd_reference,
                   c.goods_description,
                   c.importer_eori,
                   c.tss_status,
                   s.id AS sfd_id,
                   s.sfd_reference AS synced_sfd_reference
            FROM {S}.StagingConsignments c
            LEFT JOIN {S}.Sfds s
              ON s.declaration_number = c.dec_reference
              OR (c.sfd_reference IS NOT NULL AND s.sfd_reference = c.sfd_reference)
            WHERE c.staging_ens_id = ?
            ORDER BY CASE WHEN s.id IS NOT NULL OR c.sfd_reference IS NOT NULL THEN 0 ELSE 1 END,
                     c.staging_id DESC
        """, [ens_id])
        for r in rows or []:
            synced_sfd_id = r.get('sfd_id')
            synced_sfd_ref = r.get('synced_sfd_reference') or r.get('sfd_reference') or ''
            if synced_sfd_id:
                results.append({
                    'id': str(synced_sfd_id),
                    'sfd_number': synced_sfd_ref,
                    'cons_number': r.get('dec_reference', ''),
                    'display': f"{synced_sfd_ref} - {r.get('goods_description','')[:40]} ({r.get('tss_status','')})",
                    'staging_cons_id': r['staging_id'],
                })
            elif r.get('dec_reference'):
                results.append({
                    'id': f"cons:{r['staging_id']}",
                    'sfd_number': r.get('sfd_reference', ''),
                    'cons_number': r.get('dec_reference', ''),
                    'display': f"[Consignment] {r['dec_reference']} - {r.get('goods_description','')[:40]}",
                    'staging_cons_id': r['staging_id'],
                })
    except Exception:
        logger.exception("Failed to load SDI parent candidates for ENS %s", ens_id)
    return results


# ── LIST ──────────────────────────────────────────────

def _eligible_sfd_parent_rows(where_sql='', params=None):
    rows = query_all(f"""
        SELECT s.id AS sfd_id,
               COALESCE(NULLIF(s.sfd_number, ''), NULLIF(s.sfd_reference, '')) AS sfd_number,
               COALESCE(NULLIF(s.ens_consignment_reference, ''), NULLIF(s.declaration_number, '')) AS consignment_number,
               s.tss_status AS sfd_tss_status,
               c.staging_id,
               c.staging_ens_id,
               c.dec_reference,
               c.goods_description,
               c.transport_document_number,
               c.controlled_goods,
               c.goods_domestic_status,
               c.importer_eori,
               c.importer_name,
               c.exporter_eori,
               c.exporter_name,
               c.no_sfd_reason,
               c.generate_SD,
               c.tss_status AS cons_tss_status,
               e.ens_reference
        FROM {S}.Sfds s
        LEFT JOIN {S}.StagingConsignments c
          ON c.dec_reference = s.ens_consignment_reference
          OR c.dec_reference = s.declaration_number
          OR c.sfd_reference = s.sfd_number
          OR c.sfd_reference = s.sfd_reference
        LEFT JOIN {S}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
        WHERE COALESCE(NULLIF(s.sfd_number, ''), NULLIF(s.sfd_reference, '')) IS NOT NULL
          {where_sql}
        ORDER BY s.id DESC
    """, params or [])
    return [row for row in rows if not _consignment_sdi_block_reason(row)]


def get_sfd_parents():
    """Get eligible SFDs that can spawn SDIs."""
    try:
        return [_sfd_parent_option(row) for row in _eligible_sfd_parent_rows()]
    except Exception:
        logger.exception("Failed to load eligible SDI parent SFDs")
        return []


def get_sfd_parents_for_ens(ens_id):
    try:
        rows = _eligible_sfd_parent_rows("AND c.staging_ens_id = ?", [ens_id])
        return [_sfd_parent_option(row) for row in rows]
    except Exception:
        logger.exception("Failed to load eligible SDI parent SFDs for ENS %s", ens_id)
        return []


def get_sfd_parents_for_consignment(staging_cons_id):
    try:
        rows = _eligible_sfd_parent_rows("AND c.staging_id = ?", [staging_cons_id])
        return [_sfd_parent_option(row) for row in rows]
    except Exception:
        logger.exception("Failed to load eligible SDI parent SFDs for consignment %s", staging_cons_id)
        return []


PRD_SDI_STATUS_TABS = tuple(TSS_FILTER_STATUS_TABS)
PRD_SDI_PAGE_SIZE = 50


def _prd_sdi_status_key(value):
    return canonical_filter_status(value or 'PENDING')


def _prd_sdi_filter_status(item):
    """Return the SDI list/filter status, preferring the live TSS state."""
    item = item or {}
    sup_ref = _first_text(item.get('sup_dec_number'), item.get('tss_sup_dec_number'))
    if sup_ref and not OFFICIAL_TSS_SUP_RE.match(sup_ref):
        return 'API_INACCESSIBLE'
    return effective_tss_filter_status(
        item.get('status') or item.get('sub_status'),
        item.get('tss_status'),
        fallback='DRAFT',
    )


def _clean_sdi_text(value):
    return str(value or '').strip()


def _safe_positive_int(value, default=1):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _safe_count(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _next_monthly_sdi_deadline(today=None):
    current = today or date.today()
    year = current.year
    month = current.month
    if current.day > 10:
        month += 1
    if month > 12:
        month = 1
        year += 1
    return date(year, month, 10)


def _json_message_lines(value):
    if value in (None, ''):
        return []
    if isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            lines.extend(_json_message_lines(item))
        return lines
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            nested = _json_message_lines(item)
            if nested:
                lines.extend(f"{key}: {line}" for line in nested)
            elif item not in (None, ''):
                lines.append(f"{key}: {item}")
        return lines

    raw = str(value).strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return [raw]
    if parsed == value:
        return [raw]
    return _json_message_lines(parsed)


def _sdi_error_lines(*values):
    lines = []
    for value in values:
        for line in _json_message_lines(value):
            cleaned = line.strip()
            if cleaned and cleaned not in lines:
                lines.append(cleaned)
    return lines


def _sdi_is_api_error_line(line):
    text = str(line or '').strip()
    if not text:
        return False
    lower = text.lower()
    local_markers = (
        'missing required fields for tss update',
        'is missing required fields for tss update',
        'header payload build failed',
        'payload build failed',
        'must be valid json',
        'must be a json array',
        'must be numeric',
        'missing tss goods id',
        'missing sup reference',
        'no sdi goods staged',
        'payload_error',
        'local_blockers',
        'local blockers',
    )
    if any(marker in lower for marker in local_markers):
        return False
    api_markers = (
        'tss ',
        'trader input required',
        'goods item number',
        'missing document code',
        'missing additional code',
        'customs agent role required',
        'http ',
        'api ',
        'response',
        'rejected',
        'amendment required',
        'failed',
        'failure',
    )
    return any(marker in lower for marker in api_markers)


def _sdi_api_error_lines(*values):
    return [
        line
        for line in _sdi_error_lines(*values)
        if _sdi_is_api_error_line(line)
    ]


def _sdi_api_error_text(*values):
    lines = _sdi_api_error_lines(*values)
    return '; '.join(lines)[:2000] if lines else None


def _prd_sdi_goods_api_error_sql(alias='g'):
    value_sql = f"LOWER(COALESCE(CAST({alias}.sdi_validation_errors_json AS NVARCHAR(MAX)), ''))"
    present_sql = (
        f"NULLIF(LTRIM(RTRIM(COALESCE(CAST({alias}.sdi_validation_errors_json AS NVARCHAR(MAX)), ''))), '') "
        "IS NOT NULL"
    )
    api_sql = " OR ".join(
        f"{value_sql} LIKE '%{marker}%'"
        for marker in (
            'tss ',
            'trader input required',
            'goods item number',
            'missing document code',
            'missing additional code',
            'customs agent role required',
            'http ',
            'api ',
            'response',
            'rejected',
            'amendment required',
            'failed',
            'failure',
        )
    )
    local_sql = " OR ".join(
        f"{value_sql} LIKE '%{marker}%'"
        for marker in (
            'missing required fields for tss update',
            'is missing required fields for tss update',
            'header payload build failed',
            'payload build failed',
            'must be valid json',
            'must be a json array',
            'must be numeric',
            'missing tss goods id',
            'missing sup reference',
            'no sdi goods staged',
            'payload_error',
            'local_blockers',
            'local blockers',
        )
    )
    return f"({present_sql} AND ({api_sql}) AND NOT ({local_sql}))"


def _sdi_missing_fields_from_errors(error_lines):
    missing = []
    for line in error_lines or []:
        lower = line.lower()
        if 'missing required fields' not in lower:
            continue
        tail = line.split(':', 1)[-1]
        for part in re.split(r'[,;]', tail):
            field = part.strip().strip('.')
            if field and field not in missing:
                missing.append(field)
    return missing


def _sdi_missing_field_keys(fields):
    keys = set()
    for field in fields or []:
        raw = str(field or '').strip()
        if not raw:
            continue
        if raw == 'un_locode or delivery_location_country/delivery_location_town':
            keys.update({'un_locode', 'delivery_location_country', 'delivery_location_town'})
            continue
        for part in re.split(r'\s+or\s+|/', raw):
            cleaned = part.strip()
            if cleaned:
                keys.add(cleaned)
    return keys


def _tss_payload_safe(value):
    if isinstance(value, dict):
        return {key: _tss_payload_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_tss_payload_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.strftime('%d/%m/%Y %H:%M:%S')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _payload_value_present(value):
    if value in (None, ''):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _payload_display(value):
    if not _payload_value_present(value):
        return ''
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(_tss_payload_safe(value), default=str, ensure_ascii=True)
    return str(_tss_payload_safe(value))


def _sdi_header_source_value(sd, field_name):
    sd = sd or {}
    if field_name == 'op_type':
        return 'update'
    if field_name == 'header_additions_deductions':
        return sd.get('header_additions_deductions') or sd.get('header_additions_deductions_json')
    nested_sources = SDI_HEADER_NESTED_FIELDS.get(field_name)
    if nested_sources:
        for source_name in nested_sources:
            value = sd.get(source_name)
            if _payload_value_present(value):
                return value
        return None
    return sd.get(field_name)


def _sdi_header_payload_context(sd):
    sd = sd or {}
    payload_error = ''
    try:
        payload = build_sdi_update_payload(sd)
    except ValueError as exc:
        payload_error = str(exc)
        payload = {
            'op_type': 'update',
            'sup_dec_number': sd.get('sup_dec_number') or '',
            'declaration_choice': sd.get('declaration_choice') or 'H1',
        }

    safe_payload = _tss_payload_safe(payload)
    missing_keys = set(sd.get('missing_field_keys') or [])
    rows = []
    for field_name in SDI_HEADER_PAYLOAD_FIELDS:
        payload_value = safe_payload.get(field_name) if isinstance(safe_payload, dict) else None
        source_value = _sdi_header_source_value(sd, field_name)
        sent = field_name in safe_payload and _payload_value_present(payload_value)
        display_value = _payload_display(payload_value if sent else source_value)
        requirement = SDI_HEADER_PAYLOAD_REQUIREMENTS.get(field_name, 'Optional')
        missing = field_name in missing_keys
        if requirement == 'Required' and not _payload_value_present(payload_value if sent else source_value):
            missing = True
        if payload_error and field_name in payload_error:
            missing = True
        rows.append({
            'field': field_name,
            'requirement': requirement,
            'sent': sent,
            'value': display_value,
            'missing': missing,
        })

    submit_payload = {
        'op_type': 'submit',
        'sup_dec_number': sd.get('sup_dec_number') or '',
    }
    return {
        'rows': rows,
        'payload': safe_payload,
        'payload_json': json.dumps(safe_payload, indent=2, default=str, ensure_ascii=True),
        'payload_error': payload_error,
        'submit_payload': _tss_payload_safe(submit_payload),
        'submit_payload_json': json.dumps(_tss_payload_safe(submit_payload), indent=2, ensure_ascii=True),
    }


def _sdi_goods_payload_context(goods):
    requests = []
    for item in goods or []:
        payload, warnings = build_sdi_goods_update_payload_for_api_attempt(item)
        entry = {
            'step': 'update_sdi_goods',
            'stg_sdi_item_id': item.get('staging_id'),
            'item_seq': item.get('item_number'),
            'tss_goods_id': item.get('tss_goods_id'),
            'ok': bool(item.get('tss_goods_id')),
            'payload': _tss_payload_safe(payload),
        }
        if warnings:
            entry['payload_warnings'] = warnings
        if not item.get('tss_goods_id'):
            entry['skipped'] = True
            entry.setdefault('payload_warnings', []).append(
                'missing TSS goods id; goods update skipped, submit can still be attempted'
            )
        requests.append(entry)
    return {
        'requests': requests,
        'payload_json': json.dumps(requests, indent=2, default=str, ensure_ascii=True),
    }


def _sdi_submit_attempt_context(sd, header_payload, goods_payload):
    header_errors = list((sd or {}).get('validation_error_lines') or [])
    goods_errors = []
    for item in goods_payload.get('requests') or []:
        if item.get('error'):
            goods_errors.append(item['error'])
    payload_build_errors = [
        error
        for error in [header_payload.get('payload_error'), *goods_errors]
        if error
    ]
    api_errors = _sdi_api_error_lines(header_errors)
    payload = {
        'sup_dec_number': (sd or {}).get('sup_dec_number'),
        'stg_sdi_id': (sd or {}).get('staging_id'),
        'api_blocked': bool(api_errors),
        'api_errors': api_errors,
        'payload_ready': not payload_build_errors,
        'payload_build_errors': payload_build_errors,
        'requests_in_order': [
            *(goods_payload.get('requests') or []),
            {
                'step': 'update_sdi_header',
                'ok': not header_payload.get('payload_error'),
                'payload': header_payload.get('payload'),
                **({'error': header_payload.get('payload_error')} if header_payload.get('payload_error') else {}),
            },
            {
                'step': 'submit_sdi',
                'ok': bool((sd or {}).get('sup_dec_number')),
                'payload': header_payload.get('submit_payload'),
            },
        ],
    }
    return json.dumps(payload, indent=2, default=str, ensure_ascii=True)


def _load_prd_sdi_tss_logs(sd, *, limit=5):
    sd = sd or {}
    sup_ref = sd.get('sup_dec_number') or ''
    staging_id = sd.get('staging_id')
    if not (sup_ref or staging_id):
        return []
    try:
        return query_all(
            f"""
            SELECT TOP ({max(1, min(int(limit or 5), 20))})
                ApiExchangeId,
                CallType,
                EntityKind,
                EntityId,
                HttpMethod,
                Url,
                RequestPayloadJson,
                HttpStatus,
                ResponseStatus,
                ResponseMessage,
                ResponseJson,
                ErrorDetail,
                CalledAt
            FROM [TSS].[BKD_API_Exchanges]
            WHERE ClientCode = ?
              AND (
                  EntityId = ?
                  OR RequestPayloadJson LIKE ?
                  OR ResponseJson LIKE ?
                  OR ResponseMessage LIKE ?
                  OR ErrorDetail LIKE ?
              )
            ORDER BY CalledAt DESC, ApiExchangeId DESC
            """,
            [S, staging_id, f'%{sup_ref}%', f'%{sup_ref}%', f'%{sup_ref}%', f'%{sup_ref}%'],
        )
    except Exception:
        logger.exception("Failed to load SDI TSS logs for %s", sup_ref or staging_id)
        return []


def _load_prd_sdi_tss_mirror(sd):
    sd = sd or {}
    sup_ref = sd.get('sup_dec_number') or ''
    if not sup_ref:
        return {}
    try:
        return query_one(
            """
            SELECT TOP 1
                SupDecNumber,
                SfdReference,
                TssStatus,
                RawJson,
                LastSyncedAt,
                UpdatedAt
            FROM [TSS].[BKD_SDI_Headers]
            WHERE ClientCode = ?
              AND UPPER(SupDecNumber) = ?
            ORDER BY LastSyncedAt DESC, UpdatedAt DESC, TssSdiHeaderId DESC
            """,
            [S, _clean_ref(sup_ref)],
        ) or {}
    except Exception:
        logger.exception("Failed to load TSS SDI mirror for %s", sup_ref)
        return {}


def _json_value_or_text(value):
    if value in (None, ''):
        return None
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text


def _json_nested_text(value, *keys):
    parsed = _json_value_or_text(value)
    wanted = {str(key).lower() for key in keys if key}

    def walk(item):
        if isinstance(item, dict):
            for key, nested in item.items():
                if str(key).lower() in wanted:
                    text = _first_text(nested)
                    if text:
                        return text
            for preferred in ('response', 'data', 'result', 'error', 'errors'):
                if preferred in item:
                    text = walk(item.get(preferred))
                    if text:
                        return text
            for nested in item.values():
                text = walk(nested)
                if text:
                    return text
        elif isinstance(item, list):
            for nested in item:
                text = walk(nested)
                if text:
                    return text
        return ''

    return walk(parsed)


def _json_search_text(value):
    parsed = _json_value_or_text(value)
    if parsed in (None, ''):
        return ''
    if isinstance(parsed, str):
        return parsed
    return json.dumps(parsed, default=str, ensure_ascii=True)


def _sdi_tss_logs_json(logs):
    if not logs:
        return json.dumps({'logs': [], 'message': 'No TSS response has been logged for this SUP yet.'}, indent=2)
    return json.dumps({'logs': logs}, indent=2, default=str, ensure_ascii=True)


def _sdi_requires_tss_input(sd):
    return _prd_sdi_status_key((sd or {}).get('tss_status')) == 'TRADER INPUT REQUIRED'


def _sdi_tss_log_message(log):
    log = log or {}
    return _first_text(
        log.get('ResponseMessage'),
        log.get('ErrorDetail'),
        _json_nested_text(log.get('ResponseJson'), 'error_message', 'process_message', 'message', 'raw_response'),
    )


def _sdi_tss_review_log(logs, current_message=''):
    best_log = {}
    best_score = -1
    current = _first_text(current_message).lower()
    for log in logs or []:
        search = ' '.join(
            _first_text(part)
            for part in (
                log.get('ResponseStatus'),
                log.get('ResponseMessage'),
                log.get('ErrorDetail'),
                _json_search_text(log.get('ResponseJson')),
            )
        ).lower()
        score = 0
        if 'trader input required' in search:
            score += 100
        if current and current in search:
            score += 90
        if 'goods item number' in search:
            score += 80
        if 'missing document code' in search:
            score += 70
        if 'missing additional code' in search:
            score += 70
        if 'customs agent role required' in search:
            score -= 50
        if score > best_score:
            best_score = score
            best_log = log
    return best_log if best_score > 0 else (logs[0] if logs else {})


def _sdi_tss_review_message(sd, logs=None, mirror=None):
    sd = sd or {}
    mirror = mirror or {}
    current_message = _first_text(
        _json_nested_text(
            mirror.get('RawJson') or sd.get('tss_raw_json'),
            'error_message',
            'process_message',
            'message',
            'raw_response',
        ),
        sd.get('tss_error_message'),
    )
    if current_message:
        return current_message

    review_log = _sdi_tss_review_log(logs or [])
    log_message = _sdi_tss_log_message(review_log)
    if log_message:
        return log_message

    auto_submit_error = _first_text(sd.get('auto_submit_error'))
    if 'customs agent role required' in auto_submit_error.lower():
        return ''

    lines = _sdi_error_lines(auto_submit_error, sd.get('validation_errors_json'))
    return lines[0] if lines else ''


def _sdi_tss_review_json(sd, logs=None, mirror=None):
    sd = sd or {}
    if not _sdi_requires_tss_input(sd):
        return ''

    logs = list(logs or [])
    mirror = mirror or {}
    current_message = _sdi_tss_review_message(sd, logs, mirror)
    latest_exchange = _sdi_tss_review_log(logs, current_message)
    payload = {
        'sup_dec_number': sd.get('sup_dec_number'),
        'sfd_reference': sd.get('sfd_reference'),
        'tss_status': sd.get('tss_status'),
        'current_tss_message': current_message or None,
        'stored_tss_error_message': sd.get('tss_error_message') or None,
        'stored_auto_submit_error': sd.get('auto_submit_error') or None,
        'latest_exchange': None,
        'tss_mirror': None,
    }

    if latest_exchange:
        payload['latest_exchange'] = {
            'api_exchange_id': latest_exchange.get('ApiExchangeId'),
            'call_type': latest_exchange.get('CallType'),
            'http_method': latest_exchange.get('HttpMethod'),
            'url': latest_exchange.get('Url'),
            'http_status': latest_exchange.get('HttpStatus'),
            'response_status': latest_exchange.get('ResponseStatus'),
            'response_message': latest_exchange.get('ResponseMessage'),
            'error_detail': latest_exchange.get('ErrorDetail'),
            'called_at': latest_exchange.get('CalledAt'),
            'request_payload': _json_value_or_text(latest_exchange.get('RequestPayloadJson')),
            'response_json': _json_value_or_text(latest_exchange.get('ResponseJson')),
        }

    if mirror:
        payload['tss_mirror'] = {
            'sup_dec_number': mirror.get('SupDecNumber'),
            'sfd_reference': mirror.get('SfdReference'),
            'tss_status': mirror.get('TssStatus'),
            'last_synced_at': mirror.get('LastSyncedAt'),
            'updated_at': mirror.get('UpdatedAt'),
            'raw_json': _json_value_or_text(mirror.get('RawJson')),
        }
    elif sd.get('tss_raw_json'):
        payload['tss_mirror'] = {
            'sup_dec_number': sd.get('sup_dec_number'),
            'sfd_reference': sd.get('sfd_reference'),
            'tss_status': sd.get('tss_status'),
            'raw_json': _json_value_or_text(sd.get('tss_raw_json')),
        }

    return json.dumps(payload, indent=2, default=str, ensure_ascii=True)


def _sdi_json_dump(value):
    if value in (None, ''):
        return None
    return json.dumps(value, default=str, ensure_ascii=True)


def _sdi_autosubmit_state(item):
    status_key = _prd_sdi_status_key(item.get('status'))
    tss_status_key = _prd_sdi_status_key(item.get('tss_status')) if item.get('tss_status') else ''
    goods_count = _safe_count(item.get('goods_count'))
    blocked_goods = _safe_count(item.get('blocked_goods_count'))
    ready_goods = _safe_count(item.get('ready_goods_count'))
    error_lines = item.get('validation_error_lines') or []

    if status_key in {'CANCELLED', 'CANCELED'} or tss_status_key in {'CANCELLED', 'CANCELED'}:
        return {
            'label': 'Cancelled',
            'tone': 'info',
            'detail': 'TSS has cancelled this SDI, so it is not eligible for autosubmit.',
        }
    if status_key in {'SUBMITTED', 'SUCCESS', 'CLOSED', 'COMPLETED'} or tss_status_key in {
        'SUBMITTED', 'ACCEPTED', 'CLEARED', 'CLOSED', 'COMPLETED'
    }:
        return {
            'label': 'Submitted',
            'tone': 'success',
            'detail': 'SDI has already been sent or accepted by TSS.',
        }
    if tss_status_key == 'TRADER INPUT REQUIRED':
        return {
            'label': 'TSS review',
            'tone': 'danger',
            'detail': 'TSS returned Trader Input Required. Open the SDI to inspect the API response JSON.',
        }
    if error_lines or blocked_goods:
        return {
            'label': 'TSS review',
            'tone': 'danger',
            'detail': 'TSS/API returned an error for this SDI.',
        }
    if goods_count == 0 or ready_goods == goods_count or item.get('sdi_ready_at') or status_key == 'VALIDATED':
        return {
            'label': 'Ready',
            'tone': 'success',
            'detail': 'No TSS/API error is currently recorded for this SDI. Fusion can attempt the API submit.',
        }
    return {
        'label': 'In progress',
        'tone': 'info',
        'detail': 'SDI has been staged and is waiting for the next automation pass.',
    }


def _normalise_prd_sdi_header(row):
    item = dict(row or {})
    item['staging_id'] = item.get('stg_sdi_id')
    item['sup_dec_number'] = item.get('tss_sup_dec_number') or item.get('sup_dec_number')
    item['sfd_reference'] = item.get('tss_sfd_consignment_ref') or item.get('sfd_reference')
    item['ens_consignment_ref'] = item.get('tss_consignment_ref') or item.get('ens_consignment_ref')
    item['status'] = item.get('sub_status') or item.get('status') or 'PENDING'
    item['filter_status'] = _prd_sdi_filter_status(item)
    item['arrival_date_time'] = _coerce_template_datetime(item.get('arrival_date_time'))
    item['submission_due_date'] = _coerce_template_date(item.get('tss_submission_due_date'))
    for key in (
        'validated_at',
        'submitted_at',
        'completed_at',
        'last_sub_status_change',
        'sdi_ready_at',
        'last_autosubmit_attempt_at',
    ):
        item[key] = _coerce_template_datetime(item.get(key))
    item['goods_count'] = _safe_count(item.get('goods_count'))
    item['blocked_goods_count'] = _safe_count(item.get('blocked_goods_count'))
    item['ready_goods_count'] = _safe_count(item.get('ready_goods_count'))
    item['tss_requires_input'] = _sdi_requires_tss_input(item)
    item['tss_review_message'] = _sdi_tss_review_message(item)
    item['validation_error_lines'] = _sdi_api_error_lines(
        item['tss_review_message'] if item['tss_requires_input'] else None,
        item.get('validation_errors_json'),
        item.get('auto_submit_error'),
        item.get('tss_error_message'),
    )
    item['missing_fields'] = _sdi_missing_fields_from_errors(item['validation_error_lines'])
    item['missing_field_keys'] = _sdi_missing_field_keys(item['missing_fields'])
    state = _sdi_autosubmit_state(item)
    item['autosubmit_label'] = state['label']
    item['autosubmit_tone'] = state['tone']
    item['autosubmit_detail'] = state['detail']
    return item


def _normalise_prd_sdi_goods(row):
    item = dict(row or {})
    item['staging_id'] = item.get('stg_sdi_item_id') or item.get('stg_item_id') or item.get('staging_id')
    item['item_number'] = item.get('item_seq') or item.get('item_number')
    item['status'] = item.get('sub_status') or item.get('status') or 'PENDING'
    item['sdi_ready_at'] = _coerce_template_datetime(item.get('sdi_ready_at'))
    item['validation_error_lines'] = _sdi_api_error_lines(
        item.get('sdi_validation_errors_json'),
    )
    item['missing_fields'] = _sdi_missing_fields_from_errors(item['validation_error_lines'])
    item['missing_field_keys'] = _sdi_missing_field_keys(item['missing_fields'])
    item['autosubmit_label'] = 'TSS review' if item['validation_error_lines'] else 'Ready'
    item['autosubmit_tone'] = 'danger' if item['validation_error_lines'] else 'success'
    return item


def _prd_sdi_arrival_select_expr():
    ens_arrival_expr = (
        "eh_src.arrival_date_time"
        if _schema_table_has_column('STG', 'BKD_ENS_Headers', 'arrival_date_time')
        else "CAST(NULL AS DATETIME2(7))"
    )
    if _schema_table_has_column('STG', 'BKD_SDI_Headers', 'arrival_date_time'):
        return f"COALESCE(h.arrival_date_time, {ens_arrival_expr}) AS arrival_date_time"
    return f"{ens_arrival_expr} AS arrival_date_time"


def _prd_sdi_optional_header_expr(column_name, alias=None):
    alias = alias or column_name
    if _schema_table_has_column('STG', 'BKD_SDI_Headers', column_name):
        return f"h.{column_name} AS {alias}"
    return f"CAST(NULL AS NVARCHAR(MAX)) AS {alias}"


def _prd_sdi_header_select_sql(where_sql):
    goods_api_error_sql = _prd_sdi_goods_api_error_sql('g')
    return f"""
        WITH goods AS (
            SELECT
                g.stg_sdi_id,
                COUNT(*) AS goods_count,
                SUM(CASE
                    WHEN {goods_api_error_sql}
                        THEN 1 ELSE 0 END) AS blocked_goods_count,
                SUM(CASE
                    WHEN NOT ({goods_api_error_sql})
                        THEN 1 ELSE 0 END) AS ready_goods_count
            FROM [STG].[BKD_SDI_GoodsItems] g
            WHERE g.ClientCode = ?
              AND g.stg_sdi_id IS NOT NULL
            GROUP BY g.stg_sdi_id
        )
        SELECT
            h.stg_sdi_id,
            h.ClientCode,
            h.sub_status,
            h.error_id,
            h.validated_at,
            h.submitted_at,
            h.completed_at,
            h.last_sub_status_change,
            h.tss_sup_dec_number,
            h.tss_sfd_consignment_ref,
            h.tss_consignment_ref,
            {_prd_sdi_arrival_select_expr()},
            h.tss_submission_due_date,
            h.tss_status,
            h.tss_movement_reference_number,
            h.tss_error_message,
            tss_h.RawJson AS tss_raw_json,
            h.stg_consignment_id,
            h.trader_reference,
            h.declaration_choice,
            {_prd_sdi_optional_header_expr('authorisation_type')},
            h.representation_type,
            h.additional_procedure,
            h.goods_domestic_status,
            h.importer_eori,
            h.importer_name,
            h.importer_street_number,
            h.importer_city,
            h.importer_postcode,
            h.importer_country,
            h.exporter_eori,
            h.exporter_name,
            h.exporter_street_number,
            h.exporter_city,
            h.exporter_postcode,
            h.exporter_country,
            h.transport_document_number,
            h.controlled_goods,
            h.goods_description,
            h.movement_type,
            h.destination_country,
            h.nationality_of_transport,
            h.identity_no_of_transport,
            h.location_of_goods_border,
            h.location_of_goods_other,
            h.un_locode,
            h.supervising_customs_office,
            h.customs_warehouse_identifier,
            h.incoterm,
            h.delivery_location_country,
            h.delivery_location_town,
            h.freight_charge,
            h.freight_charge_currency,
            h.insurance,
            h.insurance_currency,
            h.postponed_vat,
            h.vat_adjustment,
            h.vat_adjust_currency,
            h.exchange_rate,
            h.vat_number,
            {_prd_sdi_optional_header_expr('header_additions_deductions_json')},
            {_prd_sdi_optional_header_expr('header_previous_document_json')},
            {_prd_sdi_optional_header_expr('holder_of_authorisation_json')},
            h.auto_submit_enabled,
            h.sdi_ready_at,
            h.validation_errors_json,
            h.last_autosubmit_attempt_at,
            h.auto_submit_error,
            COALESCE(goods.goods_count, 0) AS goods_count,
            COALESCE(goods.blocked_goods_count, 0) AS blocked_goods_count,
            COALESCE(goods.ready_goods_count, 0) AS ready_goods_count
        FROM [STG].[BKD_SDI_Headers] h
        LEFT JOIN goods ON goods.stg_sdi_id = h.stg_sdi_id
        LEFT JOIN [STG].[BKD_ENS_Consignments] c_src
               ON c_src.ClientCode = h.ClientCode
              AND c_src.stg_consignment_id = h.stg_consignment_id
        LEFT JOIN [STG].[BKD_ENS_Headers] eh_src
               ON eh_src.ClientCode = c_src.ClientCode
              AND eh_src.stg_header_id = c_src.stg_header_id
        LEFT JOIN [TSS].[BKD_SDI_Headers] tss_h
               ON tss_h.ClientCode = h.ClientCode
              AND tss_h.SupDecNumber = h.tss_sup_dec_number
        WHERE {where_sql}
    """


def _load_prd_sdi_headers(search_text=''):
    where_parts = ['h.ClientCode = ?']
    params = [S, S]
    search = _clean_sdi_text(search_text)
    if search:
        like = f'%{search}%'
        where_parts.append(
            """(
                h.tss_sup_dec_number LIKE ?
                OR h.tss_sfd_consignment_ref LIKE ?
                OR h.tss_consignment_ref LIKE ?
                OR h.importer_eori LIKE ?
                OR h.exporter_eori LIKE ?
                OR h.trader_reference LIKE ?
                OR h.goods_description LIKE ?
                OR h.auto_submit_error LIKE ?
            )"""
        )
        params.extend([like] * 8)

    rows = query_all(
        _prd_sdi_header_select_sql(' AND '.join(where_parts)) + """
        ORDER BY
            COALESCE(
                h.last_autosubmit_attempt_at,
                h.last_sub_status_change,
                h.completed_at,
                h.submitted_at,
                h.validated_at
            ) DESC,
            h.stg_sdi_id DESC
        """,
        params,
    )
    return [_normalise_prd_sdi_header(row) for row in rows or []]


def _load_prd_sdi_header_by_id(staging_id):
    row = query_one(
        _prd_sdi_header_select_sql('h.ClientCode = ? AND h.stg_sdi_id = ?'),
        [S, S, staging_id],
    )
    return _normalise_prd_sdi_header(row) if row else None


def _load_prd_sdi_header_by_ref(sup_ref):
    row = query_one(
        _prd_sdi_header_select_sql('h.ClientCode = ? AND UPPER(h.tss_sup_dec_number) = ?'),
        [S, S, _clean_ref(sup_ref)],
    )
    return _normalise_prd_sdi_header(row) if row else None


def _load_prd_sdi_goods(staging_id):
    rows = query_all(
        """
        SELECT
            g.stg_sdi_item_id,
            g.stg_sdi_id,
            g.source_stg_item_id,
            g.sub_status,
            g.tss_goods_id,
            g.tss_consignment_ref,
            g.tss_sup_dec_number,
            g.tss_sfd_number,
            g.item_seq,
            g.goods_description,
            g.commodity_code,
            g.gross_mass_kg,
            g.net_mass_kg,
            g.number_of_packages,
            g.number_of_individual_pieces,
            g.type_of_packages,
            g.package_marks,
            g.procedure_code,
            g.additional_procedure_code,
            g.controlled_goods,
            g.country_of_origin,
            g.item_invoice_amount,
            g.item_invoice_currency,
            g.line_amount_excl_vat,
            g.source_amount,
            g.unit_price_excl_vat,
            g.customs_value,
            g.valuation_method,
            g.statistical_value,
            g.nature_of_transaction,
            g.preference,
            g.ni_additional_information_codes,
            g.valuation_indicator,
            g.invoice_number,
            g.country_of_preferential_origin,
            g.supplementary_units,
            g.taric_code,
            g.cus_code,
            g.national_additional_code,
            g.quota_order_number,
            g.controlled_goods_type,
            g.tax_type,
            g.tax_base_unit,
            g.tax_base_quantity,
            g.payable_tax_amount,
            g.payable_tax_currency,
            g.additional_procedures_json,
            g.document_references_json,
            g.additional_information_json,
            g.detail_previous_document_json,
            g.item_add_ded_json,
            g.national_additional_codes_json,
            g.tax_bases_json,
            g.additional_parties_json,
            g.sdi_ready_at,
            g.sdi_validation_errors_json,
            g.sdi_auto_submit_enabled
        FROM [STG].[BKD_SDI_GoodsItems] g
        WHERE g.ClientCode = ?
          AND g.stg_sdi_id = ?
          AND UPPER(COALESCE(g.sub_status, '')) NOT IN ('TSS_REMOVED', 'DELETED', 'CANCELLED', 'CANCELED')
        ORDER BY COALESCE(g.item_seq, g.stg_sdi_item_id), g.stg_sdi_item_id
        """,
        [S, staging_id],
    )
    return [_normalise_prd_sdi_goods(row) for row in rows or []]


def _prd_sdi_goods_mapping_issue(sd, goods):
    """Return a data-integrity warning when SDI goods look source-duplicated.

    This guards the live TSS submit path from replaying the old positional
    matching bug where several TSS goods received the first DEC goods payload.
    """

    active_goods = [item for item in goods or [] if item.get('tss_goods_id')]
    source_ids = [item.get('source_stg_item_id') for item in active_goods if item.get('source_stg_item_id')]
    if len(active_goods) < 2 or len(source_ids) < 2:
        return None

    distinct_source_ids = {str(value).strip() for value in source_ids if str(value).strip()}
    if len(distinct_source_ids) == len(source_ids):
        return None

    stg_consignment_id = (sd or {}).get('stg_consignment_id')
    if not stg_consignment_id:
        return None

    source_summary = query_one(
        """
        SELECT
            COUNT(1) AS source_goods_count,
            COUNT(DISTINCT CONCAT(
                COALESCE(CONVERT(NVARCHAR(50), item_seq), ''),
                '|',
                COALESCE(goods_description, ''),
                '|',
                COALESCE(commodity_code, '')
            )) AS distinct_source_goods
        FROM [STG].[BKD_GoodsItems]
        WHERE ClientCode = ?
          AND stg_consignment_id = ?
          AND UPPER(COALESCE(goods_stage, 'ENS')) <> 'SDI'
        """,
        [S, stg_consignment_id],
    ) or {}

    source_goods_count = int(source_summary.get('source_goods_count') or 0)
    distinct_source_goods = int(source_summary.get('distinct_source_goods') or 0)
    if source_goods_count <= len(distinct_source_ids) or distinct_source_goods <= len(distinct_source_ids):
        return None

    sup_ref = (sd or {}).get('sup_dec_number') or (sd or {}).get('tss_sup_dec_number') or 'SDI'
    return (
        f"{sup_ref}: SDI goods mapping looks duplicated; "
        f"{len(active_goods)} active SDI goods map to {len(distinct_source_ids)} source goods, "
        f"but the DEC/source consignment has {source_goods_count} goods "
        f"({distinct_source_goods} distinct). Submit skipped to avoid sending duplicated goods payloads to TSS."
    )


def _sdi_missing_field_counts(sd, goods):
    counts = Counter()
    for field in (sd or {}).get('missing_fields', []):
        counts[field] += 1
    for item in goods or []:
        for field in item.get('missing_fields', []):
            counts[field] += 1
    return dict(counts.most_common())


def _render_prd_sdi_detail(sd):
    goods = _load_prd_sdi_goods(sd['staging_id'])
    header_payload = _sdi_header_payload_context(sd)
    goods_payload = _sdi_goods_payload_context(goods)
    tss_logs = _load_prd_sdi_tss_logs(sd)
    tss_mirror = _load_prd_sdi_tss_mirror(sd)
    return render_template(
        'supdec/prd_detail.html',
        sd=sd,
        goods=goods,
        badge_class=badge_class,
        missing_field_counts=_sdi_missing_field_counts(sd, goods),
        header_payload_rows=header_payload['rows'],
        header_payload_json=header_payload['payload_json'],
        header_payload_error=header_payload['payload_error'],
        goods_payload_json=goods_payload['payload_json'],
        submit_attempt_json=_sdi_submit_attempt_context(sd, header_payload, goods_payload),
        submit_payload_json=header_payload['submit_payload_json'],
        tss_review_json=_sdi_tss_review_json(sd, tss_logs, tss_mirror),
        tss_response_json=_sdi_tss_logs_json(tss_logs),
        tss_response_logs=tss_logs,
        can_cancel_tss=_supdec_can_cancel_in_tss(sd),
        today=date.today(),
    )


@supdec_bp.route('/import-tss', methods=['GET', 'POST'])
def import_tss():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@supdec_bp.route('/')
def list_view():
    status_filter = canonical_filter_status(request.args.get('status') or 'ALL')
    search_query = _clean_sdi_text(request.args.get('q'))
    page = _safe_positive_int(request.args.get('page'), 1)
    show_all = request.args.get('show_all') == '1'

    try:
        all_rows = _load_prd_sdi_headers(search_query)
    except Exception:
        logger.exception("Failed to load PRD SDI headers")
        flash('Could not load the STG/TSS SDI view. Check the technical logs for details.', 'danger')
        return redirect(url_for('ingest.queue'))

    status_counts = Counter(row.get('filter_status') or _prd_sdi_filter_status(row) for row in all_rows)
    status_counts['ALL'] = len(all_rows)
    if status_filter != 'ALL':
        rows = [row for row in all_rows if (row.get('filter_status') or _prd_sdi_filter_status(row)) == status_filter]
    else:
        rows = list(all_rows)

    total = len(rows)
    if show_all:
        paged_rows = rows
        total_pages = 1
    else:
        total_pages = max(1, (total + PRD_SDI_PAGE_SIZE - 1) // PRD_SDI_PAGE_SIZE)
        page = min(page, total_pages)
        start = (page - 1) * PRD_SDI_PAGE_SIZE
        paged_rows = rows[start:start + PRD_SDI_PAGE_SIZE]

    status_tabs = status_filter_tabs(dict(status_counts), PRD_SDI_STATUS_TABS, selected=status_filter)
    autosubmit_counts = Counter(row.get('autosubmit_label') for row in all_rows)
    today = date.today()
    monthly_deadline = _next_monthly_sdi_deadline(today)
    overdue_count = sum(
        1
        for row in all_rows
        if row.get('submission_due_date')
        and row.get('submission_due_date') < today
        and (row.get('filter_status') or _prd_sdi_filter_status(row)) not in {'SUBMITTED', 'COMPLETED', 'ACCEPTED', 'CLEARED', 'CLOSED'}
    )

    return render_template(
        'supdec/prd_list.html',
        supdecs=paged_rows,
        status_filter=status_filter,
        status_tabs=status_tabs,
        status_counts=dict(status_counts),
        search_query=search_query,
        autosubmit_counts=dict(autosubmit_counts),
        blocked_count=autosubmit_counts.get('TSS review', 0),
        ready_count=autosubmit_counts.get('Ready', 0),
        submitted_count=autosubmit_counts.get('Submitted', 0),
        page=page,
        total_pages=total_pages,
        total=total,
        show_all=show_all,
        badge_class=badge_class,
        today=today,
        deadline_date=monthly_deadline.strftime('%d %b %Y'),
        days_to_deadline=(monthly_deadline - today).days,
        overdue_count=overdue_count,
    )


@supdec_bp.route('/export.csv')
def export_csv():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))


# ── CREATE ────────────────────────────────────────────

@supdec_bp.route('/create', methods=['GET', 'POST'])
def create():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
# ── EDIT ──────────────────────────────────────────────

PRD_SDI_HEADER_EDIT_FIELDS = (
    'trader_reference', 'declaration_choice', 'authorisation_type',
    'arrival_date_time', 'representation_type', 'additional_procedure',
    'goods_domestic_status', 'importer_eori', 'importer_name',
    'importer_street_number', 'importer_city', 'importer_postcode',
    'importer_country', 'exporter_eori', 'exporter_name',
    'exporter_street_number', 'exporter_city', 'exporter_postcode',
    'exporter_country', 'transport_document_number', 'controlled_goods',
    'goods_description', 'movement_type', 'destination_country', 'nationality_of_transport',
    'identity_no_of_transport', 'location_of_goods_border',
    'location_of_goods_other', 'un_locode', 'supervising_customs_office',
    'customs_warehouse_identifier', 'incoterm',
    'delivery_location_country', 'delivery_location_town', 'freight_charge',
    'freight_charge_currency', 'insurance', 'insurance_currency',
    'postponed_vat', 'vat_adjustment', 'vat_adjust_currency',
    'exchange_rate', 'vat_number',
)

PRD_SDI_GOODS_EDIT_FIELDS = (
    'goods_description', 'commodity_code', 'gross_mass_kg', 'net_mass_kg',
    'number_of_packages', 'number_of_individual_pieces', 'type_of_packages',
    'package_marks', 'equipment_number', 'un_dangerous_goods_code',
    'procedure_code', 'additional_procedure_code', 'controlled_goods',
    'country_of_origin', 'country_of_preferential_origin',
    'item_invoice_amount', 'item_invoice_currency', 'customs_value',
    'valuation_method', 'valuation_indicator', 'invoice_number',
    'statistical_value', 'nature_of_transaction', 'preference',
    'ni_additional_information_codes', 'supplementary_units',
    'quota_order_number', 'taric_code', 'cus_code', 'national_additional_code',
)


class _SqlNowMarker:
    pass


_sql_now = _SqlNowMarker()


def _form_optional_value(form_data, key):
    value = (form_data or {}).get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _sdi_header_record_from_form(sd, form_data):
    record = dict(sd or {})
    for field in PRD_SDI_HEADER_EDIT_FIELDS:
        record[field] = _form_optional_value(form_data, field)
    return record


def _sdi_goods_record_from_form(item, form_data):
    record = dict(item or {})
    prefix = f"goods_{record.get('staging_id')}__"
    for field in PRD_SDI_GOODS_EDIT_FIELDS:
        record[field] = _form_optional_value(form_data, prefix + field)
    return record


def _sdi_set_clause(values):
    parts = []
    params = []
    for column, value in values.items():
        if value is _sql_now:
            parts.append(f'[{column}] = SYSUTCDATETIME()')
        else:
            parts.append(f'[{column}] = ?')
            params.append(value)
    return ', '.join(parts), params


def _sdi_existing_values(schema_name, table_name, values):
    columns = _schema_table_columns(schema_name, table_name)
    return {
        column: value
        for column, value in (values or {}).items()
        if str(column).lower() in columns
    }


def _manual_sdi_submit_technical_url(log_id):
    if not log_id:
        return url_for('technical.index', tab='api')
    return url_for('technical.index', tab='api', log_id=log_id, _anchor=f'api-log-{log_id}')


def _flash_manual_sdi_submit(message, category, *, log_id=None):
    if log_id:
        flash({
            'text': message,
            'technical_url': _manual_sdi_submit_technical_url(log_id),
            'technical_label': f'Technical log #{log_id}',
        }, category)
    else:
        flash(message, category)


def _log_manual_sdi_submit_event(
    *,
    sid=None,
    sup_ref=None,
    summary=None,
    errors=None,
    status='UNKNOWN',
    call_type='SUBMIT_SDI_MANUAL',
    route_url='',
):
    """Persist one manual SDI submit attempt and return its technical log id."""
    errors = [str(item) for item in (errors or []) if str(item or '').strip()]
    payload = {
        'manual': True,
        'stg_sdi_id': sid,
        'sup_dec_number': sup_ref,
        'summary': summary or {},
        'errors': errors[:20],
    }
    try:
        from app.db import insert_api_call_log

        return insert_api_call_log(
            S,
            call_type,
            staging_id=sid,
            http_method='POST',
            url=route_url,
            request_payload=payload,
            http_status=200 if str(status).upper() in {'SUBMITTED', 'NOOP'} else 500,
            response_status=status,
            response_message='; '.join(errors)[:1000] if errors else status,
            response_json=payload,
            error_detail='; '.join(errors)[:2000] if errors else None,
        )
    except Exception:
        logger.exception('Failed to audit manual SDI submit event')
        return None


def _update_prd_sdi_goods_row(cursor, item, record):
    values = {field: record.get(field) for field in PRD_SDI_GOODS_EDIT_FIELDS}
    values.update({
        'sub_status': 'VALIDATED',
        'sdi_validation_errors_json': None,
        'sdi_auto_submit_enabled': 1,
        'last_sub_status_change': _sql_now,
        'sdi_ready_at': _sql_now,
        'updated_at': _sql_now,
    })
    values = _sdi_existing_values('STG', 'BKD_SDI_GoodsItems', values)
    if not values:
        return []
    set_sql, params = _sdi_set_clause(values)
    params.extend([S, item['staging_id']])
    cursor.execute(
        f"""
        UPDATE [STG].[BKD_SDI_GoodsItems]
           SET {set_sql}
         WHERE ClientCode = ?
           AND stg_sdi_item_id = ?
        """,
        params,
    )
    return []


def _update_prd_sdi_header_row(cursor, sd, record, header_errors, blocked_goods_count):
    values = {field: record.get(field) for field in PRD_SDI_HEADER_EDIT_FIELDS}
    values.update({
        'sub_status': 'VALIDATED',
        'validation_errors_json': None,
        'auto_submit_error': None,
        'auto_submit_enabled': 1,
        'last_sub_status_change': _sql_now,
        'last_autosubmit_attempt_at': _sql_now,
        'sdi_ready_at': _sql_now,
        'updated_at': _sql_now,
    })
    values = _sdi_existing_values('STG', 'BKD_SDI_Headers', values)
    if not values:
        return
    set_sql, params = _sdi_set_clause(values)
    params.extend([S, sd['staging_id']])
    cursor.execute(
        f"""
        UPDATE [STG].[BKD_SDI_Headers]
           SET {set_sql}
         WHERE ClientCode = ?
           AND stg_sdi_id = ?
        """,
        params,
    )


@supdec_bp.route('/<int:sid>/edit', methods=['GET', 'POST'])
def edit(sid):
    sd = _load_prd_sdi_header_by_id(sid)
    if not sd:
        flash(f'SDI #{sid} is not staged in STG.BKD_SDI_Headers yet.', 'warning')
        return redirect(url_for('supdec.list_view'))

    goods = _load_prd_sdi_goods(sd['staging_id'])
    if request.method == 'POST':
        header_record = _sdi_header_record_from_form(sd, request.form)
        header_errors = []
        blocked_goods_count = 0
        with db_cursor() as cursor:
            for item in goods:
                goods_record = _sdi_goods_record_from_form(item, request.form)
                _update_prd_sdi_goods_row(cursor, item, goods_record)
            _update_prd_sdi_header_row(cursor, sd, header_record, header_errors, blocked_goods_count)

        flash('SDI saved. Fusion will attempt the TSS API submit with the available payload data.', 'success')
        return redirect(url_for('supdec.detail', sid=sd['staging_id']))

    return render_template(
        'supdec/prd_edit.html',
        sd=sd,
        goods=goods,
        choices=load_supdec_choices(),
        goods_choices=load_supdec_goods_choices(),
        missing_field_counts=_sdi_missing_field_counts(sd, goods),
        badge_class=badge_class,
    )
# ── DETAIL ────────────────────────────────────────────

@supdec_bp.route('/<string:sup_ref>/detail')
def detail_by_ref(sup_ref):
    sd = _load_prd_sdi_header_by_ref(sup_ref)
    if not sd:
        flash(f'SDI {sup_ref} is not staged in STG.BKD_SDI_Headers yet.', 'warning')
        return redirect(url_for('supdec.list_view'))
    return _render_prd_sdi_detail(sd)
@supdec_bp.route('/<int:sid>')
def detail(sid):
    sd = _load_prd_sdi_header_by_id(sid)
    if not sd:
        flash(f'SDI #{sid} is not staged in STG.BKD_SDI_Headers yet.', 'warning')
        return redirect(url_for('supdec.list_view'))
    return _render_prd_sdi_detail(sd)
def _render_detail(sd):
    goods = _load_supdec_goods_records(sd['staging_id'])
    linked_sfd = _load_sfd_link_for_supdec(sd)
    linked_cons = _load_consignment_for_supdec(sd, linked_sfd=linked_sfd)
    if not (linked_sfd or {}).get('sfd_reference'):
        linked_sfd = _load_sfd_link_for_supdec(sd, linked_cons=linked_cons)
    linked_gmr = None

    if linked_cons and linked_cons.get('staging_ens_id'):
        linked_gmr = query_one(f"""
            SELECT staging_id, gmr_id, status, gvms_status
            FROM {S}.StagingGmrs
            WHERE staging_ens_id = ?
            ORDER BY created_at DESC
        """, [linked_cons['staging_ens_id']])

    declaration_chain = _build_declaration_chain(sd, linked_cons=linked_cons, linked_sfd=linked_sfd, linked_gmr=linked_gmr)
    error_explanation = explain_tss_error(
        sd.get('error_message'),
        local_status=sd.get('status'),
        tss_status=sd.get('tss_status') or sd.get('clear_date_time'),
        entity_label='this SDI',
    )

    source_goods_rows = _load_source_goods_records(sd)
    return render_template('supdec/detail.html', sd=sd, goods=goods, badge_class=badge_class,
                           linked_cons=linked_cons, linked_sfd=linked_sfd,
                           linked_gmr=linked_gmr, declaration_chain=declaration_chain,
                           effective_act_as=_current_supdec_act_as(sd),
                           can_create_tss=_supdec_can_create_in_tss(sd),
                           can_submit_tss=_supdec_can_submit_in_tss(sd),
                           can_cancel_tss=_supdec_can_cancel_in_tss(sd),
                           can_recall_tss=_supdec_can_recall_in_tss(sd),
                           source_goods_count=len(source_goods_rows),
                           source_goods_rows=source_goods_rows,
                           error_explanation=error_explanation,
                           today=date.today())


@supdec_bp.route('/<int:sid>/discover', methods=['POST'])
def discover_from_sfd(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@supdec_bp.route('/<int:sid>/refresh', methods=['POST'])
def refresh_tss(sid):
    result = sync_prd_sdi_from_tss(sid)
    if result.get('ok'):
        flash(
            f"Synced {result.get('sup_ref')}: TSS {result.get('tss_status') or 'status refreshed'}, "
            f"{result.get('goods_synced', 0)} goods mirrored.",
            'success',
        )
    else:
        flash(f"Could not sync SDI #{sid}: {result.get('message') or result.get('stage')}", 'warning')
    return redirect(url_for('supdec.detail', sid=sid))
@supdec_bp.route('/<int:sid>/create-in-tss', methods=['POST'])
def create_in_tss(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@supdec_bp.route('/<int:sid>/submit', methods=['POST'])
def submit_tss(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@supdec_bp.route('/<int:sid>/recall', methods=['POST'])
def recall_tss(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@supdec_bp.route('/<int:sid>/cancel', methods=['POST'])
def cancel_tss(sid):
    sd = _load_prd_sdi_header_by_id(sid)
    if not sd:
        flash(f'SDI #{sid} is not staged in STG.BKD_SDI_Headers yet.', 'warning')
        return redirect(url_for('supdec.list_view'))

    sup_ref = sd.get('sup_dec_number')
    if not sup_ref:
        flash(f'SDI #{sid} has no SUP reference, so there is no TSS SDI to cancel.', 'warning')
        return redirect(url_for('supdec.detail', sid=sid))
    if not _supdec_can_cancel_in_tss(sd):
        flash(f'{sup_ref} is already terminal in TSS and cannot be cancelled from Fusion.', 'warning')
        return redirect(url_for('supdec.detail', sid=sid))

    api = build_cfg_client()
    result = api.cancel_sdi(sup_ref)
    ok = _sdi_tss_result_ok(result)
    message = _sdi_tss_result_message(result)
    log_id = None

    with db_cursor() as cursor:
        _log_prd_sdi_tss_exchange(
            cursor,
            sd,
            call_type='CANCEL_SDI',
            payload={'op_type': 'cancel', 'sup_dec_number': sup_ref},
            result=result,
        )
        if ok:
            _mark_prd_sdi_submit_state(cursor, sd, status='CANCELLED', errors=[])
            _mark_prd_tss_sdi_status(cursor, sd, 'CANCELLED')
            goods_values = _sdi_existing_values('STG', 'BKD_SDI_GoodsItems', {
                'sub_status': 'CANCELLED',
                'sdi_validation_errors_json': None,
                'last_sub_status_change': _sql_now,
                'updated_at': _sql_now,
            })
            if goods_values:
                set_sql, params = _sdi_set_clause(goods_values)
                params.extend([S, sd['staging_id']])
                cursor.execute(
                    f"""
                    UPDATE [STG].[BKD_SDI_GoodsItems]
                       SET {set_sql}
                     WHERE ClientCode = ?
                       AND stg_sdi_id = ?
                    """,
                    params,
                )
        else:
            error = f'{sup_ref}: TSS SDI cancel failed: {message}'
            _mark_prd_sdi_submit_state(cursor, sd, status='PENDING_REVIEW', errors=[error])

    log_id = _log_manual_sdi_submit_event(
        sid=sid,
        sup_ref=sup_ref,
        summary={'candidates': 1, 'cancelled': 1 if ok else 0, 'blocked': 0 if ok else 1},
        errors=[] if ok else [message],
        status='CANCELLED' if ok else 'FAILED',
        call_type='CANCEL_SDI_MANUAL',
        route_url=url_for('supdec.cancel_tss', sid=sid),
    )

    if ok:
        flash(f'SDI {sup_ref} cancelled in TSS.', 'success')
    else:
        technical_url = url_for('technical.index', tab='ingest', _anchor=f'api-log-{log_id}') if log_id else url_for('technical.index', tab='ingest')
        flash(f'SDI {sup_ref} was not cancelled. TSS response: {message} ', 'warning')
        flash(f'Technical log #{log_id}' if log_id else 'Open technical logs', 'info')
        return redirect(technical_url)
    return redirect(url_for('supdec.detail', sid=sid))
# ── DELETE / RETRY ────────────────────────────────────

def _selected_supdec_ids_from_form():
    ids = []
    for raw_id in request.form.getlist('selected_ids'):
        try:
            ids.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    return sorted(set(ids))


def _supdec_list_redirect_args_from_form():
    redirect_args = {'status': request.form.get('status', '').strip().upper() or 'ALL'}
    search_query = request.form.get('q', '').strip()
    ens_ref = request.form.get('ens_ref', '').strip()
    cons_ref = request.form.get('cons_ref', '').strip()
    if search_query:
        redirect_args['q'] = search_query
    if ens_ref:
        redirect_args['ens_ref'] = ens_ref
    if cons_ref:
        redirect_args['cons_ref'] = cons_ref
    return redirect_args


def _prd_sdi_tir_error_kind(error_message):
    text = str(error_message or '').lower()
    if 'missing item value' in text or 'must be greater than 0' in text:
        return 'ITEM_VALUE'
    if (
        'missing document code' in text
        or 'cds12068' in text
        or 'document identifier' in text
        or 'additionaldocument' in text
    ):
        return 'DOCUMENTS'
    if 'cds40011' in text or 'additional commodity code' in text:
        return 'TARIC_ADDITIONAL_CODE'
    if 'supplementary unit' in text:
        return 'SUPPLEMENTARY_UNITS'
    return 'OTHER'


def _sdi_duplicate_norm_text(value):
    return ' '.join(str(value or '').strip().upper().split())


def _sdi_duplicate_norm_decimal(value):
    if value in (None, ''):
        return ''
    try:
        return str(Decimal(str(value)).normalize())
    except Exception:
        return _sdi_duplicate_norm_text(value)


def _sdi_duplicate_goods_line_key(item):
    return (
        _sdi_duplicate_norm_text((item or {}).get('goods_description')),
        _sdi_duplicate_norm_text((item or {}).get('commodity_code')).replace(' ', ''),
        _sdi_duplicate_norm_text((item or {}).get('country_of_origin')),
        _sdi_duplicate_norm_decimal((item or {}).get('gross_mass_kg')),
        _sdi_duplicate_norm_decimal((item or {}).get('net_mass_kg')),
        _sdi_duplicate_norm_decimal((item or {}).get('number_of_packages')),
        _sdi_duplicate_norm_text((item or {}).get('type_of_packages')),
    )


def _prd_sdi_goods_duplicate_fingerprint(staging_id):
    if not staging_id:
        return ()
    goods = [
        item for item in (_load_prd_sdi_goods(staging_id) or [])
        if item.get('tss_goods_id')
        and _clean_ref(item.get('status')) not in {'TSS_REMOVED', 'DELETED', 'CANCELLED', 'CANCELED'}
    ]
    return tuple(sorted(_sdi_duplicate_goods_line_key(item) for item in goods))


def _prd_sdi_goods_fingerprints_match(left_staging_id, right_staging_id):
    left = _prd_sdi_goods_duplicate_fingerprint(left_staging_id)
    right = _prd_sdi_goods_duplicate_fingerprint(right_staging_id)
    return bool(left and right and left == right)


def _prd_sdi_has_active_transport_duplicate(sd):
    transport = _first_text(
        (sd or {}).get('transport_document_number'),
        (sd or {}).get('trader_reference'),
    )
    sup_ref = _clean_ref((sd or {}).get('sup_dec_number'))
    current_staging_id = (sd or {}).get('staging_id')
    if not transport or not sup_ref or not current_staging_id:
        return []

    rows = query_all(
        """
        SELECT
            h.stg_sdi_id,
            h.tss_sup_dec_number,
            h.tss_status,
            h.sub_status
        FROM [STG].[BKD_SDI_Headers] h
        WHERE h.ClientCode = ?
          AND UPPER(COALESCE(h.transport_document_number, h.trader_reference, '')) = UPPER(?)
          AND UPPER(COALESCE(h.tss_sup_dec_number, '')) <> ?
          AND NULLIF(LTRIM(RTRIM(COALESCE(h.tss_sup_dec_number, ''))), '') IS NOT NULL
        """,
        [S, transport, sup_ref],
    )
    active = []
    for row in rows or []:
        status_key = _clean_ref(row.get('tss_status') or row.get('sub_status'))
        if (
            status_key not in PRD_SDI_TERMINAL_STATUSES
            and _prd_sdi_goods_fingerprints_match(current_staging_id, row.get('stg_sdi_id'))
        ):
            active.append(row.get('tss_sup_dec_number'))
    return [item for item in active if item]


def _load_sdi_document_json(value):
    if value in (None, ''):
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _sdi_document_slot_key(document):
    return (
        _first_text((document or {}).get('document_code')).upper(),
        _first_text((document or {}).get('document_reference')).upper(),
    )


def _merge_sdi_documents(existing, defaults):
    by_slot = {}
    for document in [*(existing or []), *(defaults or [])]:
        code = _first_text((document or {}).get('document_code')).upper()
        if not code or code == '1UKI':
            continue
        clean_doc = dict(document or {})
        clean_doc['document_code'] = code
        clean_doc.setdefault('op_type', 'create')
        slot_key = _sdi_document_slot_key(clean_doc)
        previous = by_slot.get(slot_key)
        if previous is None:
            by_slot[slot_key] = clean_doc
            continue
        previous_status = _first_text(previous.get('document_status'))
        new_status = _first_text(clean_doc.get('document_status'))
        if not previous_status and new_status:
            previous['document_status'] = clean_doc['document_status']
    return list(by_slot.values())


def _apply_prd_sdi_historical_documents(cursor, staging_id):
    cursor.execute(
        """
        SELECT
            stg_sdi_item_id,
            REPLACE(COALESCE(commodity_code, ''), ' ', '') AS commodity_code,
            UPPER(COALESCE(country_of_origin, '')) AS country_of_origin,
            document_references_json
        FROM [STG].[BKD_SDI_GoodsItems]
        WHERE ClientCode = ?
          AND stg_sdi_id = ?
        """,
        [S, staging_id],
    )
    goods_rows = [
        {
            'stg_sdi_item_id': row[0],
            'commodity_code': _first_text(row[1]),
            'country_of_origin': _first_text(row[2]).upper(),
            'document_references_json': row[3],
        }
        for row in cursor.fetchall()
    ]
    combos = sorted({
        (row['commodity_code'], row['country_of_origin'])
        for row in goods_rows
        if row['commodity_code'] and row['country_of_origin']
    })
    if not combos:
        return 0

    documents_by_combo = {}
    for commodity_code, origin in combos:
        cursor.execute(
            """
            SELECT
                document_code,
                document_status,
                document_reference,
                document_part,
                document_reason,
                date_of_validity,
                issuing_authority,
                amount,
                currency,
                measurement_unit,
                quantity
            FROM [BKD].[DocProductCatalogDocuments]
            WHERE active = 1
              AND COALESCE(auto_apply_to_sdi, 1) = 1
              AND COALESCE(requires_compliance_review, 0) = 0
              AND REPLACE(COALESCE(commodity_code, ''), ' ', '') = ?
              AND UPPER(COALESCE(country_of_origin, '')) = ?
              AND UPPER(LTRIM(RTRIM(COALESCE(document_code, '')))) NOT IN ('', 'N935', '1UKI')
            ORDER BY COALESCE(evidence_count, 1) DESC, id
            """,
            [commodity_code, origin],
        )
        docs = []
        for row in cursor.fetchall():
            document = {
                'op_type': 'create',
                'document_code': _first_text(row[0]).upper(),
            }
            for idx, key in enumerate(
                (
                    'document_status',
                    'document_reference',
                    'document_part',
                    'document_reason',
                    'date_of_validity',
                    'issuing_authority',
                    'amount',
                    'currency',
                    'measurement_unit',
                    'quantity',
                ),
                start=1,
            ):
                value = row[idx]
                if value not in (None, ''):
                    document[key] = value
            docs.append(document)
        documents_by_combo[(commodity_code, origin)] = docs

    updated = 0
    for goods_row in goods_rows:
        defaults = documents_by_combo.get((goods_row['commodity_code'], goods_row['country_of_origin'])) or []
        if not defaults:
            continue
        existing = _load_sdi_document_json(goods_row.get('document_references_json'))
        merged = _merge_sdi_documents(existing, defaults)
        if merged == existing:
            continue
        cursor.execute(
            """
            UPDATE [STG].[BKD_SDI_GoodsItems]
               SET document_references_json = ?,
                   updated_at = SYSUTCDATETIME()
             WHERE ClientCode = ?
               AND stg_sdi_item_id = ?
            """,
            [json.dumps(merged, default=str, ensure_ascii=True), S, goods_row['stg_sdi_item_id']],
        )
        updated += max(cursor.rowcount, 0)
    return updated


def _apply_prd_sdi_known_tir_repairs(cursor, staging_id, official_error):
    counts = {}
    for (commodity_code, origin), taric_code in PRD_SDI_KNOWN_TARIC_BY_COMMODITY_ORIGIN.items():
        cursor.execute(
            """
            UPDATE [STG].[BKD_SDI_GoodsItems]
               SET taric_code = ?,
                   updated_at = SYSUTCDATETIME()
             WHERE ClientCode = ?
               AND stg_sdi_id = ?
               AND REPLACE(COALESCE(commodity_code, ''), ' ', '') = ?
               AND UPPER(COALESCE(country_of_origin, '')) = ?
               AND NULLIF(LTRIM(RTRIM(COALESCE(taric_code, ''))), '') IS NULL
            """,
            [taric_code, S, staging_id, commodity_code, origin],
        )
        counts[f'taric_{commodity_code}_{origin}'] = max(cursor.rowcount, 0)

    if 'Missing Supplementary Unit' in official_error:
        cursor.execute(
            """
            UPDATE [STG].[BKD_SDI_GoodsItems]
               SET supplementary_units = TRY_CONVERT(DECIMAL(16,3), number_of_individual_pieces),
                   updated_at = SYSUTCDATETIME()
             WHERE ClientCode = ?
               AND stg_sdi_id = ?
               AND NULLIF(LTRIM(RTRIM(COALESCE(CONVERT(NVARCHAR(80), supplementary_units), ''))), '') IS NULL
               AND NULLIF(TRY_CONVERT(DECIMAL(16,3), number_of_individual_pieces), 0) IS NOT NULL
            """,
            [S, staging_id],
        )
        counts['supplementary_units_from_pieces'] = max(cursor.rowcount, 0)

    if 'Missing Item Value' in official_error:
        cursor.execute(
            """
            UPDATE [STG].[BKD_SDI_GoodsItems]
               SET item_invoice_amount = COALESCE(
                       NULLIF(TRY_CONVERT(DECIMAL(16,2), line_amount_excl_vat), 0),
                       NULLIF(TRY_CONVERT(DECIMAL(16,2), source_amount), 0),
                       NULLIF(TRY_CONVERT(DECIMAL(16,2), customs_value), 0),
                       NULLIF(
                           TRY_CONVERT(DECIMAL(16,2), unit_price_excl_vat)
                           * TRY_CONVERT(DECIMAL(16,2), number_of_individual_pieces),
                           0
                       )
                   ),
                   customs_value = COALESCE(
                       NULLIF(TRY_CONVERT(DECIMAL(16,2), customs_value), 0),
                       NULLIF(TRY_CONVERT(DECIMAL(16,2), line_amount_excl_vat), 0),
                       NULLIF(TRY_CONVERT(DECIMAL(16,2), source_amount), 0),
                       NULLIF(
                           TRY_CONVERT(DECIMAL(16,2), unit_price_excl_vat)
                           * TRY_CONVERT(DECIMAL(16,2), number_of_individual_pieces),
                           0
                       )
                   ),
                   updated_at = SYSUTCDATETIME()
             WHERE ClientCode = ?
               AND stg_sdi_id = ?
               AND COALESCE(
                       NULLIF(TRY_CONVERT(DECIMAL(16,2), item_invoice_amount), 0),
                       NULLIF(TRY_CONVERT(DECIMAL(16,2), customs_value), 0)
                   ) IS NULL
               AND COALESCE(
                       NULLIF(TRY_CONVERT(DECIMAL(16,2), line_amount_excl_vat), 0),
                       NULLIF(TRY_CONVERT(DECIMAL(16,2), source_amount), 0),
                       NULLIF(
                           TRY_CONVERT(DECIMAL(16,2), unit_price_excl_vat)
                           * TRY_CONVERT(DECIMAL(16,2), number_of_individual_pieces),
                           0
                       )
                   ) IS NOT NULL
            """,
            [S, staging_id],
        )
        counts['item_value_from_source'] = max(cursor.rowcount, 0)

    if (
        'Missing Document Code' in official_error
        or 'CDS12068' in official_error
        or 'AdditionalDocument' in official_error
    ):
        counts['historical_documents'] = _apply_prd_sdi_historical_documents(cursor, staging_id)

    return counts


def _known_tir_repairs_total(repairs):
    return sum(int(value or 0) for value in (repairs or {}).values())


def _repair_known_tir_single(sd, *, api=None):
    sup_ref = (sd or {}).get('sup_dec_number')
    if not sup_ref:
        return {'stage': 'skipped_missing_sup_ref', 'errors': ['Missing SUP reference']}

    api = api or build_cfg_client()
    read_result = api.read_sdi(sup_ref, fields=list(PRD_SDI_SUBMIT_READ_FIELDS))
    detail = _tss_response_payload(read_result)
    official_status = _first_text(_tss_response_value(detail, 'status'))
    official_error = _first_text(_tss_response_value(detail, 'error_message'))
    official_status_key = _clean_ref(official_status)
    event = {
        'sup_ref': sup_ref,
        'stg_sdi_id': sd.get('staging_id'),
        'official_status': official_status,
        'official_error': official_error,
        'error_kind': _prd_sdi_tir_error_kind(official_error),
    }

    with db_cursor() as cursor:
        _log_prd_sdi_tss_exchange(
            cursor,
            sd,
            call_type='READ_SDI_TIR_REPAIR',
            payload={'op_type': 'read', 'sup_dec_number': sup_ref, 'fields': list(PRD_SDI_SUBMIT_READ_FIELDS)},
            result=read_result,
        )

    if not _sdi_tss_result_ok(read_result):
        message = _sdi_tss_result_message(read_result)
        if _tss_requires_customs_agent_role(message):
            event['stage'] = 'skipped_api_inaccessible'
            event['skip_reason'] = message
            event['errors'] = []
            return event
        event['stage'] = 'read_failed'
        event['errors'] = [message]
        return event

    if official_status_key in PRD_SDI_TERMINAL_OR_IN_FLIGHT_STATUSES:
        event['stage'] = 'sync_only'
        event['sync'] = sync_prd_sdi_from_tss(sd['staging_id'], api=api)
        event['errors'] = []
        return event

    if official_status_key not in PRD_SDI_KNOWN_TIR_STATUSES:
        event['stage'] = 'skipped_not_tir'
        event['sync'] = sync_prd_sdi_from_tss(sd['staging_id'], api=api)
        event['errors'] = [f'TSS status is {official_status or "unknown"}, not Trader Input Required']
        return event

    duplicate_refs = _prd_sdi_has_active_transport_duplicate(sd)
    if duplicate_refs:
        event['stage'] = 'skipped_duplicate_transport'
        event['errors'] = [f'{sup_ref}: active duplicate SUP(s) for same transport document: {", ".join(duplicate_refs)}']
        return event

    goods = _load_prd_sdi_goods(sd['staging_id'])
    mapping_issue = _prd_sdi_goods_mapping_issue(sd, goods)
    if mapping_issue:
        event['stage'] = 'skipped_goods_mapping_duplicate'
        event['errors'] = [mapping_issue]
        return event

    if event['error_kind'] == 'OTHER':
        event['stage'] = 'skipped_unknown_tss_error'
        event['errors'] = [official_error or 'TSS did not return a known repairable error']
        return event

    with db_cursor() as cursor:
        repairs = _apply_prd_sdi_known_tir_repairs(cursor, sd['staging_id'], official_error)
    event['repairs'] = repairs

    if event['error_kind'] == 'ITEM_VALUE' and not repairs.get('item_value_from_source'):
        event['stage'] = 'skipped_missing_real_item_value'
        event['errors'] = ['TSS requires an item value, but Fusion has no non-zero source/invoice/customs value to send.']
        event['sync'] = sync_prd_sdi_from_tss(sd['staging_id'], api=api)
        return event

    if event['error_kind'] == 'SUPPLEMENTARY_UNITS' and not repairs.get('supplementary_units_from_pieces'):
        event['stage'] = 'skipped_missing_real_supplementary_units'
        event['errors'] = ['TSS requires supplementary units, but Fusion has no non-zero pieces value to send.']
        event['sync'] = sync_prd_sdi_from_tss(sd['staging_id'], api=api)
        return event

    if event['error_kind'] in {'DOCUMENTS', 'TARIC_ADDITIONAL_CODE'} and not _known_tir_repairs_total(repairs):
        event['stage'] = 'skipped_no_known_repair_data'
        event['errors'] = ['TSS error is recognised, but Fusion has no safe masterdata/history repair to apply.']
        event['sync'] = sync_prd_sdi_from_tss(sd['staging_id'], api=api)
        return event

    repaired_sd = _load_prd_sdi_header_by_id(sd['staging_id'])
    repaired_goods = _load_prd_sdi_goods(sd['staging_id'])
    summary, errors = _submit_single_prd_sdi_to_tss(repaired_sd, repaired_goods, api=api)
    event['stage'] = 'submitted_attempt'
    event['submit_summary'] = summary
    event['errors'] = errors
    event['sync'] = sync_prd_sdi_from_tss(sd['staging_id'], api=api)
    return event


def _repair_known_tir_sdis(rows, *, api=None, limit=25):
    api = api or build_cfg_client()
    selected = [
        row for row in rows or []
        if row.get('sup_dec_number')
        and row.get('filter_status') != 'API_INACCESSIBLE'
        and OFFICIAL_TSS_SUP_RE.match(str(row.get('sup_dec_number') or ''))
    ][:max(1, min(int(limit or 25), 100))]
    events = []
    for row in selected:
        try:
            events.append(_repair_known_tir_single(row, api=api))
        except Exception as exc:
            logger.exception("Known TIR repair failed for %s", row.get('sup_dec_number'))
            events.append({
                'stage': 'exception',
                'sup_ref': row.get('sup_dec_number'),
                'stg_sdi_id': row.get('staging_id'),
                'errors': [str(exc)],
            })

    counts = Counter(event.get('stage') or 'unknown' for event in events)
    submitted = sum(
        1
        for event in events
        if (event.get('submit_summary') or {}).get('submitted')
    )
    errors = []
    for event in events:
        errors.extend(str(item) for item in (event.get('errors') or []) if str(item or '').strip())
    return {
        'tenant_code': S,
        'candidates': len(selected),
        'processed': len(events),
        'submitted': submitted,
        'repaired': counts.get('submitted_attempt', 0),
        'skipped_api_inaccessible': counts.get('skipped_api_inaccessible', 0),
        'skipped': len(events) - counts.get('submitted_attempt', 0),
        'counts': dict(counts),
        'errors': errors[:30],
        'events': events,
    }


@supdec_bp.route('/sync-tss', methods=['POST'])
def sync_tss_list():
    redirect_args = _supdec_list_redirect_args_from_form()
    status_filter = canonical_filter_status(request.form.get('status') or 'ALL')
    search_query = _clean_sdi_text(request.form.get('q'))
    limit = min(_safe_positive_int(request.form.get('limit'), 25), 100)

    try:
        rows = _load_prd_sdi_headers(search_query)
        if status_filter != 'ALL':
            rows = [
                row for row in rows
                if (row.get('filter_status') or _prd_sdi_filter_status(row)) == status_filter
            ]
        skipped_api_inaccessible = sum(
            1
            for row in rows
            if row.get('sup_dec_number')
            and (
                row.get('filter_status') == 'API_INACCESSIBLE'
                or not OFFICIAL_TSS_SUP_RE.match(str(row.get('sup_dec_number') or ''))
            )
        )
        candidates = [
            row
            for row in rows
            if row.get('sup_dec_number')
            and row.get('filter_status') != 'API_INACCESSIBLE'
            and OFFICIAL_TSS_SUP_RE.match(str(row.get('sup_dec_number') or ''))
        ][:limit]
        if not candidates:
            suffix = (
                f' Skipped {skipped_api_inaccessible} non-official or unreadable SUP reference(s).'
                if skipped_api_inaccessible
                else ''
            )
            flash(f'No official TSS SUP references are available to sync for this filter.{suffix}', 'info')
            return redirect(url_for('supdec.list_view', **redirect_args))

        api = build_cfg_client()
        synced = 0
        failed = 0
        first_error = ''
        for row in candidates:
            result = sync_prd_sdi_from_tss(row['staging_id'], api=api)
            if result.get('ok'):
                synced += 1
            else:
                message = result.get('message') or result.get('stage') or 'unknown error'
                failed += 1
                if not first_error:
                    first_error = _sdi_sync_failure_message(message)

        if failed:
            parts = [f'SDI TSS sync finished: {synced} synced']
            parts.append(f'{failed} read failed. First issue: {first_error}')
            if skipped_api_inaccessible:
                parts.append(f'skipped {skipped_api_inaccessible} non-official or unreadable SUP reference(s)')
            flash(', '.join(parts) + '.', 'warning')
        else:
            suffix = (
                f' Skipped {skipped_api_inaccessible} non-official or unreadable SUP reference(s).'
                if skipped_api_inaccessible
                else ''
            )
            flash(f'SDI TSS sync finished: {synced} synced.{suffix}', 'success')
    except Exception as exc:
        logger.exception('Manual SDI list sync failed')
        flash(f'SDI TSS sync failed: {exc}', 'danger')
    return redirect(url_for('supdec.list_view', **redirect_args))


@supdec_bp.route('/repair-known-tir', methods=['POST'])
def repair_known_tir():
    redirect_args = _supdec_list_redirect_args_from_form()
    status_filter = canonical_filter_status(request.form.get('status') or 'TRADER_INPUT_REQUIRED')
    search_query = _clean_sdi_text(request.form.get('q'))
    limit = min(_safe_positive_int(request.form.get('limit'), 25), 100)

    try:
        rows = _load_prd_sdi_headers(search_query)
        if status_filter != 'ALL':
            rows = [
                row for row in rows
                if (row.get('filter_status') or _prd_sdi_filter_status(row)) == status_filter
            ]
        else:
            rows = [
                row for row in rows
                if _clean_ref(row.get('tss_status') or row.get('status')) in PRD_SDI_KNOWN_TIR_STATUSES
            ]

        result = _repair_known_tir_sdis(rows, api=build_cfg_client(), limit=limit)
        errors = result.get('errors') or []
        log_status = (
            'SUBMITTED' if result.get('submitted') else
            'BLOCKED' if errors else
            'NOOP'
        )
        log_id = _log_manual_sdi_submit_event(
            summary=result,
            errors=errors,
            status=log_status,
            call_type='REPAIR_KNOWN_TIR_SDI',
            route_url=url_for('supdec.repair_known_tir'),
        )

        counts = result.get('counts') or {}
        if result.get('submitted'):
            _flash_manual_sdi_submit(
                (
                    f"Known TIR repair finished: {result.get('submitted')} submitted, "
                    f"{result.get('skipped')} skipped. Counts={counts}."
                ),
                'success' if not errors else 'warning',
                log_id=log_id,
            )
        elif result.get('processed'):
            _flash_manual_sdi_submit(
                f"Known TIR repair ran but nothing was submitted. Skipped={result.get('skipped')}. Counts={counts}.",
                'warning',
                log_id=log_id,
            )
        else:
            _flash_manual_sdi_submit(
                'No TIR SDIs with official SUP references were available for known repair.',
                'info',
                log_id=log_id,
            )
    except Exception as exc:
        logger.exception('Known TIR repair failed')
        log_id = _log_manual_sdi_submit_event(
            summary={'blocked': 1, 'submitted': 0, 'manual': True},
            errors=[str(exc)],
            status='FAILED',
            call_type='REPAIR_KNOWN_TIR_SDI_FAILED',
            route_url=url_for('supdec.repair_known_tir'),
        )
        _flash_manual_sdi_submit(f'Known TIR repair failed: {exc}', 'danger', log_id=log_id)
    return redirect(url_for('supdec.list_view', **redirect_args))


def _sdi_autosubmit_result_summary(result):
    return {
        'tenant_code': getattr(result, 'tenant_code', S),
        'dry_run': getattr(result, 'dry_run', None),
        'submit_requested': getattr(result, 'submit_requested', None),
        'submit_enabled': getattr(result, 'submit_enabled', None),
        'candidates': getattr(result, 'candidates', 0),
        'discovered': getattr(result, 'discovered', 0),
        'staged_headers': getattr(result, 'staged_headers', 0),
        'staged_goods': getattr(result, 'staged_goods', 0),
        'ready': getattr(result, 'ready', 0),
        'blocked': getattr(result, 'blocked', 0),
        'submitted': getattr(result, 'submitted', 0),
    }


def _sdi_tss_result_ok(result):
    if isinstance(result, dict):
        if result.get('success') is False:
            return False
        statuses = [
            str(result.get('status') or '').strip().lower(),
            str(_sdi_tss_response_payload(result).get('status') or '').strip().lower(),
        ]
        if any(status in {'error', 'failure', 'failed'} for status in statuses):
            return False
        http_status = result.get('http_status')
        if http_status and int(http_status) >= 400:
            return False
    return True


def _sdi_json_dict(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sdi_unwrap_tss_result(value):
    payload = _sdi_json_dict(value)
    while isinstance(payload.get('result'), dict):
        payload = payload['result']
    return payload


def _sdi_tss_response_payload(result):
    if not isinstance(result, dict):
        return {}
    for key in ('response', 'data'):
        payload = _sdi_unwrap_tss_result(result.get(key))
        if payload:
            return payload
    payload = _sdi_unwrap_tss_result(result.get('raw_response'))
    return payload if payload else {}


def _sdi_tss_result_message(result):
    if not isinstance(result, dict):
        return str(result or 'unknown TSS response')
    response = _sdi_tss_response_payload(result)
    message_payload = _sdi_unwrap_tss_result(result.get('message'))
    for source in (response, message_payload, result):
        for key in ('message', 'error_message', 'process_message'):
            value = source.get(key) if isinstance(source, dict) else None
            if value:
                return str(value)
    raw_payload = _sdi_unwrap_tss_result(result.get('raw_response'))
    for key in ('message', 'error_message', 'process_message'):
        value = raw_payload.get(key)
        if value:
            return str(value)
    for key in ('raw_response',):
        value = result.get(key)
        if value:
            return str(value)
    return 'unknown TSS response'


def _sdi_sync_failure_message(message):
    text = str(message or '').strip()
    if 'customs agent role required' in text.lower():
        return 'TSS read did not return current SDI data; see Technical logs for the raw API response'
    return text or 'unknown TSS response'


def _tss_response_payload(result):
    if not isinstance(result, dict):
        return {}
    response = result.get('response', result.get('data', result))
    if isinstance(response, dict) and isinstance(response.get('result'), dict):
        response = response['result']
    return response if isinstance(response, dict) else {}


def _tss_response_value(payload, key):
    value = (payload or {}).get(key)
    if isinstance(value, dict):
        for nested_key in ('value', 'display_value', 'displayValue', 'label', 'name'):
            nested = value.get(nested_key)
            if nested not in (None, ''):
                return nested
        return None
    return value


def _empty_submit_value(value):
    if value in (None, ''):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _merge_prd_sdi_tss_detail_for_submit(sd, detail):
    merged = dict(sd or {})
    for field_name in PRD_SDI_SUBMIT_READ_FIELDS:
        value = _tss_response_value(detail, field_name)
        if _empty_submit_value(value):
            continue
        if _empty_submit_value(merged.get(field_name)):
            merged[field_name] = value
    return merged


def _log_prd_sdi_tss_exchange(cursor, sd, *, call_type, payload, result):
    try:
        from app.data_model import insert_tss_api_exchange

        ok = _sdi_tss_result_ok(result)
        insert_tss_api_exchange(
            cursor,
            schema_name=S,
            legacy_api_call_log_id=None,
            call_type=call_type,
            staging_id=(sd or {}).get('staging_id'),
            http_method='POST',
            url=(result or {}).get('url') if isinstance(result, dict) else '',
            request_payload=(result or {}).get('request_payload') or payload if isinstance(result, dict) else payload,
            http_status=(result or {}).get('http_status') if isinstance(result, dict) else None,
            response_status=(
                _sdi_tss_response_payload(result).get('status')
                or (result or {}).get('status')
            ) if isinstance(result, dict) else None,
            response_message=_sdi_tss_result_message(result),
            response_json=(result or {}).get('raw_response') or (result or {}).get('response') if isinstance(result, dict) else result,
            duration_ms=(result or {}).get('duration_ms') if isinstance(result, dict) else None,
            error_detail=None if ok else _sdi_tss_result_message(result),
        )
    except Exception:
        logger.exception("Failed to log SDI TSS exchange for %s", (sd or {}).get('sup_dec_number'))


def _read_prd_sdi_after_submit(api, sup_ref, *, attempts=5, delay_seconds=5, initial_delay_seconds=None):
    attempts = max(1, int(attempts or 1))
    if initial_delay_seconds is None:
        initial_delay_seconds = delay_seconds
    if initial_delay_seconds and initial_delay_seconds > 0:
        time.sleep(initial_delay_seconds)
    result = {}
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay_seconds)
        result = api.read_sdi(sup_ref, fields=list(PRD_SDI_SUBMIT_READ_FIELDS))
        detail = _tss_response_payload(result)
        status_key = _clean_ref(_first_text(_tss_response_value(detail, 'status')))
        if status_key and status_key != 'DRAFT':
            break
    return result


def _mark_prd_sdi_submit_state(cursor, sd, *, status, errors, tss_status=None):
    api_error_text = _sdi_api_error_text(errors)
    values = {
        'sub_status': status,
        'tss_status': tss_status,
        'validation_errors_json': _sdi_json_dump(errors),
        'auto_submit_error': api_error_text,
        'auto_submit_enabled': 1,
        'last_autosubmit_attempt_at': _sql_now,
        'submitted_at': _sql_now if status == 'SUBMITTED' else None,
        'completed_at': _sql_now if status == 'SUBMITTED' else None,
        'last_sub_status_change': _sql_now,
        'updated_at': _sql_now,
    }
    values = _sdi_existing_values('STG', 'BKD_SDI_Headers', values)
    if not values:
        return
    set_sql, params = _sdi_set_clause(values)
    params.extend([S, sd['staging_id']])
    cursor.execute(
        f"""
        UPDATE [STG].[BKD_SDI_Headers]
           SET {set_sql}
         WHERE ClientCode = ?
           AND stg_sdi_id = ?
        """,
        params,
    )


def _mark_prd_sdi_goods_submit_state(cursor, item, *, errors):
    values = {
        'sub_status': 'PENDING_REVIEW' if errors else 'VALIDATED',
        'sdi_validation_errors_json': _sdi_json_dump(errors),
        'sdi_auto_submit_enabled': 1,
        'last_sub_status_change': _sql_now,
        'sdi_ready_at': _sql_now if not errors else None,
        'updated_at': _sql_now,
    }
    values = _sdi_existing_values('STG', 'BKD_SDI_GoodsItems', values)
    if not values:
        return
    set_sql, params = _sdi_set_clause(values)
    params.extend([S, item['staging_id']])
    cursor.execute(
        f"""
        UPDATE [STG].[BKD_SDI_GoodsItems]
           SET {set_sql}
         WHERE ClientCode = ?
           AND stg_sdi_item_id = ?
        """,
        params,
    )


def _mark_prd_tss_sdi_submitted(cursor, sd):
    sup_ref = sd.get('sup_dec_number')
    if not sup_ref:
        return
    _mark_prd_tss_sdi_status(cursor, sd, 'SUBMITTED')


def _mark_prd_tss_sdi_status(cursor, sd, status):
    sup_ref = (sd or {}).get('sup_dec_number')
    if not sup_ref:
        return
    cursor.execute(
        """
        UPDATE [TSS].[BKD_SDI_Headers]
           SET TssStatus = ?,
               LastSyncedAt = SYSUTCDATETIME(),
               UpdatedAt = SYSUTCDATETIME()
         WHERE ClientCode = ?
           AND SupDecNumber = ?
        """,
        [status or 'UNKNOWN', S, sup_ref],
    )


def _prd_sdi_status_from_detail(detail, result=None):
    return _first_text(
        _tss_response_value(detail, 'status'),
        _tss_response_value(detail, 'state'),
        (result or {}).get('status') if isinstance(result, dict) else '',
    )


def _prd_sdi_header_updates_from_detail(detail, result=None):
    detail = detail or {}
    status = _prd_sdi_status_from_detail(detail, result)
    submission_due_date = _first_text(
        _tss_response_value(detail, 'submission_due_date'),
        _tss_response_value(detail, 'submissionDueDate'),
    )
    arrival_date_time = _first_text(
        _tss_response_value(detail, 'arrival_date_time'),
        _tss_response_value(detail, 'arrivalDateTime'),
    )
    values = {
        'tss_status': status or None,
        'tss_movement_reference_number': _first_text(
            _tss_response_value(detail, 'movement_reference_number'),
            _tss_response_value(detail, 'movementReferenceNumber'),
            _tss_response_value(detail, 'mrn'),
        ) or None,
        'tss_submission_due_date': _coerce_template_date(submission_due_date) or submission_due_date or None,
        'tss_sfd_consignment_ref': _first_text(
            _tss_response_value(detail, 'sfd_number'),
            _tss_response_value(detail, 'sfd_reference'),
            _tss_response_value(detail, 'sfdReference'),
            _tss_response_value(detail, 'parent'),
            _tss_response_value(detail, 'u_parent'),
        ) or None,
        'updated_at': _sql_now,
    }
    if arrival_date_time:
        values['arrival_date_time'] = _coerce_template_datetime(arrival_date_time) or arrival_date_time
    return {key: value for key, value in values.items() if value is not None}


def _apply_prd_sdi_header_sync(cursor, sd, detail, result=None):
    values = _sdi_existing_values('STG', 'BKD_SDI_Headers', _prd_sdi_header_updates_from_detail(detail, result))
    status = _prd_sdi_status_from_detail(detail, result)
    error_message = _first_text(_tss_response_value(detail, 'error_message'))
    status_key = _clean_ref(status)
    if status_key:
        state_values = {
            'sub_status': status_key,
            'validation_errors_json': _sdi_json_dump([error_message]) if error_message else None,
            'auto_submit_error': error_message[:2000] if error_message else None,
            'last_sub_status_change': _sql_now,
        }
        if status_key in {'PROCESSING', 'SUBMITTED', 'PENDING PAYMENT', 'CLOSED', 'COMPLETED'} and not error_message:
            state_values['sdi_ready_at'] = _sql_now
        values.update(_sdi_existing_values('STG', 'BKD_SDI_Headers', state_values))
    if values:
        set_sql, params = _sdi_set_clause(values)
        params.extend([S, sd['staging_id']])
        cursor.execute(
            f"""
            UPDATE [STG].[BKD_SDI_Headers]
               SET {set_sql}
             WHERE ClientCode = ?
               AND stg_sdi_id = ?
            """,
            params,
        )

    if status:
        _mark_prd_tss_sdi_status(cursor, sd, status)


def _upsert_prd_sdi_tss_header_from_detail(cursor, sd, detail, result=None):
    try:
        from app.ingestion.sdi_autosubmit import _upsert_tss_sdi_header

        record = dict(sd or {})
        record.update({
            'sup_dec_number': sd.get('sup_dec_number'),
            'sfd_reference': _first_text(
                _tss_response_value(detail, 'sfd_number'),
                _tss_response_value(detail, 'sfd_reference'),
                sd.get('sfd_reference'),
            ),
            'tss_status': _prd_sdi_status_from_detail(detail, result) or sd.get('tss_status'),
            'tss_movement_reference_number': _first_text(
                _tss_response_value(detail, 'movement_reference_number'),
                _tss_response_value(detail, 'movementReferenceNumber'),
                _tss_response_value(detail, 'mrn'),
                sd.get('tss_movement_reference_number'),
            ),
            'tss_submission_due_date': _first_text(
                _tss_response_value(detail, 'submission_due_date'),
                _tss_response_value(detail, 'submissionDueDate'),
                sd.get('submission_due_date'),
            ),
            'arrival_date_time': _first_text(
                _tss_response_value(detail, 'arrival_date_time'),
                _tss_response_value(detail, 'arrivalDateTime'),
                sd.get('arrival_date_time'),
            ),
        })
        _upsert_tss_sdi_header(cursor, S, record, raw_payload=detail or result or {})
    except Exception:
        logger.exception("Failed to upsert TSS SDI header mirror for %s", sd.get('sup_dec_number'))


def _sync_prd_sdi_goods_from_tss(cursor, sd, api):
    sup_ref = sd.get('sup_dec_number')
    if not sup_ref:
        return 0
    try:
        from app.ingestion.sdi_autosubmit import (
            _goods_id,
            _goods_item_number,
            _item_value,
            _upsert_tss_sdi_goods,
        )
    except Exception:
        logger.exception("Failed to import SDI autosubmit mirror helpers")
        return 0

    try:
        goods_items = [item for item in (api.lookup_sdi_goods(sup_ref) or []) if isinstance(item, dict)]
    except Exception:
        logger.exception("Manual SDI sync could not read goods for %s", sup_ref)
        return 0

    synced = 0
    for item in goods_items:
        goods_id = _goods_id(item)
        if not goods_id:
            continue
        detail_item = _read_prd_sdi_goods_detail_for_sync(api, goods_id, item)
        _upsert_tss_sdi_goods(
            cursor,
            S,
            detail_item,
            sup_ref=sup_ref,
            sfd_ref=sd.get('sfd_reference') or _first_text(_item_value(detail_item, 'sfd_number', 'sfd_reference')),
        )
        goods_status = _normalize_tss_status(_first_text(_item_value(detail_item, 'status', 'state')))
        backfilled = _backfill_prd_sdi_goods_id_from_tss_item(
            cursor,
            sd,
            goods_id=goods_id,
            item_number=_goods_item_number(detail_item),
            goods_status=goods_status,
        )
        if not backfilled:
            _backfill_prd_sdi_goods_id_from_tss_detail(
                cursor,
                sd,
                goods_id=goods_id,
                detail=detail_item,
                goods_status=goods_status,
            )
        goods_update_values = {'updated_at': _sql_now}
        if goods_status:
            goods_update_values['sub_status'] = goods_status
        goods_values = _sdi_existing_values('STG', 'BKD_SDI_GoodsItems', goods_update_values)
        if goods_values:
            set_sql, params = _sdi_set_clause(goods_values)
            params.extend([S, goods_id])
            cursor.execute(
                f"""
                UPDATE [STG].[BKD_SDI_GoodsItems]
                   SET {set_sql}
                 WHERE ClientCode = ?
                   AND tss_goods_id = ?
                """,
                params,
            )
        synced += 1
    return synced


def _read_prd_sdi_goods_detail_for_sync(api, goods_id, item):
    if not goods_id or not hasattr(api, 'read_goods'):
        return dict(item or {})
    try:
        result = api.read_goods(goods_id, list(PRD_SDI_GOODS_SYNC_READ_FIELDS))
        detail = _tss_response_payload(result)
    except Exception:
        logger.debug("Could not read SDI goods detail for %s", goods_id, exc_info=True)
        return dict(item or {})
    merged = dict(item or {})
    if isinstance(detail, dict):
        merged.update(detail)
    merged.setdefault('goods_id', goods_id)
    merged.setdefault('reference', goods_id)
    return merged


def _backfill_prd_sdi_goods_id_from_tss_item(cursor, sd, *, goods_id, item_number, goods_status=''):
    """Attach official TSS goods IDs to one local PRD SDI goods row.

    This deliberately matches by explicit TSS item number only. It avoids the
    older unsafe behavior of falling back to the first source goods row when TSS
    identity is incomplete.
    """
    if not sd or not goods_id:
        return 0
    try:
        item_seq = int(str(item_number or '').strip())
    except (TypeError, ValueError):
        return 0

    cursor.execute(
        """
        SELECT COUNT(1)
        FROM [STG].[BKD_SDI_GoodsItems]
        WHERE ClientCode = ?
          AND stg_sdi_id = ?
          AND TRY_CONVERT(INT, item_seq) = ?
          AND NULLIF(LTRIM(RTRIM(COALESCE(tss_goods_id, ''))), '') IS NULL
        """,
        [S, sd.get('staging_id'), item_seq],
    )
    row = cursor.fetchone()
    if not row or int(row[0] or 0) != 1:
        return 0
    if not _prepare_prd_sdi_goods_id_for_active_row(cursor, sd, goods_id):
        return 0

    update_values = {
        'tss_goods_id': goods_id,
        'tss_sup_dec_number': sd.get('sup_dec_number'),
        'updated_at': _sql_now,
    }
    if goods_status:
        update_values['sub_status'] = goods_status
    values = _sdi_existing_values('STG', 'BKD_SDI_GoodsItems', update_values)
    if not values:
        return 0
    set_sql, params = _sdi_set_clause(values)
    params.extend([S, sd.get('staging_id'), item_seq])
    cursor.execute(
        f"""
        UPDATE [STG].[BKD_SDI_GoodsItems]
           SET {set_sql}
         WHERE ClientCode = ?
           AND stg_sdi_id = ?
           AND TRY_CONVERT(INT, item_seq) = ?
           AND NULLIF(LTRIM(RTRIM(COALESCE(tss_goods_id, ''))), '') IS NULL
        """,
        params,
    )
    return max(cursor.rowcount or 0, 0)


def _backfill_prd_sdi_goods_id_from_tss_detail(cursor, sd, *, goods_id, detail, goods_status=''):
    if not sd or not goods_id or not isinstance(detail, dict):
        return 0

    remote = _sdi_goods_sync_fingerprint(detail)
    if not _sdi_goods_sync_has_identity(remote):
        return 0

    cursor.execute(
        """
        SELECT
            stg_sdi_item_id,
            goods_description,
            commodity_code,
            country_of_origin,
            gross_mass_kg,
            net_mass_kg,
            number_of_packages,
            type_of_packages,
            item_invoice_amount,
            customs_value,
            line_amount_excl_vat,
            item_invoice_currency
        FROM [STG].[BKD_SDI_GoodsItems]
        WHERE ClientCode = ?
          AND stg_sdi_id = ?
          AND NULLIF(LTRIM(RTRIM(COALESCE(tss_goods_id, ''))), '') IS NULL
        """,
        [S, sd.get('staging_id')],
    )
    candidates = []
    for row in cursor.fetchall():
        local = {
            'stg_sdi_item_id': row[0],
            'goods_description': row[1],
            'commodity_code': row[2],
            'country_of_origin': row[3],
            'gross_mass_kg': row[4],
            'net_mass_kg': row[5],
            'number_of_packages': row[6],
            'type_of_packages': row[7],
            'item_invoice_amount': row[8],
            'customs_value': row[9],
            'line_amount_excl_vat': row[10],
            'item_invoice_currency': row[11],
        }
        if _sdi_goods_sync_detail_matches(remote, local):
            candidates.append(local)

    if len(candidates) != 1:
        return 0
    if not _prepare_prd_sdi_goods_id_for_active_row(cursor, sd, goods_id):
        return 0

    update_values = {
        'tss_goods_id': goods_id,
        'tss_sup_dec_number': sd.get('sup_dec_number'),
        'updated_at': _sql_now,
    }
    if goods_status:
        update_values['sub_status'] = goods_status
    values = _sdi_existing_values('STG', 'BKD_SDI_GoodsItems', update_values)
    if not values:
        return 0
    set_sql, params = _sdi_set_clause(values)
    params.extend([S, candidates[0]['stg_sdi_item_id']])
    cursor.execute(
        f"""
        UPDATE [STG].[BKD_SDI_GoodsItems]
           SET {set_sql}
         WHERE ClientCode = ?
           AND stg_sdi_item_id = ?
           AND NULLIF(LTRIM(RTRIM(COALESCE(tss_goods_id, ''))), '') IS NULL
        """,
        params,
    )
    return max(cursor.rowcount or 0, 0)


def _prepare_prd_sdi_goods_id_for_active_row(cursor, sd, goods_id):
    cursor.execute(
        """
        SELECT TOP 2 stg_sdi_item_id, stg_sdi_id, sub_status
        FROM [STG].[BKD_SDI_GoodsItems]
        WHERE ClientCode = ?
          AND tss_goods_id = ?
        """,
        [S, goods_id],
    )
    rows = cursor.fetchall()
    if not rows:
        return True
    if len(rows) == 1:
        row = rows[0]
        status = _clean_ref(row[2])
        if int(row[1] or 0) == int((sd or {}).get('staging_id') or 0) and status in {
            'TSS_REMOVED',
            'DELETED',
            'CANCELLED',
            'CANCELED',
        }:
            cursor.execute(
                """
                UPDATE [STG].[BKD_SDI_GoodsItems]
                   SET tss_goods_id = NULL,
                       updated_at = SYSUTCDATETIME()
                 WHERE ClientCode = ?
                   AND stg_sdi_item_id = ?
                   AND tss_goods_id = ?
                """,
                [S, row[0], goods_id],
            )
            return True
    return False


def _sdi_goods_sync_fingerprint(item):
    return {
        'goods_description': _sync_text(item.get('goods_description') or item.get('goodsDescription')),
        'commodity_code': _sync_compact(item.get('commodity_code') or item.get('commodityCode')),
        'country_of_origin': _sync_text(item.get('country_of_origin') or item.get('countryOfOrigin')),
        'gross_mass_kg': _sync_decimal(item.get('gross_mass_kg') or item.get('grossMassKg')),
        'net_mass_kg': _sync_decimal(item.get('net_mass_kg') or item.get('netMassKg')),
        'number_of_packages': _sync_decimal(item.get('number_of_packages') or item.get('numberOfPackages')),
        'type_of_packages': _sync_text(item.get('type_of_packages') or item.get('typeOfPackages')),
        'item_invoice_amount': _sync_decimal(item.get('item_invoice_amount') or item.get('itemInvoiceAmount')),
        'item_invoice_currency': _sync_text(item.get('item_invoice_currency') or item.get('itemInvoiceCurrency')),
    }


def _sdi_goods_sync_has_identity(fp):
    required = ('commodity_code', 'country_of_origin', 'gross_mass_kg', 'number_of_packages', 'item_invoice_amount')
    return all(fp.get(name) for name in required)


def _sdi_goods_sync_detail_matches(remote, local):
    local_desc = _sync_text(local.get('goods_description'))
    remote_desc = remote.get('goods_description')
    if remote_desc and local_desc and remote_desc not in local_desc and local_desc not in remote_desc:
        return False

    checks = {
        'commodity_code': _sync_compact(local.get('commodity_code')),
        'country_of_origin': _sync_text(local.get('country_of_origin')),
        'gross_mass_kg': _sync_decimal(local.get('gross_mass_kg')),
        'net_mass_kg': _sync_decimal(local.get('net_mass_kg')),
        'number_of_packages': _sync_decimal(local.get('number_of_packages')),
        'type_of_packages': _sync_text(local.get('type_of_packages')),
        'item_invoice_amount': _sync_decimal(
            local.get('item_invoice_amount') or local.get('customs_value') or local.get('line_amount_excl_vat')
        ),
        'item_invoice_currency': _sync_text(local.get('item_invoice_currency')),
    }
    for key, remote_value in remote.items():
        if key == 'goods_description' or not remote_value:
            continue
        if checks.get(key) != remote_value:
            return False
    return True


def _sync_text(value):
    return ' '.join(str(value or '').strip().upper().split())


def _sync_compact(value):
    return _sync_text(value).replace(' ', '')


def _sync_decimal(value):
    if value in (None, ''):
        return ''
    try:
        return str(Decimal(str(value).strip()).normalize())
    except Exception:
        return _sync_text(value)


def sync_prd_sdi_from_tss(sid, *, api=None):
    sd = _load_prd_sdi_header_by_id(sid)
    if not sd:
        return {'ok': False, 'stage': 'not_found', 'message': f'SDI #{sid} is not staged.'}
    sup_ref = sd.get('sup_dec_number')
    if not sup_ref:
        return {'ok': False, 'stage': 'missing_sup_ref', 'message': f'SDI #{sid} has no SUP reference yet.'}

    api = api or build_cfg_client()
    payload = {'op_type': 'read', 'sup_dec_number': sup_ref, 'fields': list(PRD_SDI_SUBMIT_READ_FIELDS)}
    try:
        result = api.read_sdi(sup_ref, fields=list(PRD_SDI_SUBMIT_READ_FIELDS))
    except Exception as exc:
        logger.exception("Manual SDI sync read failed for %s", sup_ref)
        return {
            'ok': False,
            'stage': 'read_exception',
            'message': str(exc),
            'sup_ref': sup_ref,
            'goods_synced': 0,
        }
    detail = _tss_response_payload(result)

    with db_cursor() as cursor:
        _log_prd_sdi_tss_exchange(
            cursor,
            sd,
            call_type='READ_SDI_MANUAL_SYNC',
            payload=payload,
            result=result,
        )
        if not _sdi_tss_result_ok(result):
            return {
                'ok': False,
                'stage': 'read_failed',
                'message': _sdi_tss_result_message(result),
                'sup_ref': sup_ref,
                'goods_synced': 0,
            }
        _apply_prd_sdi_header_sync(cursor, sd, detail, result)
        _upsert_prd_sdi_tss_header_from_detail(cursor, sd, detail, result)
        goods_synced = _sync_prd_sdi_goods_from_tss(cursor, sd, api)

    return {
        'ok': True,
        'stage': 'synced',
        'message': f'SDI {sup_ref} synced from TSS.',
        'sup_ref': sup_ref,
        'tss_status': _prd_sdi_status_from_detail(detail, result),
        'goods_synced': goods_synced,
    }


def _submit_single_prd_sdi_to_tss(sd, goods, *, api=None):
    summary = {
        'tenant_code': S,
        'candidates': 1,
        'discovered': 0,
        'staged_headers': 1,
        'staged_goods': len(goods or []),
        'ready': 0,
        'blocked': 0,
        'submitted': 0,
    }
    errors = []
    hard_errors = []
    sup_ref = sd.get('sup_dec_number')
    if not sup_ref:
        hard_errors.append(f"SDI #{sd.get('staging_id')}: missing SUP reference")

    mapping_issue = _prd_sdi_goods_mapping_issue(sd, goods)
    if mapping_issue:
        hard_errors.append(mapping_issue)

    goods_payloads = []
    for item in goods or []:
        if not item.get('tss_goods_id'):
            logger.info(
                "SDI %s goods item %s has no TSS goods id; skipping goods update and letting TSS submit respond",
                sup_ref,
                item.get('staging_id'),
            )
            continue
        payload_record = dict(item)
        payload_record.setdefault('transport_document_number', sd.get('transport_document_number'))
        payload_record.setdefault('source_transport_document_number', sd.get('transport_document_number'))
        if not payload_record.get('invoice_number'):
            payload_record['invoice_number'] = sd.get('transport_document_number')
        payload, payload_warnings = build_sdi_goods_update_payload_for_api_attempt(payload_record)
        goods_payloads.append((item, payload, payload_warnings))

    if hard_errors:
        summary['blocked'] = 1
        with db_cursor() as cursor:
            _mark_prd_sdi_submit_state(cursor, sd, status='PENDING_REVIEW', errors=hard_errors)
        return summary, hard_errors

    duplicate_refs = _prd_sdi_has_active_transport_duplicate(sd)
    if duplicate_refs:
        duplicate_error = (
            f"{sup_ref}: active duplicate SUP(s) with the same transport document "
            f"and same goods fingerprint: {', '.join(duplicate_refs)}"
        )
        summary['blocked'] = 1
        with db_cursor() as cursor:
            _mark_prd_sdi_submit_state(cursor, sd, status='PENDING_REVIEW', errors=[duplicate_error])
        return summary, [duplicate_error]

    summary['ready'] = 1
    api = api or build_cfg_client()
    read_result = api.read_sdi(sup_ref, fields=list(PRD_SDI_SUBMIT_READ_FIELDS))
    tss_detail = _tss_response_payload(read_result)
    header_record = _merge_prd_sdi_tss_detail_for_submit(sd, tss_detail)

    with db_cursor() as cursor:
        goods_update_errors = []
        for item, payload, _payload_warnings in goods_payloads:
            result = api.update_sdi_goods(sup_ref, item['tss_goods_id'], payload)
            _log_prd_sdi_tss_exchange(cursor, sd, call_type='UPDATE_SDI_GOODS', payload=payload, result=result)
            if not _sdi_tss_result_ok(result):
                item_error = f"{sup_ref} item {item.get('staging_id')}: TSS goods update failed: {_sdi_tss_result_message(result)}"
                goods_update_errors.append(item_error)
                _mark_prd_sdi_goods_submit_state(cursor, item, errors=[item_error])
            else:
                _mark_prd_sdi_goods_submit_state(cursor, item, errors=[])

        header_payload, _header_payload_warnings = build_sdi_update_payload_for_api_attempt(header_record)
        update_result = api.update_sdi(sup_ref, header_payload)
        _log_prd_sdi_tss_exchange(cursor, sd, call_type='UPDATE_SDI_HEADER', payload=header_payload, result=update_result)
        if not _sdi_tss_result_ok(update_result):
            errors.append(f"{sup_ref}: TSS SDI header update failed: {_sdi_tss_result_message(update_result)}")
            summary['blocked'] = 1
            _mark_prd_sdi_submit_state(cursor, sd, status='PENDING_REVIEW', errors=errors)
            return summary, errors

        submit_result = api.submit_sdi(sup_ref)
        _log_prd_sdi_tss_exchange(
            cursor,
            sd,
            call_type='SUBMIT_SDI',
            payload={'op_type': 'submit', 'sup_dec_number': sup_ref},
            result=submit_result,
        )
        if not _sdi_tss_result_ok(submit_result):
            errors.append(f"{sup_ref}: TSS SDI submit failed: {_sdi_tss_result_message(submit_result)}")
            summary['blocked'] = 1
            _mark_prd_sdi_submit_state(cursor, sd, status='PENDING_REVIEW', errors=errors)
            return summary, errors

        summary['submitted'] = 1
        final_result = _read_prd_sdi_after_submit(api, sup_ref)
        _log_prd_sdi_tss_exchange(
            cursor,
            sd,
            call_type='READ_SDI_AFTER_SUBMIT',
            payload={'op_type': 'read', 'sup_dec_number': sup_ref, 'fields': list(PRD_SDI_SUBMIT_READ_FIELDS)},
            result=final_result,
        )
        final_detail = _tss_response_payload(final_result)
        final_status = _first_text(_tss_response_value(final_detail, 'status'))
        final_error = _first_text(_tss_response_value(final_detail, 'error_message'))
        final_status_key = _clean_ref(final_status)
        _mark_prd_tss_sdi_status(cursor, sd, final_status or 'SUBMITTED')

        if final_error or final_status_key in PRD_SDI_POST_SUBMIT_REVIEW_STATUSES:
            message = final_error or f'TSS status after submit: {final_status}'
            errors.append(f'{sup_ref}: TSS accepted submit but returned review status: {message}')
            summary['blocked'] = 1
            _mark_prd_sdi_submit_state(cursor, sd, status='PENDING_REVIEW', errors=errors, tss_status=final_status)
        else:
            for item, _payload, _payload_warnings in goods_payloads:
                _mark_prd_sdi_goods_submit_state(cursor, item, errors=[])
            _mark_prd_sdi_submit_state(cursor, sd, status='SUBMITTED', errors=[], tss_status=final_status or 'SUBMITTED')
    return summary, errors


@supdec_bp.route('/submit-ready', methods=['POST'])
def submit_ready_autosubmit():
    redirect_args = _supdec_list_redirect_args_from_form()
    limit = _safe_positive_int(request.form.get('limit'), 500)
    try:
        from app.ingestion.sdi_autosubmit import run_sdi_autosubmit

        result = run_sdi_autosubmit(
            tenant_code=S,
            dry_run=False,
            submit=True,
            submit_enabled=True,
            limit=limit,
        )
        summary = _sdi_autosubmit_result_summary(result)
        errors = list(getattr(result, 'errors', []) or [])
        log_status = (
            'SUBMITTED' if result.submitted else
            'BLOCKED' if errors or result.blocked else
            'NOOP'
        )
        log_id = _log_manual_sdi_submit_event(
            summary=summary,
            errors=errors,
            status=log_status,
            call_type='SUBMIT_SDI_MANUAL_BULK',
            route_url=url_for('supdec.submit_ready_autosubmit'),
        )

        if result.submitted:
            _flash_manual_sdi_submit(
                f'SDI manual submit finished: {result.submitted} submitted, {result.ready} ready, {result.blocked} blocked.',
                'success' if not errors and not result.blocked else 'warning',
                log_id=log_id,
            )
        elif result.ready:
            _flash_manual_sdi_submit(
                f'SDI manual submit found {result.ready} ready item(s), but TSS did not accept a submit. Check TSS responses/logs.',
                'warning',
                log_id=log_id,
            )
        else:
            _flash_manual_sdi_submit(
                f'No ready SDIs were submitted. Candidates={result.candidates}, blocked={result.blocked}.',
                'warning',
                log_id=log_id,
            )
    except Exception as exc:
        logger.exception('Manual SDI autosubmit failed')
        log_id = _log_manual_sdi_submit_event(
            summary={'blocked': 1, 'submitted': 0, 'manual': True},
            errors=[str(exc)],
            status='FAILED',
            call_type='SUBMIT_SDI_MANUAL_BULK_FAILED',
            route_url=url_for('supdec.submit_ready_autosubmit'),
        )
        _flash_manual_sdi_submit(f'Manual SDI submit failed: {exc}', 'danger', log_id=log_id)
    return redirect(url_for('supdec.list_view', **redirect_args))


@supdec_bp.route('/<int:sid>/submit-ready', methods=['POST'])
def submit_ready_single(sid):
    sd = _load_prd_sdi_header_by_id(sid)
    if not sd:
        flash(f'SDI #{sid} is not staged in STG.BKD_SDI_Headers yet.', 'warning')
        return redirect(url_for('supdec.list_view'))

    goods = _load_prd_sdi_goods(sd['staging_id'])
    try:
        summary, errors = _submit_single_prd_sdi_to_tss(sd, goods)
        log_status = 'SUBMITTED' if summary.get('submitted') else 'BLOCKED' if errors else 'NOOP'
        log_id = _log_manual_sdi_submit_event(
            sid=sid,
            sup_ref=sd.get('sup_dec_number'),
            summary=summary,
            errors=errors,
            status=log_status,
            call_type='SUBMIT_SDI_MANUAL',
            route_url=url_for('supdec.submit_ready_single', sid=sid),
        )
        if errors:
            if summary.get('submitted'):
                _flash_manual_sdi_submit(
                    f"SDI {sd.get('sup_dec_number') or sid} was submitted to TSS, then returned for TSS review. Check the latest TSS response.",
                    'warning',
                    log_id=log_id,
                )
            else:
                _flash_manual_sdi_submit(
                    f"SDI {sd.get('sup_dec_number') or sid} was not submitted. Check the latest TSS response.",
                    'warning',
                    log_id=log_id,
                )
        elif summary.get('submitted'):
            _flash_manual_sdi_submit(
                f"SDI {sd.get('sup_dec_number') or sid} submitted to TSS.",
                'success',
                log_id=log_id,
            )
        else:
            _flash_manual_sdi_submit(
                f"SDI {sd.get('sup_dec_number') or sid} did not submit. Check technical logs.",
                'warning',
                log_id=log_id,
            )
    except Exception as exc:
        logger.exception('Manual single SDI submit failed for %s', sid)
        log_id = _log_manual_sdi_submit_event(
            sid=sid,
            sup_ref=sd.get('sup_dec_number'),
            summary={'candidates': 1, 'blocked': 1, 'submitted': 0},
            errors=[str(exc)],
            status='FAILED',
            call_type='SUBMIT_SDI_MANUAL_FAILED',
            route_url=url_for('supdec.submit_ready_single', sid=sid),
        )
        _flash_manual_sdi_submit(f'Manual SDI submit failed: {exc}', 'danger', log_id=log_id)
    return redirect(url_for('supdec.detail', sid=sid))


@supdec_bp.route('/bulk-delete-selected', methods=['POST'])
def bulk_delete_selected():
    selected_ids = _selected_supdec_ids_from_form()
    redirect_args = _supdec_list_redirect_args_from_form()
    if not selected_ids:
        flash('Select at least one SDI to delete.', 'warning')
        return redirect(url_for('supdec.list_view', **redirect_args))

    placeholders = ','.join('?' for _ in selected_ids)
    with db_cursor() as cursor:
        cursor.execute(
            f"""
            DELETE FROM [STG].[BKD_SDI_GoodsItems]
            WHERE ClientCode = ?
              AND stg_sdi_id IN ({placeholders})
            """,
            [S, *selected_ids],
        )
        deleted_goods = max(cursor.rowcount, 0)
        cursor.execute(
            f"""
            DELETE FROM [STG].[BKD_SDI_Headers]
            WHERE ClientCode = ?
              AND stg_sdi_id IN ({placeholders})
            """,
            [S, *selected_ids],
        )
        deleted_headers = max(cursor.rowcount, 0)

    flash(
        f'Deleted {deleted_headers} local SDI row(s) and {deleted_goods} staged goods row(s). TSS records were not cancelled.',
        'success' if deleted_headers else 'warning',
    )
    return redirect(url_for('supdec.list_view', **redirect_args))


@supdec_bp.route('/bulk-export-selected', methods=['POST'])
def bulk_export_selected():
    selected_ids = _selected_supdec_ids_from_form()
    redirect_args = _supdec_list_redirect_args_from_form()
    if not selected_ids:
        flash('Select at least one SDI to export.', 'warning')
        return redirect(url_for('supdec.list_view', **redirect_args))

    placeholders = ','.join('?' for _ in selected_ids)
    rows = query_all(
        f"""
        SELECT
            h.stg_sdi_id,
            h.tss_sup_dec_number,
            h.tss_sfd_consignment_ref,
            h.tss_consignment_ref,
            h.sub_status,
            h.tss_status,
            h.tss_submission_due_date,
            h.importer_eori,
            h.trader_reference,
            h.transport_document_number,
            h.validation_errors_json,
            h.auto_submit_error,
            h.sdi_ready_at,
            h.last_autosubmit_attempt_at
        FROM [STG].[BKD_SDI_Headers] h
        WHERE h.ClientCode = ?
          AND h.stg_sdi_id IN ({placeholders})
        ORDER BY h.stg_sdi_id
        """,
        [S, *selected_ids],
    )
    output = io.StringIO()
    fieldnames = [
        'stg_sdi_id',
        'tss_sup_dec_number',
        'tss_sfd_consignment_ref',
        'tss_consignment_ref',
        'sub_status',
        'tss_status',
        'tss_submission_due_date',
        'importer_eori',
        'trader_reference',
        'transport_document_number',
        'validation_errors_json',
        'auto_submit_error',
        'sdi_ready_at',
        'last_autosubmit_attempt_at',
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows or []:
        writer.writerow({key: row.get(key) for key in fieldnames})
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=sdi_selected.csv'},
    )
@supdec_bp.route('/<int:sid>/delete', methods=['POST'])
def delete(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@supdec_bp.route('/<int:sid>/retry', methods=['POST'])
def retry(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
# ── NOTIFY ────────────────────────────────────────────

@supdec_bp.route('/<int:sid>/notify', methods=['POST'])
def notify(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
# ── SDI GOODS: CREATE ─────────────────────────────────

def _form_text_value(form_data, key, default=''):
    value = (form_data or {}).get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _form_int_value(form_data, key, default=1):
    value = _form_text_value(form_data, key)
    if not value:
        return default
    return int(float(value))


def _form_float_value(form_data, key, default=0):
    value = _form_text_value(form_data, key)
    if not value:
        return default
    return float(value)


def _insert_manual_supdec_goods(sid, form_data):
    """Insert one SDI goods row from a form mapping. Returns assigned item number."""
    goods_parent_column = _supdec_goods_parent_column()
    goods_item_column = _supdec_goods_item_column()
    if not goods_parent_column or not goods_item_column:
        raise RuntimeError('Missing SDI goods linkage columns')

    next_item = (query_one(
        f"SELECT ISNULL(MAX({goods_item_column}),0)+1 AS n FROM {S}.StagingSupDecGoods WHERE {goods_parent_column}=?",
        [sid],
    ) or {}).get('n', 1)

    invoice_amount_raw = _form_text_value(form_data, 'item_invoice_amount')
    statistical_value_raw = _form_text_value(form_data, 'statistical_value') or invoice_amount_raw
    package_marks = _form_text_value(form_data, 'package_marks')

    insert_values = [
        (goods_parent_column, sid),
        (goods_item_column, next_item),
        ('label', _form_text_value(form_data, 'label')),
        ('goods_description', _form_text_value(form_data, 'goods_description')),
        (_first_existing_column('StagingSupDecGoods', 'type_of_packages', 'type_of_package'), _form_text_value(form_data, 'type_of_packages')),
        ('number_of_packages', _form_int_value(form_data, 'number_of_packages', 1)),
        ('package_marks', package_marks or None),
        (_first_existing_column('StagingSupDecGoods', 'gross_mass_kg', 'gross_weight_kg'), _form_float_value(form_data, 'gross_mass_kg', 0)),
        (_first_existing_column('StagingSupDecGoods', 'net_mass_kg', 'net_weight_kg'), _form_float_value(form_data, 'net_mass_kg', 0) or None),
        ('commodity_code', _form_text_value(form_data, 'commodity_code') or None),
        ('procedure_code', _form_text_value(form_data, 'procedure_code') or None),
        ('additional_procedure_code', _form_text_value(form_data, 'additional_procedure_code') or None),
        ('country_of_origin', _form_text_value(form_data, 'country_of_origin') or None),
        ('item_invoice_amount', invoice_amount_raw or None),
        ('item_invoice_currency', _form_text_value(form_data, 'item_invoice_currency') or None),
        ('valuation_method', _form_text_value(form_data, 'valuation_method') or None),
        ('valuation_indicator', _form_text_value(form_data, 'valuation_indicator') or None),
        ('invoice_number', _form_text_value(form_data, 'invoice_number') or None),
        ('nature_of_transaction', _form_text_value(form_data, 'nature_of_transaction') or None),
        ('preference', _form_text_value(form_data, 'preference') or None),
        (_first_existing_column('StagingSupDecGoods', 'ni_additional_information_codes', 'national_additional_codes'), _form_text_value(form_data, 'ni_additional_information_codes') or None),
        ('country_of_preferential_origin', _form_text_value(form_data, 'country_of_preferential_origin') or None),
        ('statistical_value', statistical_value_raw or None),
        ('status', 'PENDING'),
        ('retry_count', 0),
        ('max_retries', 3),
    ]

    columns = []
    values = []
    for column_name, value in insert_values:
        if column_name and _first_existing_column('StagingSupDecGoods', column_name):
            columns.append(column_name)
            values.append(value)

    execute(
        f"INSERT INTO {S}.StagingSupDecGoods ({', '.join(columns)}, created_at) "
        f"VALUES ({', '.join(['?'] * len(values))}, SYSUTCDATETIME())",
        values,
    )
    return next_item


@supdec_bp.route('/<int:sid>/goods/create', methods=['GET', 'POST'])
def create_goods(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@supdec_bp.route('/<int:sid>/goods/copy-from-source', methods=['POST'])
def copy_goods_from_source(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
def _source_goods_inline_decimal(form_data, source_id, field_name):
    key = f"source_{field_name}__{source_id}"
    if key not in (form_data or {}):
        return None
    raw = _form_text_value(form_data, key)
    if not raw:
        return None
    return raw.replace(',', '.')


def _apply_source_goods_inline_overrides(form_data, source_id, request_form):
    gross_override = _source_goods_inline_decimal(request_form, source_id, 'gross_mass_kg')
    if gross_override is not None:
        form_data['gross_mass_kg'] = gross_override
    return form_data


@supdec_bp.route('/<int:sid>/goods/copy-selected', methods=['POST'])
def copy_selected_from_source(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@supdec_bp.route('/<int:sid>/product-lookup', methods=['GET'])
def product_lookup(sid):
    return jsonify({
        'found': False,
        'error': 'This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.',
    }), 410
