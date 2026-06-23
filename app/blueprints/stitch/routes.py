"""Live data endpoints for the static Stitch surfaces.

The HTML lives under /static/stitch so it can remain isolated from the
operational Flask templates. Data still comes from the authenticated app API.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, current_app, jsonify, session

from app.db import query_all, query_one


stitch_bp = Blueprint("stitch", __name__, url_prefix="/api/stitch")
stitch_pages_bp = Blueprint("stitch_pages", __name__, url_prefix="/stitch")


@stitch_pages_bp.get("/dashboard")
def dashboard():
    """Authenticated shell for the Stitch shipment tracking dashboard."""
    return current_app.send_static_file("stitch/shipment_track.html")


def _tenant_code() -> str:
    return session.get("tenant_code") or current_app.config.get("CLIENT_CODE", "BKD")


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row_safe(row: dict | None) -> dict:
    return {k: _json_safe(v) for k, v in (row or {}).items()}


def _as_int(value, default=0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _status_text(row: dict) -> str:
    return (
        row.get("tss_status")
        or row.get("sub_status")
        or row.get("status")
        or "Pending"
    )


def _capacity_pct(row: dict) -> int:
    status = str(_status_text(row)).upper()
    if "AUTHORI" in status or "MOVEMENT" in status:
        return 100
    if "SYNC" in status or row.get("tss_ens_header_ref"):
        return 86
    if "VALID" in status:
        return 72
    if "FAIL" in status or "ERROR" in status or "INVALID" in status:
        return 34
    return 52


def _format_reference(row: dict) -> str:
    return (
        row.get("label")
        or row.get("tss_ens_header_ref")
        or row.get("conveyance_ref")
        or f"ENS-{row.get('stg_header_id') or 'pending'}"
    )


def _selected_payload(row: dict | None) -> dict:
    row = row or {}
    consignments = _as_int(row.get("consignment_count"))
    goods = _as_int(row.get("goods_count"))
    return {
        "id": row.get("stg_header_id"),
        "reference": _format_reference(row),
        "tenant": row.get("ClientCode") or _tenant_code(),
        "status": _status_text(row),
        "stage": "TSS" if row.get("tss_ens_header_ref") else "STG",
        "tss_ref": row.get("tss_ens_header_ref"),
        "vehicle": row.get("identity_no_of_transport") or row.get("conveyance_ref") or "Route A movement",
        "carrier": row.get("carrier_name") or "Carrier pending",
        "arrival_port": row.get("arrival_port") or "UK arrival",
        "arrival_date": _json_safe(row.get("arrival_date_time")) or "Pending",
        "consignments": consignments,
        "goods_items": goods,
        "capacity_pct": _capacity_pct(row),
        "last_sync": _json_safe(row.get("updated_at") or row.get("stg_created_at")),
        "source": row.get("source") or "STG",
    }


def _shipment_payload(row: dict) -> dict:
    return {
        "id": row.get("stg_header_id"),
        "reference": _format_reference(row),
        "status": _status_text(row),
        "stage": "TSS" if row.get("tss_ens_header_ref") else "STG",
        "tss_ref": row.get("tss_ens_header_ref"),
        "vehicle": row.get("identity_no_of_transport") or row.get("conveyance_ref"),
        "arrival_port": row.get("arrival_port"),
        "consignments": _as_int(row.get("consignment_count")),
        "goods_items": _as_int(row.get("goods_count")),
        "last_sync": _json_safe(row.get("updated_at") or row.get("stg_created_at")),
    }


@stitch_bp.get("/shipments/live")
def live_shipments():
    """Return live shipment/ENS data for the Stitch shipment tracker.

    This endpoint is intentionally protected by the normal session guard. The
    static HTML shell can be public, but real shipment data should only hydrate
    for authenticated app users.
    """
    tenant = _tenant_code()
    try:
        summary = query_one(
            """
            SELECT
                COUNT(*) AS active_shipments,
                SUM(CASE
                    WHEN UPPER(COALESCE(h.sub_status, '')) IN ('VALIDATED', 'READY', 'READY_FOR_TSS')
                    THEN 1 ELSE 0 END) AS ready_for_tss,
                SUM(CASE
                    WHEN UPPER(COALESCE(h.sub_status, '')) LIKE '%FAIL%'
                      OR UPPER(COALESCE(h.sub_status, '')) LIKE '%ERROR%'
                      OR UPPER(COALESCE(h.sub_status, '')) LIKE '%INVALID%'
                      OR UPPER(COALESCE(t.TssStatus, '')) LIKE '%TRADER INPUT%'
                    THEN 1 ELSE 0 END) AS in_error,
                SUM(CASE
                    WHEN UPPER(COALESCE(t.TssStatus, '')) LIKE '%AUTHORI%'
                      OR h.movement_notified_at >= DATEADD(day, -1, SYSUTCDATETIME())
                    THEN 1 ELSE 0 END) AS authorised_today
            FROM STG.BKD_ENS_Headers h
            LEFT JOIN TSS.BKD_ENS_Headers t
              ON t.ClientCode = h.ClientCode
             AND NULLIF(t.DeclarationNumber, '') = NULLIF(h.tss_ens_header_ref, '')
            WHERE h.ClientCode = ?
              AND UPPER(COALESCE(h.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
            """,
            [tenant],
        ) or {}

        rows = query_all(
            """
            SELECT TOP 12
                h.stg_header_id,
                h.ClientCode,
                h.label,
                h.source,
                h.sub_status,
                h.tss_ens_header_ref,
                h.conveyance_ref,
                h.identity_no_of_transport,
                h.arrival_date_time,
                h.arrival_port,
                h.carrier_name,
                h.stg_created_at,
                h.updated_at,
                t.TssStatus AS tss_status,
                (SELECT COUNT(*)
                   FROM STG.BKD_ENS_Consignments c
                  WHERE c.ClientCode = h.ClientCode
                    AND c.stg_header_id = h.stg_header_id) AS consignment_count,
                (SELECT COUNT(*)
                   FROM STG.BKD_GoodsItems g
                   JOIN STG.BKD_ENS_Consignments c
                     ON c.ClientCode = g.ClientCode
                    AND c.stg_consignment_id = g.stg_consignment_id
                  WHERE c.ClientCode = h.ClientCode
                    AND c.stg_header_id = h.stg_header_id) AS goods_count
            FROM STG.BKD_ENS_Headers h
            LEFT JOIN TSS.BKD_ENS_Headers t
              ON t.ClientCode = h.ClientCode
             AND NULLIF(t.DeclarationNumber, '') = NULLIF(h.tss_ens_header_ref, '')
            WHERE h.ClientCode = ?
              AND UPPER(COALESCE(h.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
            ORDER BY COALESCE(h.updated_at, h.stg_created_at) DESC, h.stg_header_id DESC
            """,
            [tenant],
        )

        exception_rows = query_all(
            """
            SELECT TOP 5
                h.stg_header_id,
                h.label,
                h.sub_status,
                h.validation_errors_json,
                t.TssStatus AS tss_status,
                h.updated_at
            FROM STG.BKD_ENS_Headers h
            LEFT JOIN TSS.BKD_ENS_Headers t
              ON t.ClientCode = h.ClientCode
             AND NULLIF(t.DeclarationNumber, '') = NULLIF(h.tss_ens_header_ref, '')
            WHERE h.ClientCode = ?
              AND (
                    NULLIF(LTRIM(RTRIM(COALESCE(h.validation_errors_json, ''))), '') IS NOT NULL
                 OR UPPER(COALESCE(h.sub_status, '')) LIKE '%FAIL%'
                 OR UPPER(COALESCE(h.sub_status, '')) LIKE '%ERROR%'
                 OR UPPER(COALESCE(h.sub_status, '')) LIKE '%INVALID%'
                 OR UPPER(COALESCE(t.TssStatus, '')) LIKE '%TRADER INPUT%'
              )
            ORDER BY COALESCE(h.updated_at, h.stg_created_at) DESC, h.stg_header_id DESC
            """,
            [tenant],
        )
    except Exception as exc:  # pragma: no cover - exercised in production logs
        current_app.logger.exception("Stitch live shipment query failed")
        return jsonify({
            "ok": False,
            "tenant": tenant,
            "error": str(exc),
            "summary": {
                "active_shipments": 0,
                "ready_for_tss": 0,
                "in_error": 0,
                "authorised_today": 0,
            },
            "selected": _selected_payload(None),
            "shipments": [],
            "exceptions": [],
        }), 200

    safe_rows = [_row_safe(row) for row in rows]
    selected = _selected_payload(safe_rows[0] if safe_rows else None)
    summary = _row_safe(summary)
    total = _as_int(summary.get("active_shipments"))
    ready = _as_int(summary.get("ready_for_tss"))

    return jsonify({
        "ok": True,
        "tenant": tenant,
        "summary": {
            "active_shipments": total,
            "ready_for_tss": ready,
            "in_error": _as_int(summary.get("in_error")),
            "authorised_today": _as_int(summary.get("authorised_today")),
            "route_optimization_pct": round((ready / total) * 100) if total else 0,
        },
        "selected": selected,
        "shipments": [_shipment_payload(row) for row in safe_rows],
        "exceptions": [
            {
                "id": row.get("stg_header_id"),
                "reference": _format_reference(row),
                "status": _status_text(row),
                "message": row.get("validation_errors_json") or row.get("tss_status") or row.get("sub_status"),
                "last_sync": _json_safe(row.get("updated_at")),
            }
            for row in (_row_safe(r) for r in exception_rows)
        ],
    })
