#!/usr/bin/env python3
"""03 - Sales Orders Excel to consignments/goods and TSS cargo submit.

Finds ENS headers with a live ENS reference and pending consignments/goods, then
uses the existing Fusion cargo submitter. This preserves TDN grouping, goods
payload validation, TSS consignment creation, goods creation, completion check,
and consignment submit in one operational step.

Set FUSION_FLOW_APP_ROOT when this QAS folder does not contain the Flask app.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _app_root() -> Path:
    candidate = Path(os.environ.get("FUSION_FLOW_APP_ROOT") or Path(__file__).resolve().parents[2])
    if not (candidate / "app").exists():
        raise SystemExit(
            "Fusion app root not found. Set FUSION_FLOW_APP_ROOT to the repo that contains app/ and scripts/."
        )
    sys.path.insert(0, str(candidate))
    try:
        from dotenv import load_dotenv
        load_dotenv(candidate / ".env")
    except Exception:
        pass
    return candidate


ROOT = _app_root()

from app.db import db_cursor  # noqa: E402
from app.blueprints.declarations.routes import _submit_prd_cargo_for_header  # noqa: E402


def _candidate_header_ids(tenant_code: str, limit: int) -> list[int]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP (?) h.stg_header_id
            FROM [STG].[BKD_ENS_Headers] h
            WHERE h.ClientCode = ?
              AND NULLIF(LTRIM(RTRIM(COALESCE(h.tss_ens_header_ref, ''))), '') IS NOT NULL
              AND UPPER(COALESCE(h.sub_status, '')) NOT IN ('CANCELLED', 'CANCELED', 'DELETED')
              AND EXISTS (
                    SELECT 1
                    FROM [STG].[BKD_ENS_Consignments] c
                    LEFT JOIN [STG].[BKD_GoodsItems] g
                      ON g.ClientCode = c.ClientCode
                     AND g.stg_consignment_id = c.stg_consignment_id
                    WHERE c.ClientCode = h.ClientCode
                      AND c.stg_header_id = h.stg_header_id
                      AND UPPER(COALESCE(c.sub_status, '')) NOT IN ('CANCELLED', 'CANCELED', 'DELETED', 'COMPLETED')
                      AND (
                            NULLIF(LTRIM(RTRIM(COALESCE(c.tss_consignment_ref, ''))), '') IS NULL
                            OR UPPER(COALESCE(c.sub_status, '')) NOT IN ('SUBMITTED', 'COMPLETED')
                            OR NULLIF(LTRIM(RTRIM(COALESCE(g.tss_hex_id, ''))), '') IS NULL
                          )
              )
            ORDER BY COALESCE(h.stg_created_at, h.created_at, h.updated_at) DESC, h.stg_header_id DESC
            """,
            [limit, tenant_code],
        )
        return [int(row[0]) for row in cursor.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-code", default=os.environ.get("TENANT_CODE") or "BKD")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--header-id", type=int, action="append", default=[])
    args = parser.parse_args()

    tenant_code = str(args.tenant_code or "BKD").strip().upper()
    header_ids = args.header_id or _candidate_header_ids(tenant_code, args.limit)
    print(f"FLOW V3 03 -> Sales Orders cargo submit tenant={tenant_code} headers={header_ids}")

    results = []
    failures = 0
    for header_id in header_ids:
        summary = _submit_prd_cargo_for_header(header_id, client_code=tenant_code)
        results.append({"stg_header_id": header_id, **summary})
        if summary.get("cons_failed") or summary.get("goods_failed"):
            failures += 1

    print(json.dumps(results, indent=2, default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
