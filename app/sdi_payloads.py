"""Helpers for building TSS SDI payloads from staging records."""

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.tss_text import tss_safe_text_suggestion

SDI_UPDATE_FIELDS = [
    'declaration_choice',
    'authorisation_type',
    'arrival_date_time',
    'representation_type',
    'goods_domestic_status',
    'additional_procedure',
    'supervising_customs_office',
    'customs_warehouse_identifier',
    'exporter_eori',
    'exporter_name',
    'exporter_street_number',
    'exporter_city',
    'exporter_postcode',
    'exporter_country',
    'importer_name',
    'importer_street_number',
    'importer_city',
    'importer_postcode',
    'importer_country',
    'movement_type',
    'destination_country',
    'nationality_of_transport',
    'identity_no_of_transport',
    'location_of_goods_border',
    'location_of_goods_other',
    'un_locode',
    'incoterm',
    'delivery_location_country',
    'delivery_location_town',
    'freight_charge',
    'freight_charge_currency',
    'insurance',
    'insurance_currency',
    'postponed_vat',
    'vat_adjustment',
    'vat_adjust_currency',
    'exchange_rate',
    'vat_number',
    'trader_reference',
    'transport_document_number',
    'controlled_goods',
    'goods_description',
]

SDI_HEADER_NESTED_FIELDS = {
    'header_previous_document': ('header_previous_document', 'header_previous_document_json'),
    'holder_of_authorisation': ('holder_of_authorisation', 'holder_of_authorisation_json'),
}

_TARIC_SEPARATOR_RE = re.compile(r'[\s,;:/\\|_-]+')
SDI_DOCUMENT_CODE_DENYLIST = {'1UKI'}

SDI_GOODS_REQUIRED_FIELDS = {
    'goods_description': ('goods_description',),
    'commodity_code': ('commodity_code',),
    'procedure_code': ('procedure_code',),
    'number_of_packages': ('number_of_packages',),
    'type_of_packages': ('type_of_packages', 'type_of_package'),
    'package_marks': ('package_marks',),
    'gross_mass_kg': ('gross_mass_kg', 'gross_weight_kg'),
    'item_invoice_amount': ('item_invoice_amount', 'customs_value'),
    'item_invoice_currency': ('item_invoice_currency',),
    'valuation_method': ('valuation_method',),
    'nature_of_transaction': ('nature_of_transaction',),
    'preference': ('preference',),
    'additional_procedure_code': ('additional_procedure_code', 'additional_procedure_codes'),
    'ni_additional_information_codes': ('ni_additional_information_codes', 'national_additional_codes'),
}

SDI_GOODS_OPTIONAL_FIELDS = {
    'net_mass_kg': ('net_mass_kg', 'net_weight_kg'),
    'country_of_origin': ('country_of_origin',),
    'valuation_indicator': ('valuation_indicator',),
    'invoice_number': ('invoice_number',),
    'country_of_preferential_origin': ('country_of_preferential_origin',),
    'statistical_value': ('statistical_value',),
    'supplementary_units': ('supplementary_units',),
    'quota_order_number': ('quota_order_number',),
    'equipment_number': ('equipment_number',),
    'taric_code': ('taric_code',),
    'cus_code': ('cus_code',),
    'national_additional_code': ('national_additional_code',),
    'un_dangerous_goods_code': ('un_dangerous_goods_code',),
}

SDI_GOODS_NESTED_FIELDS = {
    'additional_procedures': ('additional_procedures', 'additional_procedures_json'),
    'document_references': ('document_references', 'document_references_json'),
    'additional_information': ('additional_information', 'additional_information_json'),
    'detail_previous_document': ('detail_previous_document', 'detail_previous_document_json'),
    'item_add_ded': ('item_add_ded', 'item_add_ded_json'),
    'national_additional_codes': ('national_additional_codes', 'national_additional_codes_json'),
    'tax_bases': ('tax_bases', 'tax_bases_json'),
    'additional_parties': ('additional_parties', 'additional_parties_json'),
}


def _pick(record, *names):
    for name in names:
        value = record.get(name)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        return value
    return None


def _safe_goods_description(value):
    if value in (None, ''):
        return value
    return tss_safe_text_suggestion(str(value).strip())


def _format_sdi_datetime(value):
    if value in (None, ''):
        return value
    if isinstance(value, datetime):
        return value.strftime('%d/%m/%Y %H:%M:%S')
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).strftime('%d/%m/%Y %H:%M:%S')

    text = str(value).strip()
    if not text:
        return text
    normalized = text.replace('T', ' ').replace('Z', '').split('.')[0].strip()
    for fmt in (
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
        '%d/%m/%Y',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
        '%d-%m-%Y %H:%M:%S',
        '%d-%m-%Y %H:%M',
        '%d-%m-%Y',
    ):
        try:
            return datetime.strptime(normalized, fmt).strftime('%d/%m/%Y %H:%M:%S')
        except ValueError:
            continue
    return text


def _format_sdi_mass(value):
    if value in (None, ''):
        return value
    text = str(value).strip().replace(',', '')
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"SDI goods mass must be numeric: {value}") from exc
    amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return format(amount, 'f').rstrip('0').rstrip('.') or '0'


def normalise_taric_code(value):
    """Return TARIC additional codes as one compact string, per TSS v2.9.5."""
    if value in (None, ''):
        return None
    compact = _TARIC_SEPARATOR_RE.sub('', str(value).strip())
    return compact or None


def validate_taric_code(value):
    """Return validation errors for TSS v2.9.5 TARIC additional-code format."""
    if value in (None, ''):
        return []
    raw = str(value).strip()
    compact = normalise_taric_code(raw) or ''
    errors = []
    if raw != compact:
        errors.append('FORMAT: TARIC Code must be a continuous string with no spaces or separators')
    if len(compact) > 20:
        errors.append('LENGTH: TARIC Code exceeds 20 characters')
    if len(compact) % 4 != 0:
        errors.append('FORMAT: TARIC Code must be made of 4-character code segments')
    return errors


def _load_additions_deductions(value):
    if value in (None, ''):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ValueError('header_additions_deductions must be valid JSON') from exc
    else:
        parsed = value
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError('header_additions_deductions must be a JSON array')
    return parsed


def _load_nested_array(value, field_name):
    if value in (None, ''):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ValueError(f'{field_name} must be valid JSON') from exc
    else:
        parsed = value
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError(f'{field_name} must be a JSON array')
    return [item for item in parsed if item]


def _add_nested_arrays(payload, record, mapping, *, require_op_type=False):
    for target_name, source_names in mapping.items():
        value = _pick(record, *source_names)
        items = _load_nested_array(value, target_name)
        if require_op_type:
            items = [
                item for item in items
                if isinstance(item, dict) and str(item.get('op_type') or '').strip()
            ]
        if items:
            payload[target_name] = items


def _ensure_invoice_document_reference(payload, record):
    invoice_number = _pick(
        record,
        'invoice_number',
        'transport_document_number',
        'source_transport_document_number',
        'trader_reference',
    )
    if not invoice_number:
        return
    document_reference = normalise_invoice_document_reference(invoice_number)

    documents = payload.setdefault('document_references', [])
    for item in documents:
        if not isinstance(item, dict):
            continue
        if str(item.get('document_code') or '').strip().upper() == 'N935':
            item.setdefault('op_type', 'create')
            item['document_status'] = item.get('document_status') or 'AC'
            item['document_reference'] = document_reference
            return

    documents.append({
        'op_type': 'create',
        'document_code': 'N935',
        'document_status': 'AC',
        'document_reference': document_reference,
    })


def _sanitize_sdi_document_references(payload):
    documents = payload.get('document_references')
    if not isinstance(documents, list):
        return

    sanitized = []
    seen_n935 = False
    seen_other: set[tuple[str, str]] = set()  # (code, reference) dedup for non-N935 docs
    for item in documents:
        if not isinstance(item, dict):
            continue
        code = str(item.get('document_code') or '').strip().upper()
        if not code or code in SDI_DOCUMENT_CODE_DENYLIST:
            continue
        item = dict(item)
        item['document_code'] = code
        if code == 'N935':
            document_reference = str(item.get('document_reference') or '').strip()
            if not document_reference or seen_n935:
                continue
            item['document_reference'] = document_reference
            item['document_status'] = item.get('document_status') or 'AC'
            seen_n935 = True
        else:
            ref = str(item.get('document_reference') or '').strip()
            slot_key = (code, ref.upper())
            if slot_key in seen_other:
                continue
            seen_other.add(slot_key)
        sanitized.append(item)

    if sanitized:
        payload['document_references'] = sanitized
    else:
        payload.pop('document_references', None)


def normalise_invoice_document_reference(value):
    """Return the invoice reference format expected by TSS for N935."""
    text = str(value or '').strip()
    if not text:
        return text
    return text


def build_header_additions_deductions(record):
    """Build TSS v2.9.5-compliant header_additions_deductions items."""
    record = record or {}
    raw_items = _load_additions_deductions(
        _pick(record, 'header_additions_deductions', 'header_additions_deductions_json')
    )

    direct_code = _pick(record, 'addition_deduction_code')
    direct_value = _pick(record, 'addition_deduction_value')
    direct_currency = _pick(record, 'addition_deduction_currency')
    if direct_code or direct_value or direct_currency:
        raw_items.append({
            'op_type': _pick(record, 'addition_deduction_op_type') or 'create',
            'addition_deduction_code': direct_code,
            'addition_deduction_value': direct_value,
            'addition_deduction_currency': direct_currency,
        })

    items = []
    for idx, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f'header_additions_deductions item {idx} must be an object')
        code = _pick(item, 'addition_deduction_code', 'code')
        amount = _pick(item, 'addition_deduction_value', 'value')
        currency = _pick(item, 'addition_deduction_currency', 'currency')
        if not any((code, amount, currency)):
            continue
        missing = []
        if not code:
            missing.append('addition_deduction_code')
        if amount in (None, ''):
            missing.append('addition_deduction_value')
        if not currency:
            missing.append('addition_deduction_currency')
        elif len(str(currency).strip()) != 3:
            missing.append('addition_deduction_currency length 3')
        if missing:
            raise ValueError(
                f"header_additions_deductions item {idx} missing/invalid: {', '.join(missing)}"
            )
        items.append({
            'op_type': _pick(item, 'op_type') or 'create',
            'addition_deduction_code': str(code).strip(),
            'addition_deduction_value': str(amount).strip(),
            'addition_deduction_currency': str(currency).strip(),
        })
    return items


SDI_CREATE_EXTRA_FIELDS = [
    'authorisation_type',
    'arrival_date_time',
    'representation_type',
    'additional_procedure',
    'supervising_customs_office',
    'customs_warehouse_identifier',
]


def build_sdi_create_payload(record):
    """Return TSS payload for op_type=create standalone SDI from a staging record."""
    record = record or {}
    payload = {
        'op_type': 'create',
        'sup_dec_number': '',
        'declaration_choice': (_pick(record, 'declaration_choice') or 'H1').strip() or 'H1',
    }

    for field_name in SDI_UPDATE_FIELDS + SDI_CREATE_EXTRA_FIELDS:
        value = record.get(field_name)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        if field_name == 'arrival_date_time':
            value = _format_sdi_datetime(value)
        if field_name == 'goods_description':
            value = _safe_goods_description(value)
        payload[field_name] = value

    if payload.get('un_locode'):
        payload.pop('delivery_location_country', None)
        payload.pop('delivery_location_town', None)

    header_additions_deductions = build_header_additions_deductions(record)
    if header_additions_deductions:
        payload['header_additions_deductions'] = header_additions_deductions
    _add_nested_arrays(payload, record, SDI_HEADER_NESTED_FIELDS)

    return payload


def build_sdi_update_payload(record):
    """Return minimal, API-safe SDI update payload from a staging record."""
    record = record or {}
    sup_ref = (_pick(record, 'sup_dec_number') or '').strip()
    payload = {
        'op_type': 'update',
        'sup_dec_number': sup_ref,
        'declaration_choice': (_pick(record, 'declaration_choice') or 'H1').strip() or 'H1',
    }

    for field_name in SDI_UPDATE_FIELDS:
        value = record.get(field_name)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        if field_name == 'arrival_date_time':
            value = _format_sdi_datetime(value)
        if field_name == 'goods_description':
            value = _safe_goods_description(value)
        payload[field_name] = value

    if payload.get('un_locode'):
        payload.pop('delivery_location_country', None)
        payload.pop('delivery_location_town', None)

    header_additions_deductions = build_header_additions_deductions(record)
    if header_additions_deductions:
        payload['header_additions_deductions'] = header_additions_deductions
    _add_nested_arrays(payload, record, SDI_HEADER_NESTED_FIELDS, require_op_type=True)

    return payload


def validate_sdi_update_payload(record):
    """Return local blockers for a TSS supplementary declaration update."""
    record = record or {}
    missing = []

    for field_name in (
        'sup_dec_number',
        'declaration_choice',
        'authorisation_type',
        'arrival_date_time',
        'representation_type',
        'controlled_goods',
        'additional_procedure',
        'goods_domestic_status',
        'movement_type',
        'destination_country',
        'nationality_of_transport',
        'identity_no_of_transport',
        'postponed_vat',
        'incoterm',
    ):
        if _pick(record, field_name) is None:
            missing.append(field_name)

    if _pick(record, 'un_locode') is None and (
        _pick(record, 'delivery_location_country') is None
        or _pick(record, 'delivery_location_town') is None
    ):
        missing.append('un_locode or delivery_location_country/delivery_location_town')

    postponed_vat = str(_pick(record, 'postponed_vat') or '').strip().lower()
    if postponed_vat in {'yes', 'true', '1'} and _pick(record, 'vat_number') is None:
        missing.append('vat_number')

    incoterm = str(_pick(record, 'incoterm') or '').strip().upper()
    if incoterm in {'EXW', 'FCA', 'FAS'}:
        if _pick(record, 'freight_charge') is None:
            missing.append('freight_charge')
        if _pick(record, 'freight_charge_currency') is None:
            missing.append('freight_charge_currency')

    if _pick(record, 'exporter_eori') is None:
        exporter_address_missing = [
            field_name for field_name in (
                'exporter_name',
                'exporter_street_number',
                'exporter_city',
                'exporter_postcode',
                'exporter_country',
            )
            if _pick(record, field_name) is None
        ]
        if exporter_address_missing:
            missing.extend(exporter_address_missing)

    if missing:
        return [
            'SDI header is missing required fields for TSS update: '
            + ', '.join(dict.fromkeys(missing))
        ]
    try:
        build_sdi_update_payload(record)
    except ValueError as exc:
        return [str(exc)]
    return []


def build_sdi_goods_update_payload(record):
    """Return full-replacement SDI goods update payload for TSS."""
    record = record or {}
    payload = {}
    missing = []

    for target_name, source_names in SDI_GOODS_REQUIRED_FIELDS.items():
        value = _pick(record, *source_names)
        if value is None:
            missing.append(target_name)
            continue
        if target_name == 'goods_description':
            value = _safe_goods_description(value)
        if target_name == 'gross_mass_kg':
            value = _format_sdi_mass(value)
        payload[target_name] = value

    if missing:
        item_ref = _pick(record, 'item_number', 'goods_item_number', 'staging_id', 'id') or '?'
        raise ValueError(
            f"SDI goods item {item_ref} is missing required fields for TSS update: {', '.join(missing)}"
        )

    for target_name, source_names in SDI_GOODS_OPTIONAL_FIELDS.items():
        value = _pick(record, *source_names)
        if value is not None:
            if target_name == 'taric_code':
                value = normalise_taric_code(value)
            if target_name == 'net_mass_kg':
                value = _format_sdi_mass(value)
            payload[target_name] = value

    _default_net_mass_from_gross(payload)

    if str(payload.get('valuation_method') or '').strip() == '1' and not payload.get('valuation_indicator'):
        payload['valuation_indicator'] = '0000'

    _add_nested_arrays(payload, record, SDI_GOODS_NESTED_FIELDS, require_op_type=True)
    _ensure_invoice_document_reference(payload, record)
    _sanitize_sdi_document_references(payload)

    return payload


def _add_nested_arrays_best_effort(payload, record, mapping, *, require_op_type=False):
    warnings = []
    for target_name, source_names in mapping.items():
        value = _pick(record, *source_names)
        try:
            items = _load_nested_array(value, target_name)
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        if require_op_type:
            items = [
                item for item in items
                if isinstance(item, dict) and str(item.get('op_type') or '').strip()
            ]
        if items:
            payload[target_name] = items
    return warnings


def _default_net_mass_from_gross(payload):
    if payload.get('gross_mass_kg') in (None, ''):
        return
    if payload.get('net_mass_kg') in (None, ''):
        payload['net_mass_kg'] = payload['gross_mass_kg']
        return
    try:
        gross = Decimal(str(payload.get('gross_mass_kg')).strip())
        net = Decimal(str(payload.get('net_mass_kg')).strip())
    except (InvalidOperation, ValueError):
        return
    if net > gross:
        payload['net_mass_kg'] = payload['gross_mass_kg']


def build_sdi_update_payload_for_api_attempt(record):
    """Return a header payload for a real TSS attempt without local required-field gating."""
    record = record or {}
    try:
        return build_sdi_update_payload(record), []
    except ValueError as exc:
        warnings = [str(exc)]

    sup_ref = (_pick(record, 'sup_dec_number') or '').strip()
    payload = {
        'op_type': 'update',
        'sup_dec_number': sup_ref,
        'declaration_choice': (_pick(record, 'declaration_choice') or 'H1').strip() or 'H1',
    }

    for field_name in SDI_UPDATE_FIELDS:
        value = record.get(field_name)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        if field_name == 'arrival_date_time':
            value = _format_sdi_datetime(value)
        if field_name == 'goods_description':
            value = _safe_goods_description(value)
        payload[field_name] = value

    if payload.get('un_locode'):
        payload.pop('delivery_location_country', None)
        payload.pop('delivery_location_town', None)

    warnings.extend(
        _add_nested_arrays_best_effort(payload, record, SDI_HEADER_NESTED_FIELDS, require_op_type=True)
    )
    return payload, warnings


def build_sdi_goods_update_payload_for_api_attempt(record):
    """Return a goods payload for a real TSS attempt without local required-field gating."""
    record = record or {}
    try:
        return build_sdi_goods_update_payload(record), []
    except ValueError as exc:
        warnings = [str(exc)]

    payload = {}
    field_sources = {
        **SDI_GOODS_REQUIRED_FIELDS,
        **SDI_GOODS_OPTIONAL_FIELDS,
    }
    for target_name, source_names in field_sources.items():
        value = _pick(record, *source_names)
        if value is None:
            continue
        if target_name == 'goods_description':
            value = _safe_goods_description(value)
        elif target_name in {'gross_mass_kg', 'net_mass_kg'}:
            try:
                value = _format_sdi_mass(value)
            except ValueError as exc:
                warnings.append(str(exc))
        elif target_name == 'taric_code':
            value = normalise_taric_code(value) or value
        payload[target_name] = value

    _default_net_mass_from_gross(payload)

    if str(payload.get('valuation_method') or '').strip() == '1' and not payload.get('valuation_indicator'):
        payload['valuation_indicator'] = '0000'

    warnings.extend(
        _add_nested_arrays_best_effort(payload, record, SDI_GOODS_NESTED_FIELDS, require_op_type=True)
    )
    _ensure_invoice_document_reference(payload, record)
    _sanitize_sdi_document_references(payload)
    return payload, warnings
