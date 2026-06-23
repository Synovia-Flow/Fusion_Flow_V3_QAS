"""Feature flags and helpers for the ING/STG/TSS production model.

New-model reads are the default on this production branch. Legacy and
compatibility modes remain explicit overrides for diagnostics only.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping


logger = logging.getLogger(__name__)

READ_MODES = {"legacy", "compat", "new"}
DUAL_WRITE_MODES = {"off", "shadow", "strict"}


def _normalise_flow(flow: str | None) -> str | None:
    if not flow:
        return None
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(flow).strip().upper())
    return cleaned or None


def _flag_value(base_name: str, default: str, flow: str | None = None, env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    flow_name = _normalise_flow(flow)
    candidates = []
    if flow_name:
        candidates.extend(
            [
                f"{base_name}_{flow_name}",
                f"FUSION_DATA_MODEL_{flow_name}_{base_name.removeprefix('FUSION_DATA_MODEL_')}",
            ]
        )
    candidates.append(base_name)

    for key in candidates:
        value = source.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().lower()
    return default


def data_model_read_mode(flow: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Return the configured read mode for a flow, defaulting to new."""

    mode = _flag_value("FUSION_DATA_MODEL_READ_MODE", "new", flow=flow, env=env)
    return mode if mode in READ_MODES else "new"


def data_model_dual_write_mode(flow: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Return the configured dual-write mode for a flow, defaulting to off."""

    mode = _flag_value("FUSION_DATA_MODEL_DUAL_WRITE", "off", flow=flow, env=env)
    return mode if mode in DUAL_WRITE_MODES else "off"


def dual_write_enabled(flow: str | None = None, env: Mapping[str, str] | None = None) -> bool:
    return data_model_dual_write_mode(flow=flow, env=env) in {"shadow", "strict"}


def dual_write_strict(flow: str | None = None, env: Mapping[str, str] | None = None) -> bool:
    return data_model_dual_write_mode(flow=flow, env=env) == "strict"


def _json_text(value: Any, max_len: int = 4000) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:max_len]
    try:
        return json.dumps(value, ensure_ascii=True, default=str)[:max_len]
    except TypeError:
        return json.dumps(str(value), ensure_ascii=True)[:max_len]


def infer_tss_entity_kind(call_type: str | None = None, url: str | None = None) -> str:
    """Best-effort entity classification for API audit rows."""

    text = f"{call_type or ''} {url or ''}".upper()
    if "SUPPLEMENTARY" in text or "SUPDEC" in text or "SDI" in text or "SUP_" in text:
        return "SDI"
    if "GVMS" in text or "GMR" in text:
        return "GMR"
    if "GOODS" in text:
        return "GOODS"
    if "SFD" in text or "SIMPLIFIED_FRONTIER" in text:
        return "SFD"
    if "CONSIGNMENT" in text or "DEC" in text:
        return "CONSIGNMENT"
    if "HEADER" in text or "ENS" in text:
        return "ENS"
    if "EMAIL" in text:
        return "EMAIL"
    if "JOB" in text or "ORCHESTRATOR" in text:
        return "JOB"
    return "TSS_API"


def insert_tss_api_exchange(
    cursor,
    *,
    schema_name: str,
    legacy_api_call_log_id: int | None,
    call_type: str | None,
    staging_id: int | None,
    http_method: str | None,
    url: str | None,
    request_payload: Any = None,
    http_status: int | None = None,
    response_status: str | None = None,
    response_message: str | None = None,
    response_json: Any = None,
    duration_ms: int | None = None,
    error_detail: str | None = None,
):
    """Insert the new TSS API exchange row.

    This function deliberately assumes the TSS migration has been applied. The
    caller decides whether errors are shadow-logged or strict failures.
    """

    client_code = str(schema_name or "BKD").strip().upper()[:10]
    entity_kind = infer_tss_entity_kind(call_type, url)
    cursor.execute(
        """
        INSERT INTO [TSS].[BKD_API_Exchanges]
            (ClientCode, LegacySchemaName, LegacyApiCallLogId, Flow, EntityKind,
             EntityId, CallType, HttpMethod, Url, RequestPayloadJson, HttpStatus,
             ResponseStatus, ResponseMessage, ResponseJson, DurationMs,
             ErrorDetail, CalledAt)
        OUTPUT INSERTED.ApiExchangeId
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME())
        """,
        [
            client_code,
            client_code,
            legacy_api_call_log_id,
            "TSS_API",
            entity_kind,
            staging_id,
            str(call_type or "")[:80],
            str(http_method or "GET")[:20],
            str(url or "")[:500],
            _json_text(request_payload),
            http_status,
            str(response_status)[:80] if response_status is not None else None,
            str(response_message)[:1000] if response_message is not None else None,
            _json_text(response_json),
            duration_ms,
            str(error_detail)[:2000] if error_detail is not None else None,
        ],
    )
    row = cursor.fetchone()
    return row[0] if row else None


def maybe_dual_write_tss_api_exchange(cursor, *, flow: str = "TSS_API", **kwargs) -> bool:
    """Dual-write a TSS API exchange according to migration flags.

    Returns True when a new-model row was written. In shadow mode failures are
    logged and suppressed. In strict mode failures propagate to the caller.
    """

    if not dual_write_enabled(flow):
        return False

    try:
        insert_tss_api_exchange(cursor, **kwargs)
        return True
    except Exception as exc:
        if dual_write_strict(flow):
            raise
        logger.warning("Shadow TSS API exchange dual-write failed: %s", exc)
        return False
