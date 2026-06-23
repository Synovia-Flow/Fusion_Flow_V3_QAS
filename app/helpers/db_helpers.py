"""
================================================================================
  Synovia Flow — BKD Portal: Database & Validation Helpers
  Licensed Component: Synovia Digital Ltd
================================================================================
  Version:   2.0.0
  Database:  Fusion_TSS (Azure SQL)
  Schema:    BKD (staging), TSS (choice values)
================================================================================
"""

import pyodbc
from contextlib import contextmanager
from flask import g, current_app

from app.tenant import get_tenant, qualified_table, tenant_aware_cursor
from config.db_connection import build_connection_string, detect_odbc_driver
from app.status_utils import badge_class_for_status


def _detect_odbc_driver():
    return detect_odbc_driver()

# ══════════════════════════════════════════════════════════════
#  DATABASE CONNECTION
# ══════════════════════════════════════════════════════════════

pyodbc.pooling = False  # Required on Linux — unixODBC pooling is a no-op

def get_db_connection():
    """Get or create a request-scoped pyodbc connection via Flask g."""
    if 'db_conn' not in g:
        conn_str = build_connection_string(current_app.config, timeout=30)
        g.db_conn = pyodbc.connect(conn_str)
    return g.db_conn


def close_db_connection(e=None):
    """Teardown: close the connection at end of request."""
    conn = g.pop('db_conn', None)
    if conn is not None:
        conn.close()


@contextmanager
def db_cursor():
    """Context manager: yields a cursor, auto-commits or rolls back."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        yield tenant_aware_cursor(cursor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def init_db(app):
    """Register teardown with the Flask app."""
    app.teardown_appcontext(close_db_connection)


# ══════════════════════════════════════════════════════════════
#  CHOICE VALUE HELPERS
# ══════════════════════════════════════════════════════════════

# Cache choice values per-request (avoid repeated DB hits within one page load)
def _cv_cache():
    if '_cv_cache' not in g:
        g._cv_cache = {}
    return g._cv_cache


def get_choice_values(cv_table, value_col='value', display_col='name', extra_cols=None):
    """
    Load choice values from a TSS.CV_* table.
    Returns list of dicts: [{'value': 'GB', 'name': 'United Kingdom'}, ...]
    """
    cache = _cv_cache()
    cache_key = f"{cv_table}:{value_col}:{display_col}"

    if cache_key in cache:
        return cache[cache_key]

    cols = f"[{value_col}], [{display_col}]"
    if extra_cols:
        cols += ", " + ", ".join(f"[{c}]" for c in extra_cols)

    try:
        with db_cursor() as cur:
            cur.execute(f"SELECT {cols} FROM TSS.{cv_table} ORDER BY [{display_col}]")
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            cache[cache_key] = rows
            return rows
    except Exception as e:
        current_app.logger.warning(f"Choice values load failed for {cv_table}: {e}")
        return []


def get_cv_options(cv_table, value_col='value', display_col='name'):
    """
    Returns list of (value, display_text) tuples for HTML <select> options.
    """
    rows = get_choice_values(cv_table, value_col, display_col)
    return [(r[value_col], f"{r[value_col]} — {r[display_col]}") for r in rows]


# Preload commonly used choice values for forms
def load_consignment_choices():
    """Load all choice values needed for consignment create/edit forms."""
    return {
        'countries': get_cv_options('CV_country'),
        'goods_domestic_status': get_cv_options('CV_goods_domestic_status'),
        'no_sfd_reason': get_cv_options('CV_no_sfd_reason'),
        'declaration_choice': get_cv_options('CV_sfd_declaration_choice'),
        'prev_doc_types': get_cv_options('CV_previous_document_type'),
        'auth_type_codes': get_cv_options('CV_auth_type_code'),
    }


def load_goods_choices():
    """Load all choice values needed for goods item create/edit forms."""
    return {
        'type_of_packages': get_cv_options('CV_type_of_package'),
        'controlled_goods_type': get_cv_options('CV_controlled_goods_type'),
        'commodity_codes': get_cv_options('CV_commodity_code'),
        'countries': get_cv_options('CV_country'),
        'currencies': get_cv_options('CV_currency'),
        'procedure_codes': get_cv_options('CV_procedure_code'),
        'addl_procedure_codes': get_cv_options('CV_additional_procedure_code'),
        'preferences': get_cv_options('CV_preference'),
        'valuation_methods': get_cv_options('CV_valuation_method'),
        'nature_of_transaction': get_cv_options('CV_nature_of_transaction'),
        'document_codes': get_cv_options('CV_document_code'),
        'document_statuses': get_cv_options('CV_document_status'),
        'prev_doc_types': get_cv_options('CV_previous_document_type'),
    }


# ══════════════════════════════════════════════════════════════
#  VALIDATION ENGINE
# ══════════════════════════════════════════════════════════════

def load_validation_rules(entity_type):
    """Load active validation rules for an entity type."""
    schema = get_tenant()["schema"]
    with db_cursor() as cur:
        cur.execute(f"""
            SELECT field_name, rule_type, condition_expression,
                   choice_table, choice_value_column, min_length,
                   max_length, regex_pattern, error_message
            FROM {qualified_table('ValidationRules', schema)}
            WHERE entity_type = ? AND is_active = 1
            ORDER BY sort_order
        """, entity_type)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def validate_form_data(form_data, entity_type, parent_data=None):
    """
    Validate form data against ValidationRules.
    Returns: {'valid': bool, 'errors': {'field_name': ['error msg', ...]}}
    """
    rules = load_validation_rules(entity_type)
    errors = {}

    for rule in rules:
        field = rule['field_name']
        value = form_data.get(field, '').strip() if form_data.get(field) else ''

        # REQUIRED check
        if rule['rule_type'] == 'REQUIRED' and not value:
            errors.setdefault(field, []).append(rule['error_message'])

        # CONDITIONAL check
        elif rule['rule_type'] == 'CONDITIONAL' and rule['condition_expression']:
            if _evaluate_condition(rule['condition_expression'], form_data):
                if not value:
                    errors.setdefault(field, []).append(rule['error_message'])

        # CHOICE validation
        elif rule['rule_type'] == 'CHOICE' and value and rule['choice_table']:
            cv_col = rule['choice_value_column'] or 'value'
            valid_values = [r[cv_col] for r in get_choice_values(
                rule['choice_table'].replace('TSS.', ''), cv_col)]
            if value not in valid_values:
                errors.setdefault(field, []).append(rule['error_message'])

        # FORMAT / RANGE
        elif rule['rule_type'] in ('FORMAT', 'RANGE') and value:
            if rule['max_length'] and len(value) > rule['max_length']:
                errors.setdefault(field, []).append(
                    f"{rule['error_message']} (max {rule['max_length']} chars)")
            if rule['regex_pattern']:
                import re
                if not re.match(rule['regex_pattern'], value):
                    errors.setdefault(field, []).append(rule['error_message'])

    return {'valid': len(errors) == 0, 'errors': errors}


def _evaluate_condition(expression, form_data):
    """Simple condition evaluator for validation rules."""
    # Handles: "field == 'value'", "field IN ('a','b','c')"
    try:
        if '==' in expression:
            parts = expression.split('==')
            field = parts[0].strip()
            expected = parts[1].strip().strip("'\"")
            return form_data.get(field, '') == expected
        elif 'IN' in expression.upper():
            field = expression.split('IN')[0].strip()
            values_str = expression.split('IN')[1].strip()
            values = [v.strip().strip("'\"") for v in values_str.strip('()').split(',')]
            return form_data.get(field, '') in values
    except Exception:
        pass
    return False


# ══════════════════════════════════════════════════════════════
#  STATUS BADGE HELPER
# ══════════════════════════════════════════════════════════════

def status_badge_class(status):
    """Return semantic badge class for a given status."""
    return badge_class_for_status(status)


# ══════════════════════════════════════════════════════════════
#  PAGINATION HELPER
# ══════════════════════════════════════════════════════════════

def paginate_query(base_sql, params, page=1, per_page=25):
    """
    Wraps a base SQL query with OFFSET/FETCH pagination.
    Returns (rows, total_count, total_pages).
    """
    offset = (page - 1) * per_page

    with db_cursor() as cur:
        # Count query
        count_sql = f"SELECT COUNT(*) FROM ({base_sql}) AS counted"
        cur.execute(count_sql, params)
        total = cur.fetchone()[0]

        # Paginated query
        paged_sql = f"{base_sql} OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
        cur.execute(paged_sql, params + [offset, per_page])
        columns = [desc[0] for desc in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    total_pages = (total + per_page - 1) // per_page
    return rows, total, total_pages
