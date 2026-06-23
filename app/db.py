"""
Database Connection Manager - pyodbc + Azure SQL
"""
import json
import logging
from contextlib import contextmanager

import pyodbc
from flask import current_app, g

from app.tenant import tenant_aware_cursor, tenantize_sql
from config.db_connection import build_connection_string as resolve_connection_string
from config.db_connection import detect_odbc_driver as resolve_odbc_driver

pyodbc.pooling = False
logger = logging.getLogger(__name__)


def detect_odbc_driver():
    return resolve_odbc_driver()


def build_connection_string(timeout=5):
    return resolve_connection_string(current_app.config, timeout=timeout)


def get_db():
    if 'db_conn' not in g:
        try:
            g.db_conn = pyodbc.connect(build_connection_string(), autocommit=False)
            g.db_conn.timeout = 30
        except pyodbc.Error as e:
            logger.error(f"Database connection failed: {e}")
            raise
    return g.db_conn


def close_db(exception=None):
    conn = g.pop('db_conn', None)
    if conn is not None:
        try:
            if exception:
                conn.rollback()
            conn.close()
        except Exception:
            pass


@contextmanager
def db_cursor(commit=True):
    conn = get_db()
    cursor = conn.cursor()
    try:
        yield tenant_aware_cursor(cursor)
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


@contextmanager
def standalone_connection():
    conn = pyodbc.connect(resolve_connection_string(timeout=5), autocommit=False)
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def get_standalone_connection():
    """Compatibility helper for standalone scripts and maintenance jobs."""
    conn = pyodbc.connect(resolve_connection_string(timeout=5), autocommit=False)
    conn.timeout = 30
    return conn


def init_db(app):
    app.teardown_appcontext(close_db)


def query_all(sql, params=None):
    with db_cursor(commit=False) as cursor:
        cursor.execute(tenantize_sql(sql), params or [])
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def query_one(sql, params=None):
    with db_cursor(commit=False) as cursor:
        cursor.execute(tenantize_sql(sql), params or [])
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [col[0] for col in cursor.description]
        return dict(zip(columns, row))


def execute(sql, params=None):
    with db_cursor() as cursor:
        cursor.execute(tenantize_sql(sql), params or [])
        return cursor.rowcount


def insert_api_call_log(
    schema_name,
    call_type,
    *,
    staging_id=None,
    http_method="GET",
    url="",
    request_payload=None,
    http_status=None,
    response_status=None,
    response_message=None,
    response_json=None,
    duration_ms=None,
    error_detail=None,
):
    """Write one API exchange record to TSS.BKD_API_Exchanges. Returns ApiExchangeId or None."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        from app.data_model import insert_tss_api_exchange

        new_id = insert_tss_api_exchange(
            cursor,
            schema_name=schema_name,
            legacy_api_call_log_id=None,
            call_type=call_type,
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
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        logger.exception("TSS.BKD_API_Exchanges insert failed for %s", call_type)
        return None
    finally:
        cursor.close()
