#!/usr/bin/env python3
"""02 - DETAILS email to ENS header and TSS auto-submit.

Runs the existing Fusion application logic that parses staged DETAILS emails,
builds the ENS header payload, validates it, and submits the ENS header to TSS.

Uses the local QAS app/ package copied into this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _app_root() -> Path:
    candidate = Path(__file__).resolve().parents[2]
    if not (candidate / "app").exists():
        raise SystemExit(
            "Local QAS app root not found. Expected app/ and scripts/ in this repository."
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
from app.blueprints.ingest.routes import _auto_validate_and_submit_stg_ens_header  # noqa: E402


def _candidate_header_ids(tenant_code: str, limit: int) -> list[int]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT TOP (?) h.stg_header_id
            FROM [STG].[BKD_ENS_Headers] h
            WHERE h.ClientCode = ?
              AND UPPER(COALESCE(h.source, '')) = 'EXCEL_SALES_ORDERS_DETAILS'
              AND NULLIF(LTRIM(RTRIM(COALESCE(h.tss_ens_header_ref, ''))), '') IS NULL
              AND UPPER(COALESCE(h.sub_status, '')) NOT IN ('CANCELLED', 'CANCELED', 'DELETED')
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
    print(f"FLOW V3 02 -> ENS DETAILS auto-submit tenant={tenant_code} headers={header_ids}")

    results = []
    failures = 0
    for header_id in header_ids:
        result = _auto_validate_and_submit_stg_ens_header(header_id, tenant_code=tenant_code)
        results.append({"stg_header_id": header_id, **result})
        if not result.get("ok"):
            failures += 1

    print(json.dumps(results, indent=2, default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
