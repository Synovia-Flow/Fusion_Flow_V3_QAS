# ============================================================
# app/config_store.py — Fusion Flow V2
#
# DB-backed configuration store.  Per-tenant cache.
# Table: [{schema}].AppConfiguration  (schema = BKD, CWF, …)
#
# Usage — web (auto-resolves tenant from flask.session):
#   from app.config_store import cfg
#   url = cfg.get("TSS_API", "BASE_URL")
#
# Usage — explicit tenant (cron jobs):
#   from app.config_store import get
#   url = get("TSS_API", "BASE_URL", tenant_code="BKD")
#
# Lookup order:
#   1. DB cache  ([schema].AppConfiguration)
#   2. Environment variable  CATEGORY_KEY  (e.g. TSS_API_PASSWORD)
#   3. fallback string
#
# Falls back to env vars if the DB table is not yet seeded or
# unavailable (safe during initial bootstrap).
# ============================================================

import os
import logging

from app.tenant import get_tenant_by_code, normalize_tenant_code

logger = logging.getLogger(__name__)

# ── Per-tenant caches ────────────────────────────────────────
# _caches[tenant_code] = {"CATEGORY.KEY": "value", …}
_caches = {}   # dict[str, dict[str, str]]
_loaded = set()  # set of tenant_codes already loaded


def _resolve_tenant(tenant_code=None):
    """
    Return explicit tenant_code or infer from Flask session.
    Falls back to 'BKD' outside Flask context or when session is empty.
    """
    if tenant_code:
        try:
            return normalize_tenant_code(tenant_code)
        except ValueError:
            return "BKD"
    try:
        from flask import session
        code = session.get("tenant_code")
        if code:
            try:
                return normalize_tenant_code(code)
            except ValueError:
                return "BKD"
    except RuntimeError:
        pass  # Outside Flask application context
    env_code = os.environ.get("TENANT_CODE") or os.environ.get("CLIENT_CODE")
    if env_code:
        try:
            return normalize_tenant_code(env_code)
        except ValueError:
            return "BKD"
    return "BKD"


def _schema_for_tenant(tenant_code):
    try:
        return get_tenant_by_code(tenant_code)["schema"]
    except (KeyError, ValueError):
        return tenant_code


def _load(tenant_code):
    """Load all rows from [{schema}].AppConfiguration into per-tenant cache."""
    if tenant_code in _loaded:
        return

    schema = _schema_for_tenant(tenant_code)
    try:
        from app.db import get_standalone_connection
        conn = get_standalone_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT category, config_key, config_value "
            f"FROM [{schema}].AppConfiguration"
        )
        cache = {}
        for row in cursor.fetchall():
            cache[f"{row[0]}.{row[1]}"] = row[2] or ""
        cursor.close()
        conn.close()
        _caches[tenant_code] = cache
        _loaded.add(tenant_code)
        logger.info("AppConfiguration loaded for %s — %d entries.", tenant_code, len(cache))
    except Exception as exc:
        # Non-fatal: fall back to env vars
        logger.warning("AppConfiguration unavailable for %s, using env vars: %s", tenant_code, exc)
        _caches[tenant_code] = {}
        _loaded.add(tenant_code)  # Don't retry on every call


def reload(tenant_code=None):
    """
    Force a full reload from DB.

    reload()                — reload active tenant (from session) or BKD
    reload("CWF")           — reload specific tenant
    """
    code = _resolve_tenant(tenant_code)
    _caches.pop(code, None)
    _loaded.discard(code)
    _load(code)


def get(category, key, fallback="", tenant_code=None):
    """Return config value for (category, key) under the active tenant."""
    code = _resolve_tenant(tenant_code)
    _load(code)
    cache_key = f"{category}.{key}"

    val = _caches.get(code, {}).get(cache_key)
    if val is not None:
        return val

    # Env var fallback — e.g. TSS_API_BASE_URL or SMTP_SERVER
    env_key = f"{category}_{key}"
    env_val = os.environ.get(env_key, "")
    if env_val:
        return env_val

    return fallback


def get_db_value(category, key, tenant_code=None):
    """
    Return only the AppConfiguration value for (category, key).

    Unlike get(), this does not fall back to environment variables. Use this
    when callers need to distinguish tenant config from process env fallback.
    """
    code = _resolve_tenant(tenant_code)
    _load(code)
    return _caches.get(code, {}).get(f"{category}.{key}")


# ── Convenience singleton ────────────────────────────────────
# Keeps existing call sites unchanged:
#   cfg.get("TSS_API", "BASE_URL")
#   cfg.reload()
class _Cfg:
    @staticmethod
    def get(category, key, fallback="", tenant_code=None):
        return get(category, key, fallback=fallback, tenant_code=tenant_code)

    @staticmethod
    def get_db_value(category, key, tenant_code=None):
        return get_db_value(category, key, tenant_code=tenant_code)

    @staticmethod
    def reload(tenant_code=None):
        return reload(tenant_code)


cfg = _Cfg()
