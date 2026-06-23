#!/usr/bin/env python3
"""Run a PRD-safe SDI/SupDec status sync.

This is the cron-safe companion to the SDI autosubmit worker. Autosubmit
discovers/stages SDIs from SFDs; this script refreshes already-known SUPDEC
references so status changes such as Trader Input Required, Pending Payment,
or Closed are pulled into the STG/TSS mirrors without an operator click.

No legacy BKD.Staging* tables or legacy pipeline scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

try:  # Local convenience only; production can use process env/AppConfiguration.
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT, ".env"))
except Exception:
    pass

from app.db import db_cursor  # noqa: E402


TERMINAL_SDI_STATUSES = (
    "CANCELLED",
    "CANCELED",
    "CLOSED",
    "COMPLETED",
    "DELETED",
)

ATTENTION_SDI_STATUSES = (
    "TRADER INPUT REQUIRED",
    "PENDING PAYMENT",
    "PROCESSING",
    "DRAFT",
    "PENDING REVIEW",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-code",
        default=os.environ.get("TENANT_CODE") or os.environ.get("CLIENT_CODE") or "BKD",
        help="Tenant code to process. Defaults to TENANT_CODE, CLIENT_CODE, then BKD.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_env_int("FUSION_PRD_SDI_SYNC_LIMIT", 10),
        help="Maximum SDI/SUPDEC rows to sync in one cron tick. Default: FUSION_PRD_SDI_SYNC_LIMIT or 10.",
    )
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=_env_int("FUSION_PRD_SDI_SYNC_MIN_AGE_MINUTES", 60),
        help="Skip SDIs synced more recently than this. Default: 60.",
    )
    return parser.parse_args(argv)


def _status_sql() -> str:
    return (
        "UPPER(REPLACE(COALESCE(NULLIF(tss_h.TssStatus, ''), "
        "NULLIF(h.tss_status, ''), NULLIF(h.sub_status, ''), ''), '_', ' '))"
    )


def candidate_sdis(client_code: str, *, limit: int, min_age_minutes: int) -> list[dict]:
    terminal_placeholders = ", ".join("?" for _ in TERMINAL_SDI_STATUSES)
    attention_placeholders = ", ".join("?" for _ in ATTENTION_SDI_STATUSES)
    status_sql = _status_sql()
    sql = f"""
        SELECT TOP (?)
            h.stg_sdi_id,
            h.tss_sup_dec_number,
            {status_sql} AS TssStatus,
            tss_h.LastSyncedAt
        FROM STG.BKD_SDI_Headers h
        LEFT JOIN TSS.BKD_SDI_Headers tss_h
          ON tss_h.ClientCode = h.ClientCode
         AND tss_h.SupDecNumber = h.tss_sup_dec_number
        WHERE h.ClientCode = ?
          AND NULLIF(LTRIM(RTRIM(COALESCE(h.tss_sup_dec_number, ''))), '') IS NOT NULL
          AND UPPER(COALESCE(h.sub_status, '')) NOT IN ('CANCELLED', 'CANCELED', 'DELETED')
          AND (
                tss_h.LastSyncedAt IS NULL
                OR tss_h.LastSyncedAt <= DATEADD(MINUTE, -?, SYSUTCDATETIME())
              )
          AND (
                NULLIF(LTRIM(RTRIM({status_sql})), '') IS NULL
                OR {status_sql} NOT IN ({terminal_placeholders})
              )
        ORDER BY
            CASE WHEN {status_sql} IN ({attention_placeholders}) THEN 0 ELSE 1 END,
            COALESCE(tss_h.LastSyncedAt, CONVERT(DATETIME2, '19000101')) ASC,
            h.stg_sdi_id ASC
    """
    params = [
        max(1, int(limit)),
        client_code,
        max(0, int(min_age_minutes)),
        *TERMINAL_SDI_STATUSES,
        *ATTENTION_SDI_STATUSES,
    ]
    with db_cursor(commit=False) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [col[0] for col in cur.description]
    return [dict(zip(cols, row)) for row in rows]


def run_prd_sdi_status_sync(
    *,
    tenant_code: str = "BKD",
    limit: int = 10,
    min_age_minutes: int = 60,
) -> dict:
    if not _has_app_context():
        from app import create_app

        app = create_app()
        with app.app_context():
            return run_prd_sdi_status_sync(
                tenant_code=tenant_code,
                limit=limit,
                min_age_minutes=min_age_minutes,
            )

    from app.blueprints.supdec.routes import sync_prd_sdi_from_tss

    client_code = str(tenant_code or "BKD").strip().upper()
    rows = candidate_sdis(client_code, limit=limit, min_age_minutes=min_age_minutes)
    results = []
    failures = 0

    for row in rows:
        stg_sdi_id = int(row["stg_sdi_id"])
        result = sync_prd_sdi_from_tss(stg_sdi_id)
        results.append(result)
        if not result.get("ok"):
            failures += 1

    return {
        "tenant_code": client_code,
        "utc": datetime.now(timezone.utc).isoformat(),
        "selected": len(rows),
        "synced": sum(1 for item in results if item.get("ok")),
        "failed": failures,
        "results": results,
    }


def _has_app_context() -> bool:
    try:
        from flask import has_app_context

        return bool(has_app_context())
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = run_prd_sdi_status_sync(
        tenant_code=args.tenant_code,
        limit=args.limit,
        min_age_minutes=args.min_age_minutes,
    )
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
