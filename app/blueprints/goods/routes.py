"""
Goods Item CRUD Blueprint — Fusion Flow V2 BKD Portal
Uses existing app.db module for database access.
Max 99 goods per consignment.
"""
import logging
from collections import namedtuple
import csv
import io
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, jsonify
from app.db import query_all, query_one, execute, db_cursor
from app.pipeline_validation import auto_validate_goods_record, normalise_package_type
from app.status_utils import (
    TSS_FILTER_STATUS_TABS,
    badge_class_for_status,
    canonical_filter_status,
    consignment_should_discover_sdi,
    effective_tss_filter_status,
    local_goods_status_after_parent_sync,
    normalize_status_key,
    status_display,
    status_filter_tabs,
    tss_allows_data_changes,
)
from app.tenant import get_tenant
from app.tss_api import TssApiClient, build_cfg_client
from app.tss_guidance import explain_tss_error
from app.tss_text import tss_unsafe_value_tip

CVOption = namedtuple('CVOption', ['value', 'name'])

log = logging.getLogger(__name__)

goods_bp = Blueprint('goods', __name__,
    template_folder='../../templates/goods',
    url_prefix='/goods')

S = 'BKD'

GOODS_EDIT_FIELD_META = {
    'cons_parent': {
        'label': 'Parent Consignment',
        'aliases': ['must link to a consignment', 'staging_cons_id', 'parent consignment', 'consignment'],
        'suggestion': 'Link this goods item to the correct consignment before rerunning the pipeline.',
    },
    'goods_description': {
        'label': 'Goods Description',
        'aliases': ['goods description', 'goods_description'],
        'suggestion': 'Use a clear commercial description, for example "Golf clubs" or "Cotton shirts".',
    },
    'type_of_packages': {
        'label': 'Type of Packages',
        'aliases': ['type of packages', 'type_of_packages'],
        'suggestion': 'Choose the package type from the official list used by TSS.',
    },
    'number_of_packages': {
        'label': 'Number of Packages',
        'aliases': ['number of packages', 'number_of_packages'],
        'suggestion': 'Enter a whole number greater than zero.',
    },
    'package_marks': {
        'label': 'Package Marks',
        'aliases': ['package marks', 'package_marks'],
        'suggestion': 'Enter marks or identifying text exactly as shown on the shipment.',
    },
    'gross_mass_kg': {
        'label': 'Gross Mass KG',
        'aliases': ['gross mass kg', 'gross_mass_kg', 'gross mass'],
        'suggestion': 'Use a numeric kilogram value greater than zero, with up to 2 decimal places, for example 500 or 500.12.',
    },
    'net_mass_kg': {
        'label': 'Net Mass KG',
        'aliases': ['net mass kg', 'net_mass_kg', 'net mass'],
        'suggestion': 'Use a numeric kilogram value with up to 2 decimal places, and make sure it does not exceed gross mass.',
    },
    'commodity_code': {
        'label': 'Commodity Code',
        'aliases': ['commodity code', 'commodity_code'],
        'suggestion': 'Use at least 8 digits from the tariff classification.',
    },
    'controlled_goods_type': {
        'label': 'Controlled Type',
        'aliases': ['controlled goods type', 'controlled_goods_type', 'controlled type'],
        'suggestion': 'If goods are controlled, choose the matching controlled goods type.',
    },
    'procedure_code': {
        'label': 'Procedure Code',
        'aliases': ['procedure code', 'procedure_code'],
        'suggestion': 'Choose the customs procedure code expected for this goods line.',
    },
    'additional_procedure_code': {
        'label': 'Additional Procedure',
        'aliases': ['additional procedure', 'additional_procedure_code'],
        'suggestion': 'Use an additional procedure code only when the movement needs one.',
    },
    'country_of_origin': {
        'label': 'Country of Origin',
        'aliases': ['country of origin', 'country_of_origin'],
        'suggestion': 'Choose the origin country from the official country list.',
    },
    'item_invoice_amount': {
        'label': 'Invoice Amount',
        'aliases': ['invoice amount', 'item_invoice_amount'],
        'suggestion': 'Use a numeric amount with up to 2 decimal places. Whole numbers will be formatted as 2 decimals, for example 1000.00.',
    },
    'item_invoice_currency': {
        'label': 'Currency',
        'aliases': ['currency', 'item_invoice_currency'],
        'suggestion': 'Choose GBP or EUR before validating or sending this goods item to TSS.',
    },
    'valuation_method': {
        'label': 'Valuation Method',
        'aliases': ['valuation method', 'valuation_method'],
        'suggestion': 'Choose a valuation method if this goods line requires one.',
    },
    'invoice_number': {
        'label': 'Invoice Number',
        'aliases': ['invoice number', 'invoice_number'],
        'suggestion': 'Use invoice reference exactly as it appears on the commercial document.',
    },
    'ni_additional_information_codes': {
        'label': 'NI Additional Info',
        'aliases': ['ni additional info', 'ni_additional_information_codes', 'additional information codes'],
        'suggestion': 'Use valid NI information codes such as NIDOM, NIREM, NIIMP, or NIAID when needed.',
    },
}

GOODS_TSS_CHARACTER_FIELDS = {
    'goods_description': 'Goods Description',
    'package_marks': 'Package Marks',
    'invoice_number': 'Invoice Number',
    'ni_additional_information_codes': 'NI Additional Info',
}

LOCAL_GOODS_EDITABLE_STATUSES = {
    'PENDING',
    'PENDING REVIEW',
    'FAILED',
    'INVALID',
    'VALIDATED',
}

LOCAL_CONSIGNMENT_GOODS_CREATE_STATUSES = LOCAL_GOODS_EDITABLE_STATUSES | {'CREATED', 'DRAFT'}
LOCAL_GOODS_DELETABLE_STATUSES = LOCAL_CONSIGNMENT_GOODS_CREATE_STATUSES

PRD_TSS_GOODS_EDIT_STATUSES = {'DRAFT', 'TRADER INPUT REQUIRED'}

def badge_class(status):
    return badge_class_for_status(status)


def _goods_status_tabs(counts, selected='ALL'):
    base = TSS_FILTER_STATUS_TABS
    return status_filter_tabs(counts, base, selected)


def _goods_display_status(status='', tss_status='', has_tss_ref=False):
    tss_status_key = normalize_status_key(tss_status)
    pending_sync = bool(
        has_tss_ref
        and (not tss_status_key or tss_status_key in {'PENDING SYNC', 'IMPORTED', 'SYNC PENDING'})
    )
    return effective_tss_filter_status(status, tss_status, pending_sync=pending_sync)


def _apply_parent_synced_goods_status(row, parent=None):
    if not row:
        return row
    parent = parent or row
    row['status'] = local_goods_status_after_parent_sync(
        row.get('status'),
        goods_tss_status=row.get('tss_status'),
        parent_local_status=parent.get('cons_local_status') or parent.get('parent_status') or parent.get('status'),
        parent_tss_status=parent.get('cons_tss_status') or parent.get('parent_tss_status') or parent.get('tss_status'),
    )
    return row


def _sql_status_key(alias, column):
    return f"UPPER(REPLACE(COALESCE(CAST({alias}.{column} AS NVARCHAR(100)), ''), '_', ' '))"


def _sql_has_value(alias, column):
    return f"NULLIF(LTRIM(RTRIM(COALESCE(CAST({alias}.{column} AS NVARCHAR(100)), ''))), '') IS NOT NULL"


def _sql_effective_status_expr(alias, *, local_col='status', tss_col='tss_status', ref_col=None):
    local = _sql_status_key(alias, local_col)
    remote = _sql_status_key(alias, tss_col)
    remote_has_value = _sql_has_value(alias, tss_col)
    if ref_col:
        pending_sync_when = (
            f"{_sql_has_value(alias, ref_col)} AND "
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


def _parent_consignment_tss_is_draft(item):
    return normalize_status_key(
        (item or {}).get('cons_tss_status')
        or (item or {}).get('parent_tss_status')
        or (item or {}).get('parent_tss')
    ) == 'DRAFT'


def _parent_consignment_tss_allows_prd_edit(item):
    return normalize_status_key(
        (item or {}).get('cons_tss_status')
        or (item or {}).get('parent_tss_status')
        or (item or {}).get('parent_tss')
    ) in PRD_TSS_GOODS_EDIT_STATUSES


def _prd_goods_allows_edit(item):
    if not item:
        return False
    has_goods_ref = bool(str((item or {}).get('tss_hex_id') or '').strip())
    has_cons_ref = bool(str((item or {}).get('tss_consignment_ref') or '').strip())
    local_repair = normalize_status_key((item or {}).get('sub_status') or (item or {}).get('status')) in LOCAL_GOODS_EDITABLE_STATUSES
    if has_goods_ref or has_cons_ref:
        return _parent_consignment_tss_allows_prd_edit(item)
    return local_repair


def _prd_goods_allows_local_delete(item):
    """Only local goods without a TSS Goods ID can be deleted from PRD views."""
    if not item:
        return False
    has_goods_ref = bool(str((item or {}).get('tss_hex_id') or (item or {}).get('goods_id') or '').strip())
    local_status = normalize_status_key((item or {}).get('sub_status') or (item or {}).get('status'))
    return local_status in LOCAL_GOODS_DELETABLE_STATUSES and not has_goods_ref


def _prd_goods_allows_local_failed_delete(item):
    """Backward-compatible alias for older template/tests wording."""
    return _prd_goods_allows_local_delete(item)


def _prd_consignment_allows_goods_create(parent):
    if not parent:
        return False
    has_cons_ref = bool(str((parent or {}).get('tss_consignment_ref') or '').strip())
    if has_cons_ref:
        return normalize_status_key((parent or {}).get('cons_tss_status') or (parent or {}).get('TssStatus')) in PRD_TSS_GOODS_EDIT_STATUSES
    return normalize_status_key((parent or {}).get('sub_status') or (parent or {}).get('status')) in LOCAL_CONSIGNMENT_GOODS_CREATE_STATUSES


def _safe_next_url(value, fallback):
    text = str(value or '').strip()
    if text.startswith('/') and not text.startswith('//') and '\\' not in text:
        return text
    return fallback


def _can_change_goods_data(item):
    if _parent_consignment_tss_is_draft(item):
        return True
    return tss_allows_data_changes((item or {}).get('tss_status'), (item or {}).get('status'))


def _can_edit_goods_locally(item):
    if _parent_consignment_tss_is_draft(item):
        return True
    return (
        normalize_status_key((item or {}).get('status')) in LOCAL_GOODS_EDITABLE_STATUSES
        and _can_change_goods_data(item)
    )


def _can_add_goods_to_parent(parent):
    return tss_allows_data_changes((parent or {}).get('tss_status'), (parent or {}).get('status'))


def _load_goods_with_parent(sid):
    return query_one(f"""
        SELECT g.*,
               c.status AS cons_local_status,
               c.tss_status AS cons_tss_status,
               c.dec_reference AS consignment_ref
        FROM {S}.StagingGoodsItems g
        LEFT JOIN {S}.StagingConsignments c ON c.staging_id = g.staging_cons_id
        WHERE g.staging_id=?
    """, [sid])


def _goods_local_update_where(item):
    if _parent_consignment_tss_is_draft(item):
        return "staging_id=?"
    return "staging_id=? AND status IN ('PENDING','PENDING_REVIEW','PENDING REVIEW','FAILED','INVALID','VALIDATED')"


def _goods_local_delete_where(item):
    if _parent_consignment_tss_is_draft(item):
        return "staging_id=?"
    return "staging_id=? AND status IN ('PENDING','PENDING_REVIEW','PENDING REVIEW','FAILED','INVALID')"


def _normalize_decimal_form_value(value):
    if value in (None, ''):
        return ''
    text = str(value).strip()
    if not text:
        return ''
    try:
        dec = Decimal(text).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return text
    normalized = format(dec.normalize(), 'f')
    if '.' in normalized:
        normalized = normalized.rstrip('0').rstrip('.')
    return normalized or '0'


def _normalize_currency_amount_form_value(value):
    if value in (None, ''):
        return ''
    text = str(value).strip()
    if not text:
        return ''
    try:
        dec = Decimal(text).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return text
    return format(dec, '.2f')


def _normalize_goods_form_data(values):
    form_data = dict(values or {})
    for key in ('gross_mass_kg', 'net_mass_kg'):
        if key in form_data:
            form_data[key] = _normalize_decimal_form_value(form_data.get(key))
    if 'item_invoice_amount' in form_data:
        form_data['item_invoice_amount'] = _normalize_currency_amount_form_value(form_data.get('item_invoice_amount'))
    return form_data


def _goods_character_field_suggestions(values):
    suggestions = {}
    for field in GOODS_TSS_CHARACTER_FIELDS:
        tip = tss_unsafe_value_tip(values.get(field))
        if tip:
            suggestions[field] = tip
    return suggestions


def _format_decimal_for_scale(value, scale=2):
    if value in (None, ''):
        return ''
    text = str(value).strip()
    if not text:
        return ''
    try:
        dec = Decimal(text).quantize(Decimal('1').scaleb(-scale), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return text
    normalized = format(dec.normalize(), 'f')
    if '.' in normalized:
        normalized = normalized.rstrip('0').rstrip('.')
    return normalized or '0'


def _quantize_decimal_for_scale(value, scale=2):
    dec = _decimal_or_none(value)
    if dec is None:
        return None
    return dec.quantize(Decimal('1').scaleb(-scale), rounding=ROUND_HALF_UP)


def _invoice_currency_choices(selected_value=None):
    selected = str(selected_value or '').strip().upper()
    options = [opt for opt in get_cv('CV_currency') if (opt.value or '').upper() in {'EUR', 'GBP'}]
    known_values = {(opt.value or '').upper() for opt in options}
    if selected and selected not in known_values:
        options.append(CVOption(selected, f'{selected} (current value)'))
    return options


def _products_table_exists():
    schema = get_tenant()["schema"]
    try:
        return bool(query_one(
            """
            SELECT 1 AS ok
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            """,
            [schema, 'Products'],
        ))
    except Exception:
        return False


def _products_table_columns():
    schema = get_tenant()["schema"]
    try:
        rows = query_all(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = 'Products'
            """,
            [schema],
        )
        return {row.get('COLUMN_NAME') for row in rows if row.get('COLUMN_NAME')}
    except Exception:
        return set()


def _tenant_table_columns(table_name):
    schema = get_tenant()["schema"]
    try:
        rows = query_all(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            """,
            [schema, table_name],
        )
        return {row.get('COLUMN_NAME') for row in rows if row.get('COLUMN_NAME')}
    except Exception:
        return set()


def _first_column(columns, *candidates):
    lowered = {str(col).lower(): col for col in columns}
    for candidate in candidates:
        found = lowered.get(candidate.lower())
        if found:
            return found
    return None


def _decimal_or_none(value):
    if value in (None, ''):
        return None
    text = str(value).strip().replace(',', '.')
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _first_non_empty(*values):
    for value in values:
        if value not in (None, ''):
            text = str(value).strip()
            if text:
                return text
    return ''


def _unit_weight(total_weight, quantity):
    total = _decimal_or_none(total_weight)
    qty = _decimal_or_none(quantity)
    if total is None or qty is None or total <= 0 or qty <= 0:
        return None
    return (total / qty).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)


def _goods_quantity_for_weight_learning(values, existing_record=None):
    values = values or {}
    existing_record = existing_record or {}
    return _first_non_empty(
        values.get('quantity_base'),
        existing_record.get('quantity_base'),
        values.get('quantity'),
        existing_record.get('quantity'),
        values.get('number_of_packages'),
        existing_record.get('number_of_packages'),
    )


def _product_lookup_value_for_weight_learning(values, existing_record=None):
    values = values or {}
    existing_record = existing_record or {}
    return _first_non_empty(
        values.get('sku'),
        existing_record.get('sku'),
        values.get('product_code'),
        existing_record.get('product_code'),
        values.get('stock_code'),
        existing_record.get('stock_code'),
    )


def _product_description_value_for_weight_learning(values, existing_record=None):
    values = values or {}
    existing_record = existing_record or {}
    return _first_non_empty(
        values.get('goods_description'),
        existing_record.get('goods_description'),
        values.get('description'),
        existing_record.get('description'),
        values.get('product_name'),
        existing_record.get('product_name'),
    )


def _description_weight_learning_where(columns, description_value):
    description_value = _first_non_empty(description_value)
    if not description_value:
        return '', []
    description_cols = [
        col for col in (
            _first_column(columns, 'goods_description'),
            _first_column(columns, 'description'),
            _first_column(columns, 'product_name'),
        ) if col
    ]
    if not description_cols:
        return '', []
    where = '(' + ' OR '.join(
        f"UPPER(LTRIM(RTRIM([{col}]))) = UPPER(LTRIM(RTRIM(?)))"
        for col in description_cols
    ) + ')'
    return where, [description_value] * len(description_cols)


def _product_template_id_for_weight_learning(values):
    raw = _first_non_empty((values or {}).get('product_template_id'))
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _apply_product_weight_update(table_name, assignments, where_sql, where_params):
    if not assignments or not where_sql:
        return 0
    schema = get_tenant()["schema"]
    set_sql = ', '.join(f"[{column}] = ?" for column, _ in assignments)
    if 'updated_at' in _tenant_table_columns(table_name):
        set_sql += ', [updated_at] = SYSUTCDATETIME()'
    return execute(
        f"UPDATE [{schema}].[{table_name}] SET {set_sql} WHERE {where_sql}",
        [value for _, value in assignments] + where_params,
    )


def _learn_product_unit_weights_from_goods(values, existing_record=None):
    values = values or {}
    existing_record = existing_record or {}
    quantity = _goods_quantity_for_weight_learning(values, existing_record)
    gross_unit = _unit_weight(values.get('gross_mass_kg'), quantity)
    net_unit = _unit_weight(values.get('net_mass_kg'), quantity)
    if gross_unit is None and net_unit is None:
        return 0

    product_template_id = _product_template_id_for_weight_learning(values)
    lookup_value = _product_lookup_value_for_weight_learning(values, existing_record)
    description_value = _product_description_value_for_weight_learning(values, existing_record)
    if not product_template_id and not lookup_value and not description_value:
        return 0

    updated = 0
    try:
        product_columns = _tenant_table_columns('Products')
        product_assignments = []
        gross_col = _first_column(product_columns, 'default_gross_weight_kg', 'gross_weight_kg')
        net_col = _first_column(product_columns, 'default_net_weight_kg', 'net_weight_kg')
        if gross_col and gross_unit is not None:
            product_assignments.append((gross_col, gross_unit))
        if net_col and net_unit is not None:
            product_assignments.append((net_col, net_unit))
        if product_assignments:
            if product_template_id and _first_column(product_columns, 'id'):
                updated += max(_apply_product_weight_update('Products', product_assignments, '[id] = ?', [product_template_id]), 0)
            elif lookup_value:
                lookup_cols = [
                    col for col in (
                        _first_column(product_columns, 'product_code'),
                        _first_column(product_columns, 'sku'),
                        _first_column(product_columns, 'stock_code'),
                    ) if col
                ]
                if lookup_cols:
                    where = '(' + ' OR '.join(f"[{col}] = ?" for col in lookup_cols) + ')'
                    updated += max(_apply_product_weight_update('Products', product_assignments, where, [lookup_value] * len(lookup_cols)), 0)
            elif description_value:
                where, params = _description_weight_learning_where(product_columns, description_value)
                active_col = _first_column(product_columns, 'is_active', 'active')
                if where and active_col:
                    where += f" AND [{active_col}] = 1"
                if where:
                    updated += max(_apply_product_weight_update('Products', product_assignments, where, params), 0)

        catalog_columns = _tenant_table_columns('DocProductCatalog')
        catalog_assignments = []
        gross_col = _first_column(catalog_columns, 'gross_weight_kg', 'default_gross_weight_kg')
        net_col = _first_column(catalog_columns, 'net_weight_kg', 'default_net_weight_kg')
        if gross_col and gross_unit is not None:
            catalog_assignments.append((gross_col, gross_unit))
        if net_col and net_unit is not None:
            catalog_assignments.append((net_col, net_unit))
        if catalog_assignments:
            lookup_cols = [
                col for col in (
                    _first_column(catalog_columns, 'sku'),
                    _first_column(catalog_columns, 'product_code'),
                    _first_column(catalog_columns, 'stock_code'),
                ) if col
            ]
            if lookup_value and lookup_cols:
                where = '(' + ' OR '.join(f"[{col}] = ?" for col in lookup_cols) + ')'
                if _first_column(catalog_columns, 'active'):
                    where += ' AND [active] = 1'
                updated += max(_apply_product_weight_update('DocProductCatalog', catalog_assignments, where, [lookup_value] * len(lookup_cols)), 0)
            elif description_value:
                where, params = _description_weight_learning_where(catalog_columns, description_value)
                if where and _first_column(catalog_columns, 'active'):
                    where += ' AND [active] = 1'
                if where:
                    updated += max(_apply_product_weight_update('DocProductCatalog', catalog_assignments, where, params), 0)
    except Exception:
        log.exception('Failed to learn product unit weights from goods item')
    return updated


def _load_product_templates():
    if not _products_table_exists():
        return []
    schema = get_tenant()["schema"]
    columns = _products_table_columns()
    active_col = 'is_active' if 'is_active' in columns else 'active' if 'active' in columns else None
    active_filter = f"WHERE {active_col} = 1" if active_col else ""
    template_columns = [
        'id',
        'product_code',
        'product_name',
        'commodity_code',
        'goods_description',
        'country_of_origin',
        'package_type',
        'package_marks',
        'procedure_code',
        'default_gross_weight_kg',
        'default_net_weight_kg',
        'unit_value_gbp',
        'valuation_method',
        'ni_additional_info_code',
        'preference_code',
    ]
    select_expr = ", ".join(
        col if col in columns else f"NULL AS {col}"
        for col in template_columns
    )
    order_cols = [col for col in ('product_name', 'product_code', 'id') if col in columns]
    order_clause = "ORDER BY " + ", ".join(order_cols) if order_cols else ""
    try:
        rows = query_all(f"""
            SELECT {select_expr}
            FROM [{schema}].Products
            {active_filter}
            {order_clause}
        """)
        products = []
        for row in rows:
            products.append({
                'id': str(row.get('id')),
                'display': f"{row.get('product_code') or row.get('id')} - {row.get('product_name') or 'Product'}",
                'product_code': row.get('product_code') or '',
                'product_name': row.get('product_name') or '',
                'goods_description': row.get('goods_description') or row.get('product_name') or '',
                'commodity_code': row.get('commodity_code') or '',
                'country_of_origin': row.get('country_of_origin') or '',
                'type_of_packages': row.get('package_type') or '',
                'package_marks': row.get('package_marks') or '',
                'procedure_code': row.get('procedure_code') or '',
                'gross_mass_kg': _format_decimal_for_scale(row.get('default_gross_weight_kg')),
                'net_mass_kg': _format_decimal_for_scale(row.get('default_net_weight_kg')),
                'item_invoice_amount': _normalize_currency_amount_form_value(row.get('unit_value_gbp')),
                'valuation_method': row.get('valuation_method') or '',
                'preference': row.get('preference_code') or '',
                'ni_additional_information_codes': row.get('ni_additional_info_code') or '',
            })
        return products
    except Exception:
        log.exception('Failed to load product templates')
        return []


def _detail_action(label, href=None, kind='primary', phase=None, post_to=None):
    action = {'label': label, 'kind': kind}
    if href:
        action['href'] = href
    if phase:
        if phase in {'sync', 'sync_pipeline', 'sync_gmr', 'sync_tss'}:
            phase = 'sync_all'
            if str(label).lower().startswith('sync'):
                action['label'] = 'Sync All TSS Data'
        action['phase'] = phase
        action['confirm_text'] = f"Run {action['label']} now?"
    if post_to:
        action['post_to'] = post_to
        action['confirm_text'] = action.get('confirm_text') or f'Run {label} now?'
    return action


def _parent_requires_sdi(item):
    return consignment_should_discover_sdi(item)


def _pipeline_job_markers():
    rows = query_all(f"""
        SELECT call_type, MAX(called_at) AS called_at
        FROM {S}.ApiCallLog
        WHERE call_type IN ('JOB_VALIDATE_PIPELINE', 'JOB_SUBMIT_PIPELINE', 'JOB_SYNC_PIPELINE')
        GROUP BY call_type
    """)
    return {row['call_type']: row['called_at'] for row in rows}


def _clean_pipeline_error(message):
    text = (message or '').strip()
    if not text:
        return ''
    if text.startswith('{'):
        try:
            payload = json.loads(text)
            result = payload.get('result') or {}
            return (result.get('process_message') or result.get('message') or text).strip()
        except Exception:
            return text
    return text


def _is_local_validation_failure(message):
    upper = _clean_pipeline_error(message).upper()
    if not upper:
        return False
    local_markers = (
        'REQUIRED:',
        'FORMAT:',
        'INVALID:',
        'LENGTH:',
        'MUST LINK TO A CONSIGNMENT',
    )
    return any(marker in upper for marker in local_markers)


def _looks_like_tss_rejection(message, api_calls=None):
    raw = (message or '').strip()
    upper = _clean_pipeline_error(message).upper()
    create_attempt = any((call.get('call_type') or '').upper() == 'CREATE_GOODS' for call in (api_calls or []))
    return bool(
        create_attempt
        or raw.startswith('{')
        or 'PROCESS_MESSAGE' in raw.upper()
        or upper.startswith('ERROR:')
        or 'INVALID FORMAT' in upper
        or 'MANDATORY FIELD' in upper
    )


def _match_goods_error_field(message):
    lower = (message or '').lower()
    for candidate in re.findall(r"'([a-z_]+)'", lower):
        if candidate in GOODS_EDIT_FIELD_META:
            return candidate
    if 'must link to a consignment' in lower or 'staging_cons_id' in lower:
        return 'cons_parent'
    for field, meta in GOODS_EDIT_FIELD_META.items():
        if any(alias in lower for alias in meta['aliases']):
            return field
    return None


def _build_goods_edit_guidance(item):
    cleaned = _clean_pipeline_error(item.get('error_message'))
    if not cleaned:
        return None

    parts = [part.strip() for part in cleaned.split(' | ') if part.strip()] or [cleaned]
    field_errors = {}
    char_suggestions = _goods_character_field_suggestions(item)
    field_suggestions = {}
    issues = []

    for part in parts:
        field = _match_goods_error_field(part)
        meta = GOODS_EDIT_FIELD_META.get(field, {})
        if field:
            field_errors.setdefault(field, []).append(part)
            if not char_suggestions.get(field) and meta.get('suggestion'):
                field_suggestions[field] = meta['suggestion']
        issues.append({
            'field': field,
            'label': meta.get('label', 'General issue'),
            'message': part,
            'suggestion': char_suggestions.get(field) or meta.get('suggestion'),
        })

    return {
        'title': 'This goods item needs fixes before it can move forward',
        'detail': 'Highlighted fields below are the likely blockers. Update them, save, and this goods item will be revalidated automatically.',
        'issues': issues,
        'field_errors': field_errors,
        'field_suggestions': field_suggestions,
    }


def _build_goods_reference_state(item, api_calls=None):
    local_status = (item.get('status') or '').upper()
    tss_status = (item.get('tss_status') or '').upper()
    cleaned_error = _clean_pipeline_error(item.get('error_message'))

    if item.get('goods_id'):
        detail = 'TSS accepted the goods create step and returned this Goods ID.'
        if tss_status:
            detail += f' Current TSS status: {tss_status}.'
            return {'tone': 'success', 'label': 'Goods ID linked successfully', 'detail': detail}
        detail += ' Run Sync TSS Status if you need the latest downstream TSS status.'
        return {'tone': 'info', 'label': 'Goods ID linked, waiting for status sync', 'detail': detail}

    if local_status == 'FAILED':
        if _is_local_validation_failure(cleaned_error):
            return {
                'tone': 'danger',
                'label': 'Goods ID blocked by local validation failure',
                'detail': 'No Goods ID was created because local validation failed before a successful TSS goods create. '
                          'Fix the field errors, reset to pending, then rerun Validate Pipeline and create the goods record in TSS.',
            }
        if _looks_like_tss_rejection(cleaned_error, api_calls=api_calls):
            detail = 'No Goods ID was created because TSS rejected the goods create request.'
            if cleaned_error:
                detail += f' Latest TSS response: {cleaned_error}'
            detail += ' Correct the blocking data, then rerun the pipeline.'
            return {
                'tone': 'danger',
                'label': 'Goods ID not created because TSS rejected submission',
                'detail': detail,
            }
        return {
            'tone': 'danger',
            'label': 'Goods creation blocked by pipeline failure',
            'detail': 'This goods item failed before a Goods ID could be linked. Review the failure details, then retry the pipeline.',
        }

    if local_status == 'VALIDATED':
        return {
            'tone': 'info',
            'label': 'Ready for Goods ID creation',
            'detail': 'Local validation is complete. The Goods ID will be created when the cargo send job creates this goods item in TSS successfully.',
        }

    if local_status in ('PENDING', 'PENDING REVIEW', 'INVALID'):
        return {
            'tone': 'warning',
            'label': 'Waiting for first successful pipeline pass',
            'detail': 'No Goods ID exists yet because this goods item has not completed local validation and TSS submission.',
        }

    return {
        'tone': 'info',
        'label': 'Waiting for first successful TSS goods create',
        'detail': 'A Goods ID will appear here after the first successful goods create in TSS.',
    }


def _build_goods_guidance(item, api_calls=None):
    local_status = (item.get('status') or '').upper()
    tss_status = (item.get('tss_status') or '').upper()
    updated_at = item.get('updated_at') or item.get('created_at')
    job_markers = _pipeline_job_markers()
    last_validate = job_markers.get('JOB_VALIDATE_PIPELINE')
    last_submit = job_markers.get('JOB_SUBMIT_PIPELINE')
    last_sync = job_markers.get('JOB_SYNC_PIPELINE')
    cleaned_error = _clean_pipeline_error(item.get('error_message'))
    requires_sdi = _parent_requires_sdi(item)

    if local_status == 'FAILED':
        if _is_local_validation_failure(cleaned_error):
            return {
                'tone': 'danger',
                'title': 'Local validation failed before TSS goods submission',
                'detail': f'No Goods ID could be created because local validation failed. {cleaned_error}',
                'actions': [
                    _detail_action('Edit Goods', href=url_for('goods.edit', sid=item['staging_id']), kind='warning'),
                    _detail_action('Reset to Pending', post_to=url_for('goods.retry', sid=item['staging_id']), kind='warning'),
                    _detail_action('Validate Pipeline', kind='warning', phase='validate_pipeline'),
                ],
            }
        if _looks_like_tss_rejection(cleaned_error, api_calls=api_calls):
            detail = 'TSS rejected the goods create request before issuing a Goods ID.'
            if cleaned_error:
                detail += f' Response: {cleaned_error}'
            detail += ' Fix the blocking field values, reset this record to PENDING, then rerun Validate Pipeline and create the goods record in TSS.'
            return {
                'tone': 'danger',
                'title': 'TSS rejected the goods create request',
                'detail': detail,
                'actions': [
                    _detail_action('Edit Goods', href=url_for('goods.edit', sid=item['staging_id']), kind='warning'),
                    _detail_action('Reset to Pending', post_to=url_for('goods.retry', sid=item['staging_id']), kind='warning'),
                    _detail_action('Validate Pipeline', kind='warning', phase='validate_pipeline'),
                ],
            }
        return {
            'tone': 'danger',
            'title': 'Blocked by a goods pipeline error',
            'detail': cleaned_error or 'Fix the failed fields, then retry validation or submission.',
            'actions': [
                _detail_action('Edit Goods', href=url_for('goods.edit', sid=item['staging_id']), kind='warning'),
                _detail_action('Reset to Pending', post_to=url_for('goods.retry', sid=item['staging_id']), kind='warning'),
            ],
        }

    if not item.get('goods_id'):
        if local_status in ('PENDING', 'PENDING REVIEW'):
            detail = 'This goods item is still local-only. Run Validate Pipeline, then create the goods record in TSS.'
            if last_validate and updated_at and last_validate < updated_at:
                detail = 'This goods item was created or changed after the last Validate Pipeline run, so no TSS goods create has been attempted yet.'
            return {
                'tone': 'warning',
                'title': 'Waiting for Validate Pipeline',
                'detail': detail,
                'actions': [
                    _detail_action('Validate Pipeline', kind='warning', phase='validate_pipeline'),
                ],
            }
        if local_status == 'VALIDATED':
            detail = 'Validation is complete. The next step is to create the Goods ID in TSS.'
            if last_submit and updated_at and last_submit < updated_at:
                detail = 'This goods item became VALIDATED after the last cargo send run, so it is still waiting to be sent to TSS.'
            return {
                'tone': 'info',
                'title': 'Ready for TSS goods submission',
                'detail': detail,
                'actions': [
                    _detail_action('Edit Goods', href=url_for('goods.edit', sid=item['staging_id']), kind='warning'),
                    _detail_action('Create Goods in TSS', kind='primary', phase='submit_pipeline'),
                ],
            }

    if item.get('goods_id') and not tss_status:
        detail = 'A Goods ID exists, but no TSS status has been synced back yet. Run Sync TSS Status.'
        if last_sync and updated_at and last_sync < updated_at:
            detail = 'This goods item changed after the last sync run, so the current TSS status has not been refreshed yet.'
        return {
            'tone': 'info',
            'title': 'Waiting for TSS status sync',
            'detail': detail,
            'actions': [
                _detail_action('Sync TSS Status', kind='primary', phase='sync_pipeline'),
            ],
        }

    if item.get('goods_id'):
        if not requires_sdi:
            return {
                'tone': 'success',
                'title': f'Created in TSS - no supplementary declaration required beyond {item.get("tss_status") or item.get("status")}',
                'detail': 'This goods item belongs to a consignment configured not to generate SFD/SDI, so there is no downstream supplementary declaration step to start from here.',
                'actions': [
                    _detail_action('Sync TSS Status', kind='primary', phase='sync_pipeline'),
                ],
            }
        return {
            'tone': 'info',
            'title': f'Waiting for TSS to progress beyond {item.get("tss_status") or item.get("status")}',
            'detail': 'Run Sync TSS Status if this looks stale or if you expect a newer TSS status.',
            'actions': [
                _detail_action('Sync TSS Status', kind='primary', phase='sync_pipeline'),
            ],
        }

    if not api_calls:
        return {
            'tone': 'warning',
            'title': 'No goods API calls have been recorded yet',
            'detail': 'That is expected until this goods item reaches TSS submission or TSS status sync.',
            'actions': [],
        }

    return {
        'tone': 'info',
        'title': 'Review the parent consignment journey',
        'detail': 'Use the linked ENS and consignment records to continue the Route A journey.',
        'actions': [],
    }


def _goods_detail_target(item):
    ref = (item or {}).get('goods_id')
    if ref:
        return url_for('goods.detail_by_ref', goods_ref=ref)
    return url_for('goods.detail', sid=(item or {}).get('staging_id', 0))


def _flash_auto_validation_result(staging_id, result):
    if not result:
        return
    if result['ok']:
        flash(f'Goods item #{staging_id} saved and auto-validated.', 'success')
        return
    first_error = (result.get('errors') or ['Validation failed.'])[0]
    flash(f'Goods item #{staging_id} saved, but local validation failed: {first_error}', 'warning')

def get_cv(table, val_col='value', name_col='name'):
    try:
        rows = query_all(f"SELECT [{val_col}], [{name_col}] FROM TSS.[{table}] ORDER BY [{name_col}]")
        options = [CVOption(r[val_col], r[name_col]) for r in rows]
        if options:
            return options
    except Exception:
        pass

    api_field = {
        'CV_preference': 'preference',
    }.get(table)
    if api_field:
        try:
            client = build_cfg_client()
            result = client.get_choice_values(api_field)
            items = TssApiClient.as_items(result.get('response'))
            options = []
            for item in items:
                value = str(item.get(val_col) or item.get('value') or item.get('code') or '').strip()
                name = str(item.get(name_col) or item.get('name') or item.get('description') or value).strip()
                if value:
                    options.append(CVOption(value, name))
            if options:
                options.sort(key=lambda opt: (opt.name or opt.value))
                return options
        except Exception:
            log.exception('Failed to load choice values for %s via API fallback', table)
    return []

def get_cons_parents():
    results = []
    try:
        rows = query_all(f"""
            SELECT staging_id, dec_reference, label, goods_description, status, tss_status
            FROM {S}.StagingConsignments
            WHERE status IN ('PENDING','PENDING_REVIEW','PENDING REVIEW','VALIDATED','CREATED','FAILED','INVALID')
            ORDER BY staging_id DESC""")
        for r in rows:
            if not _can_add_goods_to_parent(r):
                continue
            ref = r.get('dec_reference') or f"#{r['staging_id']}"
            lbl = r.get('label') or r.get('goods_description') or ''
            results.append({
                'id': str(r['staging_id']),
                'ref': ref,
                'label': f"{lbl} [{r['status']}]",
                'display': f"{ref} — {lbl} [{r['status']}]",
            })
    except Exception as e:
        log.error('get_cons_parents StagingConsignments query failed: %s', e)
    try:
        rows = query_all(f"""
            SELECT consignment_id, declaration_number, goods_description
            FROM {S}.EnsConsignments ORDER BY consignment_id DESC""")
        for r in rows:
            dec = r.get('declaration_number') or ''
            if dec and not any(dec in x['display'] for x in results):
                lbl = r.get('goods_description') or ''
                results.append({
                    'id': f"synced:{r['consignment_id']}",
                    'ref': dec,
                    'label': f"[Synced] {lbl}",
                    'display': f"[Synced] {dec} — {lbl}",
                })
    except Exception as e:
        log.error('get_cons_parents EnsConsignments query failed: %s', e)
    log.info('get_cons_parents returning %d results', len(results))
    return results

def load_goods_choices(selected_currency=None):
    return {
        'type_of_packages': get_cv('CV_type_of_package'),
        'controlled_goods_type': get_cv('CV_controlled_goods_type'),
        'countries': get_cv('CV_country'),
        'currencies': _invoice_currency_choices(selected_currency),
        'procedure_codes': get_cv('CV_procedure_code'),
        'addl_procedure_codes': get_cv('CV_additional_procedure_code'),
        'preferences': get_cv('CV_preference'),
        'valuation_methods': get_cv('CV_valuation_method'),
        'document_codes': get_cv('CV_document_code'),
        'document_statuses': get_cv('CV_document_status'),
    }


def _load_prd_consignment_for_goods_create(cons_id):
    return query_one("""
        SELECT
            c.*,
            COALESCE(c.trader_reference, c.transport_document_number, c.tss_consignment_ref) AS consignment_ref,
            tc.TssStatus AS cons_tss_status
        FROM STG.BKD_ENS_Consignments c
        LEFT JOIN TSS.BKD_ENS_Consignments tc
            ON tc.ClientCode = c.ClientCode
           AND tc.ConsignmentReference = c.tss_consignment_ref
        WHERE c.stg_consignment_id = ?
    """, [cons_id])


def _prd_goods_create_item(parent):
    return {
        'stg_item_id': None,
        'stg_consignment_id': parent.get('stg_consignment_id'),
        'stg_header_id': parent.get('stg_header_id'),
        'consignment_ref': parent.get('consignment_ref'),
        'sub_status': 'PENDING',
        'tss_hex_id': None,
        'tss_consignment_ref': parent.get('tss_consignment_ref'),
        'error_message': None,
    }


def _prd_goods_create_form(parent):
    return _normalize_goods_form_data({
        'stg_consignment_id': parent.get('stg_consignment_id'),
        'goods_description': parent.get('goods_description') or '',
        'type_of_packages': 'PK',
        'number_of_packages': 1,
        'package_marks': 'ADDR',
        'gross_mass_kg': '',
        'net_mass_kg': '',
        'controlled_goods': 'no',
        'country_of_origin': parent.get('country_of_origin') or '',
        'procedure_code': '',
        'additional_procedure_code': '',
        'item_invoice_amount': '',
        'item_invoice_currency': '',
        'valuation_method': '',
        'preference': '',
        'commodity_code': '',
    })


def _render_goods_create_form(form_data, errors=None, field_suggestions=None):
    normalized = _normalize_goods_form_data(form_data)
    return render_template(
        'goods/create.html',
        form=normalized,
        errors=errors or {},
        field_suggestions=field_suggestions or {},
        choices=load_goods_choices(normalized.get('item_invoice_currency')),
        cons_parents=get_cons_parents(),
        products=_load_product_templates(),
    )


# ── LIST ──────────────────────────────────────────────

@goods_bp.route('/')
def list_view():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('consignments.list_view'))


# ── CREATE ────────────────────────────────────────────

@goods_bp.route('/create', methods=['GET', 'POST'])
def create():
    raw_cons_id = request.values.get('cons_id') or request.values.get('stg_consignment_id')
    try:
        cons_id = int(raw_cons_id)
    except (TypeError, ValueError):
        flash('Choose a consignment before adding goods.', 'warning')
        return redirect(url_for('consignments.list_view'))

    parent = _load_prd_consignment_for_goods_create(cons_id)
    if not parent:
        flash('Consignment not found.', 'warning')
        return redirect(url_for('consignments.list_view'))

    next_url = _safe_next_url(
        request.values.get('next_url'),
        url_for('consignments.detail', sid=cons_id),
    )
    if not _prd_consignment_allows_goods_create(parent):
        flash('Goods can only be added while the consignment is local repair, TSS Draft, or Trader Input Required.', 'warning')
        return redirect(next_url)

    item = _prd_goods_create_item(parent)
    if request.method == 'POST':
        form = _normalize_goods_form_data(request.form.to_dict())
        package_type = normalise_package_type(form.get('type_of_packages'), 'PK') or 'PK'
        errors = {}

        def require(field, label):
            if not str(form.get(field) or '').strip():
                errors.setdefault(field, []).append(f'{label} is required.')

        require('goods_description', 'Goods Description')
        require('gross_mass_kg', 'Gross Mass KG')
        require('number_of_packages', 'Number of Packages')
        require('package_marks', 'Package Marks')
        require('commodity_code', 'Commodity Code')

        gross = _quantize_decimal_for_scale(form.get('gross_mass_kg'), 3)
        net = _quantize_decimal_for_scale(form.get('net_mass_kg'), 3)
        invoice_amount = _quantize_decimal_for_scale(form.get('item_invoice_amount'), 2)
        if gross is None or gross <= 0:
            errors.setdefault('gross_mass_kg', []).append('Gross Mass KG must be greater than zero.')
        if net is not None and gross is not None and net > gross:
            errors.setdefault('net_mass_kg', []).append('Net Mass KG cannot exceed Gross Mass KG.')
        try:
            number_of_packages = int(str(form.get('number_of_packages') or '').strip())
            if number_of_packages < 1:
                raise ValueError()
        except (TypeError, ValueError):
            number_of_packages = None
            errors.setdefault('number_of_packages', []).append('Number of Packages must be at least 1.')

        form['type_of_packages'] = package_type
        if errors:
            return render_template(
                'goods/prd_edit.html',
                is_create=True,
                next_url=next_url,
                item=item,
                form=form,
                errors=errors,
                choices=load_goods_choices(form.get('item_invoice_currency')),
                badge_class=badge_class_for_status,
            )

        next_seq = query_one("""
            SELECT COALESCE(MAX(item_seq), 0) + 1 AS next_item_seq
            FROM STG.BKD_GoodsItems
            WHERE ClientCode = ? AND stg_consignment_id = ?
        """, [parent.get('ClientCode'), cons_id])
        item_seq = int((next_seq or {}).get('next_item_seq') or 1)
        with db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO STG.BKD_GoodsItems (
                    ClientCode,
                    stg_consignment_id,
                    sub_status,
                    source,
                    goods_stage,
                    item_seq,
                    goods_description,
                    commodity_code,
                    type_of_packages,
                    number_of_packages,
                    package_marks,
                    gross_mass_kg,
                    net_mass_kg,
                    controlled_goods,
                    country_of_origin,
                    procedure_code,
                    additional_procedure_code,
                    item_invoice_amount,
                    item_invoice_currency,
                    valuation_method,
                    preference,
                    error_message,
                    last_sub_status_change,
                    updated_at
                )
                OUTPUT INSERTED.stg_item_id
                VALUES (
                    ?, ?, 'PENDING', 'MANUAL_PRD_UI', 'ENS', ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, SYSUTCDATETIME(), SYSUTCDATETIME()
                )
            """, [
                parent.get('ClientCode'),
                cons_id,
                item_seq,
                (form.get('goods_description') or '').strip(),
                (form.get('commodity_code') or '').strip(),
                package_type,
                number_of_packages,
                (form.get('package_marks') or '').strip() or 'ADDR',
                gross,
                net,
                (form.get('controlled_goods') or 'no').strip() or 'no',
                (form.get('country_of_origin') or '').strip(),
                (form.get('procedure_code') or '').strip(),
                (form.get('additional_procedure_code') or '').strip(),
                invoice_amount,
                (form.get('item_invoice_currency') or '').strip(),
                (form.get('valuation_method') or '').strip(),
                (form.get('preference') or '').strip(),
            ])
            new_id = int(cursor.fetchone()[0])
        _learn_product_unit_weights_from_goods(form)
        flash(f'Goods item #{new_id} added in STG. Re-run cargo submission from the consignment or ENS detail.', 'success')
        return redirect(next_url)

    form = _prd_goods_create_form(parent)
    return render_template(
        'goods/prd_edit.html',
        is_create=True,
        next_url=next_url,
        item=item,
        form=form,
        errors={},
        choices=load_goods_choices(form.get('item_invoice_currency')),
        badge_class=badge_class_for_status,
    )


# ── EDIT ──────────────────────────────────────────────

@goods_bp.route('/<int:sid>/edit', methods=['GET', 'POST'])
def edit(sid):
    item = query_one("""
        SELECT
            g.*,
            c.stg_header_id,
            COALESCE(c.trader_reference, c.transport_document_number, c.tss_consignment_ref) AS consignment_ref,
            c.sub_status AS cons_local_status,
            c.tss_consignment_ref,
            tc.TssStatus AS cons_tss_status
        FROM STG.BKD_GoodsItems g
        LEFT JOIN STG.BKD_ENS_Consignments c
            ON c.ClientCode = g.ClientCode
           AND c.stg_consignment_id = g.stg_consignment_id
        LEFT JOIN TSS.BKD_ENS_Consignments tc
            ON tc.ClientCode = c.ClientCode
           AND tc.ConsignmentReference = c.tss_consignment_ref
        WHERE g.stg_item_id = ?
    """, [sid])
    if not item:
        flash('Goods item not found.', 'warning')
        return redirect(url_for('consignments.list_view'))

    can_edit = _prd_goods_allows_edit(item)
    if not can_edit:
        flash('This goods item can only be edited while it is local repair, TSS Draft, or Trader Input Required.', 'warning')
        return redirect(url_for('consignments.detail', sid=item.get('stg_consignment_id')))

    if request.method == 'POST':
        next_url = _safe_next_url(
            request.form.get('next_url'),
            url_for('consignments.detail', sid=item.get('stg_consignment_id')),
        )
        form = _normalize_goods_form_data(request.form.to_dict())
        package_type = normalise_package_type(form.get('type_of_packages'), 'PK') or 'PK'
        errors = {}

        def require(field, label):
            if not str(form.get(field) or '').strip():
                errors.setdefault(field, []).append(f'{label} is required.')

        require('goods_description', 'Goods Description')
        require('gross_mass_kg', 'Gross Mass KG')
        require('number_of_packages', 'Number of Packages')
        require('package_marks', 'Package Marks')
        require('commodity_code', 'Commodity Code')

        gross = _quantize_decimal_for_scale(form.get('gross_mass_kg'), 3)
        net = _quantize_decimal_for_scale(form.get('net_mass_kg'), 3)
        invoice_amount = _quantize_decimal_for_scale(form.get('item_invoice_amount'), 2)
        if gross is None or gross <= 0:
            errors.setdefault('gross_mass_kg', []).append('Gross Mass KG must be greater than zero.')
        if net is not None and gross is not None and net > gross:
            errors.setdefault('net_mass_kg', []).append('Net Mass KG cannot exceed Gross Mass KG.')
        try:
            number_of_packages = int(str(form.get('number_of_packages') or '').strip())
            if number_of_packages < 1:
                raise ValueError()
        except (TypeError, ValueError):
            number_of_packages = None
            errors.setdefault('number_of_packages', []).append('Number of Packages must be at least 1.')

        form['type_of_packages'] = package_type
        if errors:
            return render_template(
                'goods/prd_edit.html',
                item=item,
                form=form,
                errors=errors,
                choices=load_goods_choices(form.get('item_invoice_currency')),
                badge_class=badge_class_for_status,
            )

        execute("""
            UPDATE STG.BKD_GoodsItems
               SET goods_description = ?,
                   commodity_code = ?,
                   type_of_packages = ?,
                   number_of_packages = ?,
                   package_marks = ?,
                   gross_mass_kg = ?,
                   net_mass_kg = ?,
                   controlled_goods = ?,
                   country_of_origin = ?,
                   procedure_code = ?,
                   additional_procedure_code = ?,
                   item_invoice_amount = ?,
                   item_invoice_currency = ?,
                   valuation_method = ?,
                   preference = ?,
                   error_message = NULL,
                   sub_status = CASE
                       WHEN NULLIF(LTRIM(RTRIM(COALESCE(tss_hex_id, ''))), '') IS NULL
                       THEN 'PENDING'
                       ELSE sub_status
                   END,
                   last_sub_status_change = SYSUTCDATETIME(),
                   updated_at = SYSUTCDATETIME()
             WHERE stg_item_id = ?
        """, [
            (form.get('goods_description') or '').strip(),
            (form.get('commodity_code') or '').strip(),
            package_type,
            number_of_packages,
            (form.get('package_marks') or '').strip() or 'ADDR',
            gross,
            net,
            (form.get('controlled_goods') or item.get('controlled_goods') or 'no').strip() or 'no',
            (form.get('country_of_origin') or item.get('country_of_origin') or '').strip(),
            (form.get('procedure_code') or item.get('procedure_code') or '').strip(),
            (form.get('additional_procedure_code') or item.get('additional_procedure_code') or '').strip(),
            invoice_amount,
            (form.get('item_invoice_currency') or item.get('item_invoice_currency') or '').strip(),
            (form.get('valuation_method') or item.get('valuation_method') or '').strip(),
            (form.get('preference') or item.get('preference') or '').strip(),
            sid,
        ])
        flash(f'Goods item #{sid} saved in STG. Re-run cargo submission from the consignment or ENS detail.', 'success')
        return redirect(next_url)

    form = _normalize_goods_form_data(dict(item))
    form['type_of_packages'] = normalise_package_type(form.get('type_of_packages'), 'PK') or 'PK'
    return render_template(
        'goods/prd_edit.html',
        item=item,
        form=form,
        errors={},
        choices=load_goods_choices(form.get('item_invoice_currency')),
        badge_class=badge_class_for_status,
    )


# ── INLINE EDITS ──────────────────────────────────────

@goods_bp.route('/<int:sid>/inline-gross', methods=['POST'])
def inline_gross(sid):
    item = query_one("""
        SELECT
            g.*,
            c.tss_consignment_ref,
            c.sub_status AS cons_local_status,
            tc.TssStatus AS cons_tss_status
        FROM STG.BKD_GoodsItems g
        LEFT JOIN STG.BKD_ENS_Consignments c
            ON c.ClientCode = g.ClientCode
           AND c.stg_consignment_id = g.stg_consignment_id
        LEFT JOIN TSS.BKD_ENS_Consignments tc
            ON tc.ClientCode = c.ClientCode
           AND tc.ConsignmentReference = c.tss_consignment_ref
        WHERE g.stg_item_id = ?
    """, [sid])
    if not item:
        return jsonify({'ok': False, 'message': 'Goods item not found.'}), 404
    if not _prd_goods_allows_edit(item):
        return jsonify({
            'ok': False,
            'message': 'Goods can only be edited while local repair, TSS Draft, or Trader Input Required.',
        }), 409

    payload = request.get_json(silent=True) or {}
    gross = _quantize_decimal_for_scale(payload.get('gross_mass_kg'), 2)
    if gross is None or gross <= 0:
        return jsonify({'ok': False, 'message': 'Gross KG must be greater than zero.'}), 400

    next_status = item.get('sub_status') or 'PENDING'
    if not str(item.get('tss_hex_id') or '').strip():
        next_status = 'PENDING'
    execute("""
        UPDATE STG.BKD_GoodsItems
           SET gross_mass_kg = ?,
               error_message = NULL,
               sub_status = CASE
                   WHEN NULLIF(LTRIM(RTRIM(COALESCE(tss_hex_id, ''))), '') IS NULL
                   THEN 'PENDING'
                   ELSE sub_status
               END,
               last_sub_status_change = SYSUTCDATETIME(),
               updated_at = SYSUTCDATETIME()
         WHERE stg_item_id = ?
    """, [gross, sid])
    _learn_product_unit_weights_from_goods({'gross_mass_kg': gross}, existing_record=item)
    gross_text = _format_decimal_for_scale(gross, 2)
    status_label = status_display(next_status)
    return jsonify({
        'ok': True,
        'gross_mass_kg': gross_text,
        'status_label': status_label,
        'status_class': badge_class_for_status(next_status),
        'message': 'Gross KG saved in STG.',
    })


# ── DELETE / RETRY ────────────────────────────────────

@goods_bp.route('/<int:sid>/delete', methods=['POST'])
def delete(sid):
    item = query_one("""
        SELECT
            g.stg_item_id,
            g.ClientCode,
            g.stg_consignment_id,
            g.item_seq,
            g.goods_description,
            g.sub_status,
            g.tss_hex_id,
            c.stg_header_id
        FROM STG.BKD_GoodsItems g
        LEFT JOIN STG.BKD_ENS_Consignments c
            ON c.ClientCode = g.ClientCode
           AND c.stg_consignment_id = g.stg_consignment_id
        WHERE g.stg_item_id = ?
    """, [sid])
    fallback = url_for('consignments.detail', sid=item.get('stg_consignment_id')) if item else url_for('consignments.list_view')
    next_url = _safe_next_url(request.form.get('next_url'), fallback)
    if not item:
        flash('Goods item not found.', 'warning')
        return redirect(next_url)

    if not _prd_goods_allows_local_delete(item):
        flash('Only local editable goods with no TSS Goods ID can be deleted locally.', 'warning')
        return redirect(next_url)

    deleted = execute("""
        DELETE FROM STG.BKD_GoodsItems
         WHERE ClientCode = ?
           AND stg_item_id = ?
           AND UPPER(REPLACE(COALESCE(sub_status, ''), '_', ' ')) IN (
               'PENDING',
               'PENDING REVIEW',
               'FAILED',
               'INVALID',
               'VALIDATED',
               'CREATED',
               'DRAFT'
           )
           AND NULLIF(LTRIM(RTRIM(COALESCE(tss_hex_id, ''))), '') IS NULL
    """, [item.get('ClientCode') or (get_tenant().get('code') or 'BKD').upper(), sid])
    if deleted:
        label = item.get('item_seq') or sid
        flash(f'Deleted local goods item {label}. No TSS record was cancelled.', 'success')
    else:
        flash('Goods item was not deleted because it no longer matches the local/no-TSS-ID rule.', 'warning')
    return redirect(next_url)

@goods_bp.route('/<int:sid>/retry', methods=['POST'])
def retry(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('consignments.list_view'))


@goods_bp.route('/<int:sid>/delete-from-tss', methods=['POST'])
def delete_from_tss(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('consignments.list_view'))


def _selected_goods_ids_from_form():
    ids = []
    for raw_id in request.form.getlist('selected_ids'):
        try:
            ids.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    return sorted(set(ids))


def _goods_list_redirect_args_from_form():
    redirect_args = {'status': request.form.get('status', '').strip().upper() or 'ALL'}
    search = request.form.get('q', '').strip()
    cons_filter = request.form.get('cons_id', '').strip()
    ens_ref = request.form.get('ens_ref', '').strip()
    show_all = request.form.get('show_all') == '1'
    if search:
        redirect_args['q'] = search
    if cons_filter:
        redirect_args['cons_id'] = cons_filter
    if ens_ref:
        redirect_args['ens_ref'] = ens_ref
    if show_all:
        redirect_args['show_all'] = 1
    return redirect_args


@goods_bp.route('/bulk-delete-selected', methods=['POST'])
def bulk_delete_selected():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('consignments.list_view'))


@goods_bp.route('/bulk-export-selected', methods=['POST'])
def bulk_export_selected():
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('consignments.list_view'))


@goods_bp.route('/<string:goods_ref>/detail')
def detail_by_ref(goods_ref):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('consignments.list_view'))


@goods_bp.route('/<int:sid>')
def detail(sid):
    flash('This legacy portal view is not available in Automation PRD. Use Ingestion to monitor email automation and use STG/TSS-backed pages only.', 'info')
    return redirect(url_for('consignments.list_view'))


def _render_detail(item):
    item = _apply_parent_synced_goods_status(dict(item))
    api_calls = []
    try:
        api_calls = query_all(f"""
            SELECT TOP 50 id, call_type, http_method, url,
                   http_status, response_status, response_message,
                   duration_ms, error_detail, called_at
            FROM {S}.ApiCallLog
            WHERE staging_id = ?
            ORDER BY called_at DESC
        """, [item['staging_id']])
    except Exception:
        api_calls = []

    sibling_goods = query_all(f"""
        SELECT staging_id, item_number, goods_description, goods_id, status, tss_status
        FROM {S}.StagingGoodsItems
        WHERE staging_cons_id = ?
        ORDER BY item_number, staging_id
    """, [item['staging_cons_id']])
    sibling_goods = [
        _apply_parent_synced_goods_status(dict(row), parent=item)
        for row in (sibling_goods or [])
    ]

    guidance = _build_goods_guidance(item, api_calls=api_calls)
    goods_state = _build_goods_reference_state(item, api_calls=api_calls)
    error_explanation = explain_tss_error(
        item.get('error_message'),
        local_status=item.get('status'),
        tss_status=item.get('tss_status'),
        entity_label='this goods item',
    )
    api_log_hint = None
    if not api_calls:
        if item.get('error_message') and _looks_like_tss_rejection(item.get('error_message'), api_calls=api_calls):
            api_log_hint = 'This record stores a TSS-style rejection message, but no ApiCallLog row is currently linked to this staging record.'
        else:
            api_log_hint = guidance['detail']

    return render_template(
        'goods/detail.html',
        item=item,
        api_calls=api_calls,
        sibling_goods=sibling_goods,
        guidance=guidance,
        goods_state=goods_state,
        api_log_hint=api_log_hint,
        error_explanation=error_explanation,
        badge_class=badge_class,
        can_change_goods_data=_can_change_goods_data(item),
        can_edit_goods_locally=_can_edit_goods_locally(item),
    )
