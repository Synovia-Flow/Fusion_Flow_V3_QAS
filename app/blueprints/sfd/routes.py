"""
SFD (Simplified Frontier Declaration) / EIDR mirror view.

Uses TSS.BKD_SFD as the authoritative production source. Local STG rows are
not used as an SFD fallback in the automation PRD database.
"""
import csv
import io
import json
from datetime import datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, request, Response, flash

from app.db import query_all, query_one, execute
from app.status_utils import normalize_status_key, status_filter_tabs

sfd_bp = Blueprint('sfd', __name__, url_prefix='/sfd')

S = 'BKD'

SFD_LIST_STATUS_TABS = [
    ('ALL', 'All'),
    ('SFD', 'SFD'),
    ('EIDR', 'EIDR'),
    ('AUTHORISED', 'Authorised'),
    ('PENDING', 'Pending'),
    ('OTHER', 'Other'),
]
SFD_LIST_STATUS_ORDER = [key for key, _label in SFD_LIST_STATUS_TABS]
SFD_LIST_STATUS_LABELS = dict(SFD_LIST_STATUS_TABS)
SFD_LIST_SEARCH_FIELDS = (
    'sfd_reference',
    'dec_reference',
    'ens_reference',
    'importer_eori',
    'importer_name',
    'goods_description',
    'customs_reference',
)

SFD_DETAIL_FIELD_ORDER = [
    ('local_reference_number', 'Local Reference Number'),
    ('trader_reference', 'Trader Reference'),
    ('status', 'Status'),
    ('client_job_number', 'Client Job Number'),
    ('consignor_eori', 'Consignor EORI'),
    ('consignee_eori', 'Consignee EORI'),
    ('importer_eori', 'Importer EORI'),
    ('arrival_date_time', 'Arrival Date/Time'),
    ('transport_document_number', 'Transport Document Number'),
    ('goods_description', 'Goods Description'),
    ('controlled_goods', 'Controlled Goods'),
    ('ducr', 'DUCR'),
    ('movement_reference_number', 'Movement Reference Number'),
    ('eori_for_eidr', 'EORI for EIDR'),
    ('ens_consignment_reference', 'ENS Consignment Reference'),
    ('sup_dec_number', 'Supplementary Declaration Number'),
    ('error_message', 'Error Message'),
    ('control_status', 'Control Status'),
]

SFD_DETAIL_KEY_ALIASES = {
    'sfd_reference': ('sfd_reference', 'sfd_number', 'reference', 'declaration_number', 'number'),
    'status': ('status', 'tss_status', 'sfd_status'),
    'local_reference_number': ('local_reference_number', 'localReferenceNumber', 'lrn'),
    'trader_reference': ('trader_reference', 'traderReference'),
    'client_job_number': ('client_job_number', 'clientJobNumber'),
    'consignor_eori': ('consignor_eori', 'consignorEori'),
    'consignee_eori': ('consignee_eori', 'consigneeEori'),
    'importer_eori': ('importer_eori', 'importerEori'),
    'arrival_date_time': ('arrival_date_time', 'arrivalDateTime'),
    'transport_document_number': ('transport_document_number', 'transportDocumentNumber'),
    'goods_description': ('goods_description', 'goodsDescription'),
    'controlled_goods': ('controlled_goods', 'controlledGoods'),
    'ducr': ('ducr', 'DUCR'),
    'movement_reference_number': ('movement_reference_number', 'movementReferenceNumber', 'mrn'),
    'eori_for_eidr': ('eori_for_eidr', 'eoriForEidr'),
    'ens_consignment_reference': ('ens_consignment_reference', 'ensConsignmentReference', 'consignment_number'),
    'sup_dec_number': ('sup_dec_number', 'supDecNumber'),
    'error_message': ('error_message', 'errorMessage', 'process_message', 'processMessage'),
    'control_status': ('control_status', 'controlStatus'),
}


def _table_columns(table_name):
    rows = query_all(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [S, table_name],
    )
    return {row['COLUMN_NAME'].lower() for row in rows}


def _first_existing_column(table_name, *candidates):
    cols = _table_columns(table_name)
    for candidate in candidates:
        if candidate.lower() in cols:
            return candidate
    return None


def _compat_select_expr(table_name, alias, *candidates):
    cols = _table_columns(table_name)
    for candidate in candidates:
        if candidate.lower() in cols:
            return f"{alias}.[{candidate}]"
    return "NULL"


def _parse_raw_json(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def _clean(value):
    if value in (None, ''):
        return ''
    return str(value).strip()


def _pick(*values):
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return ''


def _business_status(value):
    cleaned = _pick(value)
    if normalize_status_key(cleaned) in {'OK', 'SUCCESS'}:
        return ''
    return cleaned


def _raw_pick(raw, *keys):
    if not isinstance(raw, dict):
        return ''
    normalised = {
        ''.join(ch for ch in str(key).lower() if ch.isalnum()): value
        for key, value in raw.items()
    }
    for key in keys:
        if key in raw and raw.get(key) not in (None, ''):
            return raw.get(key)
        compact = ''.join(ch for ch in str(key).lower() if ch.isalnum())
        value = normalised.get(compact)
        if value not in (None, ''):
            return value
    return ''


def _display_raw_value(value):
    if value in (None, ''):
        return ''
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)
    return str(value)


def _human_label(key):
    text = str(key or '').replace('_', ' ').replace('-', ' ').strip()
    if not text:
        return ''
    return ' '.join(part.upper() if part.upper() in {'SFD', 'ENS', 'EORI', 'EIDR', 'MRN', 'LRN'} else part.capitalize() for part in text.split())


def _raw_field_rows(raw):
    if not isinstance(raw, dict):
        return []
    preferred = []
    seen = set()
    for key, label in SFD_DETAIL_FIELD_ORDER:
        value = _raw_pick(raw, *SFD_DETAIL_KEY_ALIASES.get(key, (key,)))
        rendered = _display_raw_value(value)
        if rendered:
            preferred.append({'key': key, 'label': label, 'value': rendered})
            seen.update(
                ''.join(ch for ch in alias.lower() if ch.isalnum())
                for alias in SFD_DETAIL_KEY_ALIASES.get(key, (key,))
            )
    for key in sorted(raw.keys(), key=lambda item: str(item).lower()):
        compact = ''.join(ch for ch in str(key).lower() if ch.isalnum())
        if compact in seen:
            continue
        rendered = _display_raw_value(raw.get(key))
        if rendered:
            preferred.append({'key': key, 'label': _human_label(key), 'value': rendered})
    return preferred


def _parse_path_fields(row):
    raw = _parse_raw_json(row.get('raw_json'))
    sfd_mrn = _pick(
        row.get('movement_reference_number'),
        row.get('mrn'),
        row.get('sfd_mrn'),
        _raw_pick(raw, 'movement_reference_number', 'movementReferenceNumber', 'mrn'),
    )
    eidr_ref = _pick(
        row.get('eori_for_eidr'),
        _raw_pick(raw, 'eori_for_eidr', 'eoriForEidr', 'eidr'),
    )
    if eidr_ref and not sfd_mrn:
        return 'EIDR', eidr_ref
    return 'SFD', sfd_mrn


def _normalize_sfd_detail(row):
    raw = _parse_raw_json((row or {}).get('raw_json'))
    normalised = _normalize_sfd_row(row or {})
    detail = dict(normalised)
    for key, _label in SFD_DETAIL_FIELD_ORDER:
        detail[key] = _pick(
            (row or {}).get(key),
            _raw_pick(raw, *SFD_DETAIL_KEY_ALIASES.get(key, (key,))),
        )
    detail['sfd_reference'] = _pick(
        detail.get('sfd_reference'),
        _raw_pick(raw, *SFD_DETAIL_KEY_ALIASES['sfd_reference']),
    )
    detail['tss_status'] = _pick(
        _business_status(detail.get('tss_status')),
        _business_status(detail.get('status')),
        _business_status((row or {}).get('dec_tss_status')),
    )
    detail['status'] = _pick(_business_status(detail.get('status')), detail.get('tss_status'))
    detail['dec_reference'] = _pick(detail.get('dec_reference'), (row or {}).get('parent_dec_reference'))
    detail['ens_reference'] = _pick(detail.get('ens_reference'), (row or {}).get('parent_ens_reference'))
    detail['dec_tss_status'] = _pick((row or {}).get('dec_tss_status'))
    detail['raw_json'] = raw
    detail['raw_fields'] = _raw_field_rows(raw)
    detail['updated_at'] = (row or {}).get('updated_at') or (row or {}).get('created_at')
    return detail


def _normalize_sfd_row(row):
    raw = _parse_raw_json(row.get('raw_json'))
    path_kind, customs_reference = _parse_path_fields(row)
    status = _pick(
        _business_status(row.get('tss_status')),
        _business_status(row.get('status')),
        _business_status(row.get('sfd_status')),
        _business_status(_raw_pick(raw, 'status', 'tss_status', 'tssStatus', 'sfd_status', 'sfdStatus')),
        _business_status(row.get('dec_tss_status')),
    )
    return {
        'sfd_reference': _pick(row.get('sfd_reference'), row.get('sfd_number'), row.get('reference'), raw.get('sfd_number')),
        'dec_reference': _pick(row.get('dec_reference'), row.get('declaration_number'), row.get('consignment_number')),
        'customs_reference': customs_reference,
        'customs_reference_label': 'EIDR' if path_kind == 'EIDR' else 'MRN',
        'path_kind': path_kind,
        'importer_eori': _pick(row.get('importer_eori')),
        'importer_name': _pick(row.get('importer_name')),
        'goods_description': _pick(row.get('goods_description')),
        'staging_id': row.get('staging_id'),
        'staging_ens_id': row.get('staging_ens_id'),
        'tss_status': status,
        'updated_at': row.get('updated_at') or row.get('created_at'),
        'ens_reference': _pick(row.get('ens_reference')),
        'ens_label': _pick(row.get('ens_label')),
        'arrival_port': _pick(row.get('arrival_port')),
        'source': row.get('source') or 'SFD_MIRROR',
        'generates_sdi': str(row.get('generate_SD') or '').strip().lower() in {'yes', 'y', 'true', '1', 'on'},
    }


def _load_sfd_mirror_rows():
    rows = query_all(
        """
        SELECT
            s.SfdReference            AS sfd_reference,
            s.DeclarationNumber       AS dec_reference,
            s.MovementReferenceNumber AS movement_reference_number,
            NULL                      AS eori_for_eidr,
            s.TssStatus               AS tss_status,
            s.UpdatedAt               AS updated_at,
            s.RawJson                 AS raw_json,
            c.generate_SD,
            c.stg_consignment_id      AS staging_id,
            c.stg_header_id           AS staging_ens_id,
            c.importer_eori,
            c.importer_name,
            c.goods_description,
            NULL                      AS sfd_status,
            NULL                      AS sfd_mrn,
            h.tss_ens_header_ref      AS ens_reference,
            h.label                   AS ens_label,
            h.arrival_port
        FROM [TSS].[BKD_SFD] s
        LEFT JOIN [STG].[BKD_ENS_Consignments] c
          ON c.tss_consignment_ref = s.DeclarationNumber
        LEFT JOIN [STG].[BKD_ENS_Headers] h
          ON h.stg_header_id = c.stg_header_id
        WHERE s.SfdReference IS NOT NULL
        ORDER BY s.UpdatedAt DESC
        """
    )
    return [_normalize_sfd_row(row) for row in rows]


def _load_staging_fallback_rows():
    # STG.BKD_ENS_Consignments does not carry sfd_reference;
    # TSS.BKD_SFD is the authoritative source on this database.
    return []


def _merge_sfd_rows(primary_rows, fallback_rows):
    merged = []
    seen = set()

    def key_for(row):
        return (
            _pick(row.get('sfd_reference')).upper(),
            _pick(row.get('dec_reference')).upper(),
        )

    for row in primary_rows + fallback_rows:
        key = key_for(row)
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def _resolve_consignment_ref_for_sfd(sfd_ref):
    if not sfd_ref:
        return ''
    row = query_one(
        """
        SELECT TOP 1 s.DeclarationNumber AS dec_reference
        FROM [TSS].[BKD_SFD] s
        WHERE s.SfdReference = ?
        ORDER BY s.UpdatedAt DESC
        """,
        [sfd_ref],
    )
    return _pick((row or {}).get('dec_reference'))


def _load_sfd_detail(sfd_ref):
    row = query_one(
        """
        SELECT TOP 1
            s.SfdReference             AS sfd_reference,
            s.DeclarationNumber        AS dec_reference,
            s.DeclarationNumber        AS parent_dec_reference,
            s.EnsReference             AS ens_reference,
            s.EnsReference             AS parent_ens_reference,
            s.MovementReferenceNumber  AS movement_reference_number,
            s.TssStatus                AS tss_status,
            s.RawJson                  AS raw_json,
            s.CreatedAt                AS created_at,
            COALESCE(s.UpdatedAt, s.LastSyncedAt, s.CreatedAt) AS updated_at,
            c.stg_consignment_id       AS staging_id,
            c.stg_header_id            AS staging_ens_id,
            c.trader_reference,
            c.transport_document_number,
            c.consignor_eori,
            c.consignee_eori,
            c.importer_eori,
            c.importer_name,
            c.goods_description,
            h.tss_ens_header_ref       AS ens_reference_from_header,
            h.arrival_date_time,
            h.arrival_port,
            h.label                    AS ens_label,
            tc.TssStatus               AS dec_tss_status
        FROM [TSS].[BKD_SFD] s
        LEFT JOIN [STG].[BKD_ENS_Consignments] c
          ON c.ClientCode = s.ClientCode
         AND c.tss_consignment_ref = s.DeclarationNumber
        LEFT JOIN [STG].[BKD_ENS_Headers] h
          ON h.ClientCode = c.ClientCode
         AND h.stg_header_id = c.stg_header_id
        LEFT JOIN [TSS].[BKD_ENS_Consignments] tc
          ON tc.ClientCode = s.ClientCode
         AND tc.ConsignmentReference = s.DeclarationNumber
        WHERE s.ClientCode = ?
          AND (s.SfdReference = ? OR s.DeclarationNumber = ?)
        ORDER BY
            CASE WHEN s.SfdReference = ? THEN 0 ELSE 1 END,
            COALESCE(s.UpdatedAt, s.LastSyncedAt, s.CreatedAt) DESC
        """,
        [S, sfd_ref, sfd_ref, sfd_ref],
    )
    if not row:
        return None
    detail = _normalize_sfd_detail(row)
    detail['ens_reference'] = _pick(detail.get('ens_reference'), row.get('ens_reference_from_header'))
    detail['arrival_date_time'] = _pick(detail.get('arrival_date_time'), row.get('arrival_date_time'))
    detail['transport_document_number'] = _pick(detail.get('transport_document_number'), row.get('transport_document_number'))
    detail['trader_reference'] = _pick(detail.get('trader_reference'), row.get('trader_reference'))
    detail['consignor_eori'] = _pick(detail.get('consignor_eori'), row.get('consignor_eori'))
    detail['consignee_eori'] = _pick(detail.get('consignee_eori'), row.get('consignee_eori'))
    return detail


def _is_sfd_authorised(row):
    status = normalize_status_key((row or {}).get('tss_status'))
    if not status:
        return False
    return any(token in status for token in ('AUTHORISED', 'AUTHORIZED', 'ARRIVED'))


def _is_sfd_pending(row):
    status = normalize_status_key((row or {}).get('tss_status'))
    if not status:
        return True
    return status in {'PENDING', 'PENDING SYNC', 'PENDING MIRROR'}


def _sfd_status_tabs(counts, selected='ALL'):
    return [
        (key, SFD_LIST_STATUS_LABELS.get(key, key.title()))
        for key in status_filter_tabs(counts, SFD_LIST_STATUS_ORDER, selected or 'ALL')
    ]


def _sfd_status_counts(rows):
    counts = {key: 0 for key, _ in SFD_LIST_STATUS_TABS}
    counts['ALL'] = len(rows)
    for row in rows:
        if row.get('path_kind') == 'EIDR':
            counts['EIDR'] += 1
        else:
            counts['SFD'] += 1
        if _is_sfd_authorised(row):
            counts['AUTHORISED'] += 1
        elif _is_sfd_pending(row):
            counts['PENDING'] += 1
        else:
            counts['OTHER'] += 1
    return counts


def _search_sfd_rows(rows, search_query):
    query = (search_query or '').strip().casefold()
    if not query:
        return list(rows or [])
    return [
        row for row in (rows or [])
        if any(
            query in str(row.get(field) or '').casefold()
            for field in SFD_LIST_SEARCH_FIELDS
        )
    ]


def _filter_sfd_rows(rows, status_filter, search_query):
    filtered = _search_sfd_rows(rows, search_query)
    status_filter = (status_filter or 'ALL').strip().upper()
    if status_filter == 'SFD':
        filtered = [r for r in filtered if r.get('path_kind') != 'EIDR']
    elif status_filter == 'EIDR':
        filtered = [r for r in filtered if r.get('path_kind') == 'EIDR']
    elif status_filter == 'AUTHORISED':
        filtered = [r for r in filtered if _is_sfd_authorised(r)]
    elif status_filter == 'PENDING':
        filtered = [r for r in filtered if _is_sfd_pending(r)]
    elif status_filter == 'OTHER':
        filtered = [
            r for r in filtered
            if not _is_sfd_authorised(r) and not _is_sfd_pending(r)
        ]

    return filtered


@sfd_bp.route('/')
def list_view():
    mirror_rows = _load_sfd_mirror_rows()
    fallback_rows = _load_staging_fallback_rows()
    all_sfds = _merge_sfd_rows(mirror_rows, fallback_rows)

    raw_status = (request.args.get('status') or 'ALL').strip().upper()
    valid_statuses = {key for key, _ in SFD_LIST_STATUS_TABS}
    status_filter = raw_status if raw_status in valid_statuses else 'ALL'
    search_query = (request.args.get('q') or '').strip()

    search_scoped_sfds = _search_sfd_rows(all_sfds, search_query)
    status_counts = _sfd_status_counts(search_scoped_sfds)
    sfds = _filter_sfd_rows(search_scoped_sfds, status_filter, '')
    summary = {
        'mirror_total': len(mirror_rows),
        'sfd_total': sum(1 for row in all_sfds if row.get('path_kind') == 'SFD'),
        'eidr_total': sum(1 for row in all_sfds if row.get('path_kind') == 'EIDR'),
    }
    return render_template(
        'sfd/list.html',
        sfds=sfds,
        summary=summary,
        status_tabs=_sfd_status_tabs(status_counts, status_filter),
        status_counts=status_counts,
        status_filter=status_filter,
        search_query=search_query,
        total_unfiltered=len(all_sfds),
    )


@sfd_bp.route('/<string:sfd_ref>/detail')
def detail(sfd_ref):
    sfd = _load_sfd_detail(sfd_ref)
    if not sfd:
        flash('SFD record not found in the TSS mirror. Run Sync TSS statuses and try again.', 'warning')
        return redirect(url_for('sfd.list_view'))
    return render_template('sfd/detail.html', sfd=sfd)


@sfd_bp.route('/bulk-delete-selected', methods=['POST'])
def bulk_delete_selected():
    """Delete local SFD mirror rows by sfd_reference. TSS records are not
    touched - a subsequent Sync TSS Tables will re-import any that still
    exist remotely. Mirrors the local-only delete semantics used by the
    SDI and consignment bulk-delete actions."""
    selected = request.form.getlist('selected_refs') or []
    selected_refs = [ref.strip() for ref in selected if (ref or '').strip()]
    if not selected_refs:
        flash('No SFDs selected.', 'warning')
        return redirect(url_for('sfd.list_view'))

    placeholders = ', '.join(['?'] * len(selected_refs))
    deleted = 0
    try:
        deleted = execute(
            f"DELETE FROM [TSS].[BKD_SFD] WHERE SfdReference IN ({placeholders})",
            selected_refs,
        )
    except Exception as exc:
        flash(f'Bulk delete failed: {exc}', 'danger')
        return redirect(url_for('sfd.list_view'))

    if deleted in (None, 0):
        flash(
            'No matching local SFD mirror rows were deleted. The references may already be gone or the mirror was never populated.',
            'info',
        )
    elif deleted == 1:
        flash('1 local SFD mirror row deleted. TSS records were not changed.', 'success')
    else:
        flash(f'{deleted} local SFD mirror rows deleted. TSS records were not changed.', 'success')
    return redirect(url_for('sfd.list_view'))


@sfd_bp.route('/bulk-export-selected', methods=['POST'])
def bulk_export_selected():
    """Return a CSV of the SFD rows whose sfd_reference appears in the form
    selected_refs list. Mirrors the bulk export pattern used by consignments
    and SDI lists."""
    selected = request.form.getlist('selected_refs') or []
    selected_keys = {(ref or '').strip().upper() for ref in selected if (ref or '').strip()}
    mirror_rows = _load_sfd_mirror_rows()
    fallback_rows = _load_staging_fallback_rows()
    all_sfds = _merge_sfd_rows(mirror_rows, fallback_rows)
    if selected_keys:
        rows = [
            row for row in all_sfds
            if (row.get('sfd_reference') or '').upper() in selected_keys
        ]
    else:
        rows = all_sfds

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        'Path', 'SFD Reference', 'DEC Reference', 'ENS Reference',
        'Customs Reference', 'Customs Reference Kind',
        'Importer EORI', 'Importer Name', 'Goods Description',
        'Arrival Port', 'TSS Status', 'Source', 'Updated At',
    ])
    for row in rows:
        updated = row.get('updated_at')
        if hasattr(updated, 'strftime'):
            updated_str = updated.strftime('%Y-%m-%d %H:%M:%S')
        else:
            updated_str = str(updated or '')
        writer.writerow([
            row.get('path_kind') or '',
            row.get('sfd_reference') or '',
            row.get('dec_reference') or '',
            row.get('ens_reference') or '',
            row.get('customs_reference') or '',
            row.get('customs_reference_label') or '',
            row.get('importer_eori') or '',
            row.get('importer_name') or '',
            row.get('goods_description') or '',
            row.get('arrival_port') or '',
            row.get('tss_status') or '',
            row.get('source') or '',
            updated_str,
        ])

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    filename = f'sfd_export_{stamp}.csv'
    return Response(
        buffer.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )
