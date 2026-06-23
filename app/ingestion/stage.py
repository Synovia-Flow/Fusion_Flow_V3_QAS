from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
import re

from app.db import db_cursor
from app.ens_validation import auto_validate_declaration_record
from app.ingestion.defaults import resolve_ingest_defaults
from app.ingestion.ing_process_log import log_consignment as _log_consignment
from app.ingestion.ing_process_log import log_goods as _log_goods
from app.ingestion.ing_process_log import log_failure as _log_ing_failure
from app.ingestion.parser import ParsedInvoice, ParsedInvoiceLine, ParsedParty
from app.pipeline_validation import auto_validate_consignment_record, auto_validate_goods_record
from app.status_utils import tss_allows_data_changes, tss_data_lock_reason
from app.tenant import get_tenant, get_tenant_by_code
from app.tss_text import format_tss_unsafe_character as _shared_format_tss_unsafe_character
from app.tss_text import tss_unsafe_characters as _shared_tss_unsafe_characters


_ACTIVE_STAGE_SCHEMA = ContextVar("active_stage_schema", default=None)


def _staging_schema() -> str:
    return _ACTIVE_STAGE_SCHEMA.get() or get_tenant()["schema"]


def _env_code() -> str:
    return "PRD"


def _master_schema(tenant_code: str | None = None) -> str:
    if tenant_code:
        try:
            return get_tenant_by_code(tenant_code)["schema"]
        except KeyError:
            pass
    return get_tenant()["schema"]


def _columns(cursor, table_name: str) -> set[str]:
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [_staging_schema(), table_name],
    )
    return {row[0].lower() for row in cursor.fetchall()}


def _table_exists(cursor, table_name: str) -> bool:
    return _schema_table_exists(cursor, _staging_schema(), table_name)


def _schema_table_exists(cursor, schema_name: str, table_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [schema_name, table_name],
    )
    return cursor.fetchone() is not None


def _row_as_dict(cursor):
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


def _normalize_name(value: str) -> str:
    return re.sub(r'[^A-Z0-9]+', ' ', (value or '').upper()).strip()


def _to_decimal(value):
    text = str(value or '').strip().replace(',', '')
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _to_intish(value, default=1) -> int:
    dec = _to_decimal(value)
    if dec is None:
        return default
    try:
        return max(int(dec), 1)
    except Exception:
        return default


def _uom_package_type(uom: str, default_type: str) -> str:
    text = (uom or '').strip().lower()
    if not text:
        return default_type
    if 'pallet' in text:
        return 'PK'
    if 'box' in text:
        return 'PK'
    if 'tub' in text:
        return 'PK'
    if 'each' in text:
        return 'PK'
    return default_type


def _best_partner_match(cursor, name: str, postcode: str = '', master_schema: str | None = None) -> dict | None:
    master_schema = master_schema or _staging_schema()
    if not name:
        return None
    token = (name or '').strip().split()[0]
    if not token:
        return None
    cursor.execute(
        f"""
        SELECT TOP 20 id, partner_name, eori, address_line1, city, postcode, country
        FROM [{master_schema}].Partners
        WHERE active = 1 AND partner_name LIKE ?
        ORDER BY id DESC
        """,
        [f'%{token}%'],
    )
    rows = []
    columns = [col[0] for col in cursor.description]
    for row in cursor.fetchall():
        rows.append(dict(zip(columns, row)))
    if not rows:
        return None

    wanted_name = _normalize_name(name)
    wanted_postcode = (postcode or '').replace(' ', '').upper()

    def _score(row):
        score = 0
        row_name = _normalize_name(row.get('partner_name') or '')
        row_postcode = ((row.get('postcode') or '').replace(' ', '').upper())
        if row_name == wanted_name:
            score += 100
        elif wanted_name and wanted_name in row_name:
            score += 60
        if wanted_postcode and row_postcode == wanted_postcode:
            score += 40
        return score

    rows.sort(key=_score, reverse=True)
    return rows[0]


def _product_match(cursor, stock_code: str, master_schema: str | None = None) -> dict | None:
    master_schema = master_schema or _staging_schema()
    if not stock_code or not _schema_table_exists(cursor, master_schema, 'Products'):
        return None
    columns = _columns(cursor, 'Products')
    selected_fields = [
        'product_code',
        'product_name',
        'commodity_code',
        'goods_description',
        'country_of_origin',
        'package_type',
        'package_marks',
        'procedure_code',
        'valuation_method',
        'ni_additional_info_code',
        'preference_code',
    ]
    lookup_field = next(
        (field for field in ('product_code', 'sku', 'stock_code', 'item_code', 'code') if field in columns),
        None,
    )
    if lookup_field is None:
        return None

    select_exprs = [
        field if field in columns else f"NULL AS {field}"
        for field in selected_fields
    ]
    active_filter = ''
    if 'is_active' in columns:
        active_filter = 'AND is_active = 1'
    elif 'active' in columns:
        active_filter = 'AND active = 1'

    cursor.execute(
        f"""
        SELECT TOP 1
            {', '.join(select_exprs)}
        FROM [{master_schema}].Products
        WHERE {lookup_field} = ?
        {active_filter}
        """,
        [stock_code],
    )
    return _row_as_dict(cursor)


def _build_batch_label(invoices: list[ParsedInvoice], email_meta: dict | None) -> str:
    if email_meta and email_meta.get('subject'):
        return f"Email batch - {email_meta['subject']}"[:200]
    if len(invoices) == 1:
        inv = invoices[0]
        ref = inv.carrier_reference or inv.invoice_number or inv.filename
        return f"Ingest - {ref}"[:200]
    return f"Ingest batch - {len(invoices)} invoices"[:200]


HEADER_REVIEW_FIELDS = [
    ('movement_type', 'Movement Type'),
    ('type_of_passive_transport', 'Passive Transport'),
    ('identity_no_of_transport', 'Identity of Transport'),
    ('nationality_of_transport', 'Nationality of Transport'),
    ('conveyance_ref', 'Conveyance Ref'),
    ('arrival_date_time', 'Arrival Date/Time'),
    ('arrival_port', 'Arrival Port'),
    ('place_of_loading', 'Place of Loading'),
    ('place_of_unloading', 'Place of Unloading'),
    ('place_of_acceptance_same_as_loading', 'Accept = Loading'),
    ('place_of_acceptance', 'Place of Acceptance'),
    ('place_of_delivery_same_as_unloading', 'Delivery = Unloading'),
    ('place_of_delivery', 'Place of Delivery'),
    ('seal_number', 'Seal Number'),
    ('transport_charges', 'Transport Charges'),
    ('carrier_eori', 'Carrier EORI'),
    ('carrier_name', 'Carrier Name'),
    ('carrier_street_number', 'Carrier Street / Number'),
    ('carrier_city', 'Carrier City'),
    ('carrier_postcode', 'Carrier Postcode'),
    ('carrier_country', 'Carrier Country'),
    ('haulier_eori', 'Haulier EORI'),
]

CONSIGNMENT_REVIEW_FIELDS = [
    ('label', 'Label'),
    ('goods_description', 'Goods Description'),
    ('trader_reference', 'Trader Reference'),
    ('transport_document_number', 'Transport Document'),
    ('importer_eori', 'Importer EORI'),
    ('importer_name', 'Importer Name'),
    ('exporter_eori', 'Exporter EORI'),
    ('exporter_name', 'Exporter Name'),
    ('consignor_eori', 'Consignor EORI'),
    ('consignee_eori', 'Consignee EORI'),
    ('destination_country', 'Destination Country'),
    ('controlled_goods', 'Controlled Goods'),
    ('container_indicator', 'Container Indicator'),
]

GOODS_REVIEW_FIELDS = [
    ('item_number', 'Item'),
    ('goods_description', 'Description'),
    ('commodity_code', 'HS Code'),
    ('type_of_packages', 'Package Type'),
    ('number_of_packages', 'Packages'),
    ('gross_mass_kg', 'Gross KG'),
    ('net_mass_kg', 'Net KG'),
    ('country_of_origin', 'Origin'),
    ('procedure_code', 'Procedure'),
    ('item_invoice_amount', 'Invoice Amount'),
    ('item_invoice_currency', 'Currency'),
    ('label', 'Stock Code'),
    ('package_marks', 'Package Marks'),
    ('controlled_goods', 'Controlled Goods'),
    ('additional_procedure_code', 'Additional Procedure'),
    ('valuation_method', 'Valuation'),
]

HEADER_REVIEW_LABELS = dict(HEADER_REVIEW_FIELDS)
CONSIGNMENT_REVIEW_LABELS = dict(CONSIGNMENT_REVIEW_FIELDS)
GOODS_REVIEW_LABELS = dict(GOODS_REVIEW_FIELDS)

HEADER_REQUIRED_FIELDS = [
    'movement_type',
    'identity_no_of_transport',
    'nationality_of_transport',
    'arrival_date_time',
    'arrival_port',
    'place_of_loading',
    'place_of_unloading',
    'transport_charges',
    'carrier_eori',
]

MARITIME_RORO_MOVEMENT_TYPES = {'1', '1a', '3', '3a'}

MARITIME_RORO_HEADER_REQUIRED_FIELDS = [
    'carrier_name',
    'carrier_street_number',
    'carrier_city',
    'carrier_postcode',
    'carrier_country',
]

CONSIGNMENT_REQUIRED_FIELDS = [
    'goods_description',
    'transport_document_number',
    'importer_eori',
    'consignee_eori',
    'controlled_goods',
]

GOODS_REQUIRED_FIELDS = [
    'goods_description',
    'type_of_packages',
    'number_of_packages',
    'package_marks',
    'gross_mass_kg',
    'controlled_goods',
]

TSS_CHARACTER_WARNING_PREFIX = 'TSS character warning:'
REVIEW_MODE_BATCH_CREATE = 'batch_create'
REVIEW_MODE_ATTACH_GOODS = 'attach_goods'


def _stringify_payload(payload: dict) -> dict:
    normalized = {}
    for key, value in (payload or {}).items():
        normalized[key] = '' if value is None else str(value)
    return normalized


def _missing_required(payload: dict, required: list[str]) -> list[str]:
    missing = []
    for field in required:
        value = payload.get(field)
        if value is None or str(value).strip() == '':
            missing.append(field)
            continue
        if field == 'gross_mass_kg':
            numeric = _to_decimal(value)
            if numeric is None or numeric <= 0:
                missing.append(field)
        elif field == 'number_of_packages':
            numeric = _to_decimal(value)
            if numeric is None or int(numeric) < 1:
                missing.append(field)
    return missing


def _strip_tss_character_warnings(warnings: list[str] | None) -> list[str]:
    cleaned = []
    for warning in warnings or []:
        text = str(warning or '').strip()
        if text and not text.startswith(TSS_CHARACTER_WARNING_PREFIX):
            cleaned.append(text)
    return cleaned


def _tss_unsafe_characters(value: str) -> list[str]:
    return _shared_tss_unsafe_characters(value)


def _format_tss_unsafe_character(char: str) -> str:
    return _shared_format_tss_unsafe_character(char)


def _payload_tss_character_warnings(payload: dict, labels: dict[str, str], context: str) -> list[str]:
    warnings = []
    for field, label in labels.items():
        value = payload.get(field)
        if value is None or value == '':
            continue
        chars = _tss_unsafe_characters(str(value))
        if not chars:
            continue
        formatted = ', '.join(_format_tss_unsafe_character(char) for char in chars[:4])
        extra = f" (+{len(chars) - 4} more)" if len(chars) > 4 else ''
        warnings.append(
            f"{TSS_CHARACTER_WARNING_PREFIX} {context}: {label} contains {formatted}{extra}. "
            "Replace special characters before sending this data to TSS."
        )
    return warnings


def _header_required_fields(payload: dict) -> list[str]:
    required = list(HEADER_REQUIRED_FIELDS)
    movement_type = str((payload or {}).get('movement_type') or '').strip()
    if movement_type in MARITIME_RORO_MOVEMENT_TYPES:
        required.extend(
            field for field in MARITIME_RORO_HEADER_REQUIRED_FIELDS
            if field not in required
        )
    return required


def _review_mode(review: dict) -> str:
    mode = str((review or {}).get('review_mode') or '').strip().lower()
    if mode == REVIEW_MODE_ATTACH_GOODS:
        return REVIEW_MODE_ATTACH_GOODS
    return REVIEW_MODE_BATCH_CREATE


def _consignment_reference_label(consignment: dict) -> str:
    consignment = consignment or {}
    return (
        consignment.get('dec_reference')
        or consignment.get('sfd_reference')
        or consignment.get('label')
        or f"Consignment #{consignment.get('staging_id')}"
    )


def _build_attach_target(consignment: dict) -> dict:
    consignment = dict(consignment or {})
    return {
        'staging_id': consignment.get('staging_id'),
        'staging_ens_id': consignment.get('staging_ens_id'),
        'reference_label': _consignment_reference_label(consignment),
        'dec_reference': consignment.get('dec_reference'),
        'sfd_reference': consignment.get('sfd_reference'),
        'ens_reference': consignment.get('ens_reference'),
        'status': consignment.get('status'),
        'tss_status': consignment.get('tss_status'),
        'transport_document_number': consignment.get('transport_document_number'),
        'goods_description': consignment.get('goods_description'),
        'controlled_goods': consignment.get('controlled_goods'),
        'container_indicator': consignment.get('container_indicator'),
        'goods_count': consignment.get('goods_count') or 0,
        'can_attach_goods': tss_allows_data_changes(consignment.get('tss_status'), consignment.get('status')),
        'lock_reason': tss_data_lock_reason(
            consignment.get('tss_status'),
            consignment.get('status'),
            entity_label='consignment',
        ),
    }


def _candidate_match_tokens(invoice: ParsedInvoice) -> list[tuple[str, str]]:
    seen = set()
    tokens = []
    for field, label in (
        (invoice.carrier_reference, 'carrier reference'),
        (invoice.external_document_number, 'trader reference'),
        (invoice.invoice_number, 'invoice number'),
    ):
        token = str(field or '').strip()
        if not token:
            continue
        normalized = token.upper()
        if normalized in seen:
            continue
        seen.add(normalized)
        tokens.append((label, token))
    return tokens


def _normalized_text(value) -> str:
    return re.sub(r'[^A-Z0-9]+', ' ', str(value or '').upper()).strip()


def _find_existing_consignment_suggestion(cursor, invoice: ParsedInvoice) -> dict | None:
    if not _table_exists(cursor, 'StagingConsignments'):
        return None

    tokens = _candidate_match_tokens(invoice)
    if not tokens:
        return None

    values = [token for _label, token in tokens]
    placeholders = ', '.join('?' for _ in values)
    params = values + values + values
    cursor.execute(
        f"""
        SELECT TOP 20
            c.staging_id,
            c.staging_ens_id,
            c.label,
            c.status,
            c.tss_status,
            c.dec_reference,
            c.sfd_reference,
            c.transport_document_number,
            c.trader_reference,
            c.goods_description,
            c.controlled_goods,
            c.container_indicator,
            c.consignee_name,
            c.consignee_eori,
            c.importer_name,
            c.importer_eori,
            c.destination_country,
            e.ens_reference,
            (
                SELECT COUNT(*)
                FROM {_staging_schema()}.StagingGoodsItems g
                WHERE g.staging_cons_id = c.staging_id
            ) AS goods_count
        FROM {_staging_schema()}.StagingConsignments c
        LEFT JOIN {_staging_schema()}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
        WHERE c.transport_document_number IN ({placeholders})
           OR c.trader_reference IN ({placeholders})
           OR c.label IN ({placeholders})
        ORDER BY c.staging_id DESC
        """,
        params,
    )
    columns = [col[0] for col in cursor.description]
    candidates = [dict(zip(columns, row)) for row in cursor.fetchall()]
    if not candidates:
        return None

    wanted_party = _normalized_text(invoice.delivery_to.name or invoice.consignee.name)
    wanted_country = str(invoice.delivery_to.country_code or invoice.consignee.country_code or '').strip().upper()

    best = None
    for row in candidates:
        score = 0
        reasons = []
        for label, token in tokens:
            token_upper = token.upper()
            if str(row.get('transport_document_number') or '').strip().upper() == token_upper:
                score += 140
                reasons.append(f"transport document matches {label} {token}")
            if str(row.get('trader_reference') or '').strip().upper() == token_upper:
                score += 120
                reasons.append(f"trader reference matches {label} {token}")
            if str(row.get('label') or '').strip().upper() == token_upper:
                score += 90
                reasons.append(f"label matches {label} {token}")

        row_party = _normalized_text(
            row.get('consignee_name')
            or row.get('importer_name')
            or ''
        )
        if wanted_party and row_party and row_party == wanted_party:
            score += 20
        row_country = str(row.get('destination_country') or '').strip().upper()
        if wanted_country and row_country and row_country == wanted_country:
            score += 5

        if score < 120:
            continue
        suggestion = _build_attach_target(row)
        suggestion['match_reason'] = '; '.join(reasons[:2]) if reasons else 'Invoice references align with this consignment.'
        suggestion['match_score'] = score
        if best is None or score > best.get('match_score', 0):
            best = suggestion

    return best


def _next_goods_item_number(cursor, staging_cons_id: int) -> int:
    cursor.execute(
        f"SELECT ISNULL(MAX(item_number), 0) + 1 FROM {_staging_schema()}.StagingGoodsItems WHERE staging_cons_id = ?",
        [staging_cons_id],
    )
    row = cursor.fetchone()
    return int((row or [1])[0] or 1)


def _goods_attach_review_from_invoice_reviews(
    invoice_reviews: list[dict],
    consignment: dict,
    *,
    source: str = 'PORTAL_REVIEW',
    channel: str = 'UPLOAD',
    email_meta: dict | None = None,
    tenant_code: str | None = None,
    batch_label: str | None = None,
    automation_enabled: bool | None = None,
    ingestion_mode: str | None = None,
) -> dict:
    if not invoice_reviews:
        raise ValueError('No invoice review data supplied for goods attach review.')

    attach_target = _build_attach_target(consignment)
    if not attach_target.get('can_attach_goods'):
        raise ValueError(attach_target.get('lock_reason') or 'This consignment is locked for new goods.')

    master_schema = _master_schema(tenant_code)
    schema_token = _ACTIVE_STAGE_SCHEMA.set(master_schema)
    try:
        with db_cursor(commit=False) as cursor:
            next_item = _next_goods_item_number(cursor, int(attach_target['staging_id']))
    finally:
        _ACTIVE_STAGE_SCHEMA.reset(schema_token)

    copied_invoices = deepcopy(invoice_reviews)
    target_controlled_goods = str(attach_target.get('controlled_goods') or '').strip()
    for invoice_review in copied_invoices:
        invoice_review.setdefault('consignment', {})
        invoice_review['consignment']['payload'] = {}
        invoice_review['consignment']['missing'] = []
        invoice_review['consignment']['warnings'] = []
        invoice_review['consignment']['character_warnings'] = []
        invoice_review.pop('suggested_attach', None)
        for goods_review in invoice_review.get('goods') or []:
            payload = goods_review.setdefault('payload', {})
            payload['item_number'] = str(next_item)
            next_item += 1
            if target_controlled_goods:
                payload['controlled_goods'] = target_controlled_goods

    review = {
        'schema_version': 1,
        'review_mode': REVIEW_MODE_ATTACH_GOODS,
        'tenant_code': tenant_code,
        'source': source,
        'channel': channel,
        'email_meta': email_meta or {},
        'batch_label': batch_label or f"Attach goods - {attach_target['reference_label']}",
        'automation_enabled': automation_enabled,
        'ingestion_mode': ingestion_mode,
        'attach_target': attach_target,
        'header': {'payload': {}, 'missing': [], 'required': [], 'warnings': [], 'character_warnings': []},
        'invoices': copied_invoices,
        'warnings': [],
    }
    for invoice_review in copied_invoices:
        for goods_review in invoice_review.get('goods') or []:
            review['warnings'].extend(goods_review.get('warnings') or [])
    return refresh_review_status(review)

def _review_counts(review: dict) -> tuple[int, int, int]:
    cons_count = len(review.get('invoices') or [])
    goods_count = sum(len(invoice.get('goods') or []) for invoice in review.get('invoices') or [])
    missing_total = len(review.get('header', {}).get('missing') or [])
    for invoice in review.get('invoices') or []:
        missing_total += len(invoice.get('consignment', {}).get('missing') or [])
        for goods in invoice.get('goods') or []:
            missing_total += len(goods.get('missing') or [])
    return cons_count, goods_count, missing_total


def refresh_review_status(review: dict) -> dict:
    """Refresh missing-field metadata after user edits a review payload."""
    mode = _review_mode(review)
    review['enrichment_warnings'] = _strip_tss_character_warnings(
        review.get('enrichment_warnings') or review.get('warnings') or []
    )
    review['character_warnings'] = []

    header_payload = review.get('header', {}).get('payload') or {}
    review.setdefault('header', {})['payload'] = _stringify_payload(header_payload)
    if mode == REVIEW_MODE_ATTACH_GOODS:
        review['header']['required'] = []
        review['header']['missing'] = []
        review['header']['character_warnings'] = []
        review['header']['warnings'] = []
    else:
        review['header']['required'] = _header_required_fields(review['header']['payload'])
        review['header']['missing'] = _missing_required(
            review['header']['payload'],
            review['header']['required'],
        )
        review['header']['character_warnings'] = _payload_tss_character_warnings(
            review['header']['payload'],
            HEADER_REVIEW_LABELS,
            'ENS Header',
        )
        review['header']['warnings'] = list(review['header']['character_warnings'])
        review['character_warnings'].extend(review['header']['character_warnings'])

    for invoice_index, invoice in enumerate(review.get('invoices') or [], start=1):
        cons_payload = invoice.get('consignment', {}).get('payload') or {}
        invoice.setdefault('consignment', {})['payload'] = _stringify_payload(cons_payload)
        cons_base_warnings = _strip_tss_character_warnings(invoice['consignment'].get('warnings'))
        if mode == REVIEW_MODE_ATTACH_GOODS:
            invoice['consignment']['missing'] = []
            invoice['consignment']['character_warnings'] = []
            invoice['consignment']['warnings'] = cons_base_warnings
        else:
            invoice['consignment']['missing'] = _missing_required(
                invoice['consignment']['payload'],
                CONSIGNMENT_REQUIRED_FIELDS,
            )
            invoice['consignment']['character_warnings'] = _payload_tss_character_warnings(
                invoice['consignment']['payload'],
                CONSIGNMENT_REVIEW_LABELS,
                f'Consignment {invoice_index}',
            )
            invoice['consignment']['warnings'] = cons_base_warnings + invoice['consignment']['character_warnings']
            review['character_warnings'].extend(invoice['consignment']['character_warnings'])
        for goods_index, goods in enumerate(invoice.get('goods') or [], start=1):
            goods_payload = goods.get('payload') or {}
            goods['payload'] = _stringify_payload(goods_payload)
            goods['missing'] = _missing_required(goods['payload'], GOODS_REQUIRED_FIELDS)
            goods_base_warnings = _strip_tss_character_warnings(goods.get('warnings'))
            goods['character_warnings'] = _payload_tss_character_warnings(
                goods['payload'],
                GOODS_REVIEW_LABELS,
                f'Consignment {invoice_index} / Goods {goods_index}',
            )
            goods['warnings'] = goods_base_warnings + goods['character_warnings']
            review['character_warnings'].extend(goods['character_warnings'])

    review['warnings'] = review['enrichment_warnings'] + review['character_warnings']
    review['review_mode'] = mode

    cons_count, goods_count, missing_total = _review_counts(review)
    review['summary'] = {
        'consignment_count': cons_count,
        'goods_count': goods_count,
        'missing_total': missing_total,
    }
    return review


def _parsed_invoice_from_dict(data: dict) -> ParsedInvoice:
    data = data or {}
    consignee_data = data.get('consignee') or {}
    delivery_data = data.get('delivery_to') or {}
    lines = [
        ParsedInvoiceLine(**line)
        for line in (data.get('lines') or [])
    ]
    return ParsedInvoice(
        filename=data.get('filename') or '',
        template_id=data.get('template_id') or '',
        supplier_name=data.get('supplier_name') or '',
        invoice_number=data.get('invoice_number') or '',
        carrier_reference=data.get('carrier_reference') or '',
        external_document_number=data.get('external_document_number') or '',
        trade_terms=data.get('trade_terms') or '',
        credit_terms=data.get('credit_terms') or '',
        consignee=ParsedParty(**consignee_data),
        delivery_to=ParsedParty(**delivery_data),
        total_gross_weight_kg=data.get('total_gross_weight_kg') or '',
        total_net_weight_kg=data.get('total_net_weight_kg') or '',
        total_invoice_value=data.get('total_invoice_value') or '',
        total_packages=data.get('total_packages') or '',
        page_count=int(data.get('page_count') or 1),
        raw_text=data.get('raw_text') or '',
        lines=lines,
    )


def _goods_review_payload_for_db(payload: dict) -> dict:
    payload = dict(payload or {})
    for key, value in list(payload.items()):
        if isinstance(value, str):
            payload[key] = value.strip()
    payload['item_number'] = _to_intish(payload.get('item_number'), 1)
    payload['number_of_packages'] = _to_intish(payload.get('number_of_packages'), 1)
    for field in ('gross_mass_kg', 'net_mass_kg', 'item_invoice_amount'):
        payload[field] = _to_decimal(payload.get(field))
    for key, value in list(payload.items()):
        if value == '':
            payload[key] = None
    return payload


def _string_payload_for_db(payload: dict) -> dict:
    payload = dict(payload or {})
    for key, value in list(payload.items()):
        if isinstance(value, str):
            value = value.strip()
        if value == '':
            payload[key] = None
        else:
            payload[key] = value
    return payload


def _header_review_payload_for_db(payload: dict) -> dict:
    payload = _string_payload_for_db(payload)
    raw_dt = payload.get('arrival_date_time')
    if isinstance(raw_dt, str) and 'T' in raw_dt:
        try:
            payload['arrival_date_time'] = datetime.fromisoformat(raw_dt).strftime('%d/%m/%Y %H:%M:%S')
        except ValueError:
            pass
    return payload


def build_invoice_batch_review(
    invoices: list[ParsedInvoice],
    source: str = 'PORTAL_REVIEW',
    channel: str = 'UPLOAD',
    email_meta: dict | None = None,
    tenant_code: str | None = None,
) -> dict:
    """Build an editable review payload without creating staging records."""
    if not invoices:
        raise ValueError('No invoices supplied for review.')

    defaults = resolve_ingest_defaults(tenant_code=tenant_code)
    master_schema = _master_schema(tenant_code)
    schema_token = _ACTIVE_STAGE_SCHEMA.set(master_schema)

    try:
        header_payload = _build_header_payload(invoices, defaults, email_meta)
        review = {
            'schema_version': 1,
            'review_mode': REVIEW_MODE_BATCH_CREATE,
            'tenant_code': tenant_code,
            'source': source,
            'channel': channel,
            'email_meta': email_meta or {},
            'batch_label': _build_batch_label(invoices, email_meta),
            'automation_enabled': defaults.enabled,
            'ingestion_mode': defaults.mode,
            'header': {'payload': _stringify_payload(header_payload), 'missing': []},
            'invoices': [],
            'warnings': [],
        }

        with db_cursor(commit=False) as cursor:
            for invoice in invoices:
                consignment_payload, cons_warnings = _build_consignment_payload(cursor, invoice, defaults, master_schema)
                invoice_review = {
                    'filename': invoice.filename,
                    'template_id': invoice.template_id,
                    'invoice_number': invoice.invoice_number,
                    'carrier_reference': invoice.carrier_reference,
                    'parsed_invoice': asdict(invoice),
                    'suggested_attach': _find_existing_consignment_suggestion(cursor, invoice),
                    'consignment': {
                        'payload': _stringify_payload(consignment_payload),
                        'missing': [],
                        'warnings': cons_warnings,
                    },
                    'goods': [],
                }
                review['warnings'].extend(cons_warnings)

                for line in invoice.lines:
                    goods_payload, goods_warnings = _build_goods_payload(cursor, invoice, line, defaults, master_schema)
                    invoice_review['goods'].append({
                        'line_number': line.line_number,
                        'stock_code': line.stock_code,
                        'payload': _stringify_payload(goods_payload),
                        'missing': [],
                        'warnings': goods_warnings,
                    })
                    review['warnings'].extend(goods_warnings)

                review['invoices'].append(invoice_review)
    finally:
        _ACTIVE_STAGE_SCHEMA.reset(schema_token)

    return refresh_review_status(review)


def build_goods_attach_review(
    invoices: list[ParsedInvoice],
    consignment: dict,
    source: str = 'PORTAL_REVIEW',
    channel: str = 'UPLOAD',
    email_meta: dict | None = None,
    tenant_code: str | None = None,
) -> dict:
    review = build_invoice_batch_review(
        invoices,
        source=source,
        channel=channel,
        email_meta=email_meta,
        tenant_code=tenant_code,
    )
    return _goods_attach_review_from_invoice_reviews(
        review.get('invoices') or [],
        consignment,
        source=review.get('source') or source,
        channel=review.get('channel') or channel,
        email_meta=review.get('email_meta') or email_meta,
        tenant_code=review.get('tenant_code') or tenant_code,
        batch_label=review.get('batch_label'),
        automation_enabled=review.get('automation_enabled'),
        ingestion_mode=review.get('ingestion_mode'),
    )


def build_goods_attach_review_from_review(
    review: dict,
    consignment: dict,
    invoice_indexes: list[int] | None = None,
) -> dict:
    review = refresh_review_status(deepcopy(review or {}))
    invoice_reviews = review.get('invoices') or []
    if invoice_indexes is None:
        selected = invoice_reviews
    else:
        selected = [
            invoice_reviews[idx]
            for idx in invoice_indexes
            if 0 <= idx < len(invoice_reviews)
        ]
    return _goods_attach_review_from_invoice_reviews(
        selected,
        consignment,
        source=review.get('source') or 'PORTAL_REVIEW',
        channel=review.get('channel') or 'UPLOAD',
        email_meta=review.get('email_meta') or {},
        tenant_code=review.get('tenant_code'),
        batch_label=review.get('batch_label'),
        automation_enabled=review.get('automation_enabled'),
        ingestion_mode=review.get('ingestion_mode'),
    )


def _email_meta_received_at(email_meta: dict | None) -> datetime | None:
    raw_date = (email_meta or {}).get('date') or ''
    if not raw_date:
        return None
    try:
        return parsedate_to_datetime(raw_date)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _apply_email_body_metadata(payload: dict, email_meta: dict | None) -> dict:
    body = (email_meta or {}).get('body') or (email_meta or {}).get('email_body') or ''
    if not str(body).strip():
        return payload
    from app.ingestion.excel_sales_orders import parse_email_carrier_block

    meta = parse_email_carrier_block(str(body), received_at=_email_meta_received_at(email_meta))
    field_map = {
        'movement_type': meta.movement_type,
        'type_of_passive_transport': meta.type_of_passive_transport,
        'identity_no_of_transport': meta.identity_no_of_transport,
        'nationality_of_transport': meta.nationality_of_transport,
        'conveyance_ref': meta.conveyance_ref,
        'arrival_date_time': meta.arrival_date_time.strftime('%d/%m/%Y %H:%M:%S') if meta.arrival_date_time else '',
        'arrival_port': meta.arrival_port,
        'place_of_loading': meta.place_of_loading,
        'place_of_acceptance_same_as_loading': meta.place_of_acceptance_same_as_loading,
        'place_of_acceptance': meta.place_of_acceptance,
        'place_of_unloading': meta.place_of_unloading,
        'place_of_delivery_same_as_unloading': meta.place_of_delivery_same_as_unloading,
        'place_of_delivery': meta.place_of_delivery,
        'transport_charges': meta.transport_charges,
        'carrier_eori': meta.carrier_eori,
        'carrier_name': meta.carrier_name,
        'carrier_street_number': meta.carrier_street_number,
        'carrier_city': meta.carrier_city,
        'carrier_postcode': meta.carrier_postcode,
        'carrier_country': meta.carrier_country,
        'haulier_eori': meta.haulier_eori,
    }
    for key, value in field_map.items():
        if value not in (None, ''):
            payload[key] = value
    return payload


def _build_header_payload(invoices: list[ParsedInvoice], defaults, email_meta: dict | None = None) -> dict:
    first = invoices[0]
    arrival_port = defaults.arrival_port
    place_of_unloading = defaults.place_of_unloading or arrival_port
    payload = {
        'movement_type': defaults.movement_type,
        'type_of_passive_transport': '',
        'identity_no_of_transport': defaults.identity_no_of_transport,
        'nationality_of_transport': defaults.nationality_of_transport,
        'conveyance_ref': '',
        'arrival_date_time': defaults.build_arrival_datetime(),
        'arrival_port': arrival_port,
        'place_of_loading': defaults.place_of_loading,
        'place_of_unloading': place_of_unloading,
        'place_of_acceptance_same_as_loading': 'yes',
        'place_of_acceptance': '',
        'place_of_delivery_same_as_unloading': 'yes',
        'place_of_delivery': '',
        'seal_number': '',
        'transport_charges': defaults.transport_charges,
        'carrier_eori': defaults.carrier_eori,
        'carrier_name': defaults.carrier_name or defaults.supplier_name,
        'carrier_street_number': defaults.carrier_street_number,
        'carrier_city': defaults.carrier_city,
        'carrier_postcode': defaults.carrier_postcode,
        'carrier_country': defaults.carrier_country,
        'haulier_eori': defaults.haulier_eori,
        'batch_invoice_numbers': ', '.join(inv.invoice_number for inv in invoices if inv.invoice_number)[:500],
        'batch_carrier_references': ', '.join(inv.carrier_reference for inv in invoices if inv.carrier_reference)[:500],
        'batch_delivery_party': first.delivery_to.name or first.consignee.name,
    }
    return _apply_email_body_metadata(payload, email_meta)


def _resolve_counterparty(cursor, invoice: ParsedInvoice, defaults, master_schema: str):
    consignee = _best_partner_match(
        cursor,
        invoice.delivery_to.name or invoice.consignee.name,
        invoice.delivery_to.postcode or invoice.consignee.postcode,
        master_schema=master_schema,
    )
    supplier = _best_partner_match(cursor, defaults.supplier_name, master_schema=master_schema) if defaults.supplier_name else None
    destination_country = invoice.delivery_to.country_code or invoice.consignee.country_code
    consignee_name = (invoice.delivery_to.name or invoice.consignee.name or (consignee or {}).get('partner_name') or '').strip()
    supplier_name = defaults.supplier_name or invoice.supplier_name or (supplier or {}).get('partner_name') or ''

    consignee_street = (consignee or {}).get('address_line1') or None
    consignee_city = (consignee or {}).get('city') or None
    consignee_postcode = (consignee or {}).get('postcode') or None
    consignee_country = (consignee or {}).get('country') or destination_country or None
    default_importer_eori = str(getattr(defaults, 'importer_eori', '') or '').strip()
    default_importer_name = str(getattr(defaults, 'importer_name', '') or '').strip()
    default_importer_street = str(getattr(defaults, 'importer_street_number', '') or '').strip()
    default_importer_city = str(getattr(defaults, 'importer_city', '') or '').strip()
    default_importer_postcode = str(getattr(defaults, 'importer_postcode', '') or '').strip()
    default_importer_country = str(getattr(defaults, 'importer_country', '') or '').strip()

    supplier_street = (supplier or {}).get('address_line1') or None
    supplier_city = (supplier or {}).get('city') or None
    supplier_postcode = (supplier or {}).get('postcode') or None
    supplier_country = (supplier or {}).get('country') or None

    return {
        'destination_country': destination_country,
        'consignee_name': consignee_name,
        'consignee_eori': (consignee or {}).get('eori') or '',
        'consignee_street_number': consignee_street,
        'consignee_city': consignee_city,
        'consignee_postcode': consignee_postcode,
        'consignee_country': consignee_country,
        'importer_name': default_importer_name or consignee_name,
        'importer_eori': default_importer_eori or (consignee or {}).get('eori') or '',
        'importer_street_number': default_importer_street or consignee_street,
        'importer_city': default_importer_city or consignee_city,
        'importer_postcode': default_importer_postcode or consignee_postcode,
        'importer_country': default_importer_country or consignee_country,
        'consignor_name': supplier_name,
        'consignor_eori': (supplier or {}).get('eori') or defaults.consignor_eori,
        'consignor_street_number': supplier_street,
        'consignor_city': supplier_city,
        'consignor_postcode': supplier_postcode,
        'consignor_country': supplier_country,
        'exporter_name': supplier_name,
        'exporter_eori': (supplier or {}).get('eori') or defaults.exporter_eori,
        'exporter_street_number': supplier_street,
        'exporter_city': supplier_city,
        'exporter_postcode': supplier_postcode,
        'exporter_country': supplier_country,
    }


def _build_consignment_payload(cursor, invoice: ParsedInvoice, defaults, master_schema: str) -> tuple[dict, list[str]]:
    counterparty = _resolve_counterparty(cursor, invoice, defaults, master_schema)
    first_line = invoice.lines[0] if invoice.lines else None
    warnings = []
    if not counterparty.get('importer_eori'):
        warnings.append(f'Importer/consignee EORI not resolved for {invoice.delivery_to.name or invoice.consignee.name}.')
    if not counterparty.get('exporter_eori'):
        warnings.append(f'Exporter/consignor EORI not resolved for supplier {defaults.supplier_name}.')
    payload = {
        'label': invoice.carrier_reference or invoice.invoice_number or invoice.filename,
        'goods_description': (first_line.description if first_line else f'Invoice {invoice.invoice_number}')[:254],
        'trader_reference': invoice.external_document_number or invoice.invoice_number,
        'transport_document_number': invoice.carrier_reference or invoice.external_document_number or invoice.invoice_number,
        'controlled_goods': defaults.controlled_goods,
        'goods_domestic_status': defaults.goods_domestic_status or None,
        'destination_country': counterparty.get('destination_country') or '',
        'supervising_customs_office': None,
        'customs_warehouse_identifier': None,
        'ducr': None,
        'no_sfd_reason': None,
        'consignor_eori': counterparty.get('consignor_eori') or None,
        'consignor_name': counterparty.get('consignor_name') or None,
        'consignor_street_number': counterparty.get('consignor_street_number'),
        'consignor_city': counterparty.get('consignor_city'),
        'consignor_postcode': counterparty.get('consignor_postcode'),
        'consignor_country': counterparty.get('consignor_country'),
        'consignee_eori': counterparty.get('consignee_eori') or None,
        'consignee_name': counterparty.get('consignee_name') or None,
        'consignee_street_number': counterparty.get('consignee_street_number'),
        'consignee_city': counterparty.get('consignee_city'),
        'consignee_postcode': counterparty.get('consignee_postcode'),
        'consignee_country': counterparty.get('consignee_country'),
        'importer_eori': counterparty.get('importer_eori') or None,
        'importer_name': counterparty.get('importer_name') or None,
        'importer_street_number': counterparty.get('importer_street_number'),
        'importer_city': counterparty.get('importer_city'),
        'importer_postcode': counterparty.get('importer_postcode'),
        'importer_country': counterparty.get('importer_country'),
        'exporter_eori': counterparty.get('exporter_eori') or None,
        'exporter_name': counterparty.get('exporter_name') or None,
        'exporter_street_number': counterparty.get('exporter_street_number'),
        'exporter_city': counterparty.get('exporter_city'),
        'exporter_postcode': counterparty.get('exporter_postcode'),
        'exporter_country': counterparty.get('exporter_country'),
        'buyer_same_as_importer': 'yes',
        'seller_same_as_exporter': 'yes',
        'container_indicator': defaults.container_indicator,
        'align_ukims': None,
        'use_importer_sde': None,
        'declaration_choice': None,
        'generate_SD': 'no',
        'internal_notes': 'Auto-created from invoice ingest.',
    }
    return payload, warnings


def _build_goods_payload(cursor, invoice: ParsedInvoice, line, defaults, master_schema: str) -> tuple[dict, list[str]]:
    product = _product_match(cursor, line.stock_code, master_schema=master_schema)
    warnings = []
    if not product:
        warnings.append(f'Product defaults not found for stock code {line.stock_code}; using ingest defaults.')

    payload = {
        'item_number': line.line_number,
        'label': line.stock_code,
        'goods_description': (product or {}).get('goods_description') or line.description[:254],
        'type_of_packages': (product or {}).get('package_type') or _uom_package_type(line.uom, defaults.package_type),
        'number_of_packages': _to_intish(line.quantity, 1),
        'package_marks': (product or {}).get('package_marks') or invoice.carrier_reference or invoice.invoice_number or line.stock_code,
        'gross_mass_kg': _to_decimal(line.gross_mass_kg),
        'net_mass_kg': _to_decimal(line.net_mass_kg),
        'equipment_number': None,
        'controlled_goods': defaults.controlled_goods,
        'controlled_goods_type': None,
        'commodity_code': line.commodity_code or (product or {}).get('commodity_code') or None,
        'taric_code': None,
        'procedure_code': (product or {}).get('procedure_code') or defaults.procedure_code,
        'additional_procedure_code': defaults.additional_procedure_code,
        'country_of_origin': (product or {}).get('country_of_origin') or defaults.country_of_origin,
        'national_additional_code': None,
        'preference': (product or {}).get('preference_code') or None,
        'item_invoice_amount': _to_decimal(line.line_value),
        'item_invoice_currency': invoice.lines[0].currency if invoice.lines else defaults.invoice_currency,
        'valuation_method': (product or {}).get('valuation_method') or defaults.valuation_method,
        'invoice_number': invoice.invoice_number,
        'ni_additional_information_codes': (product or {}).get('ni_additional_info_code') or None,
        'internal_notes': f'Auto-created from invoice {invoice.invoice_number}.',
    }
    return payload, warnings


def _insert_ingest_document(cursor, invoice: ParsedInvoice, source: str, channel: str, email_meta: dict | None) -> int | None:
    if not _table_exists(cursor, 'DocIngestDocument'):
        return None
    columns = _columns(cursor, 'DocIngestDocument')
    values = {
        'original_filename': invoice.filename,
        'file_size_bytes': None,
        'mime_type': 'application/pdf',
        'page_count': invoice.page_count,
        'customer_code': 'BIRKDALE',
        'doc_type': 'COMMERCIAL_INVOICE',
        'doc_sub_type': invoice.template_id,
        'routing_notes': (email_meta or {}).get('subject') or 'Auto-staged via local invoice ingestion.',
        'status': 'STAGED',
        'overall_confidence': Decimal('0.95'),
        'uploaded_by': 'ingestion',
        'source': source,
        'created_at': 'SYSUTCDATETIME()',
        'routing_at': 'SYSUTCDATETIME()',
        'processing_completed_at': 'SYSUTCDATETIME()',
        'updated_at': 'SYSUTCDATETIME()',
    }
    insert_cols = []
    insert_vals = []
    params = []
    for key, value in values.items():
        if key.lower() not in columns:
            continue
        insert_cols.append(key)
        if isinstance(value, str) and value == 'SYSUTCDATETIME()':
            insert_vals.append(value)
        else:
            insert_vals.append('?')
            params.append(value)
    cursor.execute(
        f"INSERT INTO {_staging_schema()}.DocIngestDocument ({', '.join(insert_cols)}) OUTPUT INSERTED.id VALUES ({', '.join(insert_vals)})",
        params,
    )
    return int(cursor.fetchone()[0])


def _insert_ingest_header(cursor, document_id: int | None, invoice: ParsedInvoice, consignment_payload: dict):
    if not document_id or not _table_exists(cursor, 'DocIngestHeader'):
        return
    columns = _columns(cursor, 'DocIngestHeader')
    values = {
        'document_id': document_id,
        'document_number': invoice.invoice_number,
        'trader_reference': invoice.carrier_reference or invoice.external_document_number,
        'consignee_name': consignment_payload.get('consignee_name'),
        'consignee_eori': consignment_payload.get('consignee_eori'),
        'consignee_address': invoice.delivery_to.address_text or invoice.consignee.address_text,
        'consignee_country': invoice.delivery_to.country_code or invoice.consignee.country_code,
        'consignor_name': consignment_payload.get('consignor_name'),
        'consignor_eori': consignment_payload.get('consignor_eori'),
        'consignor_address': '',
        'consignor_country': 'GB',
        'buyer_name': consignment_payload.get('importer_name'),
        'buyer_eori': consignment_payload.get('importer_eori'),
        'seller_name': consignment_payload.get('exporter_name'),
        'seller_eori': consignment_payload.get('exporter_eori'),
        'importer_name': consignment_payload.get('importer_name'),
        'importer_eori': consignment_payload.get('importer_eori'),
        'exporter_name': consignment_payload.get('exporter_name'),
        'exporter_eori': consignment_payload.get('exporter_eori'),
        'currency': 'GBP',
        'incoterms_code': (invoice.trade_terms or '')[:5] or None,
        'delivery_location': invoice.delivery_to.address_text or invoice.consignee.address_text,
        'total_invoice_value': _to_decimal(invoice.total_invoice_value),
        'total_gross_weight_kg': _to_decimal(invoice.total_gross_weight_kg),
        'total_net_weight_kg': _to_decimal(invoice.total_net_weight_kg),
        'total_packages': _to_intish(invoice.total_packages, 0),
        'country_of_destination': invoice.delivery_to.country_code or invoice.consignee.country_code,
        'confidence_score': Decimal('0.95'),
        'updated_at': 'SYSUTCDATETIME()',
    }
    insert_cols = []
    insert_vals = []
    params = []
    for key, value in values.items():
        if key.lower() not in columns:
            continue
        insert_cols.append(key)
        if isinstance(value, str) and value == 'SYSUTCDATETIME()':
            insert_vals.append(value)
        else:
            insert_vals.append('?')
            params.append(value)
    cursor.execute(f"INSERT INTO {_staging_schema()}.DocIngestHeader ({', '.join(insert_cols)}) VALUES ({', '.join(insert_vals)})", params)


def _insert_ingest_summary(cursor, document_id: int | None, invoice: ParsedInvoice):
    if not document_id or not _table_exists(cursor, 'DocIngestSummary'):
        return
    cursor.execute(
        f"""
        INSERT INTO {_staging_schema()}.DocIngestSummary
            (document_id, total_lines, total_packages, total_gross_weight_kg,
             total_net_weight_kg, total_invoice_value, currency, confidence_score, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME())
        """,
        [
            document_id,
            len(invoice.lines),
            _to_intish(invoice.total_packages, 0),
            _to_decimal(invoice.total_gross_weight_kg),
            _to_decimal(invoice.total_net_weight_kg),
            _to_decimal(invoice.total_invoice_value),
            'GBP',
            Decimal('0.95'),
        ],
    )


def _insert_ingest_lines(cursor, document_id: int | None, invoice: ParsedInvoice):
    if not document_id or not _table_exists(cursor, 'DocIngestLine'):
        return
    for line in invoice.lines:
        cursor.execute(
            f"""
            INSERT INTO {_staging_schema()}.DocIngestLine
                (document_id, line_number, description, product_code, commodity_code,
                 quantity, unit_of_measure, number_of_packages, type_of_package,
                 marks_and_numbers, line_value, currency, gross_weight_kg, net_weight_kg, confidence_score, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME())
            """,
            [
                document_id,
                line.line_number,
                line.description[:500],
                line.stock_code,
                line.commodity_code,
                _to_decimal(line.quantity),
                line.uom[:20],
                _to_intish(line.quantity, 1),
                _uom_package_type(line.uom, 'PK'),
                invoice.carrier_reference[:200] if invoice.carrier_reference else line.stock_code,
                _to_decimal(line.line_value),
                line.currency,
                _to_decimal(line.gross_mass_kg),
                _to_decimal(line.net_mass_kg),
                Decimal('0.95'),
            ],
        )


def _insert_declaration(cursor, payload: dict, source: str) -> int:
    cursor.execute(
        f"""
        INSERT INTO {_staging_schema()}.StagingDeclarations
            (declaration_type, status, source,
             movement_type, arrival_port, arrival_date_time,
             carrier_name, carrier_eori, identity_no_of_transport,
             nationality_of_transport, seal_number, transport_charges,
             type_of_passive_transport, conveyance_ref,
             place_of_loading, place_of_unloading,
             place_of_acceptance_same, place_of_acceptance,
             place_of_delivery_same, place_of_delivery,
             carrier_street_number, carrier_city, carrier_postcode,
             carrier_country, haulier_eori,
             payload_json, created_by)
        OUTPUT INSERTED.id
        VALUES ('ENS_HEADER', 'Inserted', ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            source,
            payload['movement_type'], payload['arrival_port'],
            payload['arrival_date_time'], payload['carrier_name'],
            payload['carrier_eori'], payload['identity_no_of_transport'],
            payload['nationality_of_transport'], payload['seal_number'],
            payload['transport_charges'], payload['type_of_passive_transport'],
            payload['conveyance_ref'], payload['place_of_loading'],
            payload['place_of_unloading'],
            payload['place_of_acceptance_same_as_loading'],
            payload['place_of_acceptance'],
            payload['place_of_delivery_same_as_unloading'],
            payload['place_of_delivery'],
            payload['carrier_street_number'], payload['carrier_city'],
            payload['carrier_postcode'], payload['carrier_country'],
            payload['haulier_eori'], json.dumps(payload), 'ingestion',
        ],
    )
    return int(cursor.fetchone()[0])


def _insert_ens_stub(cursor, declaration_id: int, payload: dict, label: str, source: str) -> int:
    columns = _columns(cursor, 'StagingEnsHeaders')
    data = {
        'label': label,
        'ens_reference': None,
        'status': 'PENDING',
        'movement_type': payload.get('movement_type'),
        'arrival_port': payload.get('arrival_port'),
        'arrival_date_time': payload.get('arrival_date_time'),
        'identity_no_of_transport': payload.get('identity_no_of_transport'),
        'nationality_of_transport': payload.get('nationality_of_transport'),
        'source': source,
        'updated_at': 'SYSUTCDATETIME()',
        'staging_declaration_id': declaration_id,
    }
    insert_cols = []
    insert_vals = []
    params = []
    for key, value in data.items():
        if key.lower() not in columns:
            continue
        insert_cols.append(key)
        if isinstance(value, str) and value == 'SYSUTCDATETIME()':
            insert_vals.append(value)
        else:
            insert_vals.append('?')
            params.append(value)
    cursor.execute(
        f"INSERT INTO {_staging_schema()}.StagingEnsHeaders ({', '.join(insert_cols)}) OUTPUT INSERTED.staging_id VALUES ({', '.join(insert_vals)})",
        params,
    )
    return int(cursor.fetchone()[0])


def _insert_consignment(cursor, staging_ens_id: int, payload: dict, source: str) -> int:
    cursor.execute(
        f"""
        INSERT INTO {_staging_schema()}.StagingConsignments (
            staging_ens_id, label, goods_description,
            trader_reference, transport_document_number,
            controlled_goods, goods_domestic_status,
            destination_country, supervising_customs_office,
            customs_warehouse_identifier, ducr, no_sfd_reason,
            consignor_eori, consignor_name,
            consignor_street_number, consignor_city, consignor_postcode, consignor_country,
            consignee_eori, consignee_name,
            consignee_street_number, consignee_city, consignee_postcode, consignee_country,
            importer_eori, importer_name,
            importer_street_number, importer_city, importer_postcode, importer_country,
            exporter_eori, exporter_name,
            exporter_street_number, exporter_city, exporter_postcode, exporter_country,
            buyer_same_as_importer, seller_same_as_exporter,
            container_indicator, source, internal_notes,
            status, retry_count, max_retries, created_at, updated_at
        )
        OUTPUT INSERTED.staging_id
        VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            'PENDING', 0, 3, SYSUTCDATETIME(), SYSUTCDATETIME()
        )
        """,
        [
            staging_ens_id,
            payload.get('label'),
            payload.get('goods_description'),
            payload.get('trader_reference'),
            payload.get('transport_document_number'),
            payload.get('controlled_goods'),
            payload.get('goods_domestic_status'),
            payload.get('destination_country'),
            payload.get('supervising_customs_office'),
            payload.get('customs_warehouse_identifier'),
            payload.get('ducr'),
            payload.get('no_sfd_reason'),
            payload.get('consignor_eori'),
            payload.get('consignor_name'),
            payload.get('consignor_street_number'),
            payload.get('consignor_city'),
            payload.get('consignor_postcode'),
            payload.get('consignor_country'),
            payload.get('consignee_eori'),
            payload.get('consignee_name'),
            payload.get('consignee_street_number'),
            payload.get('consignee_city'),
            payload.get('consignee_postcode'),
            payload.get('consignee_country'),
            payload.get('importer_eori'),
            payload.get('importer_name'),
            payload.get('importer_street_number'),
            payload.get('importer_city'),
            payload.get('importer_postcode'),
            payload.get('importer_country'),
            payload.get('exporter_eori'),
            payload.get('exporter_name'),
            payload.get('exporter_street_number'),
            payload.get('exporter_city'),
            payload.get('exporter_postcode'),
            payload.get('exporter_country'),
            payload.get('buyer_same_as_importer'),
            payload.get('seller_same_as_exporter'),
            payload.get('container_indicator'),
            source,
            payload.get('internal_notes'),
        ],
    )
    return int(cursor.fetchone()[0])


def _insert_goods(cursor, staging_cons_id: int, payload: dict, source: str) -> int:
    cursor.execute(
        f"""
        INSERT INTO {_staging_schema()}.StagingGoodsItems (
            staging_cons_id, item_number, label, goods_description,
            type_of_packages, number_of_packages, package_marks,
            gross_mass_kg, net_mass_kg, equipment_number,
            controlled_goods, controlled_goods_type,
            commodity_code, taric_code, procedure_code, additional_procedure_code,
            country_of_origin, national_additional_code, preference,
            item_invoice_amount, item_invoice_currency, valuation_method,
            invoice_number, ni_additional_information_codes,
            source, internal_notes, status, retry_count, max_retries, created_at, updated_at
        )
        OUTPUT INSERTED.staging_id
        VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            'PENDING', 0, 3, SYSUTCDATETIME(), SYSUTCDATETIME()
        )
        """,
        [
            staging_cons_id,
            payload.get('item_number'),
            payload.get('label'),
            payload.get('goods_description'),
            payload.get('type_of_packages'),
            payload.get('number_of_packages'),
            payload.get('package_marks'),
            payload.get('gross_mass_kg'),
            payload.get('net_mass_kg'),
            payload.get('equipment_number'),
            payload.get('controlled_goods'),
            payload.get('controlled_goods_type'),
            payload.get('commodity_code'),
            payload.get('taric_code'),
            payload.get('procedure_code'),
            payload.get('additional_procedure_code'),
            payload.get('country_of_origin'),
            payload.get('national_additional_code'),
            payload.get('preference'),
            payload.get('item_invoice_amount'),
            payload.get('item_invoice_currency'),
            payload.get('valuation_method'),
            payload.get('invoice_number'),
            payload.get('ni_additional_information_codes'),
            source,
            payload.get('internal_notes'),
        ],
    )
    return int(cursor.fetchone()[0])


def _update_document_links(cursor, document_id: int | None, staging_ens_id: int, staging_cons_id: int):
    if not document_id or not _table_exists(cursor, 'DocIngestDocument'):
        return
    columns = _columns(cursor, 'DocIngestDocument')
    assignments = []
    params = []
    if 'staging_ens_id' in columns:
        assignments.append('staging_ens_id = ?')
        params.append(staging_ens_id)
    if 'staging_cons_id' in columns:
        assignments.append('staging_cons_id = ?')
        params.append(staging_cons_id)
    if 'processing_completed_at' in columns:
        assignments.append('processing_completed_at = SYSUTCDATETIME()')
    if 'updated_at' in columns:
        assignments.append('updated_at = SYSUTCDATETIME()')
    if not assignments:
        return
    cursor.execute(
        f"UPDATE {_staging_schema()}.DocIngestDocument SET {', '.join(assignments)} WHERE id = ?",
        params + [document_id],
    )


def _insert_integration_log(cursor, document_id: int | None, target_table: str, target_record_id: int, target_ref: str, payload: dict):
    if not document_id or not _table_exists(cursor, 'IngestIntegrationLog'):
        return
    cursor.execute(
        f"""
        INSERT INTO {_staging_schema()}.IngestIntegrationLog
            (document_id, target_service, target_table, target_record_id, target_ref,
             field_mapping_json, payload_json, status, integrated_by)
        VALUES (?, 'FUSION_FLOW', ?, ?, ?, ?, ?, 'SUCCESS', 'ingestion')
        """,
        [
            document_id,
            target_table,
            target_record_id,
            target_ref[:100] if target_ref else None,
            json.dumps({}, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False, default=str)[:4000],
        ],
    )


def stage_invoice_batch(
    invoices: list[ParsedInvoice],
    source: str = 'EMAIL_INGEST',
    channel: str = 'EMAIL',
    email_meta: dict | None = None,
    tenant_code: str | None = None,
    no_sfd_reason: str = '',
):
    if not invoices:
        raise ValueError('No invoices supplied for staging.')

    defaults = resolve_ingest_defaults(tenant_code=tenant_code)
    if not defaults.enabled:
        raise ValueError('Local ingest automation is disabled by INGEST_AUTO.ENABLED.')
    no_sfd_reason = (no_sfd_reason or '').strip()
    master_schema = _master_schema(tenant_code)
    schema_token = _ACTIVE_STAGE_SCHEMA.set(master_schema)

    try:
        warnings: list[str] = []
        batch_label = _build_batch_label(invoices, email_meta)
        header_payload = _build_header_payload(invoices, defaults, email_meta)

        with db_cursor() as cursor:
            declaration_id = _insert_declaration(cursor, header_payload, source)
            staging_ens_id = _insert_ens_stub(cursor, declaration_id, header_payload, batch_label, source)

            document_ids: list[int | None] = []
            consignment_ids: list[int] = []
            goods_ids: list[int] = []

            for invoice in invoices:
                doc_id = _insert_ingest_document(cursor, invoice, source, channel, email_meta)
                document_ids.append(doc_id)
                consignment_payload, cons_warnings = _build_consignment_payload(cursor, invoice, defaults, master_schema)
                if no_sfd_reason:
                    consignment_payload['no_sfd_reason'] = no_sfd_reason
                    consignment_payload['generate_SD'] = 'no'
                warnings.extend(cons_warnings)
                _insert_ingest_header(cursor, doc_id, invoice, consignment_payload)
                _insert_ingest_summary(cursor, doc_id, invoice)
                _insert_ingest_lines(cursor, doc_id, invoice)

                consignment_id = _insert_consignment(cursor, staging_ens_id, consignment_payload, source)
                consignment_ids.append(consignment_id)
                _update_document_links(cursor, doc_id, staging_ens_id, consignment_id)
                _insert_integration_log(cursor, doc_id, f'{_staging_schema()}.StagingConsignments', consignment_id, consignment_payload.get('label') or '', consignment_payload)
                _log_consignment(
                    cursor,
                    env_code=_env_code(),
                    staging_cons_id=consignment_id,
                    target_ref=consignment_payload.get('label') or consignment_payload.get('transport_document_number'),
                    source_table=f'ING.BKD_EmailAttachment' if channel == 'EMAIL' else 'app/ingestion/stage.py',
                    source_document_no=invoice.invoice_number or invoice.carrier_reference,
                    file_id=None,
                    processed_by=source,
                )

                for line in invoice.lines:
                    goods_payload, goods_warnings = _build_goods_payload(cursor, invoice, line, defaults, master_schema)
                    warnings.extend(goods_warnings)
                    goods_id = _insert_goods(cursor, consignment_id, goods_payload, source)
                    goods_ids.append(goods_id)
                    _log_goods(
                        cursor,
                        env_code=_env_code(),
                        staging_goods_id=goods_id,
                        staging_cons_id=consignment_id,
                        source_table=f'ING.BKD_EmailAttachment' if channel == 'EMAIL' else 'app/ingestion/stage.py',
                        source_document_no=invoice.invoice_number or invoice.carrier_reference,
                        source_row_num=line.line_number,
                        file_id=None,
                        processed_by=source,
                    )
    finally:
        _ACTIVE_STAGE_SCHEMA.reset(schema_token)

    validation = {'declaration': None, 'consignments': [], 'goods': []}
    if defaults.auto_validate:
        validation['declaration'] = auto_validate_declaration_record(declaration_id)
        for consignment_id in consignment_ids:
            validation['consignments'].append(auto_validate_consignment_record(consignment_id))
        for goods_id in goods_ids:
            validation['goods'].append(auto_validate_goods_record(goods_id))

    return {
        'declaration_id': declaration_id,
        'staging_ens_id': staging_ens_id,
        'document_ids': document_ids,
        'consignment_ids': consignment_ids,
        'goods_ids': goods_ids,
        'header_payload': header_payload,
        'warnings': warnings,
        'validation': validation,
    }


def stage_invoice_review(review: dict, tenant_code: str | None = None, approved_by: str = 'ingestion_review') -> dict:
    """Create ENS, consignments and goods from an approved review payload."""
    review = refresh_review_status(review or {})
    missing_total = review.get('summary', {}).get('missing_total') or 0
    if missing_total:
        raise ValueError(f'Review still has {missing_total} required field(s) missing.')

    defaults = resolve_ingest_defaults(tenant_code=tenant_code or review.get('tenant_code'))
    if not defaults.enabled:
        raise ValueError('Local ingest automation is disabled by INGEST_AUTO.ENABLED.')

    master_schema = _master_schema(tenant_code or review.get('tenant_code'))
    schema_token = _ACTIVE_STAGE_SCHEMA.set(master_schema)
    source = review.get('source') or 'PORTAL_REVIEW'
    channel = review.get('channel') or 'UPLOAD'
    email_meta = review.get('email_meta') or {}
    header_payload = _header_review_payload_for_db(review.get('header', {}).get('payload') or {})
    batch_label = review.get('batch_label') or 'Ingest review batch'

    try:
        warnings: list[str] = list(review.get('warnings') or [])
        with db_cursor() as cursor:
            declaration_id = _insert_declaration(cursor, header_payload, source)
            staging_ens_id = _insert_ens_stub(cursor, declaration_id, header_payload, batch_label, source)

            document_ids: list[int | None] = []
            consignment_ids: list[int] = []
            goods_ids: list[int] = []

            for invoice_review in review.get('invoices') or []:
                invoice = _parsed_invoice_from_dict(invoice_review.get('parsed_invoice') or {})
                consignment_payload = _string_payload_for_db(invoice_review.get('consignment', {}).get('payload') or {})
                if consignment_payload.get('no_sfd_reason'):
                    consignment_payload['generate_SD'] = 'no'
                doc_id = _insert_ingest_document(cursor, invoice, source, channel, email_meta)
                document_ids.append(doc_id)
                _insert_ingest_header(cursor, doc_id, invoice, consignment_payload)
                _insert_ingest_summary(cursor, doc_id, invoice)
                _insert_ingest_lines(cursor, doc_id, invoice)

                consignment_id = _insert_consignment(cursor, staging_ens_id, consignment_payload, source)
                consignment_ids.append(consignment_id)
                _update_document_links(cursor, doc_id, staging_ens_id, consignment_id)
                _insert_integration_log(
                    cursor,
                    doc_id,
                    f'{_staging_schema()}.StagingConsignments',
                    consignment_id,
                    consignment_payload.get('label') or '',
                    consignment_payload,
                )

                for goods_review in invoice_review.get('goods') or []:
                    goods_payload = _goods_review_payload_for_db(goods_review.get('payload') or {})
                    goods_id = _insert_goods(cursor, consignment_id, goods_payload, source)
                    goods_ids.append(goods_id)

                if doc_id and _table_exists(cursor, 'DocIngestDocument'):
                    columns = _columns(cursor, 'DocIngestDocument')
                    assignments = []
                    params = []
                    if 'reviewed_at' in columns:
                        assignments.append('reviewed_at = SYSUTCDATETIME()')
                    if 'approved_at' in columns:
                        assignments.append('approved_at = SYSUTCDATETIME()')
                    if 'approved_by' in columns:
                        assignments.append('approved_by = ?')
                        params.append(approved_by)
                    if assignments:
                        cursor.execute(
                            f"UPDATE {_staging_schema()}.DocIngestDocument SET {', '.join(assignments)} WHERE id = ?",
                            params + [doc_id],
                        )
    finally:
        _ACTIVE_STAGE_SCHEMA.reset(schema_token)

    validation = {'declaration': None, 'consignments': [], 'goods': []}
    if defaults.auto_validate:
        validation['declaration'] = auto_validate_declaration_record(declaration_id)
        for consignment_id in consignment_ids:
            validation['consignments'].append(auto_validate_consignment_record(consignment_id))
        for goods_id in goods_ids:
            validation['goods'].append(auto_validate_goods_record(goods_id))

    return {
        'declaration_id': declaration_id,
        'staging_ens_id': staging_ens_id,
        'document_ids': document_ids,
        'consignment_ids': consignment_ids,
        'goods_ids': goods_ids,
        'header_payload': header_payload,
        'warnings': warnings,
        'validation': validation,
    }


def stage_goods_attach_review(review: dict, tenant_code: str | None = None, approved_by: str = 'ingestion_review') -> dict:
    """Create goods only on an existing consignment from an approved review payload."""
    review = refresh_review_status(review or {})
    if _review_mode(review) != REVIEW_MODE_ATTACH_GOODS:
        raise ValueError('Review is not in goods attach mode.')

    missing_total = review.get('summary', {}).get('missing_total') or 0
    if missing_total:
        raise ValueError(f'Review still has {missing_total} required field(s) missing.')

    attach_target = review.get('attach_target') or {}
    staging_cons_id = int(attach_target.get('staging_id') or 0)
    if not staging_cons_id:
        raise ValueError('Attach target consignment is missing.')

    defaults = resolve_ingest_defaults(tenant_code=tenant_code or review.get('tenant_code'))
    if not defaults.enabled:
        raise ValueError('Local ingest automation is disabled by INGEST_AUTO.ENABLED.')

    master_schema = _master_schema(tenant_code or review.get('tenant_code'))
    schema_token = _ACTIVE_STAGE_SCHEMA.set(master_schema)
    source = review.get('source') or 'PORTAL_ATTACH_REVIEW'
    channel = review.get('channel') or 'UPLOAD'
    email_meta = review.get('email_meta') or {}

    try:
        warnings: list[str] = list(review.get('warnings') or [])
        with db_cursor() as cursor:
            cursor.execute(
                f"""
                SELECT c.staging_id, c.staging_ens_id, c.status, c.tss_status, c.dec_reference,
                       c.sfd_reference, c.transport_document_number, c.controlled_goods,
                       c.container_indicator, e.ens_reference
                       ,c.consignee_name, c.consignee_eori, c.consignor_name, c.consignor_eori,
                       c.importer_name, c.importer_eori, c.exporter_name, c.exporter_eori
                FROM {_staging_schema()}.StagingConsignments c
                LEFT JOIN {_staging_schema()}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
                WHERE c.staging_id = ?
                """,
                [staging_cons_id],
            )
            parent = _row_as_dict(cursor)
            if parent is None:
                raise ValueError('The selected consignment no longer exists.')
            if not tss_allows_data_changes(parent.get('tss_status'), parent.get('status')):
                raise ValueError(
                    tss_data_lock_reason(parent.get('tss_status'), parent.get('status'), entity_label='consignment')
                    or 'This consignment is locked for new goods.'
                )

            next_item = _next_goods_item_number(cursor, staging_cons_id)
            document_ids: list[int | None] = []
            goods_ids: list[int] = []

            for invoice_review in review.get('invoices') or []:
                invoice = _parsed_invoice_from_dict(invoice_review.get('parsed_invoice') or {})
                doc_id = _insert_ingest_document(cursor, invoice, source, channel, email_meta)
                document_ids.append(doc_id)
                target_summary_payload = {
                    'consignee_name': parent.get('consignee_name') or '',
                    'consignee_eori': parent.get('consignee_eori') or '',
                    'consignor_name': parent.get('consignor_name') or '',
                    'consignor_eori': parent.get('consignor_eori') or '',
                    'importer_name': parent.get('importer_name') or '',
                    'importer_eori': parent.get('importer_eori') or '',
                    'exporter_name': parent.get('exporter_name') or '',
                    'exporter_eori': parent.get('exporter_eori') or '',
                }
                _insert_ingest_header(cursor, doc_id, invoice, target_summary_payload)
                _insert_ingest_summary(cursor, doc_id, invoice)
                _insert_ingest_lines(cursor, doc_id, invoice)
                _update_document_links(cursor, doc_id, parent.get('staging_ens_id'), staging_cons_id)

                for goods_review in invoice_review.get('goods') or []:
                    goods_payload = _goods_review_payload_for_db(goods_review.get('payload') or {})
                    goods_payload['item_number'] = next_item
                    next_item += 1
                    goods_id = _insert_goods(cursor, staging_cons_id, goods_payload, source)
                    goods_ids.append(goods_id)

                if doc_id and _table_exists(cursor, 'DocIngestDocument'):
                    columns = _columns(cursor, 'DocIngestDocument')
                    assignments = []
                    params = []
                    if 'reviewed_at' in columns:
                        assignments.append('reviewed_at = SYSUTCDATETIME()')
                    if 'approved_at' in columns:
                        assignments.append('approved_at = SYSUTCDATETIME()')
                    if 'approved_by' in columns:
                        assignments.append('approved_by = ?')
                        params.append(approved_by)
                    if assignments:
                        cursor.execute(
                            f"UPDATE {_staging_schema()}.DocIngestDocument SET {', '.join(assignments)} WHERE id = ?",
                            params + [doc_id],
                        )
    finally:
        _ACTIVE_STAGE_SCHEMA.reset(schema_token)

    validation = {'consignment': None, 'goods': []}
    if defaults.auto_validate:
        validation['consignment'] = auto_validate_consignment_record(staging_cons_id)
        for goods_id in goods_ids:
            validation['goods'].append(auto_validate_goods_record(goods_id))

    return {
        'staging_cons_id': staging_cons_id,
        'document_ids': document_ids,
        'goods_ids': goods_ids,
        'warnings': warnings,
        'validation': validation,
    }
