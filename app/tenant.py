"""Tenant registry and SQL schema helpers for Fusion Flow.

The tenant code is the three-character business prefix that maps one login to
one database schema, for example BKD -> Birkdale, CWF -> Countrywide,
CLR -> Clarity Cargo, or PLE -> Primeline Express.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any


TENANT_CODE_RE = re.compile(r"^[A-Z0-9]{3}$")
_LEGACY_BKD_SCHEMA_RE = re.compile(r"(?<![\w\]])(?:\[BKD\]|BKD)\.")


DEFAULT_TENANTS: dict[str, dict[str, str]] = {
    "BKD": {
        "code": "BKD",
        "name": "Birkdale",
        "schema": "BKD",
        "username": "birkdale",
        "password": "admin",
    },
    "CWF": {
        "code": "CWF",
        "name": "Countrywide",
        "schema": "CWF",
        "username": "countrywide",
        "password": "admin",
    },
    "CLR": {
        "code": "CLR",
        "name": "Clarity Cargo",
        "schema": "CLR",
        "username": "claritycargo",
        "password": "admin",
    },
    "PLE": {
        "code": "PLE",
        "name": "Primeline Express",
        "schema": "PLE",
        "username": "primeline",
        "password": "admin",
    },
    "SYD": {
        "code": "SYD",
        "name": "Synovia Digital",
        "schema": "SYD",
        "username": "synovia",
        "password": "admin",
    },
}


def normalize_tenant_code(code: str | None) -> str:
    """Return an uppercase three-character tenant code or raise ValueError."""
    normalized = str(code or "").strip().upper()
    if not TENANT_CODE_RE.match(normalized):
        raise ValueError("Tenant code must be exactly 3 alphanumeric characters.")
    return normalized


def _tenant_entry(raw: dict[str, Any]) -> dict[str, str]:
    code = normalize_tenant_code(raw.get("code"))
    schema = normalize_tenant_code(raw.get("schema") or code)
    return {
        "code": code,
        "name": str(raw.get("name") or code).strip() or code,
        "schema": schema,
        "username": str(raw.get("username") or code.lower()).strip(),
        "password": str(raw.get("password") or "admin"),
    }


def _load_env_tenants() -> dict[str, dict[str, str]]:
    """Load optional tenant definitions from FUSION_TENANTS_JSON.

    Shape accepted:
      [{"code":"ABC","name":"Acme","schema":"ABC","username":"acme","password":"..."}]

    TENANT_REGISTRY_JSON is accepted as a backwards-compatible alias.
    """
    raw = os.environ.get("FUSION_TENANTS_JSON") or os.environ.get("TENANT_REGISTRY_JSON")
    if not raw:
        return {}

    data = json.loads(raw)
    entries = data.values() if isinstance(data, dict) else data
    tenants: dict[str, dict[str, str]] = {}
    for entry in entries:
        tenant = _tenant_entry(entry)
        tenants[tenant["code"]] = tenant
    return tenants


def _build_registry() -> dict[str, dict[str, str]]:
    registry = {code: dict(tenant) for code, tenant in DEFAULT_TENANTS.items()}
    registry.update(_load_env_tenants())
    return registry


TENANT_REGISTRY = _build_registry()

# Reverse lookup: username -> tenant dict
_BY_USERNAME = {t["username"]: t for t in TENANT_REGISTRY.values()}


def get_tenant_by_credentials(username, password):
    """Return tenant dict if username+password match, else None."""
    tenant = _BY_USERNAME.get(str(username or "").strip())
    if tenant and tenant["password"] == password:
        return tenant
    return None


def get_tenant_by_code(code):
    """Return tenant dict by tenant_code. Raises KeyError for unknown code."""
    return TENANT_REGISTRY[normalize_tenant_code(code)]


def get_tenant():
    """Return active tenant dict for the current Flask request, defaulting to BKD."""
    try:
        from flask import request, session

        code = session.get("tenant_code")
        explicit = (
            request.headers.get("X-Tenant-Code")
            or request.form.get("tenant_code")
            or request.args.get("tenant_code")
            or ""
        ).strip().upper()
        if explicit:
            try:
                explicit_tenant = get_tenant_by_code(explicit)
                session_code = normalize_tenant_code(code) if code else ""
                if not session_code or session_code == "SYD" or session_code == explicit_tenant["code"]:
                    return explicit_tenant
            except (KeyError, ValueError):
                pass
        if code:
            return get_tenant_by_code(code)
    except (RuntimeError, KeyError, ValueError):
        pass

    env_code = os.environ.get("TENANT_CODE") or os.environ.get("CLIENT_CODE")
    if env_code:
        try:
            return get_tenant_by_code(env_code)
        except (KeyError, ValueError):
            pass

    return TENANT_REGISTRY["BKD"]


def quote_schema(schema_name: str | None = None) -> str:
    """Return a safely bracketed SQL Server schema name for tenant SQL."""
    schema = normalize_tenant_code(schema_name or get_tenant()["schema"])
    return f"[{schema}]"


def qualified_table(table_name: str, schema_name: str | None = None) -> str:
    """Return [SCHEMA].[TableName] for the active tenant schema."""
    safe_table = str(table_name or "").replace("]", "]]")
    return f"{quote_schema(schema_name)}.[{safe_table}]"


def tenantize_sql(sql: str, schema_name: str | None = None) -> str:
    """Rewrite legacy BKD-qualified SQL to the active tenant schema.

    This is a bridge for older routes that still say BKD.Table. New code should
    prefer qualified_table() so tenant scope is explicit at the call site.
    """
    schema = normalize_tenant_code(schema_name or get_tenant()["schema"])
    if schema == "BKD" or not isinstance(sql, str):
        return sql
    return _LEGACY_BKD_SCHEMA_RE.sub(f"[{schema}].", sql)


class TenantAwareCursor:
    """Small pyodbc cursor proxy that tenantizes SQL before execution."""

    def __init__(self, cursor):
        object.__setattr__(self, "_cursor", cursor)

    def execute(self, sql, *params):
        self._cursor.execute(tenantize_sql(sql), *tenantize_params(sql, params))
        return self

    def executemany(self, sql, params):
        self._cursor.executemany(tenantize_sql(sql), params)
        return self

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __setattr__(self, name, value):
        if name == "_cursor":
            object.__setattr__(self, name, value)
        else:
            setattr(self._cursor, name, value)


def tenant_aware_cursor(cursor):
    return TenantAwareCursor(cursor)


def tenantize_params(sql: str, params):
    """Rewrite legacy BKD schema parameters in metadata queries.

    A lot of older code builds object SQL as ``BKD.Table`` but checks columns
    with ``WHERE TABLE_SCHEMA = ?`` and passes ``"BKD"`` as a parameter. The
    SQL text rewrite cannot see that parameter, so this covers metadata checks
    without touching normal business values.
    """
    schema = normalize_tenant_code(get_tenant()["schema"])
    if schema == "BKD" or not isinstance(sql, str) or not params:
        return params

    upper_sql = sql.upper()
    is_metadata_query = (
        "INFORMATION_SCHEMA." in upper_sql
        or "SYS.SCHEMAS" in upper_sql
        or "SCHEMA_ID" in upper_sql
        or "OBJECT_ID" in upper_sql
        or "COL_LENGTH" in upper_sql
    )
    if not is_metadata_query:
        return params

    def _replace(value):
        if isinstance(value, str) and value.strip().upper() == "BKD":
            return schema
        if isinstance(value, list):
            return [_replace(item) for item in value]
        if isinstance(value, tuple):
            return tuple(_replace(item) for item in value)
        return value

    return tuple(_replace(param) for param in params)
