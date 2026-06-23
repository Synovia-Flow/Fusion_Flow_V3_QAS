"""Write a daily tenant-usage snapshot into SYD.TenantUsageSnapshot.

Reads the live ``SYD.vw_TenantUsage`` view (migration 073) and writes one
row per tenant via MERGE so re-running the cron on the same day is
idempotent. Billing rate is applied per row from AppConfiguration.

Usage:
    python scripts/snapshot_tenant_usage.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from decimal import Decimal

# Allow running as ``python scripts/snapshot_tenant_usage.py`` from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.blueprints.tenant_analytics.routes import _billing_for_tenant  # noqa: E402
from app.db import db_cursor, query_all  # noqa: E402


logger = logging.getLogger("snapshot_tenant_usage")


MERGE_SQL = """
MERGE SYD.TenantUsageSnapshot AS target
USING (SELECT ? AS snapshot_date, ? AS tenant_code) AS source
   ON target.snapshot_date = source.snapshot_date AND target.tenant_code = source.tenant_code
WHEN MATCHED THEN UPDATE SET
    ens_count = ?, consignment_count = ?, consignment_submitted = ?,
    consignment_failed = ?, goods_count = ?, sfd_count = ?,
    sdi_count = ?, sdi_submitted = ?, gmr_count = ?, gmr_active = ?,
    api_calls_7d = ?, api_calls_30d = ?, api_failed_30d = ?,
    rate_per_consignment = ?, invoice_currency = ?, invoice_amount_30d = ?
WHEN NOT MATCHED THEN INSERT (
    snapshot_date, tenant_code, ens_count, consignment_count,
    consignment_submitted, consignment_failed, goods_count, sfd_count,
    sdi_count, sdi_submitted, gmr_count, gmr_active,
    api_calls_7d, api_calls_30d, api_failed_30d,
    rate_per_consignment, invoice_currency, invoice_amount_30d
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=None,
        help="Override snapshot date (YYYY-MM-DD). Defaults to today UTC.",
    )
    return parser.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args()
    snapshot_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    )
    logger.info("Snapshot date: %s", snapshot_date.isoformat())

    app = create_app()
    with app.test_request_context("/analytics/tenants/"):
        from flask import session
        session["tenant_code"] = "SYD"
        session["tenant_name"] = "Synovia Digital"
        session["logged_in"] = True

        rows = query_all(
            "SELECT tenant_code, ens_count, consignment_count, consignment_submitted, "
            "       consignment_failed, consignments_billed_30d, goods_count, sfd_count, "
            "       sdi_count, sdi_submitted, gmr_count, gmr_active, "
            "       api_calls_7d, api_calls_30d, api_failed_30d "
            "FROM SYD.vw_TenantUsage ORDER BY tenant_code"
        ) or []
        logger.info("Tenants in view: %d", len(rows))

        with db_cursor(commit=True) as cur:
            for row in rows:
                tenant_code = str(row.get("tenant_code") or "").upper()
                if not tenant_code:
                    continue
                rate, currency = _billing_for_tenant(tenant_code)
                billed = int(row.get("consignments_billed_30d") or 0)
                invoice = float((rate * Decimal(billed)).quantize(Decimal("0.01")))

                params = [
                    snapshot_date, tenant_code,
                    # UPDATE block
                    int(row.get("ens_count") or 0),
                    int(row.get("consignment_count") or 0),
                    int(row.get("consignment_submitted") or 0),
                    int(row.get("consignment_failed") or 0),
                    int(row.get("goods_count") or 0),
                    int(row.get("sfd_count") or 0),
                    int(row.get("sdi_count") or 0),
                    int(row.get("sdi_submitted") or 0),
                    int(row.get("gmr_count") or 0),
                    int(row.get("gmr_active") or 0),
                    int(row.get("api_calls_7d") or 0),
                    int(row.get("api_calls_30d") or 0),
                    int(row.get("api_failed_30d") or 0),
                    float(rate), currency, invoice,
                    # INSERT block
                    snapshot_date, tenant_code,
                    int(row.get("ens_count") or 0),
                    int(row.get("consignment_count") or 0),
                    int(row.get("consignment_submitted") or 0),
                    int(row.get("consignment_failed") or 0),
                    int(row.get("goods_count") or 0),
                    int(row.get("sfd_count") or 0),
                    int(row.get("sdi_count") or 0),
                    int(row.get("sdi_submitted") or 0),
                    int(row.get("gmr_count") or 0),
                    int(row.get("gmr_active") or 0),
                    int(row.get("api_calls_7d") or 0),
                    int(row.get("api_calls_30d") or 0),
                    int(row.get("api_failed_30d") or 0),
                    float(rate), currency, invoice,
                ]
                cur.execute(MERGE_SQL, params)
                logger.info(
                    "  %s: cons=%d submitted=%d goods=%d sdi=%d invoice=%s %.2f",
                    tenant_code,
                    int(row.get("consignment_count") or 0),
                    int(row.get("consignment_submitted") or 0),
                    int(row.get("goods_count") or 0),
                    int(row.get("sdi_count") or 0),
                    currency, invoice,
                )

    logger.info("Snapshot complete.")


if __name__ == "__main__":
    main()
