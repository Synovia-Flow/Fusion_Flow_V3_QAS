#!/usr/bin/env python3
"""Run a PRD-safe general ENS status sync.

This is the cron-safe companion to the event-driven ENS watcher. It scans
STG.BKD_ENS_Headers for live TSS ENS references, then calls
sync_ens_status_once() for each selected header. The watcher handles header,
consignment, goods, SFD/MRN, RawJson mirrors, TSS status attention emails, and
the final movement-authorised notification.

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
from app.ingestion.ens_status_watcher import sync_ens_status_once  # noqa: E402


TERMINAL_HEADER_STATUSES = (
    "ARRIVED",
    "CANCELLED",
    "CANCELED",
    "CLOSED",
    "COMPLETED",
    "DELETED",
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
        default=_env_int("FUSION_PRD_ENS_SYNC_LIMIT", 2),
        help="Maximum ENS headers to sync in one cron tick. Default: FUSION_PRD_ENS_SYNC_LIMIT or 2.",
    )
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=_env_int("FUSION_PRD_ENS_SYNC_MIN_AGE_MINUTES", 20),
        help="Skip headers synced more recently than this. Default: 20.",
    )
    parser.add_argument(
        "--skip-notified",
        action="store_true",
        help="Do not continue syncing headers after movement_notified_at is set.",
    )
    return parser.parse_args(argv)


def candidate_headers(client_code: str, *, limit: int, min_age_minutes: int) -> list[dict]:
    terminal_placeholders = ", ".join("?" for _ in TERMINAL_HEADER_STATUSES)
    sql = f"""
        SELECT TOP (?)
            h.stg_header_id,
            h.tss_ens_header_ref,
            h.movement_notified_at,
            th.TssStatus,
            th.LastSyncedAt
        FROM STG.BKD_ENS_Headers h
        LEFT JOIN TSS.BKD_ENS_Headers th
          ON th.ClientCode = h.ClientCode
         AND th.DeclarationNumber = h.tss_ens_header_ref
        WHERE h.ClientCode = ?
          AND NULLIF(LTRIM(RTRIM(COALESCE(h.tss_ens_header_ref, ''))), '') IS NOT NULL
          AND UPPER(COALESCE(h.sub_status, '')) NOT IN ('CANCELLED', 'CANCELED', 'DELETED')
          AND (
                NULLIF(LTRIM(RTRIM(COALESCE(th.TssStatus, ''))), '') IS NULL
                OR UPPER(REPLACE(COALESCE(th.TssStatus, ''), '_', ' ')) <> 'ARRIVED'
              )
          AND (
                th.LastSyncedAt IS NULL
                OR th.LastSyncedAt <= DATEADD(MINUTE, -?, SYSUTCDATETIME())
              )
          AND (
                NULLIF(LTRIM(RTRIM(COALESCE(th.TssStatus, ''))), '') IS NULL
                OR UPPER(REPLACE(COALESCE(th.TssStatus, ''), '_', ' ')) NOT IN ({terminal_placeholders})
                OR EXISTS (
                    SELECT 1
                    FROM STG.BKD_ENS_Consignments c
                    LEFT JOIN TSS.BKD_ENS_Consignments tc
                      ON tc.ClientCode = c.ClientCode
                     AND tc.ConsignmentReference = c.tss_consignment_ref
                    LEFT JOIN TSS.BKD_SFD sfd
                      ON sfd.ClientCode = c.ClientCode
                     AND sfd.DeclarationNumber = c.tss_consignment_ref
                    WHERE c.ClientCode = h.ClientCode
                      AND c.stg_header_id = h.stg_header_id
                      AND NULLIF(LTRIM(RTRIM(COALESCE(c.tss_consignment_ref, ''))), '') IS NOT NULL
                      AND UPPER(REPLACE(COALESCE(tc.TssStatus, ''), '_', ' ')) IN (
                          'AUTHORISED FOR MOVEMENT',
                          'AUTHORIZED FOR MOVEMENT',
                          'ARRIVED'
                      )
                      AND (
                            NULLIF(LTRIM(RTRIM(COALESCE(sfd.SfdReference, ''))), '') IS NULL
                            OR NULLIF(LTRIM(RTRIM(COALESCE(sfd.MovementReferenceNumber, ''))), '') IS NULL
                          )
                )
              )
        ORDER BY
            CASE WHEN h.movement_notified_at IS NULL THEN 0 ELSE 1 END,
            COALESCE(th.LastSyncedAt, CONVERT(DATETIME2, '19000101')) ASC,
            h.stg_header_id ASC
    """
    params = [max(1, int(limit)), client_code, max(0, int(min_age_minutes)), *TERMINAL_HEADER_STATUSES]
    with db_cursor(commit=False) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [col[0] for col in cur.description]
    return [dict(zip(cols, row)) for row in rows]


def run_prd_ens_status_sync(
    *,
    tenant_code: str = "BKD",
    limit: int = 50,
    min_age_minutes: int = 20,
    continue_after_notified: bool = True,
) -> dict:
    if not _has_app_context():
        from app import create_app

        app = create_app()
        with app.app_context():
            return run_prd_ens_status_sync(
                tenant_code=tenant_code,
                limit=limit,
                min_age_minutes=min_age_minutes,
                continue_after_notified=continue_after_notified,
            )

    client_code = str(tenant_code or "BKD").strip().upper()
    headers = candidate_headers(client_code, limit=limit, min_age_minutes=min_age_minutes)
    results = []
    failures = 0

    for header in headers:
        stg_header_id = int(header["stg_header_id"])
        result = sync_ens_status_once(
            stg_header_id,
            tenant_code=client_code,
            continue_after_notified=continue_after_notified,
        )
        results.append(result)
        if result.get("stage") == "error":
            failures += 1

    return {
        "tenant_code": client_code,
        "utc": datetime.now(timezone.utc).isoformat(),
        "selected": len(headers),
        "synced": sum(1 for item in results if item.get("stage") == "synced"),
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
    summary = run_prd_ens_status_sync(
        tenant_code=args.tenant_code,
        limit=args.limit,
        min_age_minutes=args.min_age_minutes,
        continue_after_notified=not args.skip_notified,
    )
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
