"""
Declarations Blueprint -- Full ENS Workflow
Inline editing on detail page for error fields with TSS choice value dropdowns.
"""
import json, subprocess, sys, os, csv, io, re
from collections import namedtuple
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, session, current_app
from app.db import query_all, query_one, execute, db_cursor, insert_api_call_log
from app.pipeline_validation import normalise_package_type
from app.search_utils import search_matches_values
from app.sdi_links import attach_sdi_links_to_consignments, load_prd_sdi_links_for_context, merge_sdi_links
from app.sdi_payloads import normalise_taric_code
from app.status_utils import (
    TSS_FILTER_STATUS_TABS,
    canonical_filter_status,
    consignment_should_discover_sdi,
    effective_tss_filter_status,
    local_goods_status_after_parent_sync,
    normalize_status_key,
    status_filter_tabs,
    tss_allows_data_changes,
)
from app.tenant import get_tenant, qualified_table
from app.tss_guidance import clean_tss_message, explain_tss_error
from app.tss_text import tss_safe_text_suggestion

CVOption = namedtuple('CVOption', ['value', 'name'])

declarations_bp = Blueprint('declarations', __name__, template_folder='../../templates/declarations')

ENS_AUTHORISED_FOR_MOVEMENT_STATUSES = {
    'AUTHORISED_FOR_MOVEMENT',
    'Authorised for Movement',
    'authorised for movement',
}

# Statuses that unlock the Email ENS Pack action on the detail page.
# Authorised-for-movement is the earliest point where the pack is meaningful;
# arrived/cleared/completed remain valid because operators frequently re-send
# the pack post-arrival as a confirmation to hauliers or partners.
ENS_PACK_EMAIL_STATUSES = ENS_AUTHORISED_FOR_MOVEMENT_STATUSES | {
    'ARRIVED', 'Arrived', 'arrived',
    'CLEARED', 'Cleared', 'cleared',
    'COMPLETED', 'Completed', 'completed',
}


def _ens_is_authorised_for_movement(dec, pipeline_header):
    """Return True when TSS has marked the ENS or its header as authorised
    for movement. Checks both `StagingDeclarations.external_status` and the
    pipeline header `tss_status` because callers come in via either path."""
    candidates = (
        (dec or {}).get('external_status'),
        (pipeline_header or {}).get('tss_status'),
    )
    return any(
        str(value or '').strip() in ENS_AUTHORISED_FOR_MOVEMENT_STATUSES
        or normalize_status_key(value) == 'AUTHORISED FOR MOVEMENT'
        for value in candidates
    )


def _ens_pack_email_unlocked(dec, pipeline_header):
    """Return True when the ENS is authorised-for-movement OR already past
    that point (arrived/cleared/completed). Same dual-source check as the
    movement helper so the badge mirroring TSS still drives the gate."""
    candidates = (
        (dec or {}).get('external_status'),
        (pipeline_header or {}).get('tss_status'),
    )
    unlocked_keys = {'AUTHORISED FOR MOVEMENT', 'ARRIVED', 'CLEARED', 'COMPLETED'}
    return any(
        str(value or '').strip() in ENS_PACK_EMAIL_STATUSES
        or normalize_status_key(value) in unlocked_keys
        for value in candidates
    )


def _can_email_ens_pack(dec, pipeline_header, consignments=None):
    """Operator can email the ENS movement pack once TSS authorises the
    movement (or later: arrived/cleared/completed) AND at least one linked
    consignment carries a DEC reference, otherwise there is nothing
    meaningful to include in the pack."""
    if not _ens_pack_email_unlocked(dec, pipeline_header):
        return False
    cons_rows = consignments or []
    return any((row or {}).get('dec_reference') for row in cons_rows)

def _find_project_root():
    candidate = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isdir(os.path.join(candidate, 'scripts')):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return '/app'

_PROJECT_ROOT = _find_project_root()

ENS_HEADER_FIELDS = [
    'movement_type', 'type_of_passive_transport',
    'identity_no_of_transport', 'nationality_of_transport',
    'conveyance_ref', 'arrival_date_time', 'arrival_port',
    'place_of_loading', 'place_of_unloading',
    'place_of_acceptance_same_as_loading', 'place_of_acceptance',
    'place_of_delivery_same_as_unloading', 'place_of_delivery',
    'seal_number', 'transport_charges',
    'carrier_eori', 'carrier_name', 'carrier_street_number',
    'carrier_city', 'carrier_postcode', 'carrier_country',
    'haulier_eori',
]

PRD_ENS_HEADER_EDIT_FIELDS = [
    'movement_type', 'type_of_passive_transport',
    'identity_no_of_transport', 'nationality_of_transport',
    'conveyance_ref', 'arrival_date_time', 'arrival_port',
    'place_of_loading', 'place_of_unloading',
    'place_of_acceptance_same_as_loading', 'place_of_acceptance',
    'place_of_delivery_same_as_unloading', 'place_of_delivery',
    'seal_number', 'transport_charges',
    'carrier_eori', 'carrier_name', 'carrier_street_number',
    'carrier_city', 'carrier_postcode', 'carrier_country',
    'haulier_eori',
]

ENS_HEADER_STORAGE_ALIASES = {
    'place_of_acceptance_same_as_loading': ('place_of_acceptance_same',),
    'place_of_delivery_same_as_unloading': ('place_of_delivery_same',),
}

ENS_EXPORT_FIELDNAMES = [
    'record_type',
    'local_id',
    'pipeline_staging_id',
    'ens_reference',
    'local_status',
    'tss_status',
    'display_status',
    'declaration_type',
    'label',
    'source',
    *ENS_HEADER_FIELDS,
    'consignment_count',
    'goods_count',
    'error_message',
    'created_at',
    'updated_at',
]

TSS_IMPORT_HEADER_FIELDS = [
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

TSS_IMPORT_CONSIGNMENT_FIELDS = [
    # Keep the first read aligned with the v2.9.5 Postman collection.
    'status', 'transport_document_number', 'consignor_eori',
    'importer_eori', 'controlled_goods', 'holder_of_authorisation',
    'movement_reference_number', 'error_message', 'control_status',
    'eori_for_eidr', 'error_code', 'total_packages', 'gross_mass_kg',
    'declaration_number', 'consignment_number', 'reference',
]

TSS_IMPORT_GOODS_FIELDS = [
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
    'un_dangerous_goods_code', 'number_of_individual_pieces',
    'tax_type', 'tax_base_unit', 'tax_base_quantity',
    'payable_tax_amount', 'payable_tax_currency',
    'ni_additional_information_codes', 'goods_id', 'reference',
]

TSS_IMPORT_CONSIGNMENT_FALLBACK_FIELDS = [
    # Best-effort detail read: some TSS records expose more than the documented
    # minimal read fields, especially when imported from the portal.
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

TSS_IMPORT_GOODS_FALLBACK_FIELDS = [
    'status', 'error_message', 'goods_description', 'commodity_code',
    'type_of_packages', 'number_of_packages', 'gross_mass_kg',
    'net_mass_kg', 'country_of_origin', 'goods_id', 'reference',
]

TSS_IMPORT_HEADER_FALLBACK_FIELDS = [
    'status', 'error_message',
    'movement_type', 'type_of_passive_transport',
    'identity_no_of_transport', 'identity_no_transport',
    'nationality_of_transport', 'conveyance_ref', 'arrival_date_time',
    'arrival_port', 'place_of_loading', 'place_of_unloading',
    'seal_number', 'route',
    'vehicle_registration', 'trailer_registration',
    'carrier_eori', 'carrier_name', 'carrier_country',
    'haulier_eori',
]

ERROR_FIELD_MAP = {
    'accept=load': 'place_of_acceptance_same_as_loading',
    'accept = load': 'place_of_acceptance_same_as_loading',
    'deliv=unload': 'place_of_delivery_same_as_unloading',
    'deliv = unload': 'place_of_delivery_same_as_unloading',
    'identity': 'identity_no_of_transport',
    'passive transport': 'type_of_passive_transport',
    'movement type': 'movement_type',
    'nationality': 'nationality_of_transport',
    'arrival port': 'arrival_port',
    'arrival date': 'arrival_date_time',
    'transport charges': 'transport_charges',
    'carrier eori': 'carrier_eori',
    'carrier name': 'carrier_name',
    'carrier country': 'carrier_country',
    'carrier street': 'carrier_street_number',
    'carrier city': 'carrier_city',
    'carrier postcode': 'carrier_postcode',
    'conveyance': 'conveyance_ref',
    'seal': 'seal_number',
    'place of loading': 'place_of_loading',
    'loading': 'place_of_loading',
    'place of unloading': 'place_of_unloading',
    'unloading': 'place_of_unloading',
    'place of acceptance': 'place_of_acceptance',
    'place of delivery': 'place_of_delivery',
    'haulier': 'haulier_eori',
}

ENS_FIELD_LABELS = {
    'movement_type': 'Movement Type',
    'type_of_passive_transport': 'Passive Transport Type',
    'identity_no_of_transport': 'Identity of Transport',
    'nationality_of_transport': 'Transport Nationality',
    'conveyance_ref': 'Conveyance Ref / ICR',
    'arrival_date_time': 'Arrival Date/Time',
    'arrival_port': 'Arrival Port',
    'place_of_loading': 'Place of Loading',
    'place_of_unloading': 'Place of Unloading',
    'place_of_acceptance_same_as_loading': 'Accept=Load?',
    'place_of_acceptance': 'Place of Acceptance',
    'place_of_delivery_same_as_unloading': 'Deliv=Unload?',
    'place_of_delivery': 'Place of Delivery',
    'seal_number': 'Seal Number',
    'transport_charges': 'Transport Charges',
    'carrier_eori': 'Carrier EORI',
    'carrier_name': 'Carrier Name',
    'carrier_street_number': 'Carrier Street / Number',
    'carrier_city': 'Carrier City',
    'carrier_postcode': 'Carrier Postcode',
    'carrier_country': 'Carrier Country',
    'haulier_eori': 'Haulier EORI',
}

ENS_FIELD_FIX_HINTS = {
    'movement_type': 'Select the TSS movement type that matches the journey. For RoRo accompanied, use 3a.',
    'type_of_passive_transport': 'Choose a valid passive transport option from the TSS list, for example trailer for RoRo accompanied movements.',
    'identity_no_of_transport': 'For RoRo, use the expected TSS transport identity format such as IMO number plus vehicle/trailer reference.',
    'nationality_of_transport': 'Use the two-letter nationality/country code accepted by TSS.',
    'conveyance_ref': 'Enter the ICR or conveyance reference for this movement.',
    'arrival_date_time': 'Use a valid future arrival date/time in the expected TSS format.',
    'arrival_port': 'Choose the official TSS arrival port code.',
    'place_of_loading': 'Fill the loading place used for the movement.',
    'place_of_unloading': 'Fill the unloading place used for the movement.',
    'place_of_acceptance_same_as_loading': 'Set Accept=Load? to Yes if acceptance is the same as loading. If No, also fill Place of Acceptance.',
    'place_of_acceptance': 'Required only when Accept=Load? is No.',
    'place_of_delivery_same_as_unloading': 'Set Deliv=Unload? to Yes if delivery is the same as unloading. If No, also fill Place of Delivery.',
    'place_of_delivery': 'Required only when Deliv=Unload? is No.',
    'transport_charges': 'Choose a valid TSS transport charges code.',
    'carrier_eori': 'Fill the carrier EORI used for the ENS header.',
    'carrier_name': 'Fill the legal carrier name. This is required for Maritime/RoRo movements.',
    'carrier_street_number': 'Fill the carrier street and number. This is required for Maritime/RoRo movements.',
    'carrier_city': 'Fill the carrier city. This is required for Maritime/RoRo movements.',
    'carrier_postcode': 'Fill the carrier postcode. This is required for Maritime/RoRo movements.',
    'carrier_country': 'Fill the carrier country code. This is required for Maritime/RoRo movements.',
    'haulier_eori': 'Optional unless the haulier is different from the carrier and required for the movement.',
}

# Which fields have choice value dropdowns and which TSS table
FIELD_CHOICE_MAP = {
    'movement_type': 'CV_movement_type',
    'type_of_passive_transport': 'CV_passive_transport_types',
    'nationality_of_transport': 'CV_country',
    'arrival_port': 'CV_port',
    'transport_charges': 'CV_transport_charge',
    'carrier_country': 'CV_country',
    'place_of_acceptance_same_as_loading': '_yesno',
    'place_of_delivery_same_as_unloading': '_yesno',
}

INGESTED_ENS_INITIAL_STATUSES = {
    '',
    'INSERTED',
    'PENDING',
    'PENDING REVIEW',
    'DRAFT',
    'CREATED',
}

INGESTED_SOURCE_EXACT = {
    'EMAIL_INGEST',
    'EMAIL_INJECT',
    'EXCEL_SALES_ORDERS',
    'EXCEL_SALES_ORDERS_DETAILS',
    'GRAPH_EMAIL',
    'HTTP_RECEIVE',
    'IMAP_EMAIL',
    'LOCAL_WATCHDOG',
    'PORTAL_EMAIL_REVIEW',
    'PORTAL_EMAIL_REVIEW_ATTACH',
    'PORTAL_EMAIL_UPLOAD',
    'PORTAL_REVIEW',
    'PORTAL_REVIEW_ATTACH',
    'PORTAL_UPLOAD',
    'SMTP_INJECT',
}

INGESTED_SOURCE_MARKERS = (
    'EMAIL',
    'EXCEL',
    'INGEST',
    'PDF',
    'CSV',
    'XLS',
    'UPLOAD',
    'REVIEW',
    'SMTP',
    'IMAP',
    'GRAPH',
)

NON_INGESTED_SOURCE_EXACT = {
    'APP_FORM',
    'DECLARATIONS_PORTAL',
    'TSS_IMPORT',
}


def _normalise_source_key(source):
    return str(source or '').strip().replace('-', '_').replace(' ', '_').upper()


def _is_tss_import_source(source):
    return _normalise_source_key(source) == 'TSS_IMPORT'


def _is_ingested_ens_source(source):
    source_key = _normalise_source_key(source)
    if not source_key or source_key in NON_INGESTED_SOURCE_EXACT:
        return False
    if source_key in INGESTED_SOURCE_EXACT:
        return True
    return any(marker in source_key for marker in INGESTED_SOURCE_MARKERS)

def get_choices(table_name):
    try:
        rows = query_all(f"SELECT value, name FROM TSS.[{table_name}] ORDER BY name")
        return [CVOption(r['value'], r['name']) for r in rows]
    except:
        return []

def get_port_choices():
    try: return query_all("SELECT location_code AS value, operator_facility_name AS name FROM TSS.CV_port WHERE ens_allowed = 'True' ORDER BY operator_facility_name")
    except: return []

def get_country_choices():
    try: return query_all("SELECT value, name FROM TSS.CV_country ORDER BY name")
    except: return []

def load_form_choices():
    return {
        'movement_types': get_choices('CV_movement_type'),
        'ports': get_port_choices(),
        'countries': get_country_choices(),
        'transport_charges': get_choices('CV_transport_charge'),
        'passive_transport_types': query_all("SELECT value, description AS name FROM TSS.CV_passive_transport_types ORDER BY description"),
    }

def load_field_choices():
    """Load choice values keyed by field name for the detail page inline editor."""
    choices = {}
    choices['movement_type'] = get_choices('CV_movement_type')
    choices['type_of_passive_transport'] = query_all("SELECT value, description AS name FROM TSS.CV_passive_transport_types ORDER BY description")
    choices['nationality_of_transport'] = get_country_choices()
    choices['arrival_port'] = get_port_choices()
    choices['transport_charges'] = get_choices('CV_transport_charge')
    choices['carrier_country'] = get_country_choices()
    choices['place_of_acceptance_same_as_loading'] = [{'value': 'yes', 'name': 'Yes'}, {'value': 'no', 'name': 'No'}]
    choices['place_of_delivery_same_as_unloading'] = [{'value': 'yes', 'name': 'Yes'}, {'value': 'no', 'name': 'No'}]
    return choices

def get_partners_by_type(pt):
    try:
        schema = get_tenant()["schema"]
        return query_all(
            f"""
            SELECT id, partner_name, eori, address_line1, city, postcode, country
            FROM [{schema}].Partners
            WHERE partner_type = ? AND active = 1
            ORDER BY partner_name
            """,
            [pt],
        )
    except: return []


def _default_value(defaults, attr):
    if defaults is None:
        return ''
    return _text_value(getattr(defaults, attr, ''))


def _text_value(value):
    if value is None:
        return ''
    return str(value).strip()


def _json_for_log(value):
    return json.dumps(value, default=str)[:4000]


def _json_for_tss_raw(value):
    """Store full TSS payloads in RawJson mirrors for downstream automation."""
    return json.dumps(value or {}, default=str)


def _active_client_code():
    try:
        tenant = get_tenant() or {}
        return str(tenant.get('code') or tenant.get('schema') or 'BKD').strip().upper()[:10]
    except Exception:
        return 'BKD'


def _log_tss_api_exchange(
    *,
    staging_id=None,
    call_type='LOCAL',
    http_method='LOCAL',
    url='',
    request_payload=None,
    http_status=0,
    response_status='',
    response_message='',
    response_json=None,
    duration_ms=0,
    error_detail='',
):
    """PRD-safe audit helper: write to TSS.BKD_API_Exchanges via app.db."""
    try:
        return insert_api_call_log(
            _active_client_code(),
            call_type,
            staging_id=staging_id,
            http_method=http_method,
            url=url,
            request_payload=request_payload,
            http_status=http_status,
            response_status=response_status,
            response_message=response_message,
            response_json=response_json,
            duration_ms=duration_ms,
            error_detail=error_detail,
        )
    except Exception:
        return None


def _choice_allows(cv, key, value):
    value = _text_value(value)
    allowed = cv.get(key, set()) if cv else set()
    return bool(value) and (not allowed or value in allowed)


def _set_autofix(payload, field, value, reason, fixes):
    value = _text_value(value)
    if not value:
        return
    old_value = _text_value(payload.get(field))
    if old_value == value:
        return
    payload[field] = value
    fixes.append({'field': field, 'from': old_value, 'to': value, 'reason': reason})


def _load_ingest_defaults_for_active_tenant():
    try:
        from app.ingestion.defaults import resolve_ingest_defaults

        return resolve_ingest_defaults(tenant_code=get_tenant()["code"])
    except Exception:
        return None


def _find_carrier_partner_for_payload(payload):
    schema = get_tenant()["schema"]
    carrier_eori = _text_value(payload.get('carrier_eori'))
    carrier_name = _text_value(payload.get('carrier_name'))
    try:
        if carrier_eori:
            partner = query_one(
                f"""
                SELECT TOP 1 partner_name, eori, address_line1, city, postcode, country
                FROM [{schema}].Partners
                WHERE active = 1
                  AND partner_type = 'Carrier'
                  AND UPPER(eori) = UPPER(?)
                ORDER BY id DESC
                """,
                [carrier_eori],
            )
            if partner:
                return partner
        if carrier_name:
            return query_one(
                f"""
                SELECT TOP 1 partner_name, eori, address_line1, city, postcode, country
                FROM [{schema}].Partners
                WHERE active = 1
                  AND partner_type = 'Carrier'
                  AND UPPER(partner_name) = UPPER(?)
                ORDER BY id DESC
                """,
                [carrier_name],
            )
    except Exception:
        return None
    return None


def _apply_ens_auto_fixes(payload, errors, cv):
    """Apply deterministic, low-risk fixes to an ENS payload before validation."""
    fixes = []
    defaults = _load_ingest_defaults_for_active_tenant()
    partner = _find_carrier_partner_for_payload(payload)

    mt = _text_value(payload.get('movement_type'))
    if not mt:
        default_mt = _default_value(defaults, 'movement_type')
        if _choice_allows(cv, 'movement_type', default_mt):
            _set_autofix(payload, 'movement_type', default_mt, 'filled from tenant defaults', fixes)
            mt = default_mt

    simple_defaults = [
        ('nationality_of_transport', 'nationality_of_transport', 'country'),
        ('arrival_port', 'arrival_port', 'port'),
        ('transport_charges', 'transport_charges', 'transport_charge'),
        ('carrier_eori', 'carrier_eori', None),
        ('carrier_name', 'carrier_name', None),
        ('carrier_street_number', 'carrier_street_number', None),
        ('carrier_city', 'carrier_city', None),
        ('carrier_postcode', 'carrier_postcode', None),
        ('carrier_country', 'carrier_country', 'country'),
        ('haulier_eori', 'haulier_eori', None),
        ('place_of_loading', 'place_of_loading', None),
        ('place_of_unloading', 'place_of_unloading', None),
    ]
    for field, attr, choice_key in simple_defaults:
        if _text_value(payload.get(field)):
            continue
        value = _default_value(defaults, attr)
        if choice_key and not _choice_allows(cv, choice_key, value):
            continue
        _set_autofix(payload, field, value, 'filled from tenant defaults', fixes)

    if not _text_value(payload.get('arrival_date_time')) and defaults is not None:
        _set_autofix(payload, 'arrival_date_time', defaults.build_arrival_datetime(), 'filled from tenant arrival default', fixes)

    if partner:
        for field, key in [
            ('carrier_eori', 'eori'),
            ('carrier_name', 'partner_name'),
            ('carrier_street_number', 'address_line1'),
            ('carrier_city', 'city'),
            ('carrier_postcode', 'postcode'),
            ('carrier_country', 'country'),
        ]:
            if _text_value(payload.get(field)):
                continue
            value = partner.get(key) or ''
            if field == 'carrier_country' and not _choice_allows(cv, 'country', value):
                continue
            _set_autofix(payload, field, value, 'filled from saved carrier partner', fixes)

    eori = _text_value(payload.get('carrier_eori'))
    if eori.upper().startswith('GB') and len(eori) > 2:
        _set_autofix(payload, 'carrier_eori', 'XI' + eori[2:], 'converted GB carrier EORI prefix to XI for ENS', fixes)

    raw_dt = _text_value(payload.get('arrival_date_time'))
    if raw_dt and 'T' in raw_dt:
        try:
            dt = datetime.fromisoformat(raw_dt)
            _set_autofix(payload, 'arrival_date_time', dt.strftime('%d/%m/%Y %H:%M:%S'), 'converted browser datetime to TSS format', fixes)
        except ValueError:
            pass

    ident = _text_value(payload.get('identity_no_of_transport'))
    default_ident = _default_value(defaults, 'identity_no_of_transport')
    if not ident and default_ident:
        if mt == '1' and re.match(r'^IMO\d{7}$', default_ident):
            _set_autofix(payload, 'identity_no_of_transport', default_ident, 'filled from tenant transport identity default', fixes)
        elif mt in ('1a', '3', '3a') and re.match(r'^IMO\d{7}#.{4,16}$', default_ident):
            _set_autofix(payload, 'identity_no_of_transport', default_ident, 'filled from tenant transport identity default', fixes)
    elif mt == '1' and re.match(r'^IMO\d{7}#.+$', ident):
        _set_autofix(payload, 'identity_no_of_transport', ident.split('#', 1)[0], 'removed RoRo trailer suffix for maritime identity', fixes)

    if mt == '3a':
        acceptance_same = _text_value(payload.get('place_of_acceptance_same_as_loading')).lower()
        if acceptance_same not in {'yes', 'no'}:
            _set_autofix(payload, 'place_of_acceptance_same_as_loading', 'yes', 'defaulted RoRo acceptance to loading place', fixes)
        elif acceptance_same == 'no' and not _text_value(payload.get('place_of_acceptance')):
            _set_autofix(payload, 'place_of_acceptance_same_as_loading', 'yes', 'used loading place because no separate acceptance place was provided', fixes)

        delivery_same = _text_value(payload.get('place_of_delivery_same_as_unloading')).lower()
        if delivery_same not in {'yes', 'no'}:
            _set_autofix(payload, 'place_of_delivery_same_as_unloading', 'yes', 'defaulted RoRo delivery to unloading place', fixes)
        elif delivery_same == 'no' and not _text_value(payload.get('place_of_delivery')):
            _set_autofix(payload, 'place_of_delivery_same_as_unloading', 'yes', 'used unloading place because no separate delivery place was provided', fixes)

    length_limits = {
        'conveyance_ref': 8 if mt == '4' else 35,
        'seal_number': 20,
        'place_of_loading': 33,
        'place_of_unloading': 33,
        'carrier_name': 35,
        'carrier_street_number': 35,
        'carrier_city': 35,
    }
    for field, max_len in length_limits.items():
        value = _text_value(payload.get(field))
        if len(value) > max_len:
            _set_autofix(payload, field, value[:max_len], f'trimmed to TSS max length {max_len}', fixes)

    return fixes


def get_all_ports():
    try: return query_all("SELECT id, location_code, operator_facility_name, area, ens_allowed, transit, ffd_allowed, sup_allowed, glr_allowed, latitude, longitude FROM TSS.CV_port ORDER BY operator_facility_name")
    except: return []

def _parse_arrival_datetime(raw_value):
    if isinstance(raw_value, datetime):
        return raw_value
    raw_value = str(raw_value or '').strip()
    if not raw_value:
        return None
    normalized = raw_value.replace('Z', '+00:00')
    if '.' in normalized:
        head, tail = normalized.split('.', 1)
        for marker in ('+', '-'):
            if marker in tail:
                frac, tz = tail.split(marker, 1)
                tail = f"{frac[:6]}{marker}{tz}"
                break
        else:
            tail = tail[:6]
        normalized = f"{head}.{tail}"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(raw_value.split('.')[0], fmt)
        except ValueError:
            continue
    return None


def _arrival_datetime_for_db(raw_value):
    """Return a value the SQL Server DATETIME column will accept.

    pyodbc binds ``datetime`` objects natively (no DATEFORMAT dependency),
    so the safe path is to parse the inbound string (TSS format
    ``dd/mm/yyyy HH:MM:SS``, ISO 8601, or anything ``_parse_arrival_datetime``
    handles) and pass the Python datetime. Empty / unparsable values become
    ``None`` so the row is stored as NULL instead of triggering 22007.
    """
    return _parse_arrival_datetime(raw_value)


def _is_tss_draft_like(status):
    normalized = (status or '').strip().lower()
    return normalized in {'draft', 'created', 'imported'}


def _normalise_yes_no(value, default=''):
    text = str(value or '').strip().lower()
    if not text:
        return default
    if text in {'yes', 'y', 'true', '1', 'on'}:
        return 'yes'
    if text in {'no', 'n', 'false', '0', 'off'}:
        return 'no'
    return text


def _is_pending_sync_status(status):
    return normalize_status_key(status) in {'PENDING SYNC', 'IMPORTED', 'SYNC PENDING'}


def _has_live_ens_reference(dec):
    return bool((dec or {}).get('external_ref') and str((dec or {}).get('external_ref')).startswith('ENS'))


def _ens_header_ready_for_dec_creation(dec=None, pipeline_header=None):
    dec = dec or {}
    pipeline_header = pipeline_header or {}
    ens_ref = (
        pipeline_header.get('ens_reference')
        or dec.get('external_ref')
        or ''
    )
    return str(ens_ref).strip().startswith('ENS')


def _ens_needs_tss_sync(dec=None, pipeline_header=None):
    dec = dec or {}
    pipeline_header = pipeline_header or {}
    ens_ref = dec.get('external_ref') or pipeline_header.get('ens_reference')
    if not ens_ref:
        return False

    tss_status = dec.get('external_status') or pipeline_header.get('tss_status')
    if not tss_status:
        return True
    return _is_pending_sync_status(tss_status)


def _ens_sync_phase(dec=None, pipeline_header=None):
    return 'sync_all'


def _ens_sync_label(dec=None, pipeline_header=None):
    return 'Sync All TSS Data'


def _is_locally_submitted_ens(dec):
    """Local Submitted means the header exists in TSS; TSS Draft remains editable."""
    status = (dec or {}).get('status')
    if status == 'Submitted':
        return True
    return _has_live_ens_reference(dec) and normalize_status_key(status) in {
        'DRAFT',
        'CREATED',
        'UPDATED',
        'SUBMIT ERROR',
        'IMPORTED',
    }


def _build_arrival_notice(dec, payload):
    if not dec or not _is_locally_submitted_ens(dec):
        return None
    if not _is_tss_draft_like(dec.get('external_status')):
        return None

    arrival_raw = (
        (payload or {}).get('arrival_date_time')
        or (dec or {}).get('arrival_date_time')
        or ''
    )
    arrival_dt = _parse_arrival_datetime(arrival_raw)
    if not arrival_dt:
        return None
    if arrival_dt.tzinfo is None:
        arrival_dt = arrival_dt.replace(tzinfo=timezone.utc)

    now_utc = datetime.now(timezone.utc)
    if arrival_dt >= now_utc:
        return None

    return {
        'summary': 'Arrival time has already passed while TSS still keeps this ENS in a draft-like state.',
        'detail': (
            'This ENS can still be updated, but it is unlikely to progress cleanly to downstream DEC/GMR '
            'steps until the arrival date/time is moved back into the future and the header is re-submitted.'
        ),
        'arrival_label': arrival_dt.strftime('%d/%m/%Y %H:%M:%S UTC'),
    }

def build_payload(form):
    payload = {}
    for field in ENS_HEADER_FIELDS:
        payload[field] = form.get(field, '').strip()
    raw_dt = payload.get('arrival_date_time', '')
    if raw_dt:
        payload['arrival_date_time'] = _tss_datetime_payload_value(raw_dt)
    return payload

def extract_error_fields(errors):
    error_fields = set()
    for err in errors:
        err_lower = err.lower()
        for keyword, field in ERROR_FIELD_MAP.items():
            if keyword in err_lower:
                error_fields.add(field)
    return error_fields


def _ens_error_kind(error_text):
    if _ens_invalid_update_op_type_error(error_text):
        return 'SYNC'
    text = (error_text or '').upper()
    for prefix in ('REQUIRED', 'INVALID', 'FORMAT', 'LENGTH'):
        if prefix in text:
            return prefix
    if 'TECHNICAL VALIDATION ERROR' in text:
        return 'TECHNICAL'
    return 'ERROR'


def _ens_invalid_update_op_type_error(errors):
    if isinstance(errors, str):
        error_text = errors
    else:
        error_text = ' '.join(str(error or '') for error in (errors or []))
    normalized = error_text.upper()
    return 'INVALID OP_TYPE' in normalized and 'UPDATE' in normalized


def _ens_remote_status_clears_header_guidance(status):
    normalized = normalize_status_key(status)
    compact = normalized.replace(' ', '')
    if compact in {'AUTHORISEDFORMOVEMENT', 'AUTHORIZEDFORMOVEMENT', 'AUTHFORMOVEMENT'}:
        return True
    return normalized in {'ARRIVED', 'ACCEPTED', 'CLEARED', 'CLOSED', 'AUTHORISED', 'AUTHORIZED'}


def _build_ens_header_guidance(dec=None, errors=None, error_fields=None):
    """Compact operator guidance for ENS header validation errors."""
    errors = [str(error).strip() for error in (errors or []) if str(error or '').strip()]
    error_fields = set(error_fields or [])
    status = normalize_status_key((dec or {}).get('status'))
    remote_status = (dec or {}).get('external_status') or (dec or {}).get('tss_status')
    if _ens_remote_status_clears_header_guidance(remote_status):
        return None
    if not errors and status != 'VALIDATION ERROR':
        return None

    kinds = {_ens_error_kind(error) for error in errors}
    if 'TECHNICAL' in kinds:
        title = 'Technical validation error'
        detail = (
            'Fusion could not complete the local ENS validation check. The technical detail below shows the exact exception. '
            'Retry Validate ENS after saving the header; if it repeats, check the latest Technical log.'
        )
    elif 'SYNC' in kinds:
        title = 'ENS status needs syncing'
        detail = (
            'TSS says this ENS can no longer be updated with op_type=update. '
            'Refresh the live TSS statuses before deciding whether the header really needs edits.'
        )
    elif 'REQUIRED' in kinds:
        title = 'Required ENS header data is missing'
        detail = (
            'This ENS header is missing fields required before it can continue. '
            'Open Full Edit, correct the highlighted fields, save, then run Validate ENS again.'
        )
    elif {'INVALID', 'FORMAT', 'LENGTH'} & kinds:
        title = 'ENS header data needs correcting'
        detail = (
            'Some header values are present but do not match the format or choice values expected by TSS. '
            'Use the suggestions below, then save and revalidate.'
        )
    else:
        title = 'ENS header validation needs attention'
        detail = 'Review the validation messages below, update the ENS header, then run Validate ENS again.'

    ordered_fields = [
        field for field in ENS_HEADER_FIELDS
        if field in error_fields
    ]
    issues = [
        {
            'field': field,
            'label': ENS_FIELD_LABELS.get(field, field.replace('_', ' ').title()),
            'suggestion': ENS_FIELD_FIX_HINTS.get(field, 'Review this field on the ENS edit screen.'),
        }
        for field in ordered_fields
    ]

    if not issues and errors:
        suggestion = 'Run Sync TSS Status, then refresh this ENS before editing header fields.'
        if 'SYNC' not in kinds:
            suggestion = 'Open Full Edit and compare the header values against the technical validation message.'
        issues = [{
            'field': '',
            'label': 'Validation message',
            'suggestion': suggestion,
        }]

    return {
        'tone': 'danger' if status == 'VALIDATION ERROR' or errors else 'warning',
        'title': title,
        'detail': detail,
        'issues': issues,
        'errors': errors,
        'next_step': (
            'Run Sync TSS Status, then refresh this ENS.'
            if 'SYNC' in kinds else
            'Open Full Edit, correct the fields, save, then run Validate ENS.'
        ),
    }


def _ens_display_status(dec_status='', external_status='', imported_only=False, has_live_ref=False, source=''):
    canonical = {
        'INSERTED': 'Inserted',
        'VALIDATED': 'Validated',
        'SUBMITTED': 'Submitted',
        'RESUBMIT': 'Resubmit',
        'VALIDATION ERROR': 'Validation_Error',
        'SUBMIT ERROR': 'Submit_Error',
        'CANCELLED': 'Cancelled',
        'CANCELED': 'Cancelled',
        'INGESTED': 'INGESTED',
        'DRAFT': 'DRAFT',
        'CREATED': 'DRAFT',
        'PENDING': 'DRAFT',
        'PENDING REVIEW': 'DRAFT',
        'PENDING_REVIEW': 'DRAFT',
    }
    normalized = normalize_status_key(dec_status)
    remote = normalize_status_key(external_status)
    if imported_only or normalized == 'IMPORTED':
        return 'IMPORTED'
    if _is_ingested_ens_source(source) and normalized in INGESTED_ENS_INITIAL_STATUSES:
        return 'INGESTED'
    if _is_pending_sync_status(external_status):
        return 'PENDING_SYNC'
    if has_live_ref and not remote and normalized in {'SUBMITTED', 'CREATED', 'DRAFT'}:
        return 'PENDING_SYNC'
    if normalized == 'SUBMIT ERROR' and remote:
        return 'Submitted'
    return canonical.get(normalized, dec_status or 'DRAFT')


def _ens_filter_status(dec_status='', external_status='', imported_only=False, has_live_ref=False, source=''):
    normalized = normalize_status_key(dec_status)
    remote = normalize_status_key(external_status)
    if imported_only or normalized == 'IMPORTED':
        if remote and not _is_pending_sync_status(external_status):
            return canonical_filter_status(remote)
        return 'IMPORTED'
    if _is_ingested_ens_source(source) and normalized in INGESTED_ENS_INITIAL_STATUSES:
        return 'INGESTED'
    if _is_pending_sync_status(external_status):
        return 'PENDING_SYNC'
    if has_live_ref and not remote and normalized in {'SUBMITTED', 'CREATED', 'DRAFT'}:
        return 'PENDING_SYNC'
    return effective_tss_filter_status(normalized, remote)


def _ens_status_tabs(counts, selected=''):
    base = [*TSS_FILTER_STATUS_TABS, 'RESUBMIT', 'VALIDATION_ERROR', 'SUBMIT_ERROR']
    normalized_counts = {}
    for raw_status, count in (counts or {}).items():
        canonical_status = _canonical_ens_filter_status(raw_status) or 'ALL'
        normalized_counts[canonical_status] = normalized_counts.get(canonical_status, 0) + count
    selected = selected or 'ALL'
    return status_filter_tabs(normalized_counts, base, selected)


def _ens_search_matches(row, search):
    if not search:
        return True
    needle = search.casefold()
    haystacks = [
        row.get('external_ref'),
        row.get('carrier_name'),
        row.get('carrier_eori'),
        row.get('arrival_port'),
        row.get('identity_no_of_transport'),
        row.get('external_status'),
        row.get('display_status'),
        row.get('filter_status'),
        row.get('source'),
        row.get('label'),
    ]
    return any(needle in str(value or '').casefold() for value in haystacks)


def _list_data(status_filter='', search=''):
    rows = query_all("""
        SELECT
            NULL                         AS id,
            h.stg_header_id              AS pipeline_staging_id,
            'ENS'                        AS declaration_type,
            h.label,
            h.tss_ens_header_ref         AS external_ref,
            h.sub_status                 AS status,
            t.TssStatus                  AS external_status,
            COALESCE(h.source, 'STG')    AS source,
            h.movement_type,
            h.arrival_port,
            h.arrival_date_time,
            h.carrier_name,
            h.carrier_eori,
            h.identity_no_of_transport,
            h.validation_errors_json     AS error_message,
            h.stg_created_at             AS created_at,
            h.updated_at,
            (SELECT COUNT(*)
             FROM [STG].[BKD_ENS_Consignments] c
             WHERE c.stg_header_id = h.stg_header_id) AS consignment_count,
            (SELECT COUNT(*)
             FROM [STG].[BKD_GoodsItems] g
             JOIN [STG].[BKD_ENS_Consignments] c ON c.stg_consignment_id = g.stg_consignment_id
             WHERE c.stg_header_id = h.stg_header_id) AS goods_count
        FROM [STG].[BKD_ENS_Headers] h
        LEFT JOIN [TSS].[BKD_ENS_Headers] t
          ON t.DeclarationNumber = h.tss_ens_header_ref
        ORDER BY h.stg_created_at DESC
    """)

    declarations = []
    counts = {}
    for raw in rows or []:
        row = dict(raw)
        imported_only = bool(row.get('external_ref')) and _is_tss_import_source(row.get('source'))
        row['display_status'] = _ens_display_status(
            row.get('status', ''),
            row.get('external_status', ''),
            imported_only=imported_only,
            has_live_ref=_has_live_ens_reference(row),
            source=row.get('source'),
        )
        row['filter_status'] = _ens_filter_status(
            row.get('status', ''),
            row.get('external_status', ''),
            imported_only=imported_only,
            has_live_ref=_has_live_ens_reference(row),
            source=row.get('source'),
        )
        row['needs_tss_sync'] = _ens_needs_tss_sync(row)
        row['sync_phase'] = _ens_sync_phase(row)
        row['sync_label'] = _ens_sync_label(row)
        if not _ens_search_matches(row, search):
            continue
        counts[row['filter_status']] = counts.get(row['filter_status'], 0) + 1
        if status_filter and row['filter_status'] != status_filter:
            continue
        declarations.append(row)

    _assign_chronological_ens_ids(declarations)
    declarations.sort(key=lambda row: row.get('created_at') or datetime.min, reverse=True)
    return declarations, counts


def _arrival_sort_key(row):
    val = row.get('arrival_date_time')
    if not val:
        return datetime.min
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    normalized = s.replace('Z', '+00:00')
    if '.' in normalized:
        head, tail = normalized.split('.', 1)
        for marker in ('+', '-'):
            if marker in tail:
                frac, tz = tail.split(marker, 1)
                tail = f"{frac[:6]}{marker}{tz}"
                break
        else:
            tail = tail[:6]
        normalized = f"{head}.{tail}"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for fmt in (
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d',
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return datetime.min


def _apply_ens_sort(declarations, sort):
    if sort == 'arrival_asc':
        declarations.sort(key=_arrival_sort_key)
    elif sort == 'arrival_desc':
        declarations.sort(key=_arrival_sort_key, reverse=True)
    return declarations


def _safe_positive_int(value, default=1):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(1, parsed)


def _assign_chronological_ens_ids(rows):
    """Display-only ENS numbering: oldest arrival is 1, newest is last."""
    ordered = sorted(
        rows or [],
        key=lambda row: (
            _arrival_sort_key(row) == datetime.min,
            _arrival_sort_key(row),
            int(row.get('stg_header_id') or row.get('pipeline_staging_id') or 0),
        ),
    )
    for index, row in enumerate(ordered, start=1):
        row['chronological_id'] = index
    return rows


def _canonical_ens_filter_status(value=''):
    raw = (value or '').strip()
    if not raw:
        return ''
    key = canonical_filter_status(raw)
    if key == 'ALL':
        return ''
    if key in {'INSERTED', 'PENDING', 'PENDING_REVIEW', 'VALIDATED'}:
        return 'DRAFT'
    if key == 'CANCELED':
        return 'CANCELLED'
    return key


def _declaration_detail_target(dec):
    ens_ref = (dec or {}).get('external_ref')
    if ens_ref:
        return url_for('declarations.detail_by_ref', ens_ref=ens_ref)
    return url_for('declarations.detail', dec_id=(dec or {}).get('id', 0))


def _payload_from_pipeline_header(pipeline_header):
    if not pipeline_header:
        return {}
    payload = {}
    for field in ENS_HEADER_FIELDS:
        value = _ens_header_value(pipeline_header, field)
        if value not in (None, ''):
            payload[field] = value
    if payload.get('arrival_date_time'):
        payload['arrival_date_time'] = _tss_datetime_payload_value(payload.get('arrival_date_time'))
    if not payload.get('identity_no_of_transport'):
        fallback_identity = (
            pipeline_header.get('identity_no_transport')
            or pipeline_header.get('vehicle_registration')
            or ''
        )
        if fallback_identity:
            payload['identity_no_of_transport'] = fallback_identity
    return _normalise_ens_submit_payload(payload)


def _normalise_ens_submit_payload(payload):
    """Make locally-stored ENS payloads match the exact TSS header API names."""
    payload = dict(payload or {})
    for canonical, aliases in ENS_HEADER_STORAGE_ALIASES.items():
        if not _text_value(payload.get(canonical)):
            for alias in aliases:
                alias_value = _text_value(payload.get(alias))
                if alias_value:
                    payload[canonical] = alias_value
                    break

    if _text_value(payload.get('movement_type')) == '3a':
        if (
            not _text_value(payload.get('place_of_acceptance_same_as_loading'))
            and not _text_value(payload.get('place_of_acceptance'))
            and _text_value(payload.get('place_of_loading'))
        ):
            payload['place_of_acceptance_same_as_loading'] = 'yes'
        if (
            not _text_value(payload.get('place_of_delivery_same_as_unloading'))
            and not _text_value(payload.get('place_of_delivery'))
            and _text_value(payload.get('place_of_unloading'))
        ):
            payload['place_of_delivery_same_as_unloading'] = 'yes'

    for field in ('place_of_acceptance_same_as_loading', 'place_of_delivery_same_as_unloading'):
        if _text_value(payload.get(field)):
            payload[field] = _normalise_yes_no(payload.get(field))
    if _text_value(payload.get('arrival_date_time')):
        payload['arrival_date_time'] = _tss_datetime_payload_value(payload.get('arrival_date_time'))
    return payload


def _tss_datetime_payload_value(value):
    dt = _parse_arrival_datetime(value)
    if dt:
        return dt.strftime('%d/%m/%Y %H:%M:%S')
    return ''


def _html_datetime_value(value):
    dt = _parse_arrival_datetime(value)
    if dt:
        return dt.strftime('%Y-%m-%dT%H:%M')
    return str(value or '')


def _pipeline_header_is_locally_editable(pipeline_header):
    if not pipeline_header:
        return False
    if not tss_allows_data_changes(pipeline_header.get('tss_status'), pipeline_header.get('status')):
        return False
    local_status = normalize_status_key(pipeline_header.get('status'))
    return local_status in {
        'INGESTED',
        'PENDING',
        'PENDING REVIEW',
        'DRAFT',
        'VALIDATED',
        'VALIDATION ERROR',
        'SUBMIT ERROR',
        'FAILED',
        'INVALID',
    }


def _pipeline_header_is_submittable(pipeline_header):
    if not pipeline_header:
        return False
    local_status = normalize_status_key(pipeline_header.get('status'))
    live_ref = str(pipeline_header.get('ens_reference') or '').startswith('ENS')
    return (
        local_status in {'VALIDATED', 'RESUBMIT'}
        or (
            live_ref
            and tss_allows_data_changes(pipeline_header.get('tss_status'), pipeline_header.get('status'))
            and local_status in {
                'SUBMITTED',
                'DRAFT',
                'CREATED',
                'UPDATED',
                'SUBMIT ERROR',
                'IMPORTED',
            }
        )
    )


def _dec_from_pipeline_header(pipeline_header):
    if not pipeline_header:
        return {}
    ens_ref = (pipeline_header.get('ens_reference') or '').strip()
    dec = {
        'id': None,
        'pipeline_staging_id': pipeline_header.get('staging_id'),
        'external_ref': ens_ref,
        'status': pipeline_header.get('status') or 'PENDING_REVIEW',
        'external_status': pipeline_header.get('tss_status'),
        'error_message': pipeline_header.get('error_message'),
        'created_at': pipeline_header.get('created_at'),
        'updated_at': pipeline_header.get('updated_at'),
        'source': pipeline_header.get('source'),
        'payload_json': None,
    }
    dec['display_status'] = _ens_display_status(
        dec.get('status'),
        dec.get('external_status'),
        imported_only=bool(ens_ref) and _is_tss_import_source(dec.get('source')),
        has_live_ref=_has_live_ens_reference(dec),
        source=dec.get('source'),
    )
    return dec


def _apply_detail_display_status(dec, pipeline_header=None):
    if not dec:
        return dec
    source = dec.get('source') or (pipeline_header or {}).get('source')
    dec['display_status'] = _ens_display_status(
        dec.get('status'),
        dec.get('external_status'),
        imported_only=bool(dec.get('external_ref')) and _is_tss_import_source(source),
        has_live_ref=_has_live_ens_reference(dec),
        source=source,
    )
    return dec


def _apply_parent_synced_goods_statuses(goods, cons):
    normalised = []
    for item in goods or []:
        row = dict(item)
        row['status'] = local_goods_status_after_parent_sync(
            row.get('status'),
            goods_tss_status=row.get('tss_status'),
            parent_local_status=(cons or {}).get('status'),
            parent_tss_status=(cons or {}).get('tss_status'),
        )
        normalised.append(row)
    return normalised


def _detail_action_flags(dec):
    status = (dec or {}).get('status')
    external_status = (dec or {}).get('external_status')
    local_status = normalize_status_key(status)
    has_live_ref = _has_live_ens_reference(dec)
    draft_like = _is_tss_draft_like(external_status)
    submitted_like = _is_locally_submitted_ens(dec)
    data_change_allowed = tss_allows_data_changes(external_status, status)
    locally_validatable = local_status in {'INSERTED', 'INGESTED', 'PENDING REVIEW', 'VALIDATION ERROR'}
    has_pipeline_header = bool((dec or {}).get('pipeline_staging_id'))
    pipeline_editable = has_pipeline_header and _pipeline_header_is_locally_editable({
        'status': status,
        'tss_status': external_status,
    })
    pipeline_validatable = has_pipeline_header and data_change_allowed and local_status in {
        'PENDING',
        'INGESTED',
        'PENDING REVIEW',
        'DRAFT',
        'VALIDATION ERROR',
    }
    pipeline_submittable = has_pipeline_header and _pipeline_header_is_submittable({
        'status': status,
        'tss_status': external_status,
        'ens_reference': (dec or {}).get('external_ref'),
    })
    return {
        'can_validate': pipeline_validatable or (bool((dec or {}).get('id')) and locally_validatable),
        'can_edit': pipeline_editable or (
            bool((dec or {}).get('id')) and (
                locally_validatable
                or (status == 'Submit_Error' and not has_live_ref)
                or (submitted_like and data_change_allowed)
            )
        ),
        'can_add_consignment': bool((dec or {}).get('id') or (dec or {}).get('external_ref')) and data_change_allowed and status not in ('Cancelled', 'Inserted'),
        'can_cancel': bool((dec or {}).get('id')) and (dec or {}).get('external_ref') and submitted_like,
        'can_resubmit': bool((dec or {}).get('id')) and (
            status == 'Validation_Error'
            or (status == 'Submit_Error' and not has_live_ref)
        ),
        'can_submit': pipeline_submittable or (
            bool((dec or {}).get('id')) and (
                status == 'Validated'
                or (submitted_like and draft_like)
            )
        ),
    }


def _nav_action(label, href=None, kind='primary', phase=None, hidden=None, confirm_text=None):
    action = {'label': label, 'kind': kind}
    if href:
        action['href'] = href
    if phase:
        if phase in {'sync', 'sync_pipeline', 'sync_gmr', 'sync_tss'}:
            phase = 'sync_all'
            if str(label).lower().startswith('sync'):
                action['label'] = 'Sync All TSS Data'
        action['phase'] = phase
        action['confirm_text'] = confirm_text or f"Run {action['label']} now?"
    if hidden:
        action['hidden'] = hidden
    return action


def _ens_header_blocks_cargo_creation(pipeline_header):
    if not pipeline_header:
        return ''
    for status in (
        normalize_status_key(pipeline_header.get('status')),
        normalize_status_key(pipeline_header.get('tss_status')),
    ):
        if status in {'VALIDATION ERROR', 'SUBMIT ERROR', 'FAILED', 'ERROR', 'INVALID', 'REJECTED'}:
            return status
    return ''


def _consignment_requires_sdi(cons):
    return consignment_should_discover_sdi(cons)


def _pipeline_job_markers():
    rows = query_all("""
        SELECT CallType AS call_type, MAX(CalledAt) AS called_at
        FROM TSS.BKD_API_Exchanges
        WHERE CallType IN ('JOB_VALIDATE_PIPELINE', 'JOB_SUBMIT_PIPELINE', 'JOB_SYNC_PIPELINE')
          AND ClientCode = ?
        GROUP BY CallType
    """, [_active_client_code()])
    return {row['call_type']: row['called_at'] for row in rows}


def _consignment_guidance(cons, goods, linked_gmr=None, linked_supdecs=None, job_markers=None):
    cons_ref = cons.get('dec_reference') or cons.get('sfd_reference') or f"consignment #{cons.get('staging_id')}"
    local_status = (cons.get('status') or '').upper()
    tss_status = (cons.get('tss_status') or '').upper()
    goods_total = len(goods or [])
    last_validate = (job_markers or {}).get('JOB_VALIDATE_PIPELINE')
    last_submit = (job_markers or {}).get('JOB_SUBMIT_PIPELINE')
    last_sync = (job_markers or {}).get('JOB_SYNC_PIPELINE')
    updated_at = cons.get('updated_at') or cons.get('created_at')
    requires_sdi = _consignment_requires_sdi(cons)

    if local_status == 'FAILED':
        return {
            'tone': 'danger',
            'summary': 'Blocked by a local validation or submission error.',
            'detail': cons.get('error_message') or 'Open the consignment and fix the failed fields before retrying.',
        }

    if goods_total == 0:
        return {
            'tone': 'warning',
            'summary': 'Goods items are still missing.',
            'detail': 'Add at least one goods item before this consignment can move through validation and TSS submission.',
        }

    if not cons.get('dec_reference'):
        if local_status in ('PENDING', 'PENDING REVIEW'):
            detail = 'Goods are present, but this consignment is still local-only. Run Validate Pipeline, then send the cargo pipeline to TSS to create the DEC reference.'
            if last_validate and updated_at and last_validate < updated_at:
                detail = 'This consignment was created or changed after the last Validate Pipeline run, so it has not been processed yet.'
            return {
                'tone': 'warning',
                'summary': 'Waiting for local pipeline processing before TSS submission.',
                'detail': detail,
            }
        if local_status == 'VALIDATED':
            detail = 'Validation is complete. The next step is to create the DEC reference in TSS.'
            if last_submit and updated_at and last_submit < updated_at:
                detail = 'This consignment became VALIDATED after the last cargo send run, so it has not been sent to TSS yet.'
            return {
                'tone': 'info',
                'summary': 'Ready to be sent to TSS.',
                'detail': detail,
            }
        return {
            'tone': 'info',
            'summary': 'No TSS consignment reference exists yet.',
            'detail': 'The next required action is still local pipeline submission.',
        }

    if not tss_status:
        detail = 'A DEC reference exists, but no TSS status has been synced back yet. Run Sync Cargo Statuses.'
        if last_sync and updated_at and last_sync < updated_at:
            detail = 'This consignment changed after the last Sync Pipeline run, so the current TSS status has not been refreshed yet.'
        return {
            'tone': 'info',
            'summary': 'Waiting for TSS status sync.',
            'detail': detail,
        }

    if tss_status == 'ARRIVED':
        if not requires_sdi:
            return {
                'tone': 'success',
                'summary': 'ARRIVED. No supplementary declaration is required.',
                'detail': 'This consignment is configured not to generate SFD/SDI, so there is no downstream supplementary declaration step to start.',
            }
        if not linked_supdecs:
            return {
                'tone': 'warning',
                'summary': 'ARRIVED. SDI is now the next business step.',
                'detail': 'Goods have arrived in NI. Create or sync the linked supplementary declaration.',
            }
        return {
            'tone': 'success',
            'summary': 'ARRIVED and downstream declaration linked.',
            'detail': 'The movement reached ARRIVED and already has its follow-on SDI records attached.',
        }

    if tss_status == 'AUTHORISED_FOR_MOVEMENT':
        if not linked_gmr:
            return {
                'tone': 'primary',
                'summary': 'Authorised for Movement. GMR is the next business step.',
                'detail': 'Create a GMR on the ENS header so the movement can proceed through GVMS.',
            }
        if linked_gmr and not linked_supdecs:
            if not requires_sdi:
                return {
                    'tone': 'success',
                    'summary': 'GMR exists and no supplementary declaration is required.',
                    'detail': 'This consignment is configured not to generate SFD/SDI, so the downstream supplementary declaration step does not apply.',
                }
            return {
                'tone': 'info',
                'summary': 'GMR exists. SDI starts after arrival.',
                'detail': 'The movement is already authorised and the GMR is in place. Create or sync the linked SDI only once the goods reach ARRIVED in NI.',
            }
        return {
            'tone': 'success',
            'summary': 'Downstream movement has been linked.',
            'detail': 'This consignment already has its Route A follow-on records attached.',
        }

    return {
        'tone': 'info',
        'summary': f'Waiting for TSS to progress beyond {cons.get("tss_status") or cons.get("status")}.',
        'detail': 'If this looks stale, run Sync Cargo Statuses to refresh the live state from TSS.',
    }


def _clean_link_ref(value):
    return str(value or '').strip().upper()


def _same_record_id(left, right):
    if left in (None, '') or right in (None, ''):
        return False
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return str(left).strip() == str(right).strip()


def _supdec_matches_consignment(sd, cons, *, single_consignment=False):
    """Match SDIs to the visible DEC row, including old header-only stubs."""
    if not sd or not cons:
        return False

    if _same_record_id(sd.get('staging_cons_id'), cons.get('staging_id')):
        return True

    cons_refs = {
        _clean_link_ref(cons.get('dec_reference')),
        _clean_link_ref(cons.get('sfd_reference')),
        _clean_link_ref(cons.get('synced_sfd_reference')),
        _clean_link_ref(cons.get('linked_sfd_reference')),
    } - {''}
    supdec_refs = {
        _clean_link_ref(sd.get('ens_consignment_ref')),
        _clean_link_ref(sd.get('ens_consignment_reference')),
        _clean_link_ref(sd.get('sfd_reference')),
        _clean_link_ref(sd.get('sfd_number')),
        _clean_link_ref(sd.get('parent')),
        _clean_link_ref(sd.get('u_parent')),
    } - {''}
    if cons_refs & supdec_refs:
        return True

    # Some discovered SDI stubs are attached only to the ENS header. If the
    # ENS has a single DEC row, show that SDI against the only visible DEC
    # instead of making the journey table look contradictory.
    if single_consignment:
        ens_refs = {
            _clean_link_ref(sd.get('ens_header_ref')),
            _clean_link_ref(sd.get('ens_header_reference')),
        } - {''}
        if _clean_link_ref(cons.get('ens_reference')) in ens_refs:
            return True

    return False


def _bkd_table_columns(table_name):
    try:
        schema = get_tenant()["schema"]
        rows = query_all(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            """,
            [schema, table_name],
        )
    except Exception:
        return set()
    return {row['COLUMN_NAME'].lower() for row in rows}


def _first_in_columns(columns, *candidates):
    for candidate in candidates:
        if candidate and candidate.lower() in columns:
            return candidate
    return None


def _ens_header_storage_candidates(field):
    return (field, *ENS_HEADER_STORAGE_ALIASES.get(field, ()))


def _ens_header_value(row, field):
    for candidate in _ens_header_storage_candidates(field):
        value = (row or {}).get(candidate)
        if value not in (None, ''):
            return value
    return None


def _expand_ens_header_values_for_table(table_name, values):
    if table_name != 'StagingEnsHeaders':
        return list(values or [])
    expanded = []
    for name, value in values or []:
        if name == 'arrival_date_time':
            value = _arrival_datetime_for_db(value)
        expanded.append((name, value))
        for alias in ENS_HEADER_STORAGE_ALIASES.get(name, ()):
            expanded.append((alias, value))
    return expanded


def _existing_column_values(values, columns):
    items = []
    seen = set()
    for name, value in values or []:
        lowered = str(name or '').lower()
        if not lowered or lowered in seen or lowered not in columns:
            continue
        items.append((name, value))
        seen.add(lowered)
    return items


def _delete_supdecs_for_consignment_ids(cursor, consignment_ids):
    """Delete local SDI children linked to consignments before parent rows."""
    if not consignment_ids:
        return 0, 0

    supdec_header_cols = _bkd_table_columns('StagingSupDecHeaders')
    supdec_goods_cols = _bkd_table_columns('StagingSupDecGoods')
    if 'staging_cons_id' not in supdec_header_cols:
        return 0, 0
    supdec_header_id_col = _first_in_columns(supdec_header_cols, 'staging_id', 'id')
    supdec_goods_parent_col = _first_in_columns(supdec_goods_cols, 'staging_supdec_id', 'supdec_header_id')
    placeholders = ','.join('?' for _ in consignment_ids)
    deleted_goods = 0

    if supdec_header_id_col and supdec_goods_parent_col:
        cursor.execute(
            f"""
            DELETE g
            FROM BKD.StagingSupDecGoods g
            JOIN BKD.StagingSupDecHeaders sd
              ON sd.[{supdec_header_id_col}] = g.[{supdec_goods_parent_col}]
            WHERE sd.staging_cons_id IN ({placeholders})
            """,
            consignment_ids,
        )
        deleted_goods = cursor.rowcount

    cursor.execute(
        f"DELETE FROM BKD.StagingSupDecHeaders WHERE staging_cons_id IN ({placeholders})",
        consignment_ids,
    )
    return deleted_goods, cursor.rowcount


def _is_present(value):
    return value not in (None, '')


def _update_bkd_existing_columns(cursor, table_name, values, where_clause, where_params):
    columns = _bkd_table_columns(table_name)
    expanded_values = [
        (name, value)
        for name, value in _expand_ens_header_values_for_table(table_name, values)
        if _is_present(value)
    ]
    update_items = _existing_column_values(expanded_values, columns)
    if not update_items:
        return 0

    assignments = ', '.join(f'[{name}] = ?' for name, _ in update_items)
    params = [value for _, value in update_items] + list(where_params or [])
    cursor.execute(
        f"UPDATE BKD.[{table_name}] SET {assignments} WHERE {where_clause}",
        params,
    )
    return cursor.rowcount


def _insert_bkd_existing_columns(cursor, table_name, values, identity_col=None):
    columns = _bkd_table_columns(table_name)
    insert_items = _existing_column_values(
        _expand_ens_header_values_for_table(table_name, values),
        columns,
    )
    if not insert_items:
        raise RuntimeError(f'No compatible columns found for BKD.{table_name}')

    column_sql = ', '.join(f'[{name}]' for name, _ in insert_items)
    placeholders = ', '.join('?' for _ in insert_items)
    params = [value for _, value in insert_items]
    output_sql = ''
    if identity_col and identity_col.lower() in columns:
        output_sql = f' OUTPUT INSERTED.[{identity_col}]'
    cursor.execute(
        f"INSERT INTO BKD.[{table_name}] ({column_sql}){output_sql} VALUES ({placeholders})",
        params,
    )
    if output_sql:
        row = cursor.fetchone()
        return row[0] if row else None
    return None


def _split_tss_import_refs(raw):
    return [
        ref.strip().upper()
        for ref in re.split(r'[\s,;]+', raw or '')
        if ref.strip()
    ]


def _tss_reference_kind(ref):
    value = (ref or '').strip().upper()
    if value.startswith('ENS'):
        return 'ENS'
    if value.startswith('DEC'):
        return 'DEC'
    if value.startswith('SUP'):
        return 'SUP'
    return 'UNKNOWN'


def _current_client_code():
    try:
        return (get_tenant().get('code') or 'BKD').strip().upper() or 'BKD'
    except Exception:
        return 'BKD'


def _qualified_columns(cursor, schema, table):
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [schema, table],
    )
    return {str(row[0]).lower(): str(row[0]) for row in cursor.fetchall()}


def _qualified_values(payload, columns, *, include_empty=False):
    values = []
    for key, value in (payload or {}).items():
        column = columns.get(str(key).lower())
        if not column:
            continue
        if value is None:
            continue
        if not include_empty and value == '':
            continue
        values.append((column, value))
    return values


def _update_qualified_existing(cursor, schema, table, values, where_sql, where_params):
    if not values:
        return
    assignments = ', '.join(f'[{column}] = ?' for column, _ in values)
    cursor.execute(
        f"UPDATE [{schema}].[{table}] SET {assignments} WHERE {where_sql}",
        [value for _, value in values] + list(where_params or []),
    )


def _insert_qualified_existing(cursor, schema, table, values, identity_col):
    if not values:
        raise RuntimeError(f'{schema}.{table} has no usable columns for TSS import')
    columns_sql = ', '.join(f'[{column}]' for column, _ in values)
    placeholders = ', '.join('?' for _ in values)
    cursor.execute(
        f"INSERT INTO [{schema}].[{table}] ({columns_sql}) "
        f"OUTPUT INSERTED.[{identity_col}] VALUES ({placeholders})",
        [value for _, value in values],
    )
    row = cursor.fetchone()
    return int(row[0]) if row else None


def _json_dump_tss_payload(payload):
    return json.dumps(payload or {}, default=str, ensure_ascii=True)


def _stg_parent_ens_ref(cursor, stg_header_id):
    if not stg_header_id:
        return ''
    cursor.execute(
        """
        SELECT TOP 1 tss_ens_header_ref
        FROM STG.BKD_ENS_Headers
        WHERE stg_header_id = ?
        """,
        [stg_header_id],
    )
    row = cursor.fetchone()
    return str(row[0] or '').strip() if row else ''


def _stg_parent_cons_ref(cursor, stg_consignment_id):
    if not stg_consignment_id:
        return ''
    cursor.execute(
        """
        SELECT TOP 1 tss_consignment_ref
        FROM STG.BKD_ENS_Consignments
        WHERE stg_consignment_id = ?
        """,
        [stg_consignment_id],
    )
    row = cursor.fetchone()
    return str(row[0] or '').strip() if row else ''


def _ensure_import_parent_header(cursor, dec_ref):
    client_code = _current_client_code()
    label = f'Imported parent for {dec_ref}'
    cursor.execute(
        """
        SELECT TOP 1 stg_header_id
        FROM STG.BKD_ENS_Headers
        WHERE ClientCode = ? AND label = ?
        ORDER BY stg_header_id DESC
        """,
        [client_code, label],
    )
    existing = cursor.fetchone()
    if existing:
        return int(existing[0])

    cols = _qualified_columns(cursor, 'STG', 'BKD_ENS_Headers')
    values = _qualified_values(
        {
            'ClientCode': client_code,
            'label': label,
            'sub_status': 'IMPORTED',
            'source': 'TSS_IMPORT',
            'validation_errors_json': json.dumps(
                {
                    'message': 'Imported DEC could not be linked to an ENS reference from TSS.',
                    'dec_reference': dec_ref,
                },
                ensure_ascii=True,
            ),
            'last_sub_status_change': _utc_now_naive(),
            'updated_at': _utc_now_naive(),
        },
        cols,
    )
    return _insert_qualified_existing(cursor, 'STG', 'BKD_ENS_Headers', values, 'stg_header_id')


def _utc_now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tss_datetime_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)

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


def _first_value(source, *keys):
    source = source or {}
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if value not in (None, ''):
            return value
    return None


def _tss_detail_payload(payload):
    """Flatten common TSS read wrappers while preserving useful detail fields."""
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
            merged.update(_tss_detail_payload(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    merged.update(_tss_detail_payload(item))
                    break
    return merged


def _tss_detail_payload_from_result(result):
    if not isinstance(result, dict):
        return {}
    return _tss_detail_payload(result.get('response') or result.get('data') or result)


def _merge_tss_payloads(*payloads):
    merged = {}
    for payload in payloads:
        detail = _tss_detail_payload(payload)
        for key, value in detail.items():
            if value not in (None, ''):
                merged[key] = value
    return merged


def _tss_response_items(result):
    from app.tss_api import TssApiClient

    if isinstance(result, dict) and 'response' in result:
        return TssApiClient.as_items(result.get('response'))
    return TssApiClient.as_items(result)


def _tss_reference_from_item(item, *keys):
    if isinstance(item, str):
        return item.strip().upper()
    if not isinstance(item, dict):
        return ''
    for key in keys:
        value = item.get(key)
        if value not in (None, ''):
            return str(value).strip().upper()
    return ''


def _tss_result_message(result):
    if not isinstance(result, dict):
        return ''

    for key in ('message', 'error_message', 'process_message'):
        value = result.get(key)
        if value:
            return str(value)

    response = result.get('response')
    if isinstance(response, dict):
        for key in ('process_message', 'error_message', 'error_details', 'message'):
            value = response.get(key)
            if value:
                return str(value)

    return str(result.get('raw_response') or '')


def _tss_access_denied(result):
    message = _tss_result_message(result).lower()
    return 'unable to access target record' in message


def _format_tss_import_failure(ref, result, record_label):
    message = _tss_result_message(result) or 'no response from TSS'
    if _tss_access_denied(result):
        return (
            f'TSS refused access to {record_label} {ref}. '
            'This usually means the reference belongs to another TSS authentication or Act As customer, '
            'the selected environment is wrong, or the current credentials cannot see it. '
            'Check Admin Settings, the Act As customer, and that the reference exists in that TSS environment.'
        )
    return (
        f'TSS {record_label} read failed for {ref}: {message}. '
        'Check the API log for the raw TSS response.'
    )


def _tss_nested_consignments(header_payload):
    if not isinstance(header_payload, dict):
        return []
    for key in (
        'consignments',
        'ens_consignments',
        'consignment_numbers',
        'consignment_references',
        'declaration_consignments',
    ):
        value = header_payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
    return []


def _compact_text(value, limit=500):
    text = str(value or '').replace('\r\n', ' ').replace('\n', ' ').strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + '...'


def _tss_import_error_detail(result, friendly_error=None):
    if not isinstance(result, dict):
        return _compact_text(friendly_error, 2000)
    if result.get('success') and not friendly_error:
        return ''

    raw = (
        result.get('raw_response')
        or result.get('message')
        or result.get('error_message')
        or result.get('process_message')
        or ''
    )
    if friendly_error and raw:
        return _compact_text(f'{friendly_error}\n\nRaw TSS response: {raw}', 2000)
    return _compact_text(friendly_error or raw, 2000)


def _log_tss_import_call(cursor, staging_id, call_type, result, friendly_error=None):
    try:
        request_payload = {}
        if isinstance(result, dict):
            request_payload = result.get('request_params') or result.get('request_payload') or {}
        response_message = (
            _compact_text(friendly_error)
            if friendly_error
            else (_compact_text(result.get('message')) if isinstance(result, dict) else '')
        )
        _log_tss_api_exchange(
            staging_id=staging_id,
            call_type=call_type,
            http_method=result.get('method', 'GET') if isinstance(result, dict) else 'GET',
            url=result.get('url', '') if isinstance(result, dict) else '',
            request_payload=request_payload if isinstance(result, dict) else None,
            http_status=result.get('http_status', 0) if isinstance(result, dict) else 0,
            response_status=result.get('status', '') if isinstance(result, dict) else '',
            response_message=response_message,
            response_json=result.get('raw_response', '')[:4000] if isinstance(result, dict) else '',
            duration_ms=result.get('duration_ms', 0) if isinstance(result, dict) else 0,
            error_detail=_tss_import_error_detail(result, friendly_error),
        )
    except Exception:
        pass


def _log_tss_import_result_error(cursor, result):
    if not isinstance(result, dict) or result.get('status') != 'error':
        return
    try:
        ref = result.get('ref', '')
        kind = result.get('kind', 'Unknown')
        message = _compact_text(result.get('msg'), 2000)
        _log_tss_api_exchange(
            staging_id=result.get('staging_id'),
            call_type='IMPORT_TSS_REFERENCE_ERROR',
            http_method='LOCAL',
            url='/declarations/import-ens',
            request_payload={'reference': ref, 'kind': kind},
            http_status=0,
            response_status='error',
            response_message=_compact_text(message),
            response_json=result,
            duration_ms=0,
            error_detail=message,
        )
    except Exception:
        pass


def _first_import_error_summary(results):
    for result in results or []:
        if isinstance(result, dict) and result.get('status') == 'error':
            ref = result.get('ref') or 'reference'
            message = _compact_text(result.get('msg'), 260)
            return f'{ref}: {message}' if message else str(ref)
    return ''


def _upsert_imported_ens_header(cursor, ens_ref, header_data):
    now = _utc_now_naive()
    client_code = _current_client_code()
    tss_status = _first_value(header_data, 'status', 'tss_status') or 'PENDING_SYNC'
    label = _first_text(
        _first_value(header_data, 'label'),
        _first_value(header_data, 'carrier_name'),
        ens_ref,
    )
    if label != ens_ref:
        label = f'{ens_ref} - {label}'

    payload = {
        'ClientCode': client_code,
        'label': label,
        'tss_ens_header_ref': ens_ref,
        'sub_status': 'IMPORTED',
        'source': 'TSS_IMPORT',
        'validation_errors_json': json.dumps({'tss_status': tss_status, 'error_message': _first_value(header_data, 'error_message')}, ensure_ascii=True) if _first_value(header_data, 'error_message') else None,
        'movement_type': _first_value(header_data, 'movement_type'),
        'type_of_passive_transport': _first_value(header_data, 'type_of_passive_transport'),
        'identity_no_of_transport': _first_value(header_data, 'identity_no_of_transport', 'identity_no_transport'),
        'nationality_of_transport': _first_value(header_data, 'nationality_of_transport'),
        'conveyance_ref': _first_value(header_data, 'conveyance_ref'),
        'arrival_date_time': _first_value(header_data, 'arrival_date_time'),
        'arrival_port': _first_value(header_data, 'arrival_port'),
        'place_of_loading': _first_value(header_data, 'place_of_loading'),
        'place_of_unloading': _first_value(header_data, 'place_of_unloading'),
        'place_of_acceptance_same_as_loading': _first_value(header_data, 'place_of_acceptance_same_as_loading'),
        'place_of_acceptance': _first_value(header_data, 'place_of_acceptance'),
        'place_of_delivery_same_as_unloading': _first_value(header_data, 'place_of_delivery_same_as_unloading'),
        'place_of_delivery': _first_value(header_data, 'place_of_delivery'),
        'seal_number': _first_value(header_data, 'seal_number'),
        'transport_charges': _first_value(header_data, 'transport_charges'),
        'route': _first_value(header_data, 'route'),
        'carrier_eori': _first_value(header_data, 'carrier_eori'),
        'carrier_name': _first_value(header_data, 'carrier_name'),
        'carrier_street_number': _first_value(header_data, 'carrier_street_number'),
        'carrier_city': _first_value(header_data, 'carrier_city'),
        'carrier_postcode': _first_value(header_data, 'carrier_postcode'),
        'carrier_country': _first_value(header_data, 'carrier_country'),
        'haulier_eori': _first_value(header_data, 'haulier_eori'),
        'last_sub_status_change': now,
        'updated_at': now,
    }
    cols = _qualified_columns(cursor, 'STG', 'BKD_ENS_Headers')
    cursor.execute(
        """
        SELECT TOP 1 stg_header_id, source
        FROM STG.BKD_ENS_Headers
        WHERE ClientCode = ? AND tss_ens_header_ref = ?
        ORDER BY stg_header_id DESC
        """,
        [client_code, ens_ref],
    )
    existing = cursor.fetchone()
    values = _qualified_values(payload, cols)

    if existing:
        existing_id = int(existing[0])
        existing_source = str(existing[1] or '').upper()
        if existing_source != 'TSS_IMPORT':
            values = [(key, value) for key, value in values if key.lower() not in {'sub_status', 'source'}]
        _update_qualified_existing(cursor, 'STG', 'BKD_ENS_Headers', values, 'stg_header_id = ?', [existing_id])
        inserted = False
        staging_id = existing_id
    else:
        staging_id = _insert_qualified_existing(cursor, 'STG', 'BKD_ENS_Headers', values, 'stg_header_id')
        inserted = True

    try:
        from app.ingestion.ens_status_watcher import _upsert_tss_ens_header_status
        _upsert_tss_ens_header_status(
            cursor,
            client_code=client_code,
            ens_ref=ens_ref,
            tss_status=tss_status,
            raw_json=_json_dump_tss_payload(header_data),
        )
    except Exception:
        current_app.logger.debug('TSS ENS mirror upsert skipped for imported %s', ens_ref, exc_info=True)

    return staging_id, inserted


def _upsert_imported_consignment(cursor, staging_ens_id, cons_ref, cons_data):
    now = _utc_now_naive()
    client_code = _current_client_code()
    tss_status = _first_value(cons_data, 'status', 'tss_status') or 'PENDING_SYNC'
    parent_ens_ref = _first_text(
        _first_value(cons_data, 'ens_reference'),
        _first_value(cons_data, 'ens_number'),
        _first_value(cons_data, 'ens_header_reference'),
        _first_value(cons_data, 'declaration_number'),
        _stg_parent_ens_ref(cursor, staging_ens_id),
    )
    label = _first_text(
        _first_value(cons_data, 'label'),
        _first_value(cons_data, 'trader_reference'),
        _first_value(cons_data, 'transport_document_number'),
        cons_ref,
    )

    payload = {
        'ClientCode': client_code,
        'stg_header_id': staging_ens_id,
        'sub_status': 'IMPORTED',
        'source': 'TSS_IMPORT',
        'tss_consignment_ref': cons_ref,
        'tss_ens_header_ref': parent_ens_ref,
        'goods_description': _first_value(cons_data, 'goods_description'),
        'trader_reference': _first_value(cons_data, 'trader_reference') or label,
        'transport_document_number': _first_value(cons_data, 'transport_document_number'),
        'controlled_goods': _first_value(cons_data, 'controlled_goods'),
        'goods_domestic_status': _first_value(cons_data, 'goods_domestic_status'),
        'destination_country': _first_value(cons_data, 'destination_country', 'country_of_destination'),
        'ducr': _first_value(cons_data, 'ducr'),
        'align_ukims': _first_value(cons_data, 'align_ukims'),
        'use_importer_sde': _first_value(cons_data, 'use_importer_sde'),
        'declaration_choice': _first_value(cons_data, 'declaration_choice'),
        'generate_SD': _first_value(cons_data, 'generate_SD', 'generate_sd'),
        'container_indicator': _first_value(cons_data, 'container_indicator'),
        'buyer_same_as_importer': _first_value(cons_data, 'buyer_same_as_importer'),
        'seller_same_as_exporter': _first_value(cons_data, 'seller_same_as_exporter'),
        'no_sfd_reason': _first_value(cons_data, 'no_sfd_reason'),
        'consignor_eori': _first_value(cons_data, 'consignor_eori'),
        'consignor_name': _first_value(cons_data, 'consignor_name'),
        'consignor_street_number': _first_value(cons_data, 'consignor_street_number'),
        'consignor_city': _first_value(cons_data, 'consignor_city'),
        'consignor_postcode': _first_value(cons_data, 'consignor_postcode'),
        'consignor_country': _first_value(cons_data, 'consignor_country'),
        'consignee_eori': _first_value(cons_data, 'consignee_eori'),
        'consignee_name': _first_value(cons_data, 'consignee_name'),
        'consignee_street_number': _first_value(cons_data, 'consignee_street_number'),
        'consignee_city': _first_value(cons_data, 'consignee_city'),
        'consignee_postcode': _first_value(cons_data, 'consignee_postcode'),
        'consignee_country': _first_value(cons_data, 'consignee_country'),
        'importer_eori': _first_value(cons_data, 'importer_eori'),
        'importer_name': _first_value(cons_data, 'importer_name'),
        'importer_street_number': _first_value(cons_data, 'importer_street_number'),
        'importer_city': _first_value(cons_data, 'importer_city'),
        'importer_postcode': _first_value(cons_data, 'importer_postcode'),
        'importer_country': _first_value(cons_data, 'importer_country'),
        'exporter_eori': _first_value(cons_data, 'exporter_eori'),
        'metadata_json': _json_dump_tss_payload(cons_data),
        'last_sub_status_change': now,
        'updated_at': now,
    }
    cols = _qualified_columns(cursor, 'STG', 'BKD_ENS_Consignments')
    cursor.execute(
        """
        SELECT TOP 1 stg_consignment_id, source
        FROM STG.BKD_ENS_Consignments
        WHERE ClientCode = ? AND tss_consignment_ref = ?
        ORDER BY stg_consignment_id DESC
        """,
        [client_code, cons_ref],
    )
    existing = cursor.fetchone()
    values = _qualified_values(payload, cols)

    if existing:
        staging_id = int(existing[0])
        existing_source = str(existing[1] or '').upper()
        if existing_source != 'TSS_IMPORT':
            values = [(key, value) for key, value in values if key.lower() not in {'sub_status', 'source'}]
        _update_qualified_existing(cursor, 'STG', 'BKD_ENS_Consignments', values, 'stg_consignment_id = ?', [staging_id])
        inserted = False
    else:
        staging_id = _insert_qualified_existing(cursor, 'STG', 'BKD_ENS_Consignments', values, 'stg_consignment_id')
        inserted = True

    try:
        from app.ingestion.ens_status_watcher import _upsert_tss_cons_status
        _upsert_tss_cons_status(
            cursor,
            client_code=client_code,
            ens_ref=parent_ens_ref,
            dec_ref=cons_ref,
            tss_status=tss_status,
            raw_json=_json_dump_tss_payload(cons_data),
        )
    except Exception:
        current_app.logger.debug('TSS consignment mirror upsert skipped for imported %s', cons_ref, exc_info=True)

    return staging_id, inserted


def _upsert_imported_goods(cursor, staging_cons_id, goods_ref, goods_data, item_number):
    now = _utc_now_naive()
    client_code = _current_client_code()
    tss_status = _first_value(goods_data, 'status', 'tss_status') or 'CREATED'
    dec_ref = _first_text(
        _first_value(goods_data, 'consignment_number'),
        _first_value(goods_data, 'parent_reference'),
        _first_value(goods_data, 'declaration_number'),
        _stg_parent_cons_ref(cursor, staging_cons_id),
    )
    label = _first_text(
        _first_value(goods_data, 'label'),
        _first_value(goods_data, 'goods_description'),
        goods_ref,
    )
    if len(label) > 100:
        label = label[:97] + '...'

    package_type = _first_value(goods_data, 'type_of_packages', 'type_of_package')
    try:
        package_type = normalise_package_type(package_type, 'PK') or package_type
    except Exception:
        pass

    payload = {
        'ClientCode': client_code,
        'stg_consignment_id': staging_cons_id,
        'sub_status': 'IMPORTED',
        'source': 'TSS_IMPORT',
        'goods_stage': 'ENS',
        'tss_hex_id': goods_ref,
        'tss_consignment_ref': dec_ref,
        'item_seq': _first_value(goods_data, 'item_number', 'goods_item_number') or item_number,
        'sku': _first_value(goods_data, 'sku'),
        'goods_description': _first_value(goods_data, 'goods_description') or label,
        'commodity_code': _first_value(goods_data, 'commodity_code'),
        'gross_mass_kg': _first_value(goods_data, 'gross_mass_kg', 'gross_weight_kg'),
        'net_mass_kg': _first_value(goods_data, 'net_mass_kg', 'net_weight_kg'),
        'number_of_packages': _first_value(goods_data, 'number_of_packages'),
        'number_of_individual_pieces': _first_value(goods_data, 'number_of_individual_pieces'),
        'type_of_packages': package_type,
        'package_marks': _first_value(goods_data, 'package_marks'),
        'equipment_number': _first_value(goods_data, 'equipment_number'),
        'procedure_code': _first_value(goods_data, 'procedure_code'),
        'additional_procedure_code': _first_value(goods_data, 'additional_procedure_code'),
        'controlled_goods': _first_value(goods_data, 'controlled_goods'),
        'country_of_origin': _first_value(goods_data, 'country_of_origin'),
        'item_invoice_amount': _first_value(goods_data, 'item_invoice_amount'),
        'item_invoice_currency': _first_value(goods_data, 'item_invoice_currency'),
        'customs_value': _first_value(goods_data, 'customs_value'),
        'valuation_method': _first_value(goods_data, 'valuation_method'),
        'statistical_value': _first_value(goods_data, 'statistical_value'),
        'nature_of_transaction': _first_value(goods_data, 'nature_of_transaction'),
        'preference': _first_value(goods_data, 'preference'),
        'supplementary_units': _first_value(goods_data, 'supplementary_units'),
        'error_message': _first_value(goods_data, 'error_message'),
        'last_sub_status_change': now,
        'updated_at': now,
    }
    cols = _qualified_columns(cursor, 'STG', 'BKD_GoodsItems')
    cursor.execute(
        """
        SELECT TOP 1 stg_item_id, source
        FROM STG.BKD_GoodsItems
        WHERE ClientCode = ? AND goods_stage = 'ENS' AND tss_hex_id = ?
        ORDER BY stg_item_id DESC
        """,
        [client_code, goods_ref],
    )
    existing = cursor.fetchone()
    values = _qualified_values(payload, cols)

    if existing:
        staging_id = int(existing[0])
        existing_source = str(existing[1] or '').upper()
        if existing_source != 'TSS_IMPORT':
            values = [(key, value) for key, value in values if key.lower() not in {'sub_status', 'source'}]
        _update_qualified_existing(cursor, 'STG', 'BKD_GoodsItems', values, 'stg_item_id = ?', [staging_id])
        inserted = False
    else:
        staging_id = _insert_qualified_existing(cursor, 'STG', 'BKD_GoodsItems', values, 'stg_item_id')
        inserted = True

    try:
        from app.ingestion.ens_status_watcher import _upsert_tss_goods
        _upsert_tss_goods(
            cursor,
            client_code=client_code,
            dec_ref=dec_ref,
            goods_ref=goods_ref,
            item_number=str(payload.get('item_seq') or item_number or ''),
            tss_status=tss_status,
            raw_json=_json_dump_tss_payload(goods_data),
        )
    except Exception:
        current_app.logger.debug('TSS goods mirror upsert skipped for imported %s', goods_ref, exc_info=True)

    return staging_id, inserted


def _read_tss_header_for_import(api, ens_ref):
    result = api.read_header(ens_ref, TSS_IMPORT_HEADER_FIELDS)
    if result.get('success') and isinstance(result.get('response'), dict):
        return result, _tss_detail_payload_from_result(result)
    if _tss_access_denied(result):
        return result, {}

    fallback = api.read_header(ens_ref, TSS_IMPORT_HEADER_FALLBACK_FIELDS)
    if fallback.get('success') and isinstance(fallback.get('response'), dict):
        return fallback, _tss_detail_payload_from_result(fallback)
    return fallback if _tss_result_message(fallback) else result, {}


def _read_tss_consignment_for_import(api, cons_ref):
    result = api.read_consignment(cons_ref, TSS_IMPORT_CONSIGNMENT_FIELDS)
    primary_data = _tss_detail_payload_from_result(result)
    if result.get('success') and isinstance(result.get('response'), dict):
        fallback = api.read_consignment(cons_ref, TSS_IMPORT_CONSIGNMENT_FALLBACK_FIELDS)
        fallback_data = _tss_detail_payload_from_result(fallback)
        if fallback.get('success') and isinstance(fallback.get('response'), dict):
            return fallback, _merge_tss_payloads(primary_data, fallback_data)
        return result, primary_data

    fallback = api.read_consignment(cons_ref, TSS_IMPORT_CONSIGNMENT_FALLBACK_FIELDS)
    if fallback.get('success') and isinstance(fallback.get('response'), dict):
        return fallback, _merge_tss_payloads(primary_data, _tss_detail_payload_from_result(fallback))
    return result, {}


def _read_tss_goods_for_import(api, goods_ref, fallback=None):
    result = api.read_goods(goods_ref, TSS_IMPORT_GOODS_FIELDS)
    if result.get('success') and isinstance(result.get('response'), dict):
        return result, _tss_detail_payload_from_result(result)

    fallback_result = api.read_goods(goods_ref, TSS_IMPORT_GOODS_FALLBACK_FIELDS)
    if fallback_result.get('success') and isinstance(fallback_result.get('response'), dict):
        return fallback_result, _tss_detail_payload_from_result(fallback_result)
    return result, dict(fallback or {})


def _sync_imported_goods_for_consignment(cursor, api, staging_cons_id, cons_ref):
    summary = {'created': 0, 'updated': 0, 'errors': 0}
    lookup = api.lookup_ens_goods(cons_ref)
    _log_tss_import_call(cursor, staging_cons_id, 'IMPORT_TSS_GOODS_LOOKUP', lookup)
    if not lookup.get('success') or not _tss_response_items(lookup):
        # ens_number lookup returned nothing — try consignment_number
        fallback_lookup = api.lookup_goods(cons_ref, parent_type='consignment_number')
        _log_tss_import_call(cursor, staging_cons_id, 'IMPORT_TSS_GOODS_LOOKUP_FALLBACK', fallback_lookup)
        if fallback_lookup.get('success') and _tss_response_items(fallback_lookup):
            lookup = fallback_lookup
        elif not lookup.get('success'):
            summary['errors'] += 1
            return summary

    for index, item in enumerate(_tss_response_items(lookup), start=1):
        goods_ref = _tss_reference_from_item(item, 'goods_id', 'reference')
        if not goods_ref:
            summary['errors'] += 1
            continue

        read_result, goods_data = _read_tss_goods_for_import(
            api,
            goods_ref,
            fallback=item if isinstance(item, dict) else {},
        )
        _log_tss_import_call(cursor, staging_cons_id, 'IMPORT_TSS_GOODS_READ', read_result)
        goods_data = _merge_tss_payloads(item if isinstance(item, dict) else {}, goods_data)
        goods_data.setdefault('goods_id', goods_ref)
        goods_data.setdefault('reference', goods_ref)

        _, inserted = _upsert_imported_goods(
            cursor,
            staging_cons_id,
            goods_ref,
            goods_data,
            index,
        )
        if inserted:
            summary['created'] += 1
        else:
            summary['updated'] += 1
    return summary


def _discover_tss_consignments_for_ens(cursor, api, staging_ens_id, ens_ref, header_data):
    discovered = []
    seen = set()

    for item in _tss_nested_consignments(header_data):
        cons_ref = _tss_reference_from_item(
            item,
            'reference',
            'consignment_number',
            'declaration_number',
            'dec_reference',
        )
        if cons_ref and cons_ref not in seen:
            discovered.append(item if isinstance(item, dict) else {'reference': cons_ref})
            seen.add(cons_ref)

    return discovered


def _upsert_imported_sfd(cursor, sfd_ref, dec_ref, ens_ref, sfd_data):
    client_code = _current_client_code()
    mrn = sfd_data.get('mrn') or sfd_data.get('movement_reference_number')
    tss_status = sfd_data.get('status') or sfd_data.get('tss_status') or 'PENDING_SYNC'
    cursor.execute(
        """
        SELECT TOP 1 TssSfdId
        FROM TSS.BKD_SFD
        WHERE ClientCode = ? AND SfdReference = ?
        """,
        [client_code, sfd_ref],
    )
    existing = cursor.fetchone()
    try:
        from app.ingestion.ens_status_watcher import _upsert_stg_sfd_tracking, _upsert_tss_sfd
        raw_json = _json_dump_tss_payload(sfd_data)
        _upsert_tss_sfd(
            cursor,
            client_code=client_code,
            ens_ref=ens_ref,
            dec_ref=dec_ref,
            sfd_ref=sfd_ref,
            movement_reference_number=mrn or '',
            tss_status=tss_status,
            raw_json=raw_json,
        )
        _upsert_stg_sfd_tracking(
            cursor,
            client_code=client_code,
            dec_ref=dec_ref,
            sfd_ref=sfd_ref,
            movement_reference_number=mrn or '',
            eori_for_eidr=sfd_data.get('eori_for_eidr') or '',
            tss_status=tss_status,
            error_message=sfd_data.get('error_message') or '',
        )
    except Exception:
        current_app.logger.debug('SFD mirror upsert skipped for imported %s', sfd_ref, exc_info=True)
    return (existing[0] if existing else None), not bool(existing)


def _upsert_tss_sdi_import_stub(
    cursor,
    sup_ref,
    sfd_ref='',
    movement_reference_number='',
    sdi_data=None,
    stg_consignment_id=None,
    dec_ref='',
):
    client_code = _current_client_code()
    sdi_data = sdi_data or {}
    tss_status = _first_value(sdi_data, 'status', 'tss_status') or 'PENDING_SYNC'
    due_date = _first_value(sdi_data, 'submission_due_date', 'due_date')
    dec_ref = dec_ref or _first_value(
        sdi_data,
        'consignment_number',
        'declaration_number',
        'ens_consignment_ref',
        'ens_consignment_reference',
    )
    cursor.execute(
        """
        SELECT TOP 1 TssSdiHeaderId
        FROM TSS.BKD_SDI_Headers
        WHERE ClientCode = ? AND SupDecNumber = ?
        """,
        [client_code, sup_ref],
    )
    existing = cursor.fetchone()
    cursor.execute(
        """
        MERGE TSS.BKD_SDI_Headers AS target
        USING (SELECT ? AS ClientCode, ? AS SupDecNumber) AS src
        ON target.ClientCode = src.ClientCode
           AND target.SupDecNumber = src.SupDecNumber
        WHEN MATCHED THEN
            UPDATE SET
                SfdReference            = COALESCE(NULLIF(?, ''), target.SfdReference),
                MovementReferenceNumber = COALESCE(NULLIF(?, ''), target.MovementReferenceNumber),
                TssStatus               = COALESCE(NULLIF(?, ''), target.TssStatus),
                SubmissionDueDate       = COALESCE(TRY_CONVERT(DATE, NULLIF(?, '')), target.SubmissionDueDate),
                RawJson                 = COALESCE(?, target.RawJson),
                LastSyncedAt            = SYSUTCDATETIME(),
                UpdatedAt               = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (ClientCode, SupDecNumber, SfdReference, MovementReferenceNumber,
                    TssStatus, SubmissionDueDate, RawJson, LastSyncedAt, UpdatedAt)
            VALUES (?, ?, NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''),
                    TRY_CONVERT(DATE, NULLIF(?, '')), ?, SYSUTCDATETIME(), SYSUTCDATETIME());
        """,
        [
            client_code, sup_ref,
            sfd_ref, movement_reference_number, tss_status, due_date, _json_dump_tss_payload(sdi_data),
            client_code, sup_ref, sfd_ref, movement_reference_number, tss_status, due_date,
            _json_dump_tss_payload(sdi_data),
        ],
    )

    cols = _qualified_columns(cursor, 'STG', 'BKD_SDI_Headers')
    payload = {
        'ClientCode': client_code,
        'sub_status': 'IMPORTED',
        'stg_consignment_id': stg_consignment_id,
        'tss_consignment_ref': dec_ref,
        'tss_sup_dec_number': sup_ref,
        'tss_sfd_consignment_ref': sfd_ref,
        'tss_submission_due_date': due_date,
        'tss_status': tss_status,
        'tss_movement_reference_number': movement_reference_number,
        'tss_error_message': _first_value(sdi_data, 'error_message'),
        'last_sub_status_change': _utc_now_naive(),
    }
    values = _qualified_values(payload, cols)
    cursor.execute(
        """
        SELECT TOP 1 stg_sdi_id
        FROM STG.BKD_SDI_Headers
        WHERE ClientCode = ? AND tss_sup_dec_number = ?
        """,
        [client_code, sup_ref],
    )
    stg_existing = cursor.fetchone()
    if stg_existing:
        _update_qualified_existing(cursor, 'STG', 'BKD_SDI_Headers', values, 'stg_sdi_id = ?', [int(stg_existing[0])])
    elif values:
        _insert_qualified_existing(cursor, 'STG', 'BKD_SDI_Headers', values, 'stg_sdi_id')
    return (existing[0] if existing else None), not bool(existing)


def _upsert_imported_sdi_stub(cursor, sup_ref, sfd_ref, dec_ref, ens_ref, staging_cons_id=None):
    return _upsert_tss_sdi_import_stub(
        cursor,
        sup_ref,
        sfd_ref=sfd_ref,
        movement_reference_number='',
        stg_consignment_id=staging_cons_id,
        dec_ref=dec_ref,
        sdi_data={
            'sup_dec_number': sup_ref,
            'sfd_reference': sfd_ref,
            'ens_reference': ens_ref,
            'consignment_number': dec_ref,
            'status': 'PENDING_SYNC',
        },
    )


def _sync_imported_sfd_sdi_for_consignment(cursor, api, cons_ref, staging_cons_id, ens_ref):
    summary = {'sfd_created': 0, 'sfd_updated': 0, 'sdi_created': 0, 'sdi_updated': 0, 'errors': 0}
    sfd_result = api.lookup_sfd(cons_ref)
    _log_tss_import_call(cursor, staging_cons_id, 'IMPORT_TSS_SFD_LOOKUP', sfd_result)
    if not sfd_result.get('success'):
        return summary

    for sfd_item in api.as_items(sfd_result.get('response')):
        sfd_ref = _tss_reference_from_item(
            sfd_item, 'sfd_reference', 'reference', 'sfd_number', 'declaration_number'
        )
        if not sfd_ref:
            summary['errors'] += 1
            continue

        _, sfd_inserted = _upsert_imported_sfd(
            cursor, sfd_ref, cons_ref, ens_ref,
            sfd_item if isinstance(sfd_item, dict) else {},
        )
        if sfd_inserted:
            summary['sfd_created'] += 1
        else:
            summary['sfd_updated'] += 1

        sdi_result = api.lookup_sdi(sfd_ref)
        _log_tss_import_call(cursor, staging_cons_id, 'IMPORT_TSS_SDI_LOOKUP', sdi_result)
        if not sdi_result.get('success'):
            continue

        for sdi_item in api.as_sdi_lookup_items(sdi_result.get('response')):
            sup_ref = _tss_reference_from_item(
                sdi_item, 'sup_dec_number', 'reference', 'supplementary_declaration_number', 'number'
            )
            if not sup_ref:
                continue
            _, sdi_inserted = _upsert_imported_sdi_stub(
                cursor,
                sup_ref,
                sfd_ref,
                cons_ref,
                ens_ref,
                staging_cons_id=staging_cons_id,
            )
            if sdi_inserted:
                summary['sdi_created'] += 1
            else:
                summary['sdi_updated'] += 1

    return summary


def _import_tss_ens_full(cursor, ref):
    from app.tss_api import build_cfg_client

    api = build_cfg_client()
    header_result, header_data = _read_tss_header_for_import(api, ref)
    header_read_failed = not header_result.get('success')
    header_failure_detail = ''

    if header_read_failed:
        header_failure_detail = _format_tss_import_failure(ref, header_result, 'ENS header')
        header_data = {
            'status': 'PENDING_SYNC',
            'error_message': header_failure_detail,
        }

    staging_ens_id, header_inserted = _upsert_imported_ens_header(cursor, ref, header_data)
    _log_tss_import_call(
        cursor,
        staging_ens_id,
        'IMPORT_TSS_ENS_READ_FAILED' if header_read_failed else 'IMPORT_TSS_ENS_READ',
        header_result,
        friendly_error=header_failure_detail,
    )

    cons_created = cons_updated = goods_created = goods_updated = child_errors = 0
    sfd_created = sfd_updated = sdi_created = sdi_updated = 0
    discovered = _discover_tss_consignments_for_ens(cursor, api, staging_ens_id, ref, header_data)

    for item in discovered:
        cons_ref = _tss_reference_from_item(
            item,
            'reference',
            'consignment_number',
            'declaration_number',
            'dec_reference',
        )
        if not cons_ref:
            child_errors += 1
            continue

        read_result, cons_data = _read_tss_consignment_for_import(api, cons_ref)
        _log_tss_import_call(cursor, staging_ens_id, 'IMPORT_TSS_CONSIGNMENT_READ', read_result)
        cons_data = _merge_tss_payloads(item if isinstance(item, dict) else {}, cons_data)
        cons_data.setdefault('reference', cons_ref)
        cons_data.setdefault('declaration_number', cons_ref)

        staging_cons_id, cons_inserted = _upsert_imported_consignment(
            cursor,
            staging_ens_id,
            cons_ref,
            cons_data,
        )
        if cons_inserted:
            cons_created += 1
        else:
            cons_updated += 1

        goods_summary = _sync_imported_goods_for_consignment(cursor, api, staging_cons_id, cons_ref)
        goods_created += goods_summary['created']
        goods_updated += goods_summary['updated']
        child_errors += goods_summary['errors']

        sfd_sdi = _sync_imported_sfd_sdi_for_consignment(cursor, api, cons_ref, staging_cons_id, ref)
        sfd_created += sfd_sdi['sfd_created']
        sfd_updated += sfd_sdi['sfd_updated']
        sdi_created += sfd_sdi['sdi_created']
        sdi_updated += sfd_sdi['sdi_updated']
        child_errors += sfd_sdi['errors']

    parts = []
    if header_read_failed:
        parts.append('ENS header read failed, but Fusion still attempted child consignment/goods discovery')
        parts.append(_format_tss_import_failure(ref, header_result, 'ENS header'))
    else:
        parts.append('ENS imported with live TSS data' if header_inserted else 'ENS refreshed from TSS')

    parts.extend([
        f'{cons_created} consignments created',
        f'{cons_updated} consignments updated',
        f'{goods_created} goods created',
        f'{goods_updated} goods updated',
    ])
    if sfd_created or sfd_updated:
        parts.append(f'{sfd_created} SFDs created, {sfd_updated} updated')
    if sdi_created or sdi_updated:
        parts.append(f'{sdi_created} SDIs created, {sdi_updated} updated')
    if not discovered:
        parts.append('header-only import: TSS does not expose child DEC discovery from an ENS reference; paste known DEC refs to import consignments and goods')
    if child_errors:
        parts.append(f'{child_errors} child lookup/read issue(s)')

    return {
        'kind': 'ENS',
        'status': 'error' if header_read_failed and not discovered else (
            'created' if header_inserted or cons_created or goods_created else 'updated'
        ),
        'msg': '; '.join(parts) + '.',
        'staging_id': staging_ens_id,
    }


def _import_tss_ens_stub(cursor, ref):
    client_code = _current_client_code()
    cursor.execute(
        """
        SELECT TOP 1 stg_header_id
        FROM STG.BKD_ENS_Headers
        WHERE ClientCode = ? AND tss_ens_header_ref = ?
        """,
        [client_code, ref],
    )
    existing = cursor.fetchone()
    if existing:
        return {'kind': 'ENS', 'status': 'skipped', 'msg': 'ENS already exists locally.'}

    cols = _qualified_columns(cursor, 'STG', 'BKD_ENS_Headers')
    _insert_qualified_existing(
        cursor,
        'STG',
        'BKD_ENS_Headers',
        _qualified_values(
            {
                'ClientCode': client_code,
                'label': f'Imported {ref}',
                'tss_ens_header_ref': ref,
                'sub_status': 'IMPORTED',
                'source': 'TSS_IMPORT',
                'last_sub_status_change': _utc_now_naive(),
                'updated_at': _utc_now_naive(),
            },
            cols,
        ),
        'stg_header_id',
    )
    return {'kind': 'ENS', 'status': 'created', 'msg': 'ENS stub created. Run Sync TSS statuses to pull full data.'}


def _import_tss_consignment_stub(cursor, ref):
    """Import a DEC ref by reading from TSS and climbing the chain up to ENS.

    1. Reads the consignment from TSS to get full data + parent ENS ref.
    2. Reads the parent ENS header from TSS and upserts it.
    3. Upserts the consignment with full data and link to parent ENS.
    4. Pulls goods items for this consignment.
    5. Pulls SFD + SDI chain (descent).

    On TSS read failure, falls back to creating a minimal stub.
    """
    from app.tss_api import build_cfg_client

    api = build_cfg_client()

    cons_result, cons_data = _read_tss_consignment_for_import(api, ref)
    cons_read_failed = not cons_result.get('success') or not cons_data
    cons_failure_detail = ''

    if cons_read_failed:
        cons_failure_detail = _format_tss_import_failure(ref, cons_result, 'Consignment')

    parent_ens_ref = ''
    if cons_data:
        parent_ens_ref = (
            (cons_data.get('declaration_number') or '').strip()
            or (cons_data.get('ens_reference') or '').strip()
            or (cons_data.get('ens_header_reference') or '').strip()
        )

    # ── 1. Parent ENS header ──
    staging_ens_id = None
    ens_inserted = False
    ens_data = {}
    if parent_ens_ref:
        cursor.execute(
            """
            SELECT TOP 1 stg_header_id
            FROM STG.BKD_ENS_Headers
            WHERE ClientCode = ? AND tss_ens_header_ref = ?
            """,
            [_current_client_code(), parent_ens_ref],
        )
        existing_ens = cursor.fetchone()
        ens_result, ens_data = _read_tss_header_for_import(api, parent_ens_ref)
        if not ens_result.get('success'):
            ens_data = ens_data or {
                'status': 'PENDING_SYNC',
                'error_message': _format_tss_import_failure(parent_ens_ref, ens_result, 'ENS header'),
            }
        staging_ens_id, ens_inserted = _upsert_imported_ens_header(cursor, parent_ens_ref, ens_data)
        _log_tss_import_call(
            cursor,
            staging_ens_id,
            'IMPORT_TSS_ENS_READ' if ens_result.get('success') else 'IMPORT_TSS_ENS_READ_FAILED',
            ens_result,
        )
    else:
        # No parent ENS could be resolved — create an orphan placeholder
        staging_ens_id = _ensure_import_parent_header(cursor, ref)

    # ── 2. Consignment ──
    if not cons_data:
        cons_data = {
            'reference': ref,
            'declaration_number': ref,
            'status': 'PENDING_SYNC',
            'error_message': cons_failure_detail or '',
        }
    cons_data = dict(cons_data)
    cons_data.setdefault('reference', ref)
    cons_data.setdefault('declaration_number', ref)

    cursor.execute(
        """
        SELECT TOP 1 stg_consignment_id
        FROM STG.BKD_ENS_Consignments
        WHERE ClientCode = ? AND tss_consignment_ref = ?
        """,
        [_current_client_code(), ref],
    )
    existing_cons = cursor.fetchone()
    staging_cons_id, cons_inserted = _upsert_imported_consignment(
        cursor,
        staging_ens_id,
        ref,
        cons_data,
    )
    _log_tss_import_call(
        cursor,
        staging_cons_id,
        'IMPORT_TSS_CONSIGNMENT_READ' if not cons_read_failed else 'IMPORT_TSS_CONSIGNMENT_READ_FAILED',
        cons_result,
        friendly_error=cons_failure_detail or None,
    )

    parts = []
    if cons_inserted:
        parts.append('consignment created')
    elif existing_cons:
        parts.append('consignment updated')
    if parent_ens_ref:
        parts.append(f"linked to ENS {parent_ens_ref}{' (created)' if ens_inserted else ' (existing)'}")

    # ── 3. Goods items ──
    if not cons_read_failed and staging_cons_id:
        goods_summary = _sync_imported_goods_for_consignment(cursor, api, staging_cons_id, ref)
        if goods_summary['created'] or goods_summary['updated']:
            parts.append(f"{goods_summary['created']} goods created, {goods_summary['updated']} updated")
        elif goods_summary['errors']:
            parts.append(f"goods sync had {goods_summary['errors']} error(s)")

    # ── 4. SFD + SDI descent ──
    if not cons_read_failed and staging_cons_id and parent_ens_ref:
        chain_summary = _sync_imported_sfd_sdi_for_consignment(
            cursor, api, ref, staging_cons_id, parent_ens_ref,
        )
        if chain_summary['sfd_created'] or chain_summary['sfd_updated']:
            parts.append(f"{chain_summary['sfd_created']} SFDs created, {chain_summary['sfd_updated']} updated")
        if chain_summary['sdi_created'] or chain_summary['sdi_updated']:
            parts.append(f"{chain_summary['sdi_created']} SDIs created, {chain_summary['sdi_updated']} updated")

    msg = '; '.join(parts) if parts else 'Saved as consignment'
    if cons_read_failed:
        msg = f"{cons_failure_detail}; {msg}" if msg else cons_failure_detail
        status = 'error'
    elif existing_cons and not cons_inserted:
        status = 'updated'
    else:
        status = 'created'

    return {'kind': 'Consignment', 'status': status, 'msg': msg}


def _import_tss_supdec_stub(cursor, ref):
    _, inserted = _upsert_tss_sdi_import_stub(
        cursor,
        ref,
        sfd_ref='',
        movement_reference_number='',
        sdi_data={'sup_dec_number': ref, 'status': 'PENDING_SYNC'},
    )
    return {
        'kind': 'SDI',
        'status': 'created' if inserted else 'updated',
        'msg': 'SUP reference saved as an imported SDI mirror.',
    }


def _first_text(*values):
    for value in values:
        if value not in (None, ''):
            cleaned = str(value).strip()
            if cleaned:
                return cleaned
    return ''


def _parse_raw_json(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def _normalize_sfd_link_row(row):
    raw = _parse_raw_json(row.get('raw_json'))
    sfd_ref = _first_text(
        row.get('sfd_reference'),
        row.get('sfd_number'),
        row.get('reference'),
        raw.get('sfd_number'),
        raw.get('reference'),
    )
    cons_ref = _first_text(
        row.get('dec_reference'),
        row.get('declaration_number'),
        row.get('consignment_number'),
        row.get('ens_consignment_reference'),
        raw.get('consignment_number'),
        raw.get('ens_consignment_reference'),
    )
    customs_reference = _first_text(
        row.get('movement_reference_number'),
        row.get('mrn'),
        raw.get('movement_reference_number'),
        raw.get('mrn'),
        row.get('eori_for_eidr'),
        raw.get('eori_for_eidr'),
    )
    result = dict(row)
    result.update({
        'sfd_reference': sfd_ref,
        'dec_reference': cons_ref,
        'tss_status': _first_text(row.get('tss_status'), row.get('status'), raw.get('status')),
        'customs_reference': customs_reference,
        'customs_reference_label': 'EIDR' if _first_text(row.get('eori_for_eidr'), raw.get('eori_for_eidr')) and not _first_text(row.get('movement_reference_number'), row.get('mrn'), raw.get('movement_reference_number'), raw.get('mrn')) else 'MRN',
        'updated_at': row.get('updated_at') or row.get('created_at'),
    })
    return result


def _sfd_matches_consignment(sfd, cons):
    """Match the auto-generated SFD DEC back to the ENS consignment DEC."""
    if not sfd or not cons:
        return False

    if _same_record_id(sfd.get('staging_id'), cons.get('staging_id')):
        return True

    cons_refs = {
        _clean_link_ref(cons.get('dec_reference')),
        _clean_link_ref(cons.get('sfd_reference')),
        _clean_link_ref(cons.get('synced_sfd_reference')),
    } - {''}
    sfd_refs = {
        _clean_link_ref(sfd.get('sfd_reference')),
        _clean_link_ref(sfd.get('sfd_number')),
        _clean_link_ref(sfd.get('reference')),
        _clean_link_ref(sfd.get('dec_reference')),
        _clean_link_ref(sfd.get('declaration_number')),
        _clean_link_ref(sfd.get('consignment_number')),
        _clean_link_ref(sfd.get('ens_consignment_reference')),
        _clean_link_ref(sfd.get('staging_dec_reference')),
        _clean_link_ref(sfd.get('staging_sfd_reference')),
    } - {''}
    return bool(cons_refs & sfd_refs)


def _route_a_status_ready(value):
    status = normalize_status_key(value)
    if status.startswith('TSS:'):
        status = status.split(':', 1)[1].strip()
    return status in ('AUTHORISED FOR MOVEMENT', 'AUTHORIZED FOR MOVEMENT', 'ARRIVED')


def _capture_tss_create_identity(dec_id, api):
    """Store the TSS identity used to create an ENS header, if columns exist."""
    if not dec_id or not api:
        return
    columns = _bkd_table_columns('StagingDeclarations')
    values = []
    if 'tss_created_username' in columns and getattr(api, 'username', ''):
        values.append(('tss_created_username', getattr(api, 'username', '')))
    if 'tss_created_base_url' in columns and getattr(api, 'base_url', ''):
        values.append(('tss_created_base_url', getattr(api, 'base_url', '')))
    if 'tss_created_at' in columns:
        values.append(('tss_created_at', None))
    if not values:
        return

    assignments = []
    params = []
    for name, value in values:
        if name == 'tss_created_at':
            assignments.append('[tss_created_at] = COALESCE([tss_created_at], GETUTCDATE())')
        else:
            assignments.append(f'[{name}] = COALESCE([{name}], ?)')
            params.append(value)
    params.append(dec_id)
    try:
        execute(
            f"""
            UPDATE BKD.StagingDeclarations
            SET {', '.join(assignments)}
            WHERE id = ?
            """,
            params,
        )
    except Exception:
        pass


def _load_sfd_rows_for_consignments(consignments):
    if not consignments:
        return []

    sfd_columns = _bkd_table_columns('Sfds')
    if not sfd_columns:
        return []

    reference_col = _first_in_columns(sfd_columns, 'sfd_reference', 'sfd_number', 'reference')
    cons_ref_col = _first_in_columns(sfd_columns, 'declaration_number', 'consignment_number', 'ens_consignment_reference')
    if not reference_col and not cons_ref_col:
        return []

    status_col = _first_in_columns(sfd_columns, 'tss_status', 'status')
    mrn_col = _first_in_columns(sfd_columns, 'movement_reference_number', 'mrn')
    eidr_col = _first_in_columns(sfd_columns, 'eori_for_eidr')
    updated_col = _first_in_columns(sfd_columns, 'updated_at', 'created_at', 'synced_at')
    raw_json_col = _first_in_columns(sfd_columns, 'raw_json')

    cons_refs = sorted({
        _clean_link_ref(cons.get('dec_reference'))
        for cons in consignments
        if cons.get('dec_reference')
    } - {''})
    sfd_refs = sorted({
        _clean_link_ref(cons.get('sfd_reference'))
        for cons in consignments
        if cons.get('sfd_reference')
    } - {''})

    where_parts = []
    params = []
    if cons_ref_col and cons_refs:
        placeholders = ', '.join('?' for _ in cons_refs)
        where_parts.append(f"UPPER(s.[{cons_ref_col}]) IN ({placeholders})")
        params.extend(cons_refs)
    if reference_col and sfd_refs:
        placeholders = ', '.join('?' for _ in sfd_refs)
        where_parts.append(f"UPPER(s.[{reference_col}]) IN ({placeholders})")
        params.extend(sfd_refs)
    if not where_parts:
        return []

    join_parts = []
    if cons_ref_col:
        join_parts.append(f"s.[{cons_ref_col}] = c.dec_reference")
    if reference_col:
        join_parts.append(f"c.sfd_reference IS NOT NULL AND c.sfd_reference = s.[{reference_col}]")
    join_on = ' OR '.join(f"({part})" for part in join_parts) or '1 = 0'
    order_by = f"s.[{updated_col}] DESC" if updated_col else (
        f"s.[{reference_col}] DESC" if reference_col else f"s.[{cons_ref_col}] DESC"
    )

    try:
        rows = query_all(
            f"""
            SELECT TOP 100
                {f"s.[{reference_col}] AS sfd_reference," if reference_col else "NULL AS sfd_reference,"}
                {f"s.[{cons_ref_col}] AS dec_reference," if cons_ref_col else "NULL AS dec_reference,"}
                {f"s.[{mrn_col}] AS movement_reference_number," if mrn_col else "NULL AS movement_reference_number,"}
                {f"s.[{eidr_col}] AS eori_for_eidr," if eidr_col else "NULL AS eori_for_eidr,"}
                {f"s.[{status_col}] AS tss_status," if status_col else "NULL AS tss_status,"}
                {f"s.[{updated_col}] AS updated_at," if updated_col else "NULL AS updated_at,"}
                {f"s.[{raw_json_col}] AS raw_json," if raw_json_col else "NULL AS raw_json,"}
                c.staging_id,
                c.dec_reference AS staging_dec_reference,
                c.sfd_reference AS staging_sfd_reference,
                c.importer_eori,
                e.ens_reference
            FROM BKD.Sfds s
            LEFT JOIN BKD.StagingConsignments c ON {join_on}
            LEFT JOIN BKD.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
            WHERE {' OR '.join(where_parts)}
            ORDER BY {order_by}
            """,
            params,
        )
    except Exception:
        return []

    return [_normalize_sfd_link_row(row) for row in rows]


def _load_related_entities(dec=None, ens_ref='', pipeline_staging_id=None):
    ref = ens_ref or (dec or {}).get('external_ref') or ''
    dec_id = (dec or {}).get('id')
    pipeline_header = None
    consignments = []
    goods_by_cons = {}
    linked_gmr = None
    linked_sfds = []
    linked_sfds_by_cons = {}
    linked_supdecs = []
    linked_supdecs_by_cons = {}
    job_markers = _pipeline_job_markers()
    guidance_by_cons = {}

    try:
        if pipeline_staging_id:
            pipeline_header = query_one(
                "SELECT * FROM BKD.StagingEnsHeaders WHERE staging_id = ?",
                [pipeline_staging_id],
            )
        if not pipeline_header and ref:
            pipeline_header = query_one(
                "SELECT * FROM BKD.StagingEnsHeaders WHERE ens_reference = ?",
                [ref],
            )
        if not pipeline_header and dec_id:
            pipeline_header = query_one(
                "SELECT * FROM BKD.StagingEnsHeaders WHERE staging_declaration_id = ?",
                [dec_id],
            )

        if pipeline_header:
            consignments = query_all(
                """
                SELECT c.*, e.ens_reference
                FROM BKD.StagingConsignments c
                LEFT JOIN BKD.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
                WHERE c.staging_ens_id = ?
                ORDER BY c.staging_id
                """,
                [pipeline_header['staging_id']],
            )
            linked_gmr = query_one(
                """
                SELECT *
                FROM BKD.StagingGmrs
                WHERE staging_ens_id = ?
                ORDER BY created_at DESC
                """,
                [pipeline_header['staging_id']],
            )
    except Exception:
        pipeline_header = None
        consignments = []
        linked_gmr = None

    for cons in consignments:
        goods = query_all(
            """
            SELECT g.*, c.dec_reference, e.ens_reference
            FROM BKD.StagingGoodsItems g
            LEFT JOIN BKD.StagingConsignments c ON c.staging_id = g.staging_cons_id
            LEFT JOIN BKD.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
            WHERE g.staging_cons_id = ?
            ORDER BY g.item_number, g.staging_id
            """,
            [cons['staging_id']],
        )
        goods_by_cons[cons['staging_id']] = _apply_parent_synced_goods_statuses(goods, cons)

    linked_sfds = _load_sfd_rows_for_consignments(consignments)

    supdec_where = []
    supdec_params = []
    if ref:
        supdec_where.append("ens_header_ref = ?")
        supdec_params.append(ref)
    cons_refs = [c.get('dec_reference') for c in consignments if c.get('dec_reference')]
    cons_ids = [c.get('staging_id') for c in consignments if c.get('staging_id')]
    cons_sfd_refs = [c.get('sfd_reference') for c in consignments if c.get('sfd_reference')]
    linked_sfd_refs = [
        sfd.get('sfd_reference') or sfd.get('sfd_number')
        for sfd in linked_sfds
        if sfd.get('sfd_reference') or sfd.get('sfd_number')
    ]
    if cons_refs:
        placeholders = ', '.join('?' for _ in cons_refs)
        supdec_where.append(f"ens_consignment_ref IN ({placeholders})")
        supdec_params.extend(cons_refs)
    supdec_columns = _bkd_table_columns('StagingSupDecHeaders')
    supdec_sfd_col = _first_in_columns(supdec_columns, 'sfd_reference', 'sfd_number')
    sdi_sfd_refs = sorted({_clean_link_ref(ref) for ref in (cons_refs + cons_sfd_refs + linked_sfd_refs)} - {''})
    if supdec_sfd_col and sdi_sfd_refs:
        placeholders = ', '.join('?' for _ in sdi_sfd_refs)
        supdec_where.append(f"UPPER([{supdec_sfd_col}]) IN ({placeholders})")
        supdec_params.extend(sdi_sfd_refs)
    if cons_ids:
        placeholders = ', '.join('?' for _ in cons_ids)
        supdec_where.append(f"staging_cons_id IN ({placeholders})")
        supdec_params.extend(cons_ids)

    if supdec_where:
        try:
            linked_supdecs = query_all(
                f"""
                SELECT *
                FROM BKD.StagingSupDecHeaders
                WHERE {' OR '.join(supdec_where)}
                ORDER BY created_at DESC
                """,
                supdec_params,
            )
        except Exception:
            linked_supdecs = []

    prd_sdi_refs = []
    for cons in consignments:
        prd_sdi_refs.extend([
            cons.get('dec_reference'),
            cons.get('sfd_reference'),
            cons.get('synced_sfd_reference'),
            cons.get('trader_reference'),
            cons.get('transport_document_number'),
        ])
    prd_sdi_refs.extend(cons_refs)
    prd_sdi_refs.extend(cons_sfd_refs)
    prd_sdi_refs.extend(linked_sfd_refs)
    prd_sdi_links = load_prd_sdi_links_for_context(
        client_code=(get_tenant().get('code') or 'BKD').upper(),
        consignment_refs=prd_sdi_refs,
        sfd_refs=cons_sfd_refs + linked_sfd_refs,
    )
    if prd_sdi_links:
        linked_supdecs = merge_sdi_links(linked_supdecs, prd_sdi_links)

    for cons in consignments:
        local_status = normalize_status_key(cons.get('status') or cons.get('sub_status'))
        remote_status = normalize_status_key(cons.get('tss_status') or cons.get('cons_tss_status'))
        cons['is_cancelled'] = local_status in {'CANCELLED', 'CANCELED', 'DELETED'} or remote_status in {'CANCELLED', 'CANCELED', 'DELETED'}
        if cons['is_cancelled'] and cons.get('sfd_reference'):
            cons['sfd_status'] = 'CANCELLED'
    active_consignments = [cons for cons in consignments if not cons.get('is_cancelled')]

    journey = {
        'consignment_total': len(consignments),
        'goods_total': sum(len(goods_by_cons.get(c['staging_id'], [])) for c in consignments),
        'authorised_total': sum(1 for c in active_consignments if _route_a_status_ready(c.get('tss_status') or c.get('status'))),
        'ens_ready_for_dec_creation': _ens_header_ready_for_dec_creation(dec, pipeline_header),
        'sfd_total': 0,
        'sfd_visible_total': 0,
        'sfd_pending_sync_total': 0,
        'route_a_ready_by_cons': {},
        'has_gmr': bool(linked_gmr),
        'sdi_total': len(linked_supdecs),
        'requires_sdi_total': 0,
        'guidance': [],
    }
    missing_goods = 0
    pending_local = 0
    validated_local = 0
    dec_create_scope_ids = []
    failed_local = 0
    pending_tss_sync = 0
    single_consignment = len(consignments) == 1
    for cons in consignments:
        journey['route_a_ready_by_cons'][cons['staging_id']] = (
            False if cons.get('is_cancelled') else _route_a_status_ready(
                cons.get('tss_status') or cons.get('status')
            )
        )
        linked_sfd_for_cons = [
            sfd for sfd in linked_sfds
            if _sfd_matches_consignment(sfd, cons)
        ]
        linked_sfds_by_cons[cons['staging_id']] = linked_sfd_for_cons
        if linked_sfd_for_cons:
            cons['synced_sfd_reference'] = (
                linked_sfd_for_cons[0].get('sfd_reference')
                or linked_sfd_for_cons[0].get('sfd_number')
                or ''
            )
        cons['requires_sdi'] = _consignment_requires_sdi(cons)
        linked_for_cons = [
            sd for sd in linked_supdecs
            if _supdec_matches_consignment(sd, cons, single_consignment=single_consignment)
        ]
        if linked_for_cons:
            cons['requires_sdi'] = True
        linked_supdecs_by_cons[cons['staging_id']] = linked_for_cons
        guidance = _consignment_guidance(
            cons,
            goods_by_cons.get(cons['staging_id'], []),
            linked_gmr=linked_gmr,
            linked_supdecs=linked_for_cons,
            job_markers=job_markers,
        )
        guidance_by_cons[cons['staging_id']] = guidance
        if len(goods_by_cons.get(cons['staging_id'], [])) == 0:
            missing_goods += 1
        cons_status = normalize_status_key(cons.get('status'))
        if cons.get('staging_id') and not cons.get('dec_reference'):
            dec_create_scope_ids.append(str(cons['staging_id']))
        if cons_status in {'PENDING', 'PENDING REVIEW', 'DRAFT', 'INGESTED'} and not cons.get('dec_reference'):
            pending_local += 1
        if cons_status == 'VALIDATED' and not cons.get('dec_reference'):
            validated_local += 1
        if (cons.get('status') or '').upper() == 'FAILED':
            failed_local += 1
        if cons.get('dec_reference') and (not cons.get('tss_status') or _is_pending_sync_status(cons.get('tss_status'))):
            pending_tss_sync += 1

    journey['requires_sdi_total'] = sum(1 for c in active_consignments if c.get('requires_sdi'))
    journey['sfd_total'] = sum(
        1 for cons in consignments
        if cons.get('sfd_reference') or linked_sfds_by_cons.get(cons['staging_id'])
    )
    journey['sfd_visible_total'] = sum(
        1 for cons in consignments
        if (
            cons.get('sfd_reference')
            or linked_sfds_by_cons.get(cons['staging_id'])
            or (journey['route_a_ready_by_cons'].get(cons['staging_id']) and not cons.get('is_cancelled'))
        )
    )
    journey['sfd_pending_sync_total'] = sum(
        1 for cons in active_consignments
        if (
            journey['route_a_ready_by_cons'].get(cons['staging_id'])
            and not cons.get('sfd_reference')
            and not linked_sfds_by_cons.get(cons['staging_id'])
        )
    )

    next_actions = []
    header_needs_sync = _ens_needs_tss_sync(dec, pipeline_header)
    header_sync_phase = _ens_sync_phase(dec, pipeline_header)
    header_sync_label = _ens_sync_label(dec, pipeline_header)
    sdi_cons_candidates = [c for c in active_consignments if c.get('requires_sdi')]
    arrived_sdi_candidates = [c for c in sdi_cons_candidates if (c.get('tss_status') or '').upper() == 'ARRIVED']
    if len(arrived_sdi_candidates) == 1:
        sdi_start_href = url_for('supdec.create', cons_id=arrived_sdi_candidates[0]['staging_id'])
    else:
        sdi_start_href = url_for('supdec.create') + (f"?ens_id={pipeline_header['staging_id']}" if pipeline_header else '')
    header_cargo_block_status = _ens_header_blocks_cargo_creation(pipeline_header)
    if header_cargo_block_status:
        journey['guidance'].append({
            'tone': 'danger',
            'summary': 'ENS header must be fixed before DECs can be created.',
            'detail': (
                f'This ENS header is currently in {header_cargo_block_status}. '
                'Fusion will not create DEC consignments in TSS until the ENS validation error is corrected and the header validates successfully.'
            ),
        })
    elif header_needs_sync:
        next_actions.append(_nav_action(header_sync_label, kind='primary', phase=header_sync_phase))
        journey['guidance'].append({
            'tone': 'info',
            'summary': 'This ENS is waiting for a live TSS sync.',
            'detail': (
                'Fusion has the ENS reference, but the latest TSS status and downstream chain have not been refreshed yet. '
                f'Run {header_sync_label} to pull the current header, consignment and goods state where available.'
            ),
        })
    elif ref and not pipeline_header:
        journey['guidance'].append({
            'tone': 'warning',
            'summary': 'This ENS exists in the declarations portal, but its cargo workflow chain has not been started yet.',
            'detail': 'No StagingEnsHeaders cargo record is linked yet, so there are no downstream consignments, GMRs or SDIs to show.',
        })
        if dec_id:
            next_actions.append(_nav_action(
                'Create First Consignment',
                href=url_for('consignments.create') + f"?from_dec={dec_id}",
                kind='primary',
            ))
    elif pipeline_header and not consignments:
        next_actions.append(_nav_action(
            'Add Consignment',
            href=url_for('consignments.create') + f"?ens_id={pipeline_header['staging_id']}",
            kind='primary',
        ))
        journey['guidance'].append({
            'tone': 'warning',
            'summary': 'No consignments exist yet under this ENS header.',
            'detail': 'Add the first consignment to start the ENS -> SFD -> GMR -> SDI journey.',
        })
    elif failed_local:
        next_actions.append(_nav_action(
            'Review Failed Consignments',
            href=url_for('consignments.list_view', ens_ref=ref) if ref else url_for('consignments.list_view'),
            kind='warning',
        ))
        journey['guidance'].append({
            'tone': 'danger',
            'summary': 'One or more consignments are blocked by local errors.',
            'detail': 'Open the failed consignments, correct the error messages, then retry validation or submission.',
        })
    elif missing_goods:
        next_actions.append(_nav_action(
            'Add Missing Goods',
            href=url_for('consignments.list_view', ens_ref=ref) if ref else url_for('consignments.list_view'),
            kind='warning',
        ))
        journey['guidance'].append({
            'tone': 'warning',
            'summary': 'Some consignments still have no goods items.',
            'detail': 'Goods must exist before those consignments can pass validation and move to TSS.',
        })
    elif dec_create_scope_ids:
        if not journey['ens_ready_for_dec_creation']:
            journey['guidance'].append({
                'tone': 'warning',
                'summary': 'Create the ENS in TSS before creating DECs.',
                'detail': (
                    'The cargo is local-only, but the parent ENS header does not have a live TSS ENS reference yet. '
                    'Create the ENS in TSS first; once TSS returns the ENS reference, Fusion will show Create DECs in TSS.'
                ),
            })
        else:
            next_actions.append(_nav_action(
                'Create DECs in TSS',
                kind='primary',
                phase='all',
                hidden={'scope_consignment_ids': ','.join(dec_create_scope_ids)},
                confirm_text='Validate cargo, then create every eligible DEC and goods item under this ENS in TSS?',
            ))
            journey['guidance'].append({
                'tone': 'info',
                'summary': 'ENS is live in TSS and cargo can be created under it.',
                'detail': 'Run Create DECs in TSS to validate this ENS cargo, then create every eligible local-only DEC and goods item in TSS.',
            })
    elif pending_tss_sync or (consignments and journey['authorised_total'] < journey['consignment_total'] and not linked_gmr):
        next_actions.append(_nav_action('Sync TSS Statuses', kind='primary', phase='sync_pipeline'))
        journey['guidance'].append({
            'tone': 'info',
            'summary': 'The cargo chain is waiting for current TSS statuses.',
            'detail': 'At least one consignment has been sent to TSS, but the latest live status has not been synced yet.',
        })
    elif journey['authorised_total'] and not linked_gmr:
        next_actions.append(_nav_action(
            'Create GMR',
            href=url_for('gmr.create') + (f"?ens_id={pipeline_header['staging_id']}" if pipeline_header else ''),
            kind='primary',
        ))
    elif linked_gmr and not linked_supdecs:
        if journey['requires_sdi_total'] > 0 and arrived_sdi_candidates:
            next_actions.append(_nav_action(
                'Start SDI',
                href=sdi_start_href,
                kind='warning',
            ))
            journey['guidance'].append({
                'tone': 'info',
                'summary': 'ARRIVED consignments are ready for SDI.',
                'detail': 'At least one linked consignment has reached ARRIVED, so you can now create or sync the downstream supplementary declaration.',
            })
        elif journey['requires_sdi_total'] > 0:
            journey['guidance'].append({
                'tone': 'info',
                'summary': 'GMR exists. SDI starts after arrival.',
                'detail': 'The GMR is already linked, but no eligible consignment has reached ARRIVED yet. Wait for ARRIVED before creating the downstream supplementary declaration.',
            })
        else:
            journey['guidance'].append({
                'tone': 'success',
                'summary': 'GMR exists and no supplementary declarations are required.',
                'detail': 'All linked consignments are configured not to generate SFD/SDI, so the journey does not need an SDI step.',
            })

    return (
        pipeline_header,
        consignments,
        goods_by_cons,
        linked_gmr,
        linked_sfds,
        linked_sfds_by_cons,
        linked_supdecs,
        linked_supdecs_by_cons,
        journey,
        next_actions,
        guidance_by_cons,
    )


def _prd_ens_list_context():
    status_filter = _canonical_ens_filter_status(request.args.get('status', '')) or 'ALL'
    search = (request.args.get('q') or '').strip()
    sort = (request.args.get('sort') or 'arrival_desc').strip()
    page = _safe_positive_int(request.args.get('page'), 1)
    page_size = 100
    client_code = (get_tenant().get('code') or 'BKD').upper()
    headers = query_all("""
        SELECT
            h.stg_header_id,
            h.conveyance_ref,
            h.arrival_date_time,
            h.arrival_port,
            h.movement_type,
            h.identity_no_of_transport,
            h.carrier_name,
            h.carrier_eori,
            h.sub_status,
            h.tss_ens_header_ref,
            h.label,
            h.source,
            h.movement_notified_at,
            h.staging_failures_notified_at,
            h.stg_created_at,
            h.updated_at,
            t.TssStatus,
            (SELECT COUNT(*)
               FROM STG.BKD_ENS_Consignments c
              WHERE c.ClientCode = h.ClientCode
                AND c.stg_header_id = h.stg_header_id
                AND UPPER(COALESCE(c.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')) AS consignment_count,
            (SELECT COUNT(*)
               FROM STG.BKD_GoodsItems g
               JOIN STG.BKD_ENS_Consignments c
                 ON c.ClientCode = g.ClientCode
                AND c.stg_consignment_id = g.stg_consignment_id
              WHERE c.ClientCode = h.ClientCode
                AND c.stg_header_id = h.stg_header_id
                AND UPPER(COALESCE(g.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')) AS goods_count
            ,
            (SELECT STRING_AGG(CAST(CONCAT(
                        ' ', c2.stg_consignment_id,
                        ' ', COALESCE(c2.tss_consignment_ref, ''),
                        ' ', COALESCE(c2.trader_reference, ''),
                        ' ', COALESCE(c2.transport_document_number, ''),
                        ' ', COALESCE(c2.goods_description, ''),
                        ' ', COALESCE(c2.importer_eori, ''),
                        ' ', COALESCE(c2.consignor_eori, ''),
                        ' ', COALESCE(c2.consignee_eori, ''),
                        ' ', COALESCE(c2.ducr, ''),
                        ' ', COALESCE(c2.sub_status, '')
                    ) AS NVARCHAR(MAX)), N' ')
               FROM STG.BKD_ENS_Consignments c2
              WHERE c2.ClientCode = h.ClientCode
                AND c2.stg_header_id = h.stg_header_id) AS consignment_search_text,
            (SELECT STRING_AGG(CAST(CONCAT(
                        ' ', g2.stg_item_id,
                        ' ', COALESCE(g2.sku, ''),
                        ' ', COALESCE(g2.tss_hex_id, ''),
                        ' ', COALESCE(g2.goods_description, ''),
                        ' ', COALESCE(g2.commodity_code, ''),
                        ' ', COALESCE(CONVERT(NVARCHAR(30), g2.item_seq), ''),
                        ' ', COALESCE(g2.sub_status, ''),
                        ' ', COALESCE(g2.error_message, '')
                    ) AS NVARCHAR(MAX)), N' ')
               FROM STG.BKD_GoodsItems g2
               JOIN STG.BKD_ENS_Consignments c3
                 ON c3.ClientCode = g2.ClientCode
                AND c3.stg_consignment_id = g2.stg_consignment_id
              WHERE c3.ClientCode = h.ClientCode
                AND c3.stg_header_id = h.stg_header_id) AS goods_search_text,
            (SELECT STRING_AGG(CAST(CONCAT(
                        ' ', COALESCE(t2.tss_consignment_ref, ''),
                        ' ', COALESCE(t2.tss_sfd_number, ''),
                        ' ', COALESCE(t2.tss_movement_reference_number, ''),
                        ' ', COALESCE(t2.tss_eori_for_eidr, ''),
                        ' ', COALESCE(t2.tss_sfd_status, '')
                    ) AS NVARCHAR(MAX)), N' ')
               FROM STG.BKD_SFD_Tracking t2
               JOIN STG.BKD_ENS_Consignments c4
                 ON c4.ClientCode = t2.ClientCode
                AND c4.tss_consignment_ref = t2.tss_consignment_ref
              WHERE c4.ClientCode = h.ClientCode
                AND c4.stg_header_id = h.stg_header_id) AS sfd_search_text,
            (SELECT STRING_AGG(CAST(CONCAT(
                        ' ', COALESCE(sd.tss_sup_dec_number, ''),
                        ' ', COALESCE(sd.tss_sfd_consignment_ref, ''),
                        ' ', COALESCE(sd.tss_consignment_ref, ''),
                        ' ', COALESCE(sd.trader_reference, ''),
                        ' ', COALESCE(sd.transport_document_number, ''),
                        ' ', COALESCE(sd.sub_status, ''),
                        ' ', COALESCE(sd.tss_status, ''),
                        ' ', COALESCE(sd.validation_errors_json, ''),
                        ' ', COALESCE(sd.auto_submit_error, '')
                    ) AS NVARCHAR(MAX)), N' ')
               FROM STG.BKD_SDI_Headers sd
               LEFT JOIN STG.BKD_ENS_Consignments c5
                 ON c5.ClientCode = sd.ClientCode
                AND (
                     c5.stg_consignment_id = sd.stg_consignment_id
                  OR c5.tss_consignment_ref = sd.tss_consignment_ref
                  OR c5.tss_consignment_ref = sd.tss_sfd_consignment_ref
                )
              WHERE sd.ClientCode = h.ClientCode
                AND (sd.stg_consignment_id IN (
                        SELECT c6.stg_consignment_id
                        FROM STG.BKD_ENS_Consignments c6
                        WHERE c6.ClientCode = h.ClientCode
                          AND c6.stg_header_id = h.stg_header_id
                    )
                 OR c5.stg_header_id = h.stg_header_id)) AS sdi_search_text
        FROM STG.BKD_ENS_Headers h
        LEFT JOIN TSS.BKD_ENS_Headers t
            ON t.ClientCode = h.ClientCode
           AND t.DeclarationNumber = h.tss_ens_header_ref
        WHERE h.ClientCode = ?
        ORDER BY h.stg_header_id DESC
    """, [client_code])

    filtered = []
    counts = {}
    for raw in headers or []:
        row = dict(raw)
        filter_status = _canonical_ens_filter_status(_ens_filter_status(
            row.get('sub_status'),
            row.get('TssStatus'),
            imported_only=bool(row.get('tss_ens_header_ref')) and _is_tss_import_source(row.get('source')),
            has_live_ref=bool(str(row.get('tss_ens_header_ref') or '').strip()),
            source=row.get('source'),
        )) or 'DRAFT'
        row['filter_status'] = filter_status
        counts[filter_status] = counts.get(filter_status, 0) + 1
        if status_filter != 'ALL' and filter_status != status_filter:
            continue
        if search:
            if not search_matches_values(search, [
                row.get('stg_header_id'),
                row.get('conveyance_ref'),
                row.get('arrival_port'),
                row.get('movement_type'),
                row.get('identity_no_of_transport'),
                row.get('carrier_name'),
                row.get('carrier_eori'),
                row.get('tss_ens_header_ref'),
                row.get('TssStatus'),
                row.get('sub_status'),
                row.get('source'),
                row.get('label'),
                row.get('consignment_search_text'),
                row.get('goods_search_text'),
                row.get('sfd_search_text'),
                row.get('sdi_search_text'),
            ]):
                continue
        filtered.append(row)

    _assign_chronological_ens_ids(filtered)

    if sort == 'arrival_asc':
        filtered.sort(key=_arrival_sort_key)
    elif sort == 'arrival_desc':
        filtered.sort(key=_arrival_sort_key, reverse=True)

    filtered_total = len(filtered)
    total_pages = max(1, (filtered_total + page_size - 1) // page_size) if filtered_total else 1
    page = min(page, total_pages)
    page_start = (page - 1) * page_size
    paged_headers = filtered[page_start:page_start + page_size]

    status_tabs = _ens_status_tabs(counts, status_filter)
    return {
        'headers': paged_headers,
        'status_filter': status_filter,
        'search': search,
        'sort': sort,
        'status_tabs': status_tabs,
        'status_counts': counts,
        'total': len(headers or []),
        'filtered_total': filtered_total,
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
    }


def _prd_ens_sync_candidate_ids(*, client_code: str, status_filter: str, search: str, limit: int = 25):
    rows = query_all("""
        SELECT
            h.stg_header_id,
            h.conveyance_ref,
            h.arrival_port,
            h.movement_type,
            h.identity_no_of_transport,
            h.carrier_name,
            h.carrier_eori,
            h.sub_status,
            h.tss_ens_header_ref,
            h.label,
            h.source,
            t.TssStatus
        FROM STG.BKD_ENS_Headers h
        LEFT JOIN TSS.BKD_ENS_Headers t
            ON t.ClientCode = h.ClientCode
           AND t.DeclarationNumber = h.tss_ens_header_ref
        WHERE h.ClientCode = ?
          AND NULLIF(LTRIM(RTRIM(COALESCE(h.tss_ens_header_ref, ''))), '') IS NOT NULL
        ORDER BY h.stg_header_id DESC
    """, [client_code])
    selected = []
    status_filter = _canonical_ens_filter_status(status_filter or '') or 'ALL'
    search = (search or '').strip()
    for raw in rows or []:
        row = dict(raw)
        filter_status = _canonical_ens_filter_status(row.get('TssStatus') or row.get('sub_status')) or 'PENDING'
        if status_filter != 'ALL' and filter_status != status_filter:
            continue
        if search:
            if not search_matches_values(search, [
                row.get('stg_header_id'),
                row.get('conveyance_ref'),
                row.get('arrival_port'),
                row.get('movement_type'),
                row.get('identity_no_of_transport'),
                row.get('carrier_name'),
                row.get('carrier_eori'),
                row.get('tss_ens_header_ref'),
                row.get('TssStatus'),
                row.get('sub_status'),
                row.get('source'),
                row.get('label'),
            ]):
                continue
        selected.append(int(row['stg_header_id']))
        if len(selected) >= limit:
            break
    return selected


@declarations_bp.route('/')
def list_declarations():
    if request.headers.get('HX-Request') == 'true':
        return render_template('declarations/_list_partial.html', **_prd_ens_list_context())
    return render_template(
        'declarations/list.html',
        **_prd_ens_list_context(),
    )


@declarations_bp.route('/partial')
def list_partial():
    return render_template('declarations/_list_partial.html', **_prd_ens_list_context())


def _pipeline_api_response_summary(api_response_json):
    summary = {
        'api_result_status': '',
        'api_process_message': '',
        'api_reference': '',
    }
    if not api_response_json:
        return summary

    try:
        payload = json.loads(api_response_json)
    except (TypeError, ValueError):
        return summary

    if not isinstance(payload, dict):
        return summary

    result = payload.get('result') if isinstance(payload.get('result'), dict) else {}
    error = payload.get('error') if isinstance(payload.get('error'), dict) else {}
    summary['api_result_status'] = str(result.get('status') or payload.get('status') or '').strip()
    summary['api_process_message'] = str(
        result.get('process_message')
        or payload.get('process_message')
        or error.get('message')
        or payload.get('message')
        or ''
    ).strip()
    summary['api_reference'] = str(result.get('reference') or payload.get('reference') or '').strip()
    return summary


def _upsert_pipeline_header_stub(dec_id, ens_ref, payload, tss_status):
    """Keep the cargo ENS stub linked to the submitted declaration."""
    if not ens_ref:
        return None

    tss_status = tss_status or 'Draft'
    carrier = (payload or {}).get('carrier_name') or (payload or {}).get('carrier_eori') or ''
    stub_label = f"{ens_ref} - {carrier}" if carrier else ens_ref
    existing = query_one(
        "SELECT staging_id FROM BKD.StagingEnsHeaders WHERE staging_declaration_id = ?",
        [dec_id],
    ) or query_one(
        "SELECT staging_id FROM BKD.StagingEnsHeaders WHERE ens_reference = ?",
        [ens_ref],
    )

    if existing:
        execute(
            "UPDATE BKD.StagingEnsHeaders SET ens_reference=?, staging_declaration_id=?, tss_status=?, "
            "status='SUBMITTED', label=?, updated_at=SYSUTCDATETIME() WHERE staging_id=?",
            [ens_ref, dec_id, tss_status, stub_label, existing['staging_id']],
        )
        return existing['staging_id']

    execute(
        "INSERT INTO BKD.StagingEnsHeaders"
        " (label, ens_reference, status, tss_status, source, updated_at, staging_declaration_id)"
        " VALUES (?,?,'SUBMITTED',?,'declarations_portal',SYSUTCDATETIME(),?)",
        [stub_label, ens_ref, tss_status, dec_id],
    )
    created = query_one(
        "SELECT staging_id FROM BKD.StagingEnsHeaders WHERE staging_declaration_id = ?",
        [dec_id],
    ) or query_one(
        "SELECT staging_id FROM BKD.StagingEnsHeaders WHERE ens_reference = ?",
        [ens_ref],
    )
    return created['staging_id'] if created else None


def _insert_sales_orders_header_stub(cursor, dec_id: int, payload: dict) -> int | None:
    """Expose a DETAILS-created ENS draft as a cargo parent before TSS submit."""
    columns = _bkd_table_columns('StagingEnsHeaders')
    if not columns:
        return None

    conveyance_ref = (payload.get('conveyance_ref') or '').strip()
    label = f"Sales-order batch {conveyance_ref}" if conveyance_ref else f"Sales-order ENS draft #{dec_id}"
    data = {
        'label': label,
        'status': 'PENDING_REVIEW',
        'tss_status': 'PENDING_REVIEW',
        'source': 'EXCEL_SALES_ORDERS_DETAILS',
        'staging_declaration_id': dec_id,
        'env_code': 'PRD',
        'movement_type': payload.get('movement_type'),
        'type_of_passive_transport': payload.get('type_of_passive_transport'),
        'identity_no_of_transport': payload.get('identity_no_of_transport'),
        'nationality_of_transport': payload.get('nationality_of_transport'),
        'conveyance_ref': conveyance_ref,
        'arrival_date_time': payload.get('arrival_date_time'),
        'arrival_port': payload.get('arrival_port'),
        'place_of_loading': payload.get('place_of_loading'),
        'place_of_acceptance_same_as_loading': payload.get('place_of_acceptance_same_as_loading'),
        'place_of_acceptance': payload.get('place_of_acceptance'),
        'place_of_unloading': payload.get('place_of_unloading'),
        'place_of_delivery_same_as_unloading': payload.get('place_of_delivery_same_as_unloading'),
        'place_of_delivery': payload.get('place_of_delivery'),
        'transport_charges': payload.get('transport_charges'),
        'carrier_eori': payload.get('carrier_eori'),
        'carrier_name': payload.get('carrier_name'),
        'carrier_street_number': payload.get('carrier_street_number'),
        'carrier_city': payload.get('carrier_city'),
        'carrier_postcode': payload.get('carrier_postcode'),
        'carrier_country': payload.get('carrier_country'),
        'haulier_eori': payload.get('haulier_eori'),
    }
    values = _existing_column_values(
        [
            (key, value)
            for key, value in _expand_ens_header_values_for_table('StagingEnsHeaders', data.items())
            if value not in (None, '')
        ],
        columns,
    )
    if not values:
        return None

    insert_cols = [key for key, _ in values]
    placeholders = ['?'] * len(values)
    params = [value for _, value in values]
    if 'created_at' in columns:
        insert_cols.append('created_at')
        placeholders.append('SYSUTCDATETIME()')
    if 'updated_at' in columns:
        insert_cols.append('updated_at')
        placeholders.append('SYSUTCDATETIME()')

    cursor.execute(
        f"INSERT INTO BKD.StagingEnsHeaders ({', '.join(f'[{col}]' for col in insert_cols)}) "
        f"OUTPUT INSERTED.staging_id VALUES ({', '.join(placeholders)})",
        params,
    )
    row = cursor.fetchone()
    return int(row[0]) if row else None


def _consignment_create_payload(row):
    def _safe_tss_party_text(value):
        text = tss_safe_text_suggestion(value)
        return re.sub(r'\s+', ' ', text.replace('&', ' and ')).strip()

    payload = {
        'goods_description': row.get('goods_description', ''),
        'transport_document_number': row.get('transport_document_number', ''),
        'controlled_goods': _normalise_yes_no(row.get('controlled_goods'), default='no'),
        'consignor_eori': row.get('consignor_eori', ''),
        'consignee_eori': row.get('consignee_eori', ''),
        'importer_eori': row.get('importer_eori', ''),
        'exporter_eori': row.get('exporter_eori', ''),
        'consignor_name': _safe_tss_party_text(row.get('consignor_name', '')),
        'consignee_name': _safe_tss_party_text(row.get('consignee_name', '')),
        'importer_name': _safe_tss_party_text(row.get('importer_name', '')),
        'exporter_name': _safe_tss_party_text(row.get('exporter_name', '')),
        'buyer_same_as_importer': _normalise_yes_no(row.get('buyer_same_as_importer'), default='yes'),
        'seller_same_as_exporter': _normalise_yes_no(row.get('seller_same_as_exporter'), default='yes'),
    }
    for field in (
        'trader_reference',
        'goods_domestic_status',
        'destination_country',
        'supervising_customs_office',
        'customs_warehouse_identifier',
        'ducr',
        'no_sfd_reason',
        'align_ukims',
        'use_importer_sde',
        'declaration_choice',
        'generate_SD',
        'container_indicator',
        'consignor_street_number',
        'consignor_city',
        'consignor_postcode',
        'consignor_country',
        'consignee_street_number',
        'consignee_city',
        'consignee_postcode',
        'consignee_country',
        'importer_street_number',
        'importer_city',
        'importer_postcode',
        'importer_country',
        'exporter_street_number',
        'exporter_city',
        'exporter_postcode',
        'exporter_country',
        'buyer_eori',
        'buyer_name',
        'buyer_street_and_number',
        'buyer_city',
        'buyer_postcode',
        'buyer_country',
        'seller_eori',
        'seller_name',
        'seller_street_and_number',
        'seller_city',
        'seller_postcode',
        'seller_country',
    ):
        if row.get(field):
            if field in {'generate_SD', 'use_importer_sde'}:
                payload[field] = _normalise_yes_no(row.get(field), default='')
            elif field.endswith('_name'):
                payload[field] = _safe_tss_party_text(row[field])
            else:
                payload[field] = row[field]
    return {key: value for key, value in payload.items() if value not in (None, '')}


def _format_tss_decimal(value, max_dp=2):
    if value in (None, ''):
        return ''
    text = str(value).strip()
    if not text:
        return ''
    try:
        dec = Decimal(text)
        if max_dp is not None:
            quant = Decimal('1').scaleb(-max_dp)
            dec = dec.quantize(quant, rounding=ROUND_HALF_UP)
        normalized = format(dec.normalize(), 'f')
    except (InvalidOperation, ValueError):
        return text
    if '.' in normalized:
        normalized = normalized.rstrip('0').rstrip('.')
    return normalized or '0'


def _goods_create_payload(row):
    payload = {
        'goods_description': tss_safe_text_suggestion(row.get('goods_description', '')),
        'type_of_packages': normalise_package_type(row.get('type_of_packages')),
        'number_of_packages': str(row.get('number_of_packages') or 1),
        'package_marks': row.get('package_marks') or 'ADDR',
        'gross_mass_kg': _format_tss_decimal(row.get('gross_mass_kg') or 0, max_dp=2),
    }
    decimal_fields = {'net_mass_kg', 'item_invoice_amount'}
    for field in (
        'net_mass_kg',
        'controlled_goods',
        'controlled_goods_type',
        'commodity_code',
        'procedure_code',
        'additional_procedure_code',
        'country_of_origin',
        'taric_code',
        'item_invoice_amount',
        'item_invoice_currency',
    ):
        value = row.get(field)
        if value is None or not str(value).strip():
            continue
        if field == 'taric_code':
            payload[field] = normalise_taric_code(value)
        elif field in decimal_fields:
            payload[field] = _format_tss_decimal(value, max_dp=2)
        elif field == 'controlled_goods':
            payload[field] = _normalise_yes_no(value, default='no')
        else:
            payload[field] = str(value)
    return {key: value for key, value in payload.items() if value not in (None, '')}


def _tss_goods_create_already_exists(result):
    parts = []
    for key in ('message', 'process_message', 'error_message', 'raw_response'):
        value = (result or {}).get(key)
        if value not in (None, ''):
            parts.append(str(value))
    response = (result or {}).get('response')
    if isinstance(response, dict):
        for key in ('message', 'process_message', 'error_message'):
            value = response.get(key)
            if value not in (None, ''):
                parts.append(str(value))
    text = ' '.join(parts).lower()
    return 'invalid op_type' in text and 'goods' in text and 'create' in text


def _normalised_goods_match_value(value):
    return re.sub(r'\s+', ' ', str(value or '').strip()).upper()


def _remote_goods_matches_local(remote_item, local_row):
    if not isinstance(remote_item, dict):
        return False

    matched = False
    for remote_key, local_key in (
        ('goods_description', 'goods_description'),
        ('package_marks', 'package_marks'),
        ('item_number', 'item_number'),
    ):
        remote_value = remote_item.get(remote_key)
        local_value = (local_row or {}).get(local_key)
        if remote_value in (None, '') or local_value in (None, ''):
            continue
        if _normalised_goods_match_value(remote_value) != _normalised_goods_match_value(local_value):
            return False
        matched = True
    return matched


def _lookup_existing_goods_id_for_created_row(api, dec_ref, row):
    for lookup_call in (
        lambda: api.lookup_ens_goods(dec_ref),
        lambda: api.lookup_goods(dec_ref, parent_type='consignment_number'),
    ):
        try:
            lookup = lookup_call()
        except Exception:
            continue
        if not lookup.get('success'):
            continue
        items = [item for item in _tss_response_items(lookup) if isinstance(item, dict)]
        matched_items = [item for item in items if _remote_goods_matches_local(item, row)]
        candidates = matched_items or (items if len(items) == 1 else [])
        for item in candidates:
            goods_ref = _tss_reference_from_item(item, 'goods_id', 'reference')
            if goods_ref:
                return goods_ref
    return ''


def _log_consignment_create_call(staging_id, result, payload):
    try:
        _log_tss_api_exchange(
            staging_id=staging_id,
            call_type='CREATE_CONSIGNMENT',
            http_method=result.get('method', 'POST'),
            url=result.get('url', ''),
            request_payload=payload,
            http_status=result.get('http_status', 0),
            response_status=result.get('status', ''),
            response_message=result.get('message', '')[:500],
            response_json=result.get('raw_response', '')[:4000],
            duration_ms=result.get('duration_ms', 0),
            error_detail='' if result.get('success') else result.get('message', '')[:2000],
        )
    except Exception:
        pass


def _log_goods_create_call(staging_id, result, payload):
    try:
        _log_tss_api_exchange(
            staging_id=staging_id,
            call_type='CREATE_GOODS',
            http_method=result.get('method', 'POST'),
            url=result.get('url', ''),
            request_payload=payload,
            http_status=result.get('http_status', 0),
            response_status=result.get('status', ''),
            response_message=result.get('message', '')[:500],
            response_json=result.get('raw_response', '')[:4000],
            duration_ms=result.get('duration_ms', 0),
            error_detail='' if result.get('success') else result.get('message', '')[:2000],
        )
    except Exception:
        pass


def _create_validated_goods_for_header(api, staging_ens_id):
    """Create goods in TSS for validated local items under created DEC refs."""
    summary = {'attempted': 0, 'created': 0, 'failed': 0, 'messages': []}
    if not staging_ens_id:
        return summary

    rows = query_all(
        """
        SELECT g.*, c.dec_reference
        FROM BKD.StagingGoodsItems g
        JOIN BKD.StagingConsignments c ON c.staging_id = g.staging_cons_id
        WHERE c.staging_ens_id = ?
          AND g.status = 'VALIDATED'
          AND c.dec_reference IS NOT NULL
          AND (g.goods_id IS NULL OR g.goods_id = '')
        ORDER BY g.staging_id
        """,
        [staging_ens_id],
    )
    summary['attempted'] = len(rows)

    for row in rows:
        staging_id = row['staging_id']
        dec_ref = row.get('dec_reference') or ''
        payload = _goods_create_payload(row)
        result = api.create_goods(dec_ref, payload)
        _log_goods_create_call(staging_id, result, payload)

        goods_id = result.get('reference') or ''
        already_exists = (not result.get('success')) and _tss_goods_create_already_exists(result)
        if already_exists and not goods_id:
            goods_id = _lookup_existing_goods_id_for_created_row(api, dec_ref, row)
        if (result.get('success') and goods_id) or already_exists:
            execute(
                """UPDATE BKD.StagingGoodsItems
                   SET status='CREATED', goods_id=?, tss_status=?,
                       error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                   WHERE staging_id=?""",
                [goods_id or None, 'Created' if already_exists else result.get('status', ''), staging_id],
            )
            from app.blueprints.goods.routes import _learn_product_unit_weights_from_goods
            _learn_product_unit_weights_from_goods(row)
            summary['created'] += 1
            if already_exists:
                summary['messages'].append(f"#{staging_id}: already existed in TSS")
            else:
                summary['messages'].append(f"#{staging_id}: {goods_id}")
        else:
            message = result.get('message') or 'TSS did not return a goods_id'
            execute(
                """UPDATE BKD.StagingGoodsItems
                   SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                   WHERE staging_id=?""",
                [message[:4000], staging_id],
            )
            summary['failed'] += 1
            summary['messages'].append(f"#{staging_id}: {message[:120]}")

    return summary


def _create_validated_consignments_for_header(api, staging_ens_id, ens_ref):
    """Create DEC refs and goods for validated local cargo after ENS submit succeeds."""
    summary = {
        'attempted': 0,
        'created': 0,
        'failed': 0,
        'messages': [],
        'goods_attempted': 0,
        'goods_created': 0,
        'goods_failed': 0,
        'goods_messages': [],
    }
    if not staging_ens_id or not ens_ref:
        return summary

    rows = query_all(
        """
        SELECT staging_id, goods_description, transport_document_number,
               controlled_goods, goods_domestic_status, destination_country,
               supervising_customs_office, customs_warehouse_identifier,
               ducr, no_sfd_reason, align_ukims, use_importer_sde,
               declaration_choice, generate_SD,
               consignor_eori, consignee_eori, importer_eori, exporter_eori,
               consignor_name, consignee_name, importer_name, exporter_name,
               buyer_same_as_importer, seller_same_as_exporter,
               trader_reference, container_indicator
        FROM BKD.StagingConsignments
        WHERE staging_ens_id = ?
          AND status = 'VALIDATED'
          AND dec_reference IS NULL
        ORDER BY staging_id
        """,
        [staging_ens_id],
    )
    summary['attempted'] = len(rows)

    for row in rows:
        staging_id = row['staging_id']
        payload = _consignment_create_payload(row)
        result = api.create_consignment(ens_ref, payload)
        _log_consignment_create_call(staging_id, result, payload)

        dec_ref = result.get('reference') or ''
        if result.get('success') and dec_ref:
            execute(
                """UPDATE BKD.StagingConsignments
                   SET status='CREATED', dec_reference=?, tss_status=?,
                       error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                   WHERE staging_id=?""",
                [dec_ref, result.get('status', ''), staging_id],
            )
            summary['created'] += 1
            summary['messages'].append(f"#{staging_id}: {dec_ref}")
        else:
            message = result.get('message') or 'TSS did not return a DEC reference'
            execute(
                """UPDATE BKD.StagingConsignments
                   SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                   WHERE staging_id=?""",
                [message[:4000], staging_id],
            )
            summary['failed'] += 1
            summary['messages'].append(f"#{staging_id}: {message[:120]}")

    goods_summary = _create_validated_goods_for_header(api, staging_ens_id)
    summary['goods_attempted'] = goods_summary['attempted']
    summary['goods_created'] = goods_summary['created']
    summary['goods_failed'] = goods_summary['failed']
    summary['goods_messages'] = goods_summary['messages']
    summary['goods'] = goods_summary

    return summary


def _sync_scope_consignment_ids(staging_ens_id):
    if not staging_ens_id:
        return ''
    try:
        rows = query_all(
            """
            SELECT staging_id
            FROM BKD.StagingConsignments
            WHERE staging_ens_id = ?
              AND dec_reference IS NOT NULL
            ORDER BY staging_id
            """,
            [staging_ens_id],
        )
    except Exception:
        return ''
    ids = [str(row.get('staging_id')) for row in rows or [] if row.get('staging_id')]
    return ','.join(ids)


def _run_post_submit_tss_status_sync(staging_ens_id=None):
    """Run the same full sync job operators would click after ENS + cargo submit."""
    try:
        from app.blueprints.orchestrator.routes import JOBS, _run_script

        scoped_cons_ids = _sync_scope_consignment_ids(staging_ens_id)
        ok, output, duration_ms = _run_script(
            'sync_all',
            JOBS['sync_all']['script'],
        )
        return {
            'ok': ok,
            'output': output,
            'duration_ms': duration_ms,
            'scoped_consignment_ids': scoped_cons_ids,
        }
    except Exception as exc:
        return {'ok': False, 'output': str(exc), 'duration_ms': 0, 'scoped_consignment_ids': ''}


def _flash_post_submit_tss_sync(sync_result):
    if not sync_result:
        return
    if sync_result.get('ok'):
        flash('Full TSS sync completed after Submit ENS + Cargo.', 'success')
        return
    detail = (sync_result.get('output') or '').strip().splitlines()
    suffix = f" {detail[-1][:180]}" if detail else ''
    flash(f'Submit ENS + Cargo completed, but automatic full TSS sync did not finish.{suffix}', 'warning')


def _submit_ens_declaration(dec_id, *, create_linked_consignments=True, api_factory=None):
    """Submit a single validated ENS header and persist the API result."""
    dec = query_one(
        "SELECT id, status, external_ref, external_status, payload_json "
        "FROM BKD.StagingDeclarations WHERE id = ?",
        [dec_id],
    )
    if not dec:
        return {'ok': False, 'message': 'Declaration not found.', 'category': 'warning'}

    status = dec.get('status', '')
    ext_ref = dec.get('external_ref', '') or ''
    ext_status = dec.get('external_status', '') or ''
    submittable = (
        status in ('Validated', 'Resubmit')
        or (_is_locally_submitted_ens(dec) and _is_tss_draft_like(ext_status))
    )
    if not submittable:
        return {'ok': False, 'message': f'Cannot submit - status is {status!r}.', 'category': 'warning'}

    try:
        payload = json.loads(dec.get('payload_json') or '{}')
    except (ValueError, TypeError):
        return {'ok': False, 'message': 'Invalid payload JSON - edit the record before submitting.', 'category': 'danger'}
    payload = _normalise_ens_submit_payload(payload)
    if payload.get('arrival_date_time'):
        payload['arrival_date_time'] = _tss_datetime_payload_value(payload.get('arrival_date_time'))

    try:
        if api_factory is None:
            from app.tss_api import build_cfg_client
            api_factory = build_cfg_client
        api = api_factory()

        if ext_ref and ext_ref.startswith('ENS'):
            result = api.update_header(ext_ref, payload)
            op = 'UPDATE_HEADER'
        else:
            result = api.create_header(payload)
            op = 'CREATE_HEADER'

        _log_tss_api_exchange(
            staging_id=dec_id,
            call_type=op,
            http_method='POST',
            url=result.get('url', ''),
            request_payload=payload,
            http_status=result.get('http_status', 0),
            response_status=result.get('status', ''),
            response_message=result.get('message', '')[:500],
            response_json=result.get('raw_response', '')[:4000],
            duration_ms=result.get('duration_ms', 0),
            error_detail='' if result.get('success') else result.get('message', '')[:2000],
        )

        if not result.get('success'):
            error_msg = result.get('message', '') or 'TSS submission failed.'
            execute(
                """UPDATE BKD.StagingDeclarations
                   SET status = 'Submit_Error',
                       api_http_status = ?,
                       api_response_status = ?,
                       api_process_message = ?,
                       api_error_message = ?,
                       api_response_json = ?,
                       api_request_json = ?,
                       api_duration_ms = ?,
                       api_called_at = GETUTCDATE(),
                       error_message = ?,
                       updated_at = GETUTCDATE()
                   WHERE id = ?""",
                [
                    result.get('http_status', 0),
                    result.get('status', ''),
                    result.get('message', '')[:500],
                    error_msg[:2000],
                    result.get('raw_response', '')[:4000],
                    json.dumps(payload)[:4000],
                    result.get('duration_ms', 0),
                    error_msg[:2000],
                    dec_id,
                ],
            )
            return {'ok': False, 'message': f'Submit failed: {error_msg}', 'category': 'danger'}

        new_ref = result.get('reference') or ext_ref
        execute(
            """UPDATE BKD.StagingDeclarations
               SET status = 'Submitted',
                   external_ref = ?,
                   external_status = ?,
                   api_http_status = ?,
                   api_response_status = ?,
                   api_process_message = ?,
                   api_response_json = ?,
                   api_request_json = ?,
                   api_duration_ms = ?,
                   api_called_at = GETUTCDATE(),
                   error_message = NULL,
                   api_error_message = NULL,
                   completed_at = GETUTCDATE(),
                   updated_at = GETUTCDATE()
               WHERE id = ?""",
            [
                new_ref,
                result.get('status', ''),
                result.get('http_status', 0),
                result.get('status', ''),
                result.get('message', '')[:500],
                result.get('raw_response', '')[:4000],
                json.dumps(payload)[:4000],
                result.get('duration_ms', 0),
                dec_id,
            ],
        )
        if op == 'CREATE_HEADER':
            _capture_tss_create_identity(dec_id, api)

        cons_summary = None
        staging_ens_id = None
        if create_linked_consignments and new_ref:
            try:
                staging_ens_id = _upsert_pipeline_header_stub(
                    dec_id,
                    new_ref,
                    payload,
                    result.get('status', '') or 'Draft',
                )
                cons_summary = _create_validated_consignments_for_header(api, staging_ens_id, new_ref)
            except Exception as exc:
                return {
                    'ok': True,
                    'reference': new_ref,
                    'staging_ens_id': staging_ens_id,
                    'message': f'ENS submitted to TSS as {new_ref}, but linked DEC creation could not run: {exc}',
                    'category': 'warning',
                }

        return {
            'ok': True,
            'reference': new_ref,
            'staging_ens_id': staging_ens_id,
            'message': f'ENS submitted to TSS as {new_ref}.',
            'category': 'success',
            'cons_summary': cons_summary,
        }
    except Exception as exc:
        return {'ok': False, 'message': f'Submit error: {exc}', 'category': 'danger'}


def _auto_validate_ens(dec_id):
    """Validate an edited ENS locally without submitting it to TSS."""
    try:
        from app.ens_validation import auto_validate_declaration_record

        validation = auto_validate_declaration_record(dec_id)
    except Exception as exc:
        return {'ok': False, 'stage': 'validate', 'message': f'Auto-validation failed: {exc}', 'category': 'danger'}

    if not validation:
        return {'ok': False, 'stage': 'validate', 'message': 'Auto-validation could not find this ENS record.', 'category': 'warning'}

    if not validation.get('ok'):
        issues = validation.get('errors') or []
        first_issue = issues[0] if issues else validation.get('message') or 'Validation failed.'
        return {
            'ok': False,
            'stage': 'validate',
            'message': f'Auto-validation found {len(issues) or 1} issue(s): {first_issue}',
            'category': 'danger',
            'validation': validation,
        }

    return {
        'ok': True,
        'stage': 'validate',
        'message': 'Auto-validation passed locally. Use Submit ENS Header when ready.',
        'category': 'success',
        'validation': validation,
    }


def _auto_validate_and_submit_ens(dec_id):
    """Validate an ENS and submit it to TSS only for explicit submit flows."""
    validation_result = _auto_validate_ens(dec_id)
    if not validation_result.get('ok'):
        return validation_result

    submit = _submit_ens_declaration(dec_id)
    submit['stage'] = 'submit'
    submit['validation'] = validation_result.get('validation')
    return submit


@declarations_bp.route('/pipeline')
def pipeline():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/<int:dec_id>/export.json')
def export_json(dec_id):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
def _ens_export_value(value):
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        try:
            return value.isoformat(sep=' ')
        except TypeError:
            return value.isoformat()
    return value


def _ens_export_row(raw, *, record_type):
    row = dict(raw or {})
    if record_type == 'StagingDeclarations':
        local_status = row.get('status')
        tss_status = row.get('external_status')
        ens_reference = row.get('external_ref')
        has_live_ref = _has_live_ens_reference({'external_ref': ens_reference})
        display_status = _ens_display_status(
            local_status,
            tss_status,
            imported_only=False,
            has_live_ref=has_live_ref,
            source=row.get('source'),
        )
        export = {
            'record_type': record_type,
            'local_id': row.get('id'),
            'pipeline_staging_id': row.get('linked_pipeline_staging_id'),
            'ens_reference': ens_reference,
            'local_status': local_status,
            'tss_status': tss_status,
            'display_status': display_status,
            'declaration_type': row.get('declaration_type'),
            'label': row.get('label'),
            'source': row.get('source'),
        }
    else:
        local_status = row.get('status')
        tss_status = row.get('tss_status')
        ens_reference = row.get('ens_reference')
        source = row.get('source') or 'TSS_IMPORT'
        imported_only = bool(ens_reference) and _is_tss_import_source(source)
        display_status = _ens_display_status(
            local_status,
            tss_status,
            imported_only=imported_only,
            has_live_ref=_has_live_ens_reference({'external_ref': ens_reference}),
            source=source,
        )
        export = {
            'record_type': record_type,
            'local_id': '',
            'pipeline_staging_id': row.get('staging_id'),
            'ens_reference': ens_reference,
            'local_status': local_status,
            'tss_status': tss_status,
            'display_status': display_status,
            'declaration_type': 'ENS',
            'label': row.get('label'),
            'source': source,
        }

    for field in ENS_HEADER_FIELDS:
        export[field] = _ens_header_value(row, field)
    export['consignment_count'] = row.get('consignment_count')
    export['goods_count'] = row.get('goods_count')
    export['error_message'] = row.get('error_message')
    export['created_at'] = row.get('created_at')
    export['updated_at'] = row.get('updated_at')
    return {key: _ens_export_value(export.get(key)) for key in ENS_EXPORT_FIELDNAMES}


def _ens_export_rows(declaration_rows=None, header_rows=None):
    rows = [
        _ens_export_row(row, record_type='StagingDeclarations')
        for row in (declaration_rows or [])
    ]
    rows.extend(
        _ens_export_row(row, record_type='StagingEnsHeaders')
        for row in (header_rows or [])
    )
    return rows


def _write_ens_export_csv(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=ENS_EXPORT_FIELDNAMES, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows or [])
    return output.getvalue()


@declarations_bp.route('/export.csv')
def export_csv():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/create', methods=['GET', 'POST'])
def create():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/<string:ens_ref>/detail')
def detail_by_ref(ens_ref):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/header/<int:staging_id>')
def header_detail(staging_id):
    client_code = (get_tenant().get('code') or 'BKD').upper()
    header = query_one("""
        SELECT
            h.stg_header_id, h.ClientCode, h.conveyance_ref, h.arrival_date_time,
            h.movement_type, h.type_of_passive_transport,
            h.identity_no_of_transport, h.nationality_of_transport,
            h.arrival_port, h.place_of_loading, h.place_of_unloading,
            h.place_of_acceptance_same_as_loading, h.place_of_acceptance,
            h.place_of_delivery_same_as_unloading, h.place_of_delivery,
            h.seal_number, h.transport_charges,
            h.carrier_name, h.carrier_eori, h.carrier_street_number,
            h.carrier_city, h.carrier_postcode, h.carrier_country,
            h.haulier_eori,
            h.sub_status, h.tss_ens_header_ref, h.label, h.source,
            h.movement_notified_at, h.staging_failures_notified_at,
            h.validation_errors_json, h.stg_created_at, h.updated_at,
            t.TssStatus,
            COALESCE(sent_by.ens_sender_email, fallback_sent_by.ens_sender_email) AS ens_sender_email,
            COALESCE(sent_by.ens_sender_name, fallback_sent_by.ens_sender_name) AS ens_sender_name
        FROM STG.BKD_ENS_Headers h
        LEFT JOIN TSS.BKD_ENS_Headers t
            ON t.ClientCode = h.ClientCode
           AND t.DeclarationNumber = h.tss_ens_header_ref
        OUTER APPLY (
            SELECT TOP 1
                m.SenderEmail AS ens_sender_email,
                m.SenderName AS ens_sender_name
            FROM ING.BKD_ProcessLog p
            JOIN ING.BKD_EmailMessage m
              ON p.SourceTable = 'ING.BKD_EmailMessage'
             AND p.SourceRecordId = m.EmailMessageId
            WHERE p.TargetTable = 'STG.BKD_ENS_Headers'
              AND p.TargetRecordId = h.stg_header_id
              AND m.ClientCode = h.ClientCode
            ORDER BY COALESCE(p.TransformedAt, p.LoadedAt) DESC, p.ProcessLogId DESC
        ) sent_by
        OUTER APPLY (
            SELECT TOP 1
                m.SenderEmail AS ens_sender_email,
                m.SenderName AS ens_sender_name
            FROM ING.BKD_EmailMessage m
            WHERE m.ClientCode = h.ClientCode
              AND h.source = 'EXCEL_SALES_ORDERS_DETAILS'
              AND COALESCE(m.AttachmentCount, 0) = 0
              AND (
                    m.Subject LIKE '%Tss Details%'
                 OR m.Subject LIKE '%TSS DETAILS%'
                 OR m.Subject LIKE '%DETAILS%'
              )
              AND ABS(DATEDIFF(minute, COALESCE(m.ReceivedAt, m.LoadedAt), h.stg_created_at)) <= 2160
            ORDER BY ABS(DATEDIFF(minute, COALESCE(m.ReceivedAt, m.LoadedAt), h.stg_created_at)),
                     m.EmailMessageId DESC
        ) fallback_sent_by
        WHERE h.ClientCode = ? AND h.stg_header_id = ?
    """, [client_code, staging_id])
    if not header:
        flash('ENS header not found.', 'warning')
        return redirect(url_for('declarations.list_declarations'))
    consignments = query_all("""
        SELECT
            c.stg_consignment_id,
            c.ClientCode,
            COALESCE(c.trader_reference, c.transport_document_number, c.tss_consignment_ref) AS document_no,
            c.sub_status,
            c.tss_consignment_ref, c.goods_description, c.importer_eori,
            c.importer_name,
            c.consignor_eori, c.consignor_name,
            c.consignor_street_number, c.consignor_city, c.consignor_postcode, c.consignor_country,
            c.consignee_eori, c.consignee_name,
            c.consignee_street_number, c.consignee_city, c.consignee_postcode, c.consignee_country,
            c.importer_street_number, c.importer_city, c.importer_postcode, c.importer_country,
            c.exporter_eori,
            c.align_ukims, c.use_importer_sde, c.declaration_choice,
            c.buyer_same_as_importer, c.seller_same_as_exporter,
            c.trader_reference, c.transport_document_number,
            c.controlled_goods, c.goods_domestic_status,
            c.destination_country, c.ducr, c.container_indicator,
            c.generate_SD, c.no_sfd_reason,
            c.metadata_json AS error_message,
            c.updated_at, tc.TssStatus AS cons_tss_status,
            COALESCE(sfd_track.tss_sfd_number, tss_sfd.SfdReference) AS sfd_reference,
            COALESCE(
                NULLIF(LTRIM(RTRIM(sfd_track.tss_movement_reference_number)), ''),
                NULLIF(LTRIM(RTRIM(tss_sfd.MovementReferenceNumber)), ''),
                CASE WHEN ISJSON(tc.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(tc.RawJson, '$.movement_reference_number'))), '') END,
                CASE WHEN ISJSON(tc.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(tc.RawJson, '$.movementReferenceNumber'))), '') END,
                CASE WHEN ISJSON(tc.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(tc.RawJson, '$.mrn'))), '') END
            ) AS sfd_mrn,
            sfd_track.tss_eori_for_eidr AS sfd_eidr,
            COALESCE(
                CASE WHEN UPPER(LTRIM(RTRIM(COALESCE(sfd_track.tss_sfd_status, '')))) IN ('OK', 'SUCCESS') THEN NULL ELSE sfd_track.tss_sfd_status END,
                CASE WHEN UPPER(LTRIM(RTRIM(COALESCE(tss_sfd.TssStatus, '')))) IN ('OK', 'SUCCESS') THEN NULL ELSE tss_sfd.TssStatus END,
                tc.TssStatus
            ) AS sfd_status,
            (SELECT COUNT(*)
               FROM STG.BKD_GoodsItems g
              WHERE g.ClientCode = c.ClientCode
                AND g.stg_consignment_id = c.stg_consignment_id) AS goods_count,
            (SELECT COUNT(*)
               FROM STG.BKD_GoodsItems g
              WHERE g.ClientCode = c.ClientCode
                AND g.stg_consignment_id = c.stg_consignment_id
                AND NULLIF(LTRIM(RTRIM(COALESCE(g.tss_hex_id, ''))), '') IS NULL
                AND UPPER(COALESCE(g.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')) AS pending_goods_count
        FROM STG.BKD_ENS_Consignments c
        LEFT JOIN TSS.BKD_ENS_Consignments tc
            ON tc.ClientCode = c.ClientCode
           AND tc.ConsignmentReference = c.tss_consignment_ref
        OUTER APPLY (
            SELECT TOP 1
                t.tss_sfd_number,
                t.tss_movement_reference_number,
                t.tss_eori_for_eidr,
                t.tss_sfd_status
            FROM STG.BKD_SFD_Tracking t
            WHERE t.ClientCode = c.ClientCode
              AND t.tss_consignment_ref = c.tss_consignment_ref
            ORDER BY t.stg_polled_at DESC, t.stg_tracking_id DESC
        ) sfd_track
        OUTER APPLY (
            SELECT TOP 1
                s.SfdReference,
                s.MovementReferenceNumber,
                s.TssStatus
            FROM TSS.BKD_SFD s
            WHERE s.ClientCode = c.ClientCode
              AND (
                    s.DeclarationNumber = c.tss_consignment_ref
                 OR (
                        NULLIF(LTRIM(RTRIM(COALESCE(s.DeclarationNumber, ''))), '') IS NULL
                    AND s.EnsReference = c.tss_ens_header_ref
                    )
              )
            ORDER BY
                CASE WHEN s.DeclarationNumber = c.tss_consignment_ref THEN 0 ELSE 1 END,
                COALESCE(s.UpdatedAt, s.LastSyncedAt, s.CreatedAt) DESC
        ) tss_sfd
        WHERE c.ClientCode = ? AND c.stg_header_id = ?
        ORDER BY c.stg_consignment_id
    """, [client_code, staging_id])
    goods_rows = query_all("""
        SELECT
            g.stg_consignment_id,
            g.stg_item_id,
            g.item_seq,
            g.goods_description,
            g.commodity_code,
            g.type_of_packages,
            g.number_of_packages,
            g.package_marks,
            g.gross_mass_kg,
            g.net_mass_kg,
            g.controlled_goods,
            g.country_of_origin,
            g.procedure_code,
            g.additional_procedure_code,
            g.item_invoice_amount,
            g.item_invoice_currency,
            g.valuation_method,
            g.preference,
            g.sku,
            g.error_message,
            g.sub_status,
            g.tss_hex_id,
            tg.TssStatus AS item_tss_status
        FROM STG.BKD_GoodsItems g
        JOIN STG.BKD_ENS_Consignments c
          ON c.ClientCode = g.ClientCode
         AND c.stg_consignment_id = g.stg_consignment_id
        LEFT JOIN TSS.BKD_GoodsItems tg
          ON tg.ClientCode = g.ClientCode
         AND tg.GoodsStage = 'ENS'
         AND tg.GoodsId = g.tss_hex_id
        WHERE g.ClientCode = ? AND c.stg_header_id = ?
        ORDER BY c.stg_consignment_id, g.item_seq, g.stg_item_id
    """, [client_code, staging_id])
    choices = load_field_choices()
    header['movement_type_display'] = _choice_display(header.get('movement_type'), choices.get('movement_type'))
    header['type_of_passive_transport_display'] = _choice_display(
        header.get('type_of_passive_transport'),
        choices.get('type_of_passive_transport'),
    )
    goods_by_consignment = {}
    can_edit_header = _stg_header_allows_edit(header)
    can_submit_header = not str(header.get('tss_ens_header_ref') or '').strip()
    can_sync_header = bool(str(header.get('tss_ens_header_ref') or '').strip())
    can_import_sales_orders = tss_allows_data_changes(header.get('TssStatus'), header.get('sub_status'))
    pack_header = {
        'staging_id': header.get('stg_header_id'),
        'ens_reference': header.get('tss_ens_header_ref'),
        'status': header.get('sub_status'),
        'tss_status': header.get('TssStatus'),
        'created_at': header.get('stg_created_at'),
        'updated_at': header.get('updated_at'),
    }
    pack_dec = {
        'id': None,
        'external_ref': header.get('tss_ens_header_ref'),
        'status': header.get('sub_status'),
        'external_status': header.get('TssStatus'),
        'created_at': header.get('stg_created_at'),
        'updated_at': header.get('updated_at'),
    }
    can_email_ens_pack = _can_email_ens_pack(
        pack_dec,
        pack_header,
        [{'dec_reference': row.get('tss_consignment_ref')} for row in consignments],
    )
    can_unlock_gmr = bool(
        normalize_status_key(header.get('TssStatus')) in {'AUTHORISED FOR MOVEMENT', 'ARRIVED'}
        and any(str(row.get('tss_consignment_ref') or '').strip() for row in consignments)
    )
    # GMR routes are still disabled in automation PRD. Keep the action visible
    # when the movement is ready, but do not route operators into a stub.
    can_create_gmr = False
    consignments_by_id = {row.get('stg_consignment_id'): row for row in consignments}
    prd_sdi_links = load_prd_sdi_links_for_context(
        client_code=client_code,
        consignment_ids=[row.get('stg_consignment_id') for row in consignments],
        consignment_refs=[
            value
            for row in consignments
            for value in (
                row.get('tss_consignment_ref'),
                row.get('trader_reference'),
                row.get('transport_document_number'),
                row.get('document_no'),
            )
        ],
        sfd_refs=[row.get('sfd_reference') for row in consignments],
    )
    attach_sdi_links_to_consignments(consignments, prd_sdi_links)
    for row in consignments:
        local_status = normalize_status_key(row.get('sub_status'))
        tss_status = normalize_status_key(row.get('cons_tss_status'))
        row['is_cancelled'] = local_status in {'CANCELLED', 'CANCELED', 'DELETED'} or tss_status in {'CANCELLED', 'CANCELED', 'DELETED'}
        if row['is_cancelled'] and row.get('sfd_reference'):
            row['sfd_status'] = 'CANCELLED'
        has_cons_ref = bool(str(row.get('tss_consignment_ref') or '').strip())
        tss_can_submit = _tss_consignment_allows_submit(row.get('cons_tss_status'))
        row['error_display'] = _prd_error_display(
            row.get('error_message'),
            local_status=row.get('sub_status'),
            tss_status=row.get('cons_tss_status'),
            entity_label='this consignment',
        )
        row['can_edit_consignment'] = _prd_consignment_allows_edit(row)
        row['can_submit_to_tss'] = bool(
            can_sync_header
            and (
                not has_cons_ref
                or (
                    tss_can_submit
                    and (
                        int(row.get('pending_goods_count') or 0) > 0
                        or local_status not in {'SUBMITTED', 'COMPLETED'}
                    )
                )
            )
        )
    for goods in goods_rows or []:
        parent = consignments_by_id.get(goods.get('stg_consignment_id'), {})
        goods['can_edit_goods'] = _prd_goods_allows_edit(goods, parent)
        goods['can_delete_failed_goods'] = _prd_goods_allows_local_delete(goods)
        goods['can_delete_local_goods'] = goods['can_delete_failed_goods']
        goods['can_inline_gross'] = bool(goods.get('can_edit_goods') and not str(goods.get('tss_hex_id') or '').strip())
        goods['type_of_packages'] = normalise_package_type(goods.get('type_of_packages'), 'PK') or 'PK'
        goods['error_display'] = _prd_error_display(
            goods.get('error_message'),
            local_status=goods.get('sub_status'),
            tss_status=goods.get('item_tss_status') or parent.get('cons_tss_status'),
            entity_label='this goods item',
        )
        goods_by_consignment.setdefault(goods.get('stg_consignment_id'), []).append(goods)
    can_submit_cargo = any(bool(row.get('can_submit_to_tss')) for row in consignments)
    sfd_summary_rows = [
        row for row in consignments
        if row.get('sfd_reference')
    ]
    sfd_expected_total = len(consignments)
    return render_template(
        'declarations/detail.html',
        header=header,
        consignments=consignments,
        sfd_summary_rows=sfd_summary_rows,
        sfd_expected_total=sfd_expected_total,
        can_edit_header=can_edit_header,
        can_submit_header=can_submit_header,
        can_submit_cargo=can_submit_cargo,
        can_sync_header=can_sync_header,
        can_import_sales_orders=can_import_sales_orders,
        can_email_ens_pack=can_email_ens_pack,
        can_unlock_gmr=can_unlock_gmr,
        can_create_gmr=can_create_gmr,
        goods_by_consignment=goods_by_consignment,
        arrival_datetime_value=_html_datetime_value(header.get('arrival_date_time')),
        choices=choices,
        goods_choices=_load_prd_goods_choices(),
        carriers=get_partners_by_type('carrier'),
    )


def _prd_tss_record(result):
    response = (result or {}).get('response')
    if isinstance(response, dict):
        return response
    items = _tss_response_items(result)
    return items[0] if items and isinstance(items[0], dict) else {}


def _prd_result_status(result):
    record = _prd_tss_record(result)
    return str(record.get('status') or (result or {}).get('status') or '').strip()


def _tss_consignment_allows_submit(status):
    return normalize_status_key(status) in {'DRAFT', 'TRADER INPUT REQUIRED'}


def _prd_tss_status_is_terminal_ok(status):
    return normalize_status_key(status) in {
        'AUTHORISED FOR MOVEMENT',
        'AUTHORIZED FOR MOVEMENT',
        'ARRIVED',
    }


def _is_invalid_tss_submit_op_type(*values):
    cleaned = clean_tss_message(*values).lower()
    return 'invalid op_type' in cleaned and 'submit' in cleaned


def _prd_error_display(message, *, local_status=None, tss_status=None, entity_label='this record'):
    cleaned = clean_tss_message(message)
    if not cleaned:
        return None
    if _prd_tss_status_is_terminal_ok(tss_status):
        return None
    explanation = explain_tss_error(
        message,
        local_status=local_status,
        tss_status=tss_status,
        entity_label=entity_label,
    )
    if explanation:
        return {
            'title': explanation.get('title') or 'Action required',
            'summary': explanation.get('summary') or cleaned,
            'detail': explanation.get('detail') or '',
            'next_step': explanation.get('next_step') or '',
            'technical': explanation.get('technical') or cleaned,
            'tone': explanation.get('tone') or 'danger',
        }
    return {
        'title': 'TSS or validation error',
        'summary': cleaned,
        'detail': '',
        'next_step': 'Open Edit, correct the value, save, then retry the automatic action from this ENS page.',
        'technical': cleaned,
        'tone': 'danger',
    }


def _tss_header_allows_edit(status):
    return normalize_status_key(status) in {'DRAFT', 'TRADER INPUT REQUIRED'}


def _stg_header_allows_edit(header):
    if not header:
        return False
    has_tss_ref = bool(str(header.get('tss_ens_header_ref') or '').strip())
    if not has_tss_ref:
        return True
    return _tss_header_allows_edit(header.get('TssStatus'))


PRD_LOCAL_DATA_REPAIR_STATUSES = {'PENDING', 'PENDING REVIEW', 'FAILED', 'INVALID', 'VALIDATED'}
PRD_LOCAL_DATA_DELETE_STATUSES = PRD_LOCAL_DATA_REPAIR_STATUSES | {'CREATED', 'DRAFT'}
PRD_TSS_DATA_EDIT_STATUSES = {'DRAFT', 'TRADER INPUT REQUIRED'}


def _tss_allows_prd_data_edit(status):
    return normalize_status_key(status) in PRD_TSS_DATA_EDIT_STATUSES


def _prd_local_allows_data_edit(status):
    return normalize_status_key(status) in PRD_LOCAL_DATA_REPAIR_STATUSES


def _prd_consignment_allows_edit(row):
    if not row:
        return False
    has_tss_ref = bool(str(row.get('tss_consignment_ref') or '').strip())
    if has_tss_ref:
        return _tss_allows_prd_data_edit(row.get('cons_tss_status') or row.get('TssStatus'))
    return _prd_local_allows_data_edit(row.get('sub_status'))


def _prd_goods_allows_edit(row, parent=None):
    if not row:
        return False
    parent = parent or {}
    has_goods_ref = bool(str(row.get('tss_hex_id') or '').strip())
    if has_goods_ref:
        return _tss_allows_prd_data_edit(
            row.get('item_tss_status') or parent.get('cons_tss_status') or parent.get('TssStatus')
        )
    return _prd_consignment_allows_edit(parent) and _prd_local_allows_data_edit(row.get('sub_status'))


def _prd_goods_allows_local_delete(row):
    if not row:
        return False
    has_goods_ref = bool(str(row.get('tss_hex_id') or row.get('goods_id') or '').strip())
    return (
        not has_goods_ref
        and normalize_status_key(row.get('sub_status') or row.get('status')) in PRD_LOCAL_DATA_DELETE_STATUSES
    )


def _load_prd_goods_choices():
    from app.blueprints.goods.routes import load_goods_choices
    return load_goods_choices()


def _choice_display(value, choices):
    raw = str(value or '').strip()
    if not raw:
        return ''
    for item in choices or []:
        item_value = str(
            getattr(item, 'value', None)
            if not isinstance(item, dict)
            else item.get('value') or ''
        ).strip()
        if item_value != raw:
            continue
        name = str(
            getattr(item, 'name', None)
            if not isinstance(item, dict)
            else item.get('name') or ''
        ).strip()
        return f'{name} ({raw})' if name else raw
    return raw


def _upsert_prd_tss_consignment(client_code, ens_ref, dec_ref, result, row=None):
    record = _prd_tss_record(result)
    goods_description = (
        _first_value(record, 'goods_description', 'goodsDescription', 'description_of_goods', 'descriptionOfGoods')
        or (row or {}).get('goods_description')
    )
    importer_eori = (
        _first_value(record, 'importer_eori', 'importerEori', 'importer_id', 'importerId')
        or (row or {}).get('importer_eori')
    )
    execute(
        """
        MERGE TSS.BKD_ENS_Consignments AS target
        USING (SELECT ? AS ClientCode, ? AS ConsignmentReference) AS src
           ON target.ClientCode = src.ClientCode
          AND target.ConsignmentReference = src.ConsignmentReference
        WHEN MATCHED THEN UPDATE SET
            DeclarationNumber = ?,
            EnsReference = ?,
            TssStatus = ?,
            GoodsDescription = ?,
            ImporterEori = ?,
            RawJson = ?,
            LastSyncedAt = SYSUTCDATETIME(),
            UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (ClientCode, DeclarationNumber, EnsReference, ConsignmentReference, TssStatus,
                    GoodsDescription, ImporterEori, RawJson, LastSyncedAt, UpdatedAt)
            VALUES (src.ClientCode, ?, ?, src.ConsignmentReference, ?, ?, ?, ?,
                    SYSUTCDATETIME(), SYSUTCDATETIME());
        """,
        [
            client_code,
            dec_ref,
            dec_ref,
            ens_ref,
            _prd_result_status(result),
            goods_description,
            importer_eori,
            _json_for_tss_raw(record or (result or {}).get('response') or {}),
            dec_ref,
            ens_ref,
            _prd_result_status(result),
            goods_description,
            importer_eori,
            _json_for_tss_raw(record or (result or {}).get('response') or {}),
        ],
    )


def _upsert_prd_tss_goods(client_code, goods_id, dec_ref, result, row=None):
    record = _prd_tss_record(result)
    execute(
        """
        MERGE TSS.BKD_GoodsItems AS target
        USING (SELECT ? AS ClientCode, ? AS GoodsStage, ? AS GoodsId) AS src
           ON target.ClientCode = src.ClientCode
          AND target.GoodsStage = src.GoodsStage
          AND target.GoodsId = src.GoodsId
        WHEN MATCHED THEN UPDATE SET
            ParentReference = ?,
            ItemNumber = ?,
            TssStatus = ?,
            RawJson = ?,
            LastSyncedAt = SYSUTCDATETIME(),
            UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (ClientCode, GoodsStage, GoodsId, ParentReference, ItemNumber,
                    TssStatus, RawJson, LastSyncedAt, UpdatedAt)
            VALUES (src.ClientCode, src.GoodsStage, src.GoodsId, ?, ?, ?, ?,
                    SYSUTCDATETIME(), SYSUTCDATETIME());
        """,
        [
            client_code,
            'ENS',
            goods_id,
            dec_ref,
            (record.get('item_number') if record else None) or (row or {}).get('item_seq'),
            _prd_result_status(result),
            _json_for_tss_raw(record or (result or {}).get('response') or {}),
            dec_ref,
            (record.get('item_number') if record else None) or (row or {}).get('item_seq'),
            _prd_result_status(result),
            _json_for_tss_raw(record or (result or {}).get('response') or {}),
        ],
    )


def _sync_prd_ens_status_once(staging_id, *, client_code):
    from app.ingestion.ens_status_watcher import sync_ens_status_once

    result = sync_ens_status_once(
        staging_id,
        tenant_code=client_code,
        continue_after_notified=True,
    )
    return {
        'ok': bool(result.get('ok')),
        'header_synced': bool(result.get('header_tss_status')),
        'consignments_synced': int(result.get('consignments_polled') or 0),
        'sfd_synced': int(result.get('sfd_synced') or 0),
        'notify_checked': 'notify_ok' in result or bool(result.get('already_notified')),
        'message': result.get('error') or result.get('message') or result.get('stage'),
        'raw': result,
    }


def _latest_prd_consignment_tss_status(client_code, dec_ref):
    row = query_one(
        """
        SELECT TOP 1 TssStatus
        FROM TSS.BKD_ENS_Consignments
        WHERE ClientCode = ?
          AND ConsignmentReference = ?
        ORDER BY COALESCE(UpdatedAt, LastSyncedAt) DESC
        """,
        [client_code, dec_ref],
    )
    return (row or {}).get('TssStatus') or ''


def _refresh_prd_consignment_status_for_submit(staging_id, *, client_code, dec_ref):
    try:
        _sync_prd_ens_status_once(staging_id, client_code=client_code)
    except Exception:
        pass
    return normalize_status_key(_latest_prd_consignment_tss_status(client_code, dec_ref))


def _prd_consignment_scope_ids(raw_ids):
    if raw_ids is None:
        return []
    if isinstance(raw_ids, str):
        parts = raw_ids.replace(';', ',').split(',')
    else:
        parts = []
        for item in raw_ids:
            parts.extend(str(item or '').replace(';', ',').split(','))
    ids = []
    for raw in parts:
        try:
            value = int(str(raw or '').strip())
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    return ids


def _prd_scope_where(column, scope_ids):
    if not scope_ids:
        return '', []
    placeholders = ', '.join('?' for _ in scope_ids)
    return f" AND {column} IN ({placeholders})", list(scope_ids)


def _generate_sd_from_form(form):
    values = [str(value or '').strip().lower() for value in form.getlist('generate_SD')]
    if 'yes' in values:
        return 'yes'
    if 'no' in values:
        return 'no'
    return None


def _apply_prd_scoped_generate_sd(staging_id, *, client_code, scope_ids, generate_sd):
    if not scope_ids or generate_sd not in {'yes', 'no'}:
        return
    scope_where, scope_params = _prd_scope_where('stg_consignment_id', scope_ids)
    execute(
        f"""
        UPDATE STG.BKD_ENS_Consignments
           SET generate_SD = ?,
               updated_at = SYSUTCDATETIME()
         WHERE ClientCode = ?
           AND stg_header_id = ?
           {scope_where}
        """,
        [generate_sd, client_code, staging_id] + scope_params,
    )


def _submit_prd_cargo_for_header(staging_id, *, client_code, scope_consignment_ids=None):
    header = query_one(
        """
        SELECT stg_header_id, tss_ens_header_ref
        FROM STG.BKD_ENS_Headers
        WHERE ClientCode = ? AND stg_header_id = ?
        """,
        [client_code, staging_id],
    )
    ens_ref = str((header or {}).get('tss_ens_header_ref') or '').strip()
    if not ens_ref:
        return {'ok': False, 'message': 'Create the ENS header in TSS before sending consignments/goods.'}

    from app.tss_api import build_cfg_client
    api = build_cfg_client()
    scope_ids = _prd_consignment_scope_ids(scope_consignment_ids)
    scope_plain_where, scope_plain_params = _prd_scope_where('stg_consignment_id', scope_ids)
    scope_alias_where, scope_alias_params = _prd_scope_where('c.stg_consignment_id', scope_ids)
    summary = {
        'cons_attempted': 0, 'cons_created': 0, 'cons_submitted': 0, 'cons_failed': 0,
        'goods_attempted': 0, 'goods_created': 0, 'goods_failed': 0,
        'cons_deferred': 0,
        'messages': [],
    }

    cons_rows = query_all(
        f"""
        SELECT *
        FROM STG.BKD_ENS_Consignments
        WHERE ClientCode = ?
          AND stg_header_id = ?
          AND NULLIF(LTRIM(RTRIM(COALESCE(tss_consignment_ref, ''))), '') IS NULL
          AND UPPER(COALESCE(sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
          {scope_plain_where}
        ORDER BY stg_consignment_id
        """,
        [client_code, staging_id] + scope_plain_params,
    )
    for row in cons_rows:
        sid = row['stg_consignment_id']
        summary['cons_attempted'] += 1
        payload = _consignment_create_payload(row)
        result = api.create_consignment(ens_ref, payload)
        _log_consignment_create_call(sid, result, payload)
        items = _tss_response_items(result)
        dec_ref = result.get('reference') or _tss_reference_from_item(
            items[0] if items else result.get('response'),
            'consignment_number',
            'reference',
            'declaration_number',
            'number',
        )
        if result.get('success') and dec_ref:
            execute(
                """
                UPDATE STG.BKD_ENS_Consignments
                   SET sub_status = 'VALIDATED',
                       tss_consignment_ref = ?,
                       tss_ens_header_ref = ?,
                       tss_api_http_status = ?,
                       metadata_json = NULL,
                       last_sub_status_change = SYSUTCDATETIME(),
                       updated_at = SYSUTCDATETIME()
                 WHERE ClientCode = ? AND stg_consignment_id = ?
                """,
                [dec_ref, ens_ref, result.get('http_status'), client_code, sid],
            )
            _upsert_prd_tss_consignment(client_code, ens_ref, dec_ref, result, row=row)
            summary['cons_created'] += 1
        else:
            message = result.get('message') or 'TSS did not return a DEC reference.'
            execute(
                """
                UPDATE STG.BKD_ENS_Consignments
                   SET sub_status = 'FAILED',
                       metadata_json = ?,
                       last_sub_status_change = SYSUTCDATETIME(),
                       updated_at = SYSUTCDATETIME()
                 WHERE ClientCode = ? AND stg_consignment_id = ?
                """,
                [json.dumps({'message': message}, default=str)[:4000], client_code, sid],
            )
            summary['cons_failed'] += 1
            summary['messages'].append(f"Consignment #{sid}: {message[:120]}")

    cons_with_refs = query_all(
        f"""
        SELECT c.*, tc.TssStatus AS cons_tss_status
        FROM STG.BKD_ENS_Consignments c
        LEFT JOIN TSS.BKD_ENS_Consignments tc
          ON tc.ClientCode = c.ClientCode
         AND tc.ConsignmentReference = c.tss_consignment_ref
        WHERE c.ClientCode = ?
          AND c.stg_header_id = ?
          AND NULLIF(LTRIM(RTRIM(COALESCE(c.tss_consignment_ref, ''))), '') IS NOT NULL
          AND UPPER(COALESCE(c.sub_status, '')) NOT IN ('CANCELLED', 'DELETED', 'COMPLETED')
          {scope_alias_where}
        ORDER BY c.stg_consignment_id
        """,
        [client_code, staging_id] + scope_alias_params,
    )
    for cons in cons_with_refs:
        dec_ref = str(cons.get('tss_consignment_ref') or '').strip()
        cons_tss_status = normalize_status_key(cons.get('cons_tss_status'))
        goods_rows = query_all(
            """
            SELECT g.*, ? AS dec_reference
            FROM STG.BKD_GoodsItems g
            WHERE g.ClientCode = ?
              AND g.stg_consignment_id = ?
              AND NULLIF(LTRIM(RTRIM(COALESCE(g.tss_hex_id, ''))), '') IS NULL
              AND UPPER(COALESCE(g.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
            ORDER BY g.item_seq, g.stg_item_id
            """,
            [dec_ref, client_code, cons['stg_consignment_id']],
        )
        for goods in goods_rows:
            gid = goods['stg_item_id']
            summary['goods_attempted'] += 1
            payload = _goods_create_payload(goods)
            result = api.create_goods(dec_ref, payload)
            _log_goods_create_call(gid, result, payload)
            items = _tss_response_items(result)
            goods_ref = result.get('reference') or _tss_reference_from_item(
                items[0] if items else result.get('response'),
                'goods_id',
                'reference',
                'goods_item_id',
                'item_number',
            )
            if result.get('success') and goods_ref:
                execute(
                    """
                    UPDATE STG.BKD_GoodsItems
                       SET sub_status = 'SUBMITTED',
                           tss_hex_id = ?,
                           tss_consignment_ref = ?,
                           error_message = NULL,
                           submitted_at = COALESCE(submitted_at, SYSUTCDATETIME()),
                           last_sub_status_change = SYSUTCDATETIME(),
                           updated_at = SYSUTCDATETIME()
                     WHERE ClientCode = ? AND stg_item_id = ?
                    """,
                    [goods_ref, dec_ref, client_code, gid],
                )
                _upsert_prd_tss_goods(client_code, goods_ref, dec_ref, result, row=goods)
                summary['goods_created'] += 1
            else:
                message = result.get('message') or 'TSS did not return a goods id.'
                execute(
                    """
                    UPDATE STG.BKD_GoodsItems
                       SET sub_status = 'FAILED',
                           error_message = ?,
                           last_sub_status_change = SYSUTCDATETIME(),
                           updated_at = SYSUTCDATETIME()
                     WHERE ClientCode = ? AND stg_item_id = ?
                    """,
                    [message[:2000], client_code, gid],
                )
                summary['goods_failed'] += 1
                summary['messages'].append(f"Goods #{gid}: {message[:120]}")

        remaining = query_one(
            """
            SELECT COUNT(*) AS pending_count
            FROM STG.BKD_GoodsItems
            WHERE ClientCode = ?
              AND stg_consignment_id = ?
              AND NULLIF(LTRIM(RTRIM(COALESCE(tss_hex_id, ''))), '') IS NULL
              AND UPPER(COALESCE(sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
            """,
            [client_code, cons['stg_consignment_id']],
        )
        if int((remaining or {}).get('pending_count') or 0) > 0:
            continue
        if not _tss_consignment_allows_submit(cons_tss_status):
            cons_tss_status = _refresh_prd_consignment_status_for_submit(
                staging_id,
                client_code=client_code,
                dec_ref=dec_ref,
            )
        if not _tss_consignment_allows_submit(cons_tss_status):
            status_label = cons_tss_status or cons.get('cons_tss_status') or 'unknown'
            summary['cons_deferred'] += 1
            summary['messages'].append(
                f"DEC {dec_ref}: cargo submit waiting for TSS status Draft/Trader Input Required; latest status is {status_label}."
            )
            continue
        result = api.submit_consignment(dec_ref)
        _log_tss_api_exchange(
            staging_id=cons['stg_consignment_id'],
            call_type='SUBMIT_PRD_ENS_CONSIGNMENT',
            http_method=result.get('method', 'POST'),
            url=result.get('url', ''),
            request_payload=result.get('request_payload') or {'consignment_number': dec_ref},
            http_status=result.get('http_status', 0),
            response_status=result.get('status', ''),
            response_message=result.get('message', '')[:500],
            response_json=result.get('raw_response', '')[:4000],
            duration_ms=result.get('duration_ms', 0),
            error_detail='' if result.get('success') else result.get('message', '')[:2000],
        )
        if not result.get('success') and _is_invalid_tss_submit_op_type(
            result.get('message'),
            result.get('process_message'),
            result.get('raw_response'),
            result.get('response'),
        ):
            refreshed_status = _refresh_prd_consignment_status_for_submit(
                staging_id,
                client_code=client_code,
                dec_ref=dec_ref,
            )
            summary['cons_deferred'] += 1
            summary['messages'].append(
                f"DEC {dec_ref}: cargo submit deferred because TSS did not accept submit yet; latest status is {refreshed_status or 'unknown'}."
            )
            continue
        if result.get('success'):
            execute(
                """
                UPDATE STG.BKD_ENS_Consignments
                   SET sub_status = 'SUBMITTED',
                       tss_api_http_status = ?,
                       metadata_json = NULL,
                       submitted_at = COALESCE(submitted_at, SYSUTCDATETIME()),
                       last_sub_status_change = SYSUTCDATETIME(),
                       updated_at = SYSUTCDATETIME()
                 WHERE ClientCode = ? AND stg_consignment_id = ?
                """,
                [result.get('http_status'), client_code, cons['stg_consignment_id']],
            )
            _upsert_prd_tss_consignment(client_code, ens_ref, dec_ref, result, row=cons)
            summary['cons_submitted'] += 1
        else:
            message = result.get('message') or 'TSS consignment submit failed.'
            execute(
                """
                UPDATE STG.BKD_ENS_Consignments
                   SET sub_status = 'FAILED',
                       metadata_json = ?,
                       last_sub_status_change = SYSUTCDATETIME(),
                       updated_at = SYSUTCDATETIME()
                 WHERE ClientCode = ? AND stg_consignment_id = ?
                """,
                [json.dumps({'message': message}, default=str)[:4000], client_code, cons['stg_consignment_id']],
            )
            summary['cons_failed'] += 1
            summary['messages'].append(f"Submit DEC {dec_ref}: {message[:120]}")

    try:
        _sync_prd_ens_status_once(staging_id, client_code=client_code)
    except Exception:
        pass
    pending_goods_scope_where, pending_goods_scope_params = _prd_scope_where('cg.stg_consignment_id', scope_ids)
    pending_scope_where, pending_scope_params = _prd_scope_where('c.stg_consignment_id', scope_ids)
    pending = query_one(
        f"""
        SELECT
            SUM(CASE
                    WHEN NULLIF(LTRIM(RTRIM(COALESCE(c.tss_consignment_ref, ''))), '') IS NULL
                    THEN 1 ELSE 0
                END) AS missing_consignment_refs,
            SUM(CASE
                    WHEN NULLIF(LTRIM(RTRIM(COALESCE(c.tss_consignment_ref, ''))), '') IS NOT NULL
                     AND UPPER(COALESCE(c.sub_status, '')) NOT IN ('SUBMITTED', 'COMPLETED', 'CANCELLED', 'DELETED')
                    THEN 1 ELSE 0
                END) AS consignments_to_submit,
            (
                SELECT COUNT(*)
                FROM STG.BKD_GoodsItems g
                INNER JOIN STG.BKD_ENS_Consignments cg
                  ON cg.ClientCode = g.ClientCode
                 AND cg.stg_consignment_id = g.stg_consignment_id
                WHERE cg.ClientCode = c.ClientCode
                  AND cg.stg_header_id = c.stg_header_id
                  AND UPPER(COALESCE(cg.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
                  AND UPPER(COALESCE(g.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
                  AND NULLIF(LTRIM(RTRIM(COALESCE(g.tss_hex_id, ''))), '') IS NULL
                  {pending_goods_scope_where}
            ) AS missing_goods_refs
        FROM STG.BKD_ENS_Consignments c
        WHERE c.ClientCode = ?
          AND c.stg_header_id = ?
          AND UPPER(COALESCE(c.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
          {pending_scope_where}
        GROUP BY c.ClientCode, c.stg_header_id
        """,
        pending_goods_scope_params + [client_code, staging_id] + pending_scope_params,
    ) or {}
    summary['pending_consignment_refs'] = int(pending.get('missing_consignment_refs') or 0)
    summary['pending_goods_refs'] = int(pending.get('missing_goods_refs') or 0)
    summary['pending_consignment_submits'] = int(pending.get('consignments_to_submit') or 0)
    if summary['pending_goods_refs']:
        summary['messages'].append(f"{summary['pending_goods_refs']} goods item(s) still need TSS goods references.")
    if summary['pending_consignment_submits']:
        summary['messages'].append(f"{summary['pending_consignment_submits']} consignment(s) still need cargo submit.")
    summary['ok'] = (summary['cons_created'] + summary['cons_submitted'] + summary['goods_created']) > 0 and not (
        summary['cons_failed'] or summary['goods_failed'] or summary['cons_deferred']
        or summary['pending_consignment_refs'] or summary['pending_goods_refs'] or summary['pending_consignment_submits']
    )
    return summary


def _normalise_prd_header_form_value(field, raw_value):
    value = str(raw_value or '').strip()
    if not value:
        return None
    if field == 'arrival_date_time':
        dt = _parse_arrival_datetime(value)
        return dt.strftime('%d/%m/%Y %H:%M:%S') if dt else value
    return value


def _prd_header_form_payload(form):
    return {
        field: _normalise_prd_header_form_value(field, form.get(field))
        for field in PRD_ENS_HEADER_EDIT_FIELDS
    }


@declarations_bp.route('/header/<int:staging_id>/edit', methods=['GET', 'POST'])
def edit_header(staging_id):
    client_code = (get_tenant().get('code') or 'BKD').upper()
    if request.method != 'POST':
        return redirect(url_for('declarations.header_detail', staging_id=staging_id))

    header = query_one(
        """
        SELECT h.stg_header_id, h.tss_ens_header_ref, t.TssStatus
        FROM STG.BKD_ENS_Headers
        LEFT JOIN TSS.BKD_ENS_Headers t
          ON t.ClientCode = h.ClientCode
         AND t.DeclarationNumber = h.tss_ens_header_ref
        WHERE h.ClientCode = ? AND h.stg_header_id = ?
        """,
        [client_code, staging_id],
    )
    if not header:
        flash('ENS header not found.', 'warning')
        return redirect(url_for('declarations.list_declarations'))
    if not _stg_header_allows_edit(header):
        flash('This ENS header cannot be edited because its TSS status is no longer Draft / Trader Input Required.', 'warning')
        return redirect(url_for('declarations.header_detail', staging_id=staging_id))

    payload = _prd_header_form_payload(request.form)
    assignments = [f'[{field}] = ?' for field in PRD_ENS_HEADER_EDIT_FIELDS]
    params = [payload[field] for field in PRD_ENS_HEADER_EDIT_FIELDS]
    assignments.extend([
        '[validation_errors_json] = NULL',
        "[sub_status] = 'PENDING'",
        'last_sub_status_change = SYSUTCDATETIME()',
        'updated_at = SYSUTCDATETIME()',
    ])
    updated = execute(
        f"""
        UPDATE STG.BKD_ENS_Headers
        SET {', '.join(assignments)}
        WHERE ClientCode = ? AND stg_header_id = ?
        """,
        params + [client_code, staging_id],
    )
    if updated:
        flash('ENS header updated.', 'success')
    else:
        flash('ENS header was not updated.', 'warning')
    return redirect(url_for('declarations.header_detail', staging_id=staging_id))
def _update_pipeline_header_fields(staging_id, values):
    columns = _bkd_table_columns('StagingEnsHeaders')
    assignments = []
    params = []
    existing_values = _existing_column_values(
        [
            (field, value)
            for field, value in _expand_ens_header_values_for_table('StagingEnsHeaders', values.items())
        ],
        columns,
    )
    for field, value in existing_values:
        assignments.append(f'[{field}] = ?')
        params.append(value)
    if 'updated_at' in columns:
        assignments.append('[updated_at] = SYSUTCDATETIME()')
    if not assignments:
        return
    execute(
        f"UPDATE BKD.StagingEnsHeaders SET {', '.join(assignments)} WHERE staging_id = ?",
        params + [staging_id],
    )


def _short_validation_message(errors, limit=2):
    items = [str(error).strip() for error in (errors or []) if str(error or '').strip()]
    if not items:
        return ''
    shown = items[:limit]
    extra = len(items) - len(shown)
    message = '; '.join(shown)
    if extra > 0:
        message = f'{message}; +{extra} more'
    return message


def _log_local_ens_validation_event(staging_id, record_type, status, message, payload=None, errors=None):
    """Persist local ENS validation attempts so the Technical link has detail."""
    try:
        detail = message
        if errors:
            detail = '\n'.join(str(error) for error in errors)
        _log_tss_api_exchange(
            staging_id=staging_id,
            call_type='LOCAL_VALIDATE_ENS_HEADER',
            http_method='LOCAL',
            url=f'local://{record_type}',
            request_payload=payload or {},
            http_status=0,
            response_status=status,
            response_message=str(message or '')[:500],
            response_json={'record_type': record_type, 'errors': errors or []},
            duration_ms=0,
            error_detail=str(detail or '')[:2000],
        )
    except Exception:
        pass


def _validate_pipeline_header_record(staging_id):
    pipeline_header = query_one("SELECT * FROM BKD.StagingEnsHeaders WHERE staging_id = ?", [staging_id])
    if not pipeline_header:
        return {'ok': False, 'message': f'ENS draft header #{staging_id} not found.', 'category': 'warning'}

    if not _pipeline_header_is_locally_editable(pipeline_header):
        return {
            'ok': False,
            'message': 'This ENS Header is not in an editable local/draft repair state.',
            'category': 'warning',
        }

    payload = _payload_from_pipeline_header(pipeline_header)
    try:
        from app.ens_validation import load_choice_values, validate_ens_payload

        with db_cursor() as cursor:
            cv = load_choice_values(cursor)
            errors = validate_ens_payload(payload, cv)
            auto_fixes = _apply_ens_auto_fixes(payload, errors, cv)
            if auto_fixes:
                _update_pipeline_header_fields(
                    staging_id,
                    {fix['field']: payload.get(fix['field'], '') for fix in auto_fixes},
                )
                errors = validate_ens_payload(payload, cv)
    except Exception as exc:
        message = f'Technical validation error: {exc}'
        _update_pipeline_header_fields(staging_id, {
            'status': 'VALIDATION_ERROR',
            'error_message': message[:4000],
        })
        _log_local_ens_validation_event(staging_id, 'StagingEnsHeaders', 'technical_error', message, payload, [message])
        return {
            'ok': False,
            'message': message,
            'category': 'danger',
            'technical_url': url_for('technical.index', tab='api'),
        }

    if errors:
        message = ' | '.join(errors)[:4000]
        _update_pipeline_header_fields(staging_id, {
            'status': 'VALIDATION_ERROR',
            'error_message': message,
        })
        _log_local_ens_validation_event(staging_id, 'StagingEnsHeaders', 'validation_error', message, payload, errors)
        return {
            'ok': False,
            'message': f'ENS Header validation failed with {len(errors)} issue(s): {_short_validation_message(errors)}',
            'category': 'danger',
            'errors': errors,
            'technical_url': url_for('technical.index', tab='api'),
        }

    _update_pipeline_header_fields(staging_id, {
        'status': 'VALIDATED',
        'error_message': None,
    })
    _log_local_ens_validation_event(staging_id, 'StagingEnsHeaders', 'validated', 'ENS Header validated successfully.', payload, [])
    return {'ok': True, 'message': 'ENS Header validated successfully.', 'category': 'success'}


def _submit_pipeline_header(staging_id, *, create_linked_consignments=True, api_factory=None):
    pipeline_header = query_one("SELECT * FROM BKD.StagingEnsHeaders WHERE staging_id = ?", [staging_id])
    if not pipeline_header:
        return {'ok': False, 'message': f'ENS draft header #{staging_id} not found.', 'category': 'warning'}

    if not _pipeline_header_is_submittable(pipeline_header):
        status = pipeline_header.get('status') or 'unknown'
        return {'ok': False, 'message': f'Cannot submit ENS Header - status is {status!r}.', 'category': 'warning'}

    payload = _payload_from_pipeline_header(pipeline_header)
    ext_ref = (pipeline_header.get('ens_reference') or '').strip()

    try:
        if api_factory is None:
            from app.tss_api import build_cfg_client
            api_factory = build_cfg_client
        api = api_factory()

        if ext_ref.startswith('ENS'):
            result = api.update_header(ext_ref, payload)
            op = 'UPDATE_HEADER'
        else:
            result = api.create_header(payload)
            op = 'CREATE_HEADER'

        _log_tss_api_exchange(
            staging_id=staging_id,
            call_type=op,
            http_method='POST',
            url=result.get('url', ''),
            request_payload=payload,
            http_status=result.get('http_status', 0),
            response_status=result.get('status', ''),
            response_message=str(result.get('message') or '')[:500],
            response_json=str(result.get('raw_response') or '')[:4000],
            duration_ms=result.get('duration_ms', 0),
            error_detail='' if result.get('success') else str(result.get('message') or '')[:2000],
        )

        if not result.get('success'):
            error_msg = result.get('message') or 'TSS submission failed.'
            _update_pipeline_header_fields(staging_id, {
                'status': 'SUBMIT_ERROR',
                'error_message': error_msg[:4000],
            })
            return {'ok': False, 'message': f'Submit failed: {error_msg}', 'category': 'danger'}

        new_ref = result.get('reference') or ext_ref
        update_values = {
            'status': 'SUBMITTED',
            'tss_status': result.get('status', ''),
            'ens_reference': new_ref,
            'error_message': None,
        }
        if 'submitted_at' in _bkd_table_columns('StagingEnsHeaders'):
            update_values['submitted_at'] = datetime.utcnow()
        _update_pipeline_header_fields(staging_id, update_values)

        cons_summary = None
        if create_linked_consignments and new_ref:
            cons_summary = _create_validated_consignments_for_header(api, staging_id, new_ref)

        return {
            'ok': True,
            'reference': new_ref,
            'staging_ens_id': staging_id,
            'message': f'ENS Header submitted to TSS as {new_ref}.',
            'category': 'success',
            'cons_summary': cons_summary,
        }
    except Exception as exc:
        return {'ok': False, 'message': f'Submit error: {exc}', 'category': 'danger'}


@declarations_bp.route('/header/<int:staging_id>/validate', methods=['POST'])
def validate_header(staging_id):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/header/<int:staging_id>/submit-to-tss', methods=['POST'])
def submit_header(staging_id):
    client_code = (get_tenant().get('code') or 'BKD').upper()
    try:
        from app.blueprints.ingest.routes import _auto_validate_and_submit_stg_ens_header
        result = _auto_validate_and_submit_stg_ens_header(staging_id, tenant_code=client_code)
    except Exception as exc:
        flash(f'Create ENS in TSS failed: {exc}', 'danger')
        return redirect(url_for('declarations.header_detail', staging_id=staging_id))

    if result.get('ok'):
        reference = result.get('reference')
        if reference:
            flash(f'ENS created in TSS as {reference}.', 'success')
        else:
            flash(result.get('message') or 'ENS is already present in TSS.', 'success')
        return redirect(url_for('declarations.header_detail', staging_id=staging_id))

    if result.get('stage') == 'validation':
        errors = result.get('errors') or []
        message = _short_validation_message(errors) or result.get('message') or 'ENS header failed validation.'
        flash(f'ENS was not sent to TSS: {message}', 'danger')
    else:
        flash(f"Create ENS in TSS failed: {result.get('message') or 'Unknown error'}", 'danger')
    return redirect(url_for('declarations.header_detail', staging_id=staging_id))


@declarations_bp.route('/header/<int:staging_id>/submit-cargo-to-tss', methods=['POST'])
def submit_header_cargo(staging_id):
    client_code = (get_tenant().get('code') or 'BKD').upper()
    scope_ids = _prd_consignment_scope_ids(request.form.getlist('scope_consignment_ids'))
    _apply_prd_scoped_generate_sd(
        staging_id,
        client_code=client_code,
        scope_ids=scope_ids,
        generate_sd=_generate_sd_from_form(request.form),
    )
    try:
        summary = _submit_prd_cargo_for_header(
            staging_id,
            client_code=client_code,
            scope_consignment_ids=scope_ids,
        )
    except Exception as exc:
        flash(f'Create consignments/goods in TSS failed: {exc}', 'danger')
        return redirect(url_for('declarations.header_detail', staging_id=staging_id))

    if summary.get('ok'):
        flash(
            (
                f"Cargo sent to TSS: {summary.get('cons_created', 0)} DEC created, "
                f"{summary.get('goods_created', 0)} goods created, "
                f"{summary.get('cons_submitted', 0)} DEC submitted."
            ),
            'success',
        )
    else:
        detail = '; '.join(summary.get('messages') or [])[:400]
        flash(detail or summary.get('message') or 'No eligible consignments/goods were sent to TSS.', 'warning')
    return redirect(url_for('declarations.header_detail', staging_id=staging_id))


@declarations_bp.route('/header/<int:staging_id>/queue-cargo-to-tss', methods=['POST'])
def queue_header_cargo(staging_id):
    client_code = (get_tenant().get('code') or 'BKD').upper()
    env_code = 'PRD'
    try:
        from app.blueprints.ingest.routes import _start_prd_cargo_auto_submit_worker
        result = _start_prd_cargo_auto_submit_worker(
            staging_id,
            tenant_code=client_code,
            env_code=env_code,
            subject=f'Manual ENS detail cargo submit #{staging_id}',
            filename='ENS detail',
        )
    except Exception as exc:
        flash(f'Create DECs in TSS could not be queued: {exc}', 'danger')
        return redirect(url_for('declarations.header_detail', staging_id=staging_id))

    if result.get('queued'):
        flash(
            'DEC/goods creation has started in the background. Use Activity Logs or Sync TSS Now to monitor progress.',
            'info',
        )
    else:
        flash(result.get('message') or 'Create DECs in TSS was not queued.', 'warning')
    return redirect(url_for('declarations.header_detail', staging_id=staging_id))


@declarations_bp.route('/header/<int:staging_id>/sync-tss', methods=['POST'])
def sync_header_tss(staging_id):
    client_code = (get_tenant().get('code') or 'BKD').upper()
    fallback_url = url_for('declarations.header_detail', staging_id=staging_id)
    next_url = (request.form.get('next_url') or '').strip()
    if not next_url.startswith('/') or next_url.startswith('//'):
        next_url = fallback_url
    try:
        summary = _sync_prd_ens_status_once(staging_id, client_code=client_code)
    except Exception as exc:
        flash(f'TSS sync failed: {exc}', 'danger')
        return redirect(next_url)

    if summary.get('ok'):
        flash(
            (
                f"TSS sync complete: header={'yes' if summary.get('header_synced') else 'no'}, "
                f"consignments={summary.get('consignments_synced', 0)}, "
                f"SFD={summary.get('sfd_synced', 0)}."
            ),
            'success',
        )
    else:
        flash(summary.get('message') or 'No TSS status was synced.', 'warning')
    return redirect(next_url)


@declarations_bp.route('/sync-visible-tss', methods=['POST'])
def sync_visible_headers_tss():
    client_code = (get_tenant().get('code') or 'BKD').upper()
    redirect_args = {
        'status': request.form.get('status') or None,
        'q': request.form.get('q') or None,
        'sort': request.form.get('sort') or None,
        'page': request.form.get('page') or None,
    }
    redirect_args = {key: value for key, value in redirect_args.items() if value}
    header_ids = _prd_ens_sync_candidate_ids(
        client_code=client_code,
        status_filter=redirect_args.get('status') or 'ALL',
        search=redirect_args.get('q') or '',
        limit=25,
    )
    if not header_ids:
        flash('No visible ENS headers with a TSS reference are available to sync.', 'warning')
        return redirect(url_for('declarations.list_declarations', **redirect_args))

    synced = failed = sfd_synced = 0
    messages = []
    for header_id in header_ids:
        try:
            summary = _sync_prd_ens_status_once(header_id, client_code=client_code)
        except Exception as exc:
            failed += 1
            messages.append(f'ENS #{header_id}: {exc}')
            continue
        if summary.get('ok'):
            synced += 1
            sfd_synced += int(summary.get('sfd_synced') or 0)
        else:
            failed += 1
            messages.append(f"ENS #{header_id}: {summary.get('message') or 'sync failed'}")

    flash(
        f'TSS sync complete: {synced} ENS refreshed, {sfd_synced} SFD/MRN record(s) updated, {failed} failed.',
        'success' if failed == 0 else 'warning',
    )
    if messages:
        flash('; '.join(messages)[:500], 'warning')
    return redirect(url_for('declarations.list_declarations', **redirect_args))
def _build_ens_pack_context(staging_id):
    """Load the PRD STG/TSS data the ENS movement pack template needs."""
    pipeline_header = query_one(
        """
        SELECT
            h.stg_header_id AS staging_id,
            h.tss_ens_header_ref AS ens_reference,
            h.sub_status AS status,
            t.TssStatus AS tss_status,
            h.stg_created_at AS created_at,
            h.updated_at,
            h.movement_type,
            h.type_of_passive_transport,
            h.identity_no_of_transport,
            h.nationality_of_transport,
            h.conveyance_ref,
            h.arrival_date_time,
            h.arrival_port,
            h.place_of_loading,
            h.place_of_unloading,
            h.place_of_acceptance_same_as_loading,
            h.place_of_acceptance,
            h.place_of_delivery_same_as_unloading,
            h.place_of_delivery,
            h.seal_number,
            h.transport_charges,
            h.carrier_eori,
            h.carrier_name,
            h.carrier_street_number,
            h.carrier_city,
            h.carrier_postcode,
            h.carrier_country,
            h.haulier_eori,
            h.source,
            h.label
        FROM STG.BKD_ENS_Headers h
        LEFT JOIN TSS.BKD_ENS_Headers t
          ON t.ClientCode = h.ClientCode
         AND t.DeclarationNumber = h.tss_ens_header_ref
        WHERE h.ClientCode = ? AND h.stg_header_id = ?
        """,
        [(get_tenant().get('code') or 'BKD').upper(), staging_id],
    )
    if not pipeline_header:
        raise RuntimeError(f'ENS header #{staging_id} not found.')

    consignments = query_all(
        """
        SELECT
            c.stg_consignment_id AS staging_id,
            c.tss_consignment_ref AS dec_reference,
            c.sub_status AS status,
            tc.TssStatus AS tss_status,
            c.goods_description,
            c.trader_reference,
            c.transport_document_number,
            c.importer_eori,
            c.importer_name,
            c.exporter_eori,
            exporter_company.party_name AS exporter_name,
            c.consignor_eori,
            COALESCE(NULLIF(LTRIM(RTRIM(c.consignor_name)), ''), consignor_company.party_name) AS consignor_name,
            c.consignor_country,
            c.consignee_eori,
            c.consignee_name,
            c.consignee_country,
            COALESCE(sfd_track.tss_sfd_number, tss_sfd.SfdReference) AS sfd_reference,
            COALESCE(
                NULLIF(LTRIM(RTRIM(sfd_track.tss_movement_reference_number)), ''),
                NULLIF(LTRIM(RTRIM(tss_sfd.MovementReferenceNumber)), ''),
                CASE WHEN ISJSON(tc.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(tc.RawJson, '$.movement_reference_number'))), '') END,
                CASE WHEN ISJSON(tc.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(tc.RawJson, '$.movementReferenceNumber'))), '') END,
                CASE WHEN ISJSON(tc.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(tc.RawJson, '$.mrn'))), '') END
            ) AS movement_reference_number,
            COALESCE(
                NULLIF(LTRIM(RTRIM(sfd_track.tss_movement_reference_number)), ''),
                NULLIF(LTRIM(RTRIM(tss_sfd.MovementReferenceNumber)), ''),
                CASE WHEN ISJSON(tc.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(tc.RawJson, '$.movement_reference_number'))), '') END,
                CASE WHEN ISJSON(tc.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(tc.RawJson, '$.movementReferenceNumber'))), '') END,
                CASE WHEN ISJSON(tc.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(tc.RawJson, '$.mrn'))), '') END
            ) AS sfd_mrn,
            sfd_track.tss_eori_for_eidr AS sfd_eidr,
            SUM(TRY_CONVERT(decimal(18, 3), g.number_of_packages)) AS total_packages,
            SUM(TRY_CONVERT(decimal(18, 3), g.gross_mass_kg)) AS gross_mass_kg
        FROM STG.BKD_ENS_Consignments c
        LEFT JOIN STG.BKD_GoodsItems g
          ON g.ClientCode = c.ClientCode
         AND g.stg_consignment_id = c.stg_consignment_id
        LEFT JOIN TSS.BKD_ENS_Consignments tc
          ON tc.ClientCode = c.ClientCode
         AND tc.ConsignmentReference = c.tss_consignment_ref
        OUTER APPLY (
            SELECT TOP 1
                COALESCE(
                    NULLIF(LTRIM(RTRIM(cm.trading_name)), ''),
                    NULLIF(LTRIM(RTRIM(cm.company_name)), '')
                ) AS party_name
            FROM BKD.CompanyMaster cm
            WHERE NULLIF(LTRIM(RTRIM(COALESCE(c.exporter_eori, ''))), '') IS NOT NULL
              AND UPPER(LTRIM(RTRIM(c.exporter_eori))) IN (
                    UPPER(LTRIM(RTRIM(COALESCE(cm.eori_xi, '')))),
                    UPPER(LTRIM(RTRIM(COALESCE(cm.eori_gb, ''))))
              )
            ORDER BY cm.id
        ) exporter_company
        OUTER APPLY (
            SELECT TOP 1
                COALESCE(
                    NULLIF(LTRIM(RTRIM(cm.trading_name)), ''),
                    NULLIF(LTRIM(RTRIM(cm.company_name)), '')
                ) AS party_name
            FROM BKD.CompanyMaster cm
            WHERE NULLIF(LTRIM(RTRIM(COALESCE(c.consignor_eori, ''))), '') IS NOT NULL
              AND UPPER(LTRIM(RTRIM(c.consignor_eori))) IN (
                    UPPER(LTRIM(RTRIM(COALESCE(cm.eori_xi, '')))),
                    UPPER(LTRIM(RTRIM(COALESCE(cm.eori_gb, ''))))
              )
            ORDER BY cm.id
        ) consignor_company
        OUTER APPLY (
            SELECT TOP 1
                t.tss_sfd_number,
                t.tss_movement_reference_number,
                t.tss_eori_for_eidr
            FROM STG.BKD_SFD_Tracking t
            WHERE t.ClientCode = c.ClientCode
              AND t.tss_consignment_ref = c.tss_consignment_ref
            ORDER BY t.stg_polled_at DESC, t.stg_tracking_id DESC
        ) sfd_track
        OUTER APPLY (
            SELECT TOP 1
                s.SfdReference,
                s.MovementReferenceNumber,
                s.TssStatus
            FROM TSS.BKD_SFD s
            WHERE s.ClientCode = c.ClientCode
              AND (
                    s.DeclarationNumber = c.tss_consignment_ref
                 OR (
                        NULLIF(LTRIM(RTRIM(COALESCE(s.DeclarationNumber, ''))), '') IS NULL
                    AND s.EnsReference = c.tss_ens_header_ref
                    )
              )
            ORDER BY
                CASE WHEN s.DeclarationNumber = c.tss_consignment_ref THEN 0 ELSE 1 END,
                COALESCE(s.UpdatedAt, s.LastSyncedAt, s.CreatedAt) DESC
        ) tss_sfd
        WHERE c.ClientCode = ? AND c.stg_header_id = ?
        GROUP BY
            c.stg_consignment_id, c.tss_consignment_ref, c.sub_status, tc.TssStatus,
            c.goods_description, c.trader_reference, c.transport_document_number,
            c.importer_eori, c.importer_name, c.exporter_eori, exporter_company.party_name,
            c.consignor_eori, c.consignor_name, consignor_company.party_name, c.consignor_country,
            c.consignee_eori, c.consignee_name, c.consignee_country,
            sfd_track.tss_sfd_number, tss_sfd.SfdReference,
            sfd_track.tss_movement_reference_number, sfd_track.tss_eori_for_eidr, tss_sfd.MovementReferenceNumber,
            tc.RawJson
        ORDER BY c.stg_consignment_id
        """,
        [(get_tenant().get('code') or 'BKD').upper(), staging_id],
    )

    goods_rows = query_all(
        """
        SELECT
            c.stg_consignment_id AS cons_staging_id,
            g.stg_item_id AS staging_id,
            g.item_seq AS item_number,
            g.goods_description,
            g.commodity_code,
            g.number_of_packages,
            g.type_of_packages,
            g.gross_mass_kg,
            g.net_mass_kg,
            g.sku
        FROM STG.BKD_GoodsItems g
        JOIN STG.BKD_ENS_Consignments c
          ON c.ClientCode = g.ClientCode
         AND c.stg_consignment_id = g.stg_consignment_id
        WHERE g.ClientCode = ? AND c.stg_header_id = ?
        ORDER BY c.stg_consignment_id, g.item_seq, g.stg_item_id
        """,
        [(get_tenant().get('code') or 'BKD').upper(), staging_id],
    )

    goods_by_cons = {}
    for item in goods_rows or []:
        goods_by_cons.setdefault(item.get('cons_staging_id'), []).append(item)

    client_code = (get_tenant().get('code') or 'BKD').upper()
    prd_sdi_links = load_prd_sdi_links_for_context(
        client_code=client_code,
        consignment_ids=[row.get('staging_id') for row in consignments],
        consignment_refs=[
            value
            for row in consignments
            for value in (
                row.get('dec_reference'),
                row.get('trader_reference'),
                row.get('transport_document_number'),
            )
        ],
        sfd_refs=[row.get('sfd_reference') for row in consignments],
    )
    attach_sdi_links_to_consignments(consignments, prd_sdi_links)

    dec = _dec_from_pipeline_header(pipeline_header)
    return (
        pipeline_header,
        dec,
        consignments,
        goods_by_cons,
        _can_email_ens_pack(dec, pipeline_header, consignments),
    )


def _ens_pack_truthy(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on', 'include'}


def _ens_pack_consignment_is_cancelled(consignment):
    if not consignment:
        return False
    for key in ('status', 'tss_status'):
        status = str(consignment.get(key) or '').strip().upper().replace('_', ' ')
        if status in {'CANCELLED', 'CANCELED', 'DELETED'}:
            return True
    return False


def _filter_ens_pack_cancelled_consignments(consignments, goods_by_cons, *, include_cancelled=False):
    rows = list(consignments or [])
    goods_by_cons = dict(goods_by_cons or {})
    if include_cancelled:
        return rows, goods_by_cons, 0

    included = [row for row in rows if not _ens_pack_consignment_is_cancelled(row)]
    included_ids = {row.get('staging_id') for row in included}
    filtered_goods = {
        cons_id: goods
        for cons_id, goods in goods_by_cons.items()
        if cons_id in included_ids
    }
    return included, filtered_goods, len(rows) - len(included)


ENS_PACK_LOGO_CID = 'fusion_logo'
ENS_PACK_SYNOVIA_LOGO_CID = 'synovia_logo'
ENS_PACK_TENANT_LOGO_CID = 'tenant_logo'

TENANT_LOGO_FILENAMES = {
    'BKD': 'img/birkdale.png',
    'CWF': 'img/countrywide.png',
    'CLR': 'img/claritycargologo.png',
    'PLE': 'img/primeline-express.png',
    'SYD': 'img/synovia_logo.jpg',
}


def _ens_pack_logo_path():
    return os.path.join(_PROJECT_ROOT, 'app', 'static', 'img', 'fusion_logo.jpg')


def _ens_pack_synovia_logo_path():
    return os.path.join(_PROJECT_ROOT, 'app', 'static', 'img', 'synovia_logo.jpg')


def _ens_pack_tenant_label():
    tenant = get_tenant() or {}
    return str(tenant.get('name') or tenant.get('code') or '').strip()


def _ens_pack_tenant_logo_path():
    tenant = get_tenant() or {}
    tenant_code = str(tenant.get('code') or '').strip().upper()
    configured = ''
    try:
        from app.config_store import cfg
        configured = (cfg.get('BRAND', 'LOGO_PATH', tenant_code=tenant_code) or '').strip()
    except Exception:
        configured = ''

    rel = configured
    if rel.startswith('/static/'):
        rel = rel[len('/static/'):]
    elif rel.startswith('static/'):
        rel = rel[len('static/'):]
    elif rel.startswith('/'):
        rel = ''
    rel = rel or TENANT_LOGO_FILENAMES.get(tenant_code, '')
    rel = rel.replace('\\', '/').lstrip('/')
    if not rel.startswith('img/') or '..' in rel.split('/'):
        return ''
    return os.path.join(_PROJECT_ROOT, 'app', 'static', *rel.split('/'))


def _build_logo_uri(path, cid, logo_mode):
    """Return either a cid: reference or a data: URI for an embedded logo,
    or '' when the file is missing. The same helper is used for both the
    Fusion header logo and the Synovia Digital footer logo."""
    if logo_mode == 'none':
        return ''
    if logo_mode == 'cid':
        return f'cid:{cid}' if os.path.exists(path) else ''
    try:
        from base64 import b64encode
        with open(path, 'rb') as fh:
            return 'data:image/jpeg;base64,' + b64encode(fh.read()).decode('ascii')
    except Exception:
        return ''


def _ens_pack_subject(staging_id, pipeline_header):
    ens_ref = (pipeline_header or {}).get('ens_reference') or f'Draft #{staging_id}'
    tenant_label = _ens_pack_tenant_label()
    prefix = f"{tenant_label.upper()} " if tenant_label else ''
    return f"{prefix}ENS Movement Pack - {ens_ref}"


def _ens_pack_pdf_filename(subject):
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', ' ', str(subject or '').strip())
    base = re.sub(r'\s+', ' ', base).strip(' .')
    return f"{base or 'ENS Movement Pack'}.pdf"


def _render_ens_pack_print_html(pack_html, subject):
    filename = _ens_pack_pdf_filename(subject)
    title = filename[:-4] if filename.lower().endswith('.pdf') else filename
    controls = f"""
<style>
  @page {{ size: A4; margin: 10mm; }}
  html, body, body * {{
    -webkit-print-color-adjust: exact !important;
    print-color-adjust: exact !important;
  }}
  .fusion-print-toolbar {{
    position: sticky;
    top: 0;
    z-index: 9999;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    background: #0b1d3a;
    color: #ffffff;
    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.18);
    font-family: 'Segoe UI', Arial, sans-serif;
  }}
  .fusion-print-toolbar button,
  .fusion-print-toolbar a {{
    border: 1px solid rgba(255,255,255,0.28);
    border-radius: 4px;
    background: #ffffff;
    color: #0b1d3a;
    padding: 6px 10px;
    font-size: 13px;
    font-weight: 700;
    text-decoration: none;
    cursor: pointer;
  }}
  .fusion-print-toolbar span {{
    color: #cbd5e1;
    font-size: 12px;
  }}
  @media print {{
    .fusion-print-toolbar {{ display: none !important; }}
    body {{ background: #ffffff !important; }}
  }}
</style>
<div class="fusion-print-toolbar" data-no-print>
  <button type="button" onclick="window.print()">Print PDF</button>
  <a href="javascript:history.back()">Back</a>
  <span>Choose "Save as PDF" in the print dialog. Suggested filename: {filename}</span>
</div>
<script>
  document.title = {json.dumps(title)};
</script>
"""
    if re.search(r'<body\b[^>]*>', pack_html or '', flags=re.IGNORECASE):
        return re.sub(
            r'(<body\b[^>]*>)',
            r'\1' + controls,
            pack_html,
            count=1,
            flags=re.IGNORECASE,
        )
    if re.search(r'</body\s*>', pack_html or '', flags=re.IGNORECASE):
        return re.sub(r'</body\s*>', controls + '\n</body>', pack_html, count=1, flags=re.IGNORECASE)
    return (pack_html or '') + controls


def _render_ens_pack_body(pipeline_header, dec, consignments, goods_by_cons, note='', logo_mode='data'):
    """Render the inline-CSS HTML body used by both the preview and the email send.

    logo_mode controls how the embedded logos (Fusion header, Synovia Digital
    footer) are referenced inside the body:
      - 'data' (default): build data:image/jpeg;base64,... URIs so the
        preview iframe in the portal renders the logos without extra plumbing.
      - 'cid': reference cid:<id> so the email send path can attach the logos
        as inline MIME parts. Gmail / Outlook reject data: URIs in <img src>,
        so the email body must use this mode.
      - 'none': omit image references while keeping the pack layout.
    """
    logo_data_uri = _build_logo_uri(_ens_pack_logo_path(), ENS_PACK_LOGO_CID, logo_mode)
    synovia_logo_uri = _build_logo_uri(
        _ens_pack_synovia_logo_path(), ENS_PACK_SYNOVIA_LOGO_CID, logo_mode,
    )
    tenant_logo_uri = _build_logo_uri(
        _ens_pack_tenant_logo_path(), ENS_PACK_TENANT_LOGO_CID, logo_mode,
    )
    return render_template(
        'declarations/_email_pack_body.html',
        pipeline_header=pipeline_header,
        dec=dec,
        consignments=consignments,
        goods_by_cons=goods_by_cons or {},
        logo_data_uri=logo_data_uri,
        synovia_logo_uri=synovia_logo_uri,
        tenant_logo_uri=tenant_logo_uri,
        tenant_label=_ens_pack_tenant_label(),
        note=(note or '').strip(),
    )


@declarations_bp.route('/header/<int:staging_id>/email-pack')
def email_pack_preview(staging_id):
    try:
        pipeline_header, dec, consignments, goods_by_cons, can_email = _build_ens_pack_context(staging_id)
    except RuntimeError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('declarations.list_declarations'))

    include_cancelled = _ens_pack_truthy(request.args.get('include_cancelled'))
    consignments, goods_by_cons, excluded_cancelled_count = _filter_ens_pack_cancelled_consignments(
        consignments,
        goods_by_cons,
        include_cancelled=include_cancelled,
    )
    can_email = _can_email_ens_pack(dec, pipeline_header, consignments)
    if not can_email:
        if excluded_cancelled_count:
            flash(
                'The pack currently has no non-cancelled DEC consignments. Enable "Include cancelled consignments" '
                'if you need to send a historical/cancelled movement pack.',
                'warning',
            )
            return redirect(url_for(
                'declarations.email_pack_preview',
                staging_id=staging_id,
                include_cancelled='1',
            ))
        flash(
            'This ENS is not yet authorised for movement, or has no consignments with a DEC reference. '
            'The movement pack is only available once TSS authorises the ENS.',
            'warning',
        )
        return redirect(url_for('declarations.header_detail', staging_id=staging_id))

    pack_html = _render_ens_pack_body(pipeline_header, dec, consignments, goods_by_cons)
    suggested_subject = _ens_pack_subject(staging_id, pipeline_header)
    return render_template(
        'declarations/email_pack_preview.html',
        pipeline_header=pipeline_header,
        dec=dec,
        consignments=consignments,
        pack_html=pack_html,
        suggested_subject=suggested_subject,
        to_email=request.args.get('to_email', '').strip(),
        cc_email=request.args.get('cc_email', '').strip(),
        include_cancelled=include_cancelled,
        excluded_cancelled_count=excluded_cancelled_count,
    )
@declarations_bp.route('/header/<int:staging_id>/email-pack/pdf')
def email_pack_pdf(staging_id):
    try:
        pipeline_header, dec, consignments, goods_by_cons, can_email = _build_ens_pack_context(staging_id)
    except RuntimeError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('declarations.list_declarations'))

    include_cancelled = _ens_pack_truthy(request.args.get('include_cancelled'))
    consignments, goods_by_cons, excluded_cancelled_count = _filter_ens_pack_cancelled_consignments(
        consignments,
        goods_by_cons,
        include_cancelled=include_cancelled,
    )
    can_email = _can_email_ens_pack(dec, pipeline_header, consignments)
    if not can_email:
        if excluded_cancelled_count:
            flash(
                'The pack currently has no non-cancelled DEC consignments. Enable cancelled consignments before printing this pack.',
                'warning',
            )
            return redirect(url_for(
                'declarations.email_pack_preview',
                staging_id=staging_id,
                include_cancelled='1',
            ))
        flash(
            'This ENS is not yet authorised for movement, or has no consignments with a DEC reference. '
            'The movement pack PDF is only available once the email pack is available.',
            'warning',
        )
        return redirect(url_for('declarations.header_detail', staging_id=staging_id))

    subject = _ens_pack_subject(staging_id, pipeline_header)
    pack_html = _render_ens_pack_body(pipeline_header, dec, consignments, goods_by_cons)
    return Response(
        _render_ens_pack_print_html(pack_html, subject),
        mimetype='text/html',
    )
@declarations_bp.route('/header/<int:staging_id>/email-pack/send', methods=['POST'])
def email_pack_send(staging_id):
    try:
        pipeline_header, dec, consignments, goods_by_cons, can_email = _build_ens_pack_context(staging_id)
    except RuntimeError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('declarations.list_declarations'))

    include_cancelled = _ens_pack_truthy(request.form.get('include_cancelled'))
    consignments, goods_by_cons, excluded_cancelled_count = _filter_ens_pack_cancelled_consignments(
        consignments,
        goods_by_cons,
        include_cancelled=include_cancelled,
    )
    can_email = _can_email_ens_pack(dec, pipeline_header, consignments)
    if not can_email:
        if excluded_cancelled_count:
            flash(
                'No non-cancelled DEC consignments are available for this pack. Tick "Include cancelled consignments" '
                'if the cancelled records must be sent.',
                'warning',
            )
            return redirect(url_for(
                'declarations.email_pack_preview',
                staging_id=staging_id,
                include_cancelled='1',
            ))
        flash(
            'This ENS is not yet authorised for movement. The movement pack cannot be emailed yet.',
            'warning',
        )
        return redirect(url_for('declarations.header_detail', staging_id=staging_id))

    from app.email_utils import send_email, _log_email, _normalise_address_list
    to_raw = request.form.get('to_email') or ''
    cc_raw = request.form.get('cc_email') or ''
    to_list = _normalise_address_list(to_raw)
    cc_list = _normalise_address_list(cc_raw)
    if not to_list:
        flash('At least one recipient email address is required.', 'warning')
        return redirect(url_for(
            'declarations.email_pack_preview',
            staging_id=staging_id,
            to_email=to_raw.strip(),
            cc_email=cc_raw.strip(),
            include_cancelled='1' if include_cancelled else '0',
        ))

    subject = (request.form.get('subject') or '').strip() or _ens_pack_subject(staging_id, pipeline_header)
    note = (request.form.get('note') or '').strip()

    body_html = _render_ens_pack_body(
        pipeline_header, dec, consignments, goods_by_cons, note=note, logo_mode='cid',
    )
    ens_ref = pipeline_header.get('ens_reference') or f'Draft #{staging_id}'
    body_text_parts = [
        f'ENS Movement Pack - {ens_ref}',
        f'Consignments included: {len(consignments)}',
        'Open Fusion Flow for the full interactive view.',
    ]
    if note:
        body_text_parts += ['', 'Note from sender:', note]
    body_text = '\n'.join(body_text_parts) + '\n'

    inline_images = []
    logo_path = _ens_pack_logo_path()
    if os.path.exists(logo_path):
        inline_images.append((ENS_PACK_LOGO_CID, logo_path, 'jpeg'))
    synovia_logo_path = _ens_pack_synovia_logo_path()
    if os.path.exists(synovia_logo_path):
        inline_images.append((ENS_PACK_SYNOVIA_LOGO_CID, synovia_logo_path, 'jpeg'))
    tenant_logo_path = _ens_pack_tenant_logo_path()
    if os.path.exists(tenant_logo_path):
        tenant_ext = os.path.splitext(tenant_logo_path)[1].lower().lstrip('.') or 'png'
        inline_images.append((ENS_PACK_TENANT_LOGO_CID, tenant_logo_path, 'jpeg' if tenant_ext in {'jpg', 'jpeg'} else tenant_ext))

    ok, error = send_email(
        to_list, subject, body_html,
        body_text=body_text, cc_addresses=cc_list,
        inline_images=inline_images,
    )
    log_target = ', '.join(to_list + ([f'cc:{a}' for a in cc_list] if cc_list else []))
    _log_email(
        staging_id,
        'ENS',
        log_target,
        subject,
        ok,
        error,
        call_type='ENS_MOVEMENT_PACK_EMAIL',
    )

    if ok:
        recipients_label = ', '.join(to_list)
        if cc_list:
            recipients_label += f' (cc {", ".join(cc_list)})'
        excluded_note = f' {excluded_cancelled_count} cancelled consignment(s) excluded.' if excluded_cancelled_count else ''
        flash(f'ENS movement pack emailed to {recipients_label}.{excluded_note}', 'success')
        return redirect(url_for('declarations.header_detail', staging_id=staging_id))

    if error and 'SMTP credentials not configured' in error:
        flash(
            'SMTP is not configured. Set SMTP.SENDER_EMAIL and SMTP.SENDER_PASSWORD in Admin Settings before sending pack emails.',
            'danger',
        )
    elif error and 'SMTP_ENABLED' in error:
        flash(
            'SMTP is disabled (SMTP_ENABLED=false). Re-enable it in app config to send pack emails.',
            'warning',
        )
    else:
        flash(f'Email send failed: {error or "unknown error"}', 'danger')
    return redirect(url_for(
        'declarations.email_pack_preview',
        staging_id=staging_id,
        to_email=', '.join(to_list),
        cc_email=', '.join(cc_list),
        include_cancelled='1' if include_cancelled else '0',
    ))
@declarations_bp.route('/<int:dec_id>')
def detail(dec_id):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/<int:dec_id>/fix', methods=['POST'])
def fix_inline(dec_id):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/<int:dec_id>/edit', methods=['GET', 'POST'])
def edit(dec_id):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/<int:dec_id>/resubmit', methods=['POST'])
def resubmit(dec_id):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/run-validation', methods=['POST'])
def run_validation():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/<int:dec_id>/validate', methods=['POST'])
def validate_single(dec_id):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/run-submission', methods=['POST'])
def run_submission():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
def _parse_bulk_ens_ids(raw_ids):
    """Parse dec:N / hdr:N bulk-select values into (declaration_ids, header_ids)."""
    declaration_ids, header_ids = [], []
    for raw in raw_ids:
        value = str(raw or '').strip()
        prefix, _, tail = value.partition(':')
        kind = prefix.lower() if tail else 'dec'
        target = tail if tail else prefix
        try:
            pid = int(target)
        except (TypeError, ValueError):
            continue
        if kind in ('hdr', 'header', 'ens_header'):
            header_ids.append(pid)
        else:
            declaration_ids.append(pid)
    return sorted(set(declaration_ids)), sorted(set(header_ids))


def _do_validate_and_submit(legacy_rows, header_rows):
    """
    Validate then submit a set of ENS rows.
    Returns (validated, failed_val, submitted, failed_sub).
    """
    from app.ens_validation import load_choice_values, validate_ens_payload

    with db_cursor() as cursor:
        cv = load_choice_values(cursor)

    validated = failed_val = submitted = failed_sub = 0
    validated_legacy_ids = []
    validated_header_ids = []

    for row in legacy_rows:
        dec_id = row['id']
        try:
            payload = json.loads(row.get('payload_json') or '{}')
            errors = validate_ens_payload(payload, cv)
        except Exception:
            failed_val += 1
            continue
        if errors:
            execute(
                "UPDATE BKD.StagingDeclarations SET status='Validation_Error', error_message=?, updated_at=GETUTCDATE() WHERE id=?",
                [' | '.join(errors)[:4000], dec_id],
            )
            failed_val += 1
        else:
            execute(
                "UPDATE BKD.StagingDeclarations SET status='Validated', error_message=NULL, updated_at=GETUTCDATE() WHERE id=?",
                [dec_id],
            )
            validated_legacy_ids.append(dec_id)
            validated += 1

    for header in header_rows:
        staging_id = header['staging_id']
        payload = _payload_from_pipeline_header(header)
        try:
            errors = validate_ens_payload(payload, cv)
            auto_fixes = _apply_ens_auto_fixes(payload, errors, cv)
            if auto_fixes:
                _update_pipeline_header_fields(staging_id, {fix['field']: payload.get(fix['field'], '') for fix in auto_fixes})
                errors = validate_ens_payload(payload, cv)
        except Exception:
            failed_val += 1
            continue
        if errors:
            _update_pipeline_header_fields(staging_id, {'status': 'VALIDATION_ERROR', 'error_message': ' | '.join(errors)[:4000]})
            failed_val += 1
        else:
            _update_pipeline_header_fields(staging_id, {'status': 'VALIDATED', 'error_message': None})
            validated_header_ids.append(staging_id)
            validated += 1

    for dec_id in validated_legacy_ids:
        result = _submit_ens_declaration(dec_id, create_linked_consignments=True)
        if result.get('ok'):
            submitted += 1
        else:
            failed_sub += 1

    for staging_id in validated_header_ids:
        result = _submit_pipeline_header(staging_id, create_linked_consignments=True)
        if result.get('ok'):
            submitted += 1
        else:
            failed_sub += 1

    return validated, failed_val, submitted, failed_sub


@declarations_bp.route('/run-validate-and-submit', methods=['POST'])
def run_validate_and_submit():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/bulk-validate-submit-selected', methods=['POST'])
def bulk_validate_submit_selected():
    header_ids = []
    for raw in request.form.getlist('selected_ids'):
        try:
            header_ids.append(int(str(raw or '').strip()))
        except (TypeError, ValueError):
            continue
    header_ids = sorted(set(header_ids))
    redirect_args = {
        'status': request.form.get('status') or None,
        'q': request.form.get('q') or None,
        'sort': request.form.get('sort') or None,
    }
    redirect_args = {key: value for key, value in redirect_args.items() if value}
    if not header_ids:
        flash('Select at least one ENS header to validate and submit.', 'warning')
        return redirect(url_for('declarations.list_declarations', **redirect_args))

    client_code = (get_tenant().get('code') or 'BKD').upper()
    submitted = already_submitted = validation_failed = submit_failed = 0
    messages = []
    try:
        from app.blueprints.ingest.routes import _auto_validate_and_submit_stg_ens_header
        from app.ingestion.ens_status_watcher import start_ens_status_watcher
    except Exception as exc:
        flash(f'Validate + Submit is unavailable: {exc}', 'danger')
        return redirect(url_for('declarations.list_declarations', **redirect_args))

    for header_id in header_ids:
        try:
            result = _auto_validate_and_submit_stg_ens_header(header_id, tenant_code=client_code)
        except Exception as exc:
            submit_failed += 1
            messages.append(f'ENS #{header_id}: {exc}')
            continue

        if result.get('ok'):
            if result.get('stage') == 'already_submitted':
                already_submitted += 1
            else:
                submitted += 1
            try:
                start_ens_status_watcher(header_id, tenant_code=client_code)
            except Exception:
                current_app.logger.exception('Could not start ENS watcher for selected header %s', header_id)
        elif result.get('stage') == 'validation':
            validation_failed += 1
            errors = result.get('errors') or []
            messages.append(f"ENS #{header_id}: {_short_validation_message(errors) or result.get('message') or 'validation failed'}")
        else:
            submit_failed += 1
            messages.append(f"ENS #{header_id}: {result.get('message') or 'submit failed'}")

    if submitted or already_submitted:
        flash(
            (
                f'Validate + Submit complete: {submitted} submitted, '
                f'{already_submitted} already submitted, {validation_failed} validation failed, '
                f'{submit_failed} submit failed.'
            ),
            'success' if not (validation_failed or submit_failed) else 'warning',
        )
    else:
        flash('Validate + Submit did not submit any ENS headers.', 'danger')
    if messages:
        flash('; '.join(messages)[:500], 'warning')
    return redirect(url_for('declarations.list_declarations', **redirect_args))
@declarations_bp.route('/<int:dec_id>/submit-to-tss', methods=['POST'])
def submit_single(dec_id):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/<int:dec_id>/cancel', methods=['POST'])
def cancel(dec_id):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/<int:dec_id>/delete', methods=['POST'])
def delete(dec_id):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))
@declarations_bp.route('/bulk-delete-selected', methods=['POST'])
def bulk_delete_selected():
    raw_ids = request.form.getlist('selected_ids')
    header_ids = []
    for raw in raw_ids:
        try:
            header_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    header_ids = sorted(set(header_ids))
    if not header_ids:
        flash('Select at least one ENS header to delete.', 'warning')
        return redirect(url_for('declarations.list_declarations'))

    client_code = (get_tenant().get('code') or 'BKD').upper()
    placeholders = ','.join('?' for _ in header_ids)
    with db_cursor() as cursor:
        cursor.execute(
            f"""
            DELETE FROM STG.BKD_ENS_Headers
            WHERE ClientCode = ?
              AND stg_header_id IN ({placeholders})
              AND COALESCE(tss_ens_header_ref, '') = ''
            """,
            [client_code, *header_ids],
        )
        deleted = cursor.rowcount
    skipped = len(header_ids) - max(deleted, 0)
    if deleted:
        flash(f'Deleted {deleted} local ENS draft(s). Linked consignments/goods were removed by STG cascade.', 'success')
    if skipped:
        flash(f'{skipped} ENS header(s) were kept because they already have a TSS reference or are not local drafts.', 'warning')
    return redirect(url_for('declarations.list_declarations'))


@declarations_bp.route('/bulk-export-selected', methods=['POST'])
def bulk_export_selected():
    header_ids = []
    for raw in request.form.getlist('selected_ids'):
        try:
            header_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    header_ids = sorted(set(header_ids))
    if not header_ids:
        flash('Select at least one ENS header to export.', 'warning')
        return redirect(url_for('declarations.list_declarations'))

    client_code = (get_tenant().get('code') or 'BKD').upper()
    placeholders = ','.join('?' for _ in header_ids)
    rows = query_all(
        f"""
        SELECT
            h.stg_header_id, h.conveyance_ref, h.arrival_date_time, h.arrival_port,
            h.sub_status, h.tss_ens_header_ref, t.TssStatus, h.source,
            h.stg_created_at, h.updated_at
        FROM STG.BKD_ENS_Headers h
        LEFT JOIN TSS.BKD_ENS_Headers t
          ON t.ClientCode = h.ClientCode
         AND t.DeclarationNumber = h.tss_ens_header_ref
        WHERE h.ClientCode = ? AND h.stg_header_id IN ({placeholders})
        ORDER BY h.stg_header_id
        """,
        [client_code, *header_ids],
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        'stg_header_id', 'conveyance_ref', 'arrival_date_time', 'arrival_port',
        'sub_status', 'tss_ens_header_ref', 'TssStatus', 'source',
        'stg_created_at', 'updated_at',
    ])
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in writer.fieldnames})
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=ens_headers_selected.csv'},
    )
@declarations_bp.route('/ports')
def ports():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('ingest.queue'))


@declarations_bp.route('/import-ens', methods=['GET', 'POST'])
def import_ens():
    """Import known TSS references into the PRD STG/TSS model."""
    created = updated = skipped = errors = 0
    created_by_kind = {'ENS': 0, 'Consignment': 0, 'SDI': 0}
    results = []

    if request.method == 'POST':
        refs = _split_tss_import_refs(request.form.get('references', ''))
        seen_refs = set()

        with db_cursor() as cursor:
            for ref in refs:
                try:
                    if ref in seen_refs:
                        results.append({
                            'ref': ref,
                            'kind': _tss_reference_kind(ref).title(),
                            'status': 'skipped',
                            'msg': 'Duplicate reference in this import batch.',
                        })
                        skipped += 1
                        continue
                    seen_refs.add(ref)

                    kind = _tss_reference_kind(ref)
                    if kind == 'ENS':
                        result = _import_tss_ens_full(cursor, ref)
                    elif kind == 'DEC':
                        result = _import_tss_consignment_stub(cursor, ref)
                    elif kind == 'SUP':
                        result = _import_tss_supdec_stub(cursor, ref)
                    else:
                        result = {
                            'kind': 'Unknown',
                            'status': 'error',
                            'msg': 'Reference must start with ENS, DEC or SUP.',
                        }

                    result['ref'] = ref
                    if kind == 'ENS' and result.get('staging_id'):
                        result['detail_url'] = url_for('declarations.header_detail', staging_id=result['staging_id'])
                    elif kind == 'DEC' and result.get('status') in {'created', 'updated'}:
                        result['detail_url'] = url_for('consignments.detail_by_ref', cons_ref=ref)
                    results.append(result)

                    if result['status'] == 'created':
                        created += 1
                        created_by_kind[result['kind']] = created_by_kind.get(result['kind'], 0) + 1
                    elif result['status'] == 'updated':
                        updated += 1
                    elif result['status'] == 'skipped':
                        skipped += 1
                    else:
                        errors += 1
                        _log_tss_import_result_error(cursor, result)
                except Exception as e:
                    result = {
                        'ref': ref,
                        'kind': _tss_reference_kind(ref).title(),
                        'status': 'error',
                        'msg': str(e),
                    }
                    results.append(result)
                    errors += 1
                    _log_tss_import_result_error(cursor, result)

        if created:
            summary = ', '.join(
                f'{count} {kind}'
                for kind, count in created_by_kind.items()
                if count
            )
            flash(
                f'{created} TSS reference(s) imported ({summary}). ENS references import headers only; paste known DEC refs for consignments and goods.',
                'success',
            )
        if updated:
            flash(f'{updated} existing TSS reference(s) refreshed from TSS.', 'success')
        if skipped:
            flash(f'{skipped} reference(s) already existed - skipped.', 'info')
        if errors:
            first_error = _first_import_error_summary(results)
            detail = f' First issue: {first_error}' if first_error else ''
            flash(f'{errors} import error(s) were logged to Technical Logs.{detail} Check results below.', 'danger')

        return render_template(
            'declarations/import_ens.html',
            results=results,
            created=created,
            updated=updated,
            skipped=skipped,
            errors=errors,
            created_by_kind=created_by_kind,
        )

    return render_template(
        'declarations/import_ens.html',
        results=results,
        created=created,
        updated=updated,
        skipped=skipped,
        errors=errors,
        created_by_kind=created_by_kind,
    )
