"""Best-effort portal login audit.

Ported from Fusion_Flow_V2_BKD dev02 (commit 11bc217, app/blueprints/auth/routes.py),
which records every login attempt in AUTH.LoginAudit and resolves the actor
against AUTH.Users.

Neither table exists in the V3 QAS database yet; the proposed DDL is in
Configuration/SQL/036_auth_login_audit.sql and has NOT been applied. Until it
is, record_login_audit() is a no-op. In V2 this was best-effort by design - an
audit write must never block or fail a login - and that property is kept here:
every failure path returns None.
"""
from __future__ import annotations

from typing import Any

from .db import DbUnavailable, execute_scalar

AUDIT_TABLE = "AUTH.LoginAudit"
USERS_TABLE = "AUTH.Users"

EVENT_LOGIN_SUCCESS = "LOGIN_SUCCESS"
EVENT_LOGIN_FAILURE = "LOGIN_FAILURE"
EVENT_LOGOUT = "LOGOUT"


def _table_exists(table_name: str) -> bool:
    try:
        return bool(execute_scalar("SELECT OBJECT_ID(?, 'U')", [table_name]))
    except Exception:
        return False


def client_ip_address(request: Any) -> str:
    headers = getattr(request, "headers", {}) or {}
    forwarded_for = headers.get("X-Forwarded-For") or ""
    client_host = getattr(getattr(request, "client", None), "host", "") or ""
    ip_address = (forwarded_for.split(",", 1)[0] or headers.get("X-Real-IP") or client_host or "").strip()
    return ip_address[:64]


def request_user_agent(request: Any) -> str:
    headers = getattr(request, "headers", {}) or {}
    return (headers.get("User-Agent") or "")[:500]


def request_correlation_id(request: Any, correlation_id: str | None = None) -> str:
    headers = getattr(request, "headers", {}) or {}
    return (correlation_id or headers.get("X-Request-ID") or headers.get("X-Correlation-ID") or "")[:120]


def _lookup_auth_user_id(username: str) -> int | None:
    normalised = str(username or "").strip().upper()
    if not normalised or not _table_exists(USERS_TABLE):
        return None
    try:
        user_id = execute_scalar(
            f"SELECT TOP 1 UserId FROM {USERS_TABLE} WHERE NormalizedUsername = ?",
            [normalised],
        )
    except Exception:
        return None
    return int(user_id) if user_id is not None else None


def record_login_audit(
    request: Any,
    *,
    event_type: str,
    success: bool,
    username: str = "",
    email: str = "",
    client_code: str | None = None,
    env_code: str | None = None,
    failure_reason: str | None = None,
    correlation_id: str | None = None,
) -> int | None:
    """Record one login attempt. Returns the audit id, or None if not recorded.

    Never raises: a portal that cannot audit must still let people in.
    """
    if not _table_exists(AUDIT_TABLE):
        return None

    username = str(username or "").strip()
    resolved_client_code = str(client_code or "").strip().upper() or None
    try:
        audit_id = execute_scalar(
            f"""
            INSERT INTO {AUDIT_TABLE} (
                UserId, Email, Username, ClientCode, EnvCode,
                EventType, Success, FailureReason, IpAddress, UserAgent, CorrelationId
            )
            OUTPUT INSERTED.LoginAuditId
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                _lookup_auth_user_id(username),
                (email or None),
                (username or None),
                resolved_client_code,
                (str(env_code).strip().upper() if env_code else None),
                event_type,
                1 if success else 0,
                (failure_reason or None),
                client_ip_address(request) or None,
                request_user_agent(request) or None,
                request_correlation_id(request, correlation_id) or None,
            ],
        )
    except DbUnavailable:
        return None
    except Exception:
        return None
    return int(audit_id) if audit_id is not None else None
