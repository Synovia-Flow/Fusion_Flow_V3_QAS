"""Tenant analytics — cross-tenant usage and invoice estimation.

URL prefix : ``/analytics/tenants``. Visible only to the SYD owner tenant;
every other tenant gets a silent 404. Two surfaces:

- Live: reads ``SYD.vw_TenantUsage`` once (single round-trip). The view
  contains conditional ``IIF(OBJECT_ID(...))`` blocks so partially
  provisioned tenant schemas do not crash the query.
- History: reads daily snapshots from ``SYD.TenantUsageSnapshot``. Populated
  by ``scripts/snapshot_tenant_usage.py`` (Render cron, daily 03:00 UTC).

Billing is decorative: per-tenant rate comes from
``AppConfiguration.BILLING.RATE_PER_CONS`` (currency from ``BILLING.CURRENCY``,
default GBP). The Invoice column hides itself when no tenant has a positive
rate, so SYD does not have to look at noise until billing is configured.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date
from decimal import Decimal
from typing import Iterable

from flask import Blueprint, Response, abort, g, render_template, request

from app.db import query_all, query_one
from app.tenant import TENANT_REGISTRY, get_tenant


logger = logging.getLogger(__name__)

tenant_analytics_bp = Blueprint(
    "tenant_analytics",
    __name__,
    template_folder="../../templates/tenant_analytics",
)

OWNER_TENANT_CODE = "SYD"
DEFAULT_CURRENCY = "GBP"
DEFAULT_RATE_PER_CONS = Decimal("0")


@tenant_analytics_bp.before_request
def _gate_to_owner():
    """Silent 404 for every non-SYD tenant. Defense in depth — server side."""
    active = get_tenant()
    if str(active.get("code") or "").upper() != OWNER_TENANT_CODE:
        abort(404)


def _table_exists(schema: str, table_name: str) -> bool:
    row = query_one(
        "SELECT COUNT(*) AS c FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        (schema, table_name),
    ) or {}
    return bool(row.get("c"))


def _billing_for_tenant(tenant_code: str) -> tuple[Decimal, str]:
    """Return (rate_per_consignment, currency) for the given tenant."""
    try:
        from app.config_store import cfg
        raw_rate = cfg.get("BILLING", "RATE_PER_CONS", tenant_code=tenant_code)
        raw_currency = cfg.get("BILLING", "CURRENCY", tenant_code=tenant_code)
    except Exception as exc:
        logger.warning("billing config lookup failed for %s: %s", tenant_code, exc)
        raw_rate = None
        raw_currency = None

    if raw_rate is None:
        try:
            from app.config_store import cfg
            raw_rate = cfg.get("BILLING", "RATE_PER_CONS", tenant_code=OWNER_TENANT_CODE)
            raw_currency = raw_currency or cfg.get("BILLING", "CURRENCY", tenant_code=OWNER_TENANT_CODE)
        except Exception:
            pass

    try:
        rate = Decimal(str(raw_rate)) if raw_rate not in (None, "") else DEFAULT_RATE_PER_CONS
    except Exception:
        rate = DEFAULT_RATE_PER_CONS
    currency = str(raw_currency or DEFAULT_CURRENCY).strip() or DEFAULT_CURRENCY
    return rate, currency


def _live_rows() -> list[dict]:
    """Return one row per tenant from SYD.vw_TenantUsage with billing attached.

    Cached on ``flask.g`` so multiple template lookups in the same request
    share the round-trip. Falls back to an empty list if the view is missing
    (migration 073 not run yet); the page renders an explicit warning.
    """
    cached = getattr(g, "_tenant_analytics_live", None)
    if cached is not None:
        return cached

    if not _table_exists(OWNER_TENANT_CODE, "vw_TenantUsage"):
        g._tenant_analytics_live = []
        return []

    raw = query_all(
        "SELECT tenant_code, ens_count, consignment_count, consignment_submitted, "
        "       consignment_failed, consignment_trader_input, consignments_billed_30d, "
        "       goods_count, sfd_count, sdi_count, sdi_submitted, "
        "       gmr_count, gmr_active, api_calls_7d, api_calls_30d, api_failed_30d "
        "FROM SYD.vw_TenantUsage ORDER BY tenant_code"
    ) or []

    rows: list[dict] = []
    for db_row in raw:
        code = str(db_row.get("tenant_code") or "").upper()
        tenant_meta = TENANT_REGISTRY.get(code) or {}
        rate, currency = _billing_for_tenant(code)
        billed = int(db_row.get("consignments_billed_30d") or 0)
        invoice = float((rate * Decimal(billed)).quantize(Decimal("0.01")))

        rows.append({
            "tenant_code": code,
            "tenant_name": tenant_meta.get("name") or code,
            "schema": tenant_meta.get("schema") or code,
            "ens_count": int(db_row.get("ens_count") or 0),
            "consignment_count": int(db_row.get("consignment_count") or 0),
            "consignment_submitted": int(db_row.get("consignment_submitted") or 0),
            "consignment_failed": int(db_row.get("consignment_failed") or 0),
            "consignment_trader_input": int(db_row.get("consignment_trader_input") or 0),
            "consignments_billed_30d": billed,
            "goods_count": int(db_row.get("goods_count") or 0),
            "sfd_count": int(db_row.get("sfd_count") or 0),
            "sdi_count": int(db_row.get("sdi_count") or 0),
            "sdi_submitted": int(db_row.get("sdi_submitted") or 0),
            "gmr_count": int(db_row.get("gmr_count") or 0),
            "gmr_active": int(db_row.get("gmr_active") or 0),
            "api_calls_7d": int(db_row.get("api_calls_7d") or 0),
            "api_calls_30d": int(db_row.get("api_calls_30d") or 0),
            "api_failed_30d": int(db_row.get("api_failed_30d") or 0),
            "rate_per_consignment": float(rate),
            "invoice_currency": currency,
            "invoice_amount_30d": invoice,
        })

    g._tenant_analytics_live = rows
    return rows


def _kpi_totals(rows: list[dict]) -> dict:
    totals = {
        "tenant_count": len(rows),
        "ens_count": 0,
        "consignment_count": 0,
        "goods_count": 0,
        "sfd_count": 0,
        "sdi_count": 0,
        "gmr_count": 0,
        "api_calls_30d": 0,
        "consignments_billed_30d": 0,
        "invoice_amount_30d": 0.0,
    }
    for row in rows:
        for key in (
            "ens_count",
            "consignment_count",
            "goods_count",
            "sfd_count",
            "sdi_count",
            "gmr_count",
            "api_calls_30d",
            "consignments_billed_30d",
        ):
            totals[key] += row.get(key, 0) or 0
        totals["invoice_amount_30d"] += row.get("invoice_amount_30d") or 0.0
    totals["invoice_amount_30d"] = round(totals["invoice_amount_30d"], 2)
    return totals


def _has_invoice_data(rows: list[dict]) -> bool:
    """Hide invoice column entirely until any tenant has a positive rate."""
    return any((row.get("rate_per_consignment") or 0) > 0 for row in rows)


def _history_rows(days: int = 90, tenant_code: str | None = None) -> list[dict]:
    if not _table_exists(OWNER_TENANT_CODE, "TenantUsageSnapshot"):
        return []
    where = "snapshot_date >= DATEADD(day, ?, CAST(SYSUTCDATETIME() AS DATE))"
    params: list = [-int(days)]
    if tenant_code:
        where += " AND tenant_code = ?"
        params.append(tenant_code.upper())
    try:
        return query_all(
            "SELECT snapshot_date, tenant_code, ens_count, consignment_count, "
            "       consignment_submitted, consignment_failed, goods_count, "
            "       sfd_count, sdi_count, sdi_submitted, gmr_count, gmr_active, "
            "       api_calls_7d, api_calls_30d, api_failed_30d, rate_per_consignment, "
            "       invoice_currency, invoice_amount_30d "
            "FROM SYD.TenantUsageSnapshot "
            f"WHERE {where} "
            "ORDER BY snapshot_date ASC, tenant_code ASC",
            params,
        ) or []
    except Exception as exc:
        logger.warning("tenant analytics history failed: %s", exc)
        return []


@tenant_analytics_bp.route("/")
def index():
    rows = _live_rows()
    totals = _kpi_totals(rows)
    view_available = _table_exists(OWNER_TENANT_CODE, "vw_TenantUsage")
    return render_template(
        "tenant_analytics/index.html",
        rows=rows,
        totals=totals,
        owner_code=OWNER_TENANT_CODE,
        default_currency=DEFAULT_CURRENCY,
        view_available=view_available,
        show_invoice=_has_invoice_data(rows),
        active_tab="tenants",
    )


@tenant_analytics_bp.route("/history")
def history():
    try:
        days = max(7, min(int(request.args.get("days", 90)), 365))
    except Exception:
        days = 90
    tenant_filter = (request.args.get("tenant") or "").strip().upper() or None
    rows = _history_rows(days=days, tenant_code=tenant_filter)
    snapshot_table_available = _table_exists(OWNER_TENANT_CODE, "TenantUsageSnapshot")
    return render_template(
        "tenant_analytics/history.html",
        rows=rows,
        days=days,
        tenant_filter=tenant_filter,
        tenants=sorted(TENANT_REGISTRY.keys()),
        snapshot_table_available=snapshot_table_available,
        active_tab="tenants",
    )


@tenant_analytics_bp.route("/export.csv")
def export_csv():
    rows = _live_rows()
    show_invoice = _has_invoice_data(rows)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header = [
        "tenant_code", "tenant_name", "schema",
        "ens_count", "consignment_count", "consignment_submitted",
        "consignment_failed", "consignment_trader_input", "goods_count",
        "sfd_count", "sdi_count", "sdi_submitted", "gmr_count", "gmr_active",
        "api_calls_7d", "api_calls_30d", "api_failed_30d",
    ]
    if show_invoice:
        header += ["rate_per_consignment", "invoice_currency",
                   "consignments_billed_30d", "invoice_amount_30d"]
    writer.writerow(header)
    for row in rows:
        line = [
            row.get("tenant_code", ""),
            row.get("tenant_name", ""),
            row.get("schema", ""),
            row.get("ens_count", 0),
            row.get("consignment_count", 0),
            row.get("consignment_submitted", 0),
            row.get("consignment_failed", 0),
            row.get("consignment_trader_input", 0),
            row.get("goods_count", 0),
            row.get("sfd_count", 0),
            row.get("sdi_count", 0),
            row.get("sdi_submitted", 0),
            row.get("gmr_count", 0),
            row.get("gmr_active", 0),
            row.get("api_calls_7d", 0),
            row.get("api_calls_30d", 0),
            row.get("api_failed_30d", 0),
        ]
        if show_invoice:
            line += [
                row.get("rate_per_consignment", 0),
                row.get("invoice_currency", DEFAULT_CURRENCY),
                row.get("consignments_billed_30d", 0),
                row.get("invoice_amount_30d", 0),
            ]
        writer.writerow(line)
    snapshot = date.today().isoformat()
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="tenant_analytics_{snapshot}.csv"'},
    )
