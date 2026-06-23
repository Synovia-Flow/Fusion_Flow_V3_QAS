#!/usr/bin/env python3
"""Read-only reconciliation checks for the ING/STG/TSS migration."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class CountCheck:
    name: str
    legacy_sql: str
    compat_sql: str
    new_sql: str | None = None


def _tenant_object(schema: str, table: str) -> str:
    safe_schema = str(schema or "BKD").replace("]", "]]")
    safe_table = str(table or "").replace("]", "]]")
    return f"[{safe_schema}].[{safe_table}]"


def _count_sql(sql_object: str) -> str:
    return f"SELECT COUNT(*) AS cnt FROM {sql_object}"


def build_count_checks(schema: str = "BKD") -> list[CountCheck]:
    """Return the core parity count checks without opening a DB connection."""

    return [
        CountCheck(
            "ens_headers",
            _count_sql(_tenant_object(schema, "StagingEnsHeaders")),
            _count_sql("[STG].[vw_BKD_Legacy_ENS_Headers]"),
            _count_sql("[STG].[BKD_ENS_Headers]"),
        ),
        CountCheck(
            "consignments",
            _count_sql(_tenant_object(schema, "StagingConsignments")),
            _count_sql("[STG].[vw_BKD_Legacy_ENS_Consignments]"),
            _count_sql("[STG].[BKD_ENS_Consignments]"),
        ),
        CountCheck(
            "goods",
            _count_sql(_tenant_object(schema, "StagingGoodsItems")),
            _count_sql("[STG].[vw_BKD_Legacy_GoodsItems]"),
            _count_sql("[STG].[BKD_GoodsItems]"),
        ),
        CountCheck(
            "sdi_headers",
            _count_sql(_tenant_object(schema, "StagingSupDecHeaders")),
            _count_sql("[STG].[vw_BKD_Legacy_SDI_Headers]"),
            _count_sql("[STG].[BKD_SDI_Headers]"),
        ),
        CountCheck(
            "gmr",
            _count_sql(_tenant_object(schema, "StagingGmrs")),
            _count_sql("[TSS].[vw_BKD_Legacy_GMR_Movements]"),
            _count_sql("[TSS].[BKD_GMR_Movements]"),
        ),
        CountCheck(
            "sfd",
            _count_sql(_tenant_object(schema, "Sfds")),
            _count_sql("[TSS].[vw_BKD_Legacy_SFD]"),
            _count_sql("[TSS].[BKD_SFD]"),
        ),
        CountCheck(
            "api_exchanges",
            _count_sql(_tenant_object(schema, "ApiCallLog")),
            _count_sql("[TSS].[vw_BKD_Legacy_API_Exchanges]"),
            _count_sql("[TSS].[BKD_API_Exchanges]"),
        ),
        CountCheck(
            "api_outbox",
            _count_sql(_tenant_object(schema, "MessageOutbox")),
            _count_sql("[TSS].[vw_BKD_Legacy_API_Outbox]"),
            _count_sql("[TSS].[BKD_API_Outbox]"),
        ),
        CountCheck(
            "job_runs",
            _count_sql(_tenant_object(schema, "JobRunLog")),
            _count_sql("[TSS].[vw_BKD_Legacy_JobRuns]"),
            _count_sql("[TSS].[BKD_JobRuns]"),
        ),
    ]


def _safe_count(query_one, sql: str) -> dict:
    try:
        row = query_one(sql) or {}
        return {"ok": True, "count": int(row.get("cnt") or 0), "error": None}
    except Exception as exc:
        return {"ok": False, "count": None, "error": str(exc)}


def run_reconciliation(schema: str = "BKD") -> list[dict]:
    """Run read-only reconciliation checks against the configured database."""

    from app.db import query_one

    results = []
    for check in build_count_checks(schema):
        legacy = _safe_count(query_one, check.legacy_sql)
        compat = _safe_count(query_one, check.compat_sql)
        new = _safe_count(query_one, check.new_sql) if check.new_sql else None
        results.append(
            {
                "name": check.name,
                "legacy": legacy,
                "compat": compat,
                "new": new,
                "legacy_matches_compat": legacy.get("ok") and compat.get("ok") and legacy.get("count") == compat.get("count"),
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only ING/STG/TSS reconciliation checks.")
    parser.add_argument("--tenant", default=os.environ.get("TENANT_CODE") or os.environ.get("CLIENT_CODE") or "BKD")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text table.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned checks without querying the database.")
    args = parser.parse_args(argv)

    checks = build_count_checks(args.tenant)
    if args.dry_run:
        payload = [{"name": c.name, "legacy_sql": c.legacy_sql, "compat_sql": c.compat_sql, "new_sql": c.new_sql} for c in checks]
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            for item in payload:
                print(f"{item['name']}: legacy={item['legacy_sql']} compat={item['compat_sql']} new={item['new_sql']}")
        return 0

    results = run_reconciliation(args.tenant)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            marker = "OK" if row["legacy_matches_compat"] else "CHECK"
            legacy = row["legacy"]
            compat = row["compat"]
            new = row["new"] or {}
            print(
                f"{marker} {row['name']}: "
                f"legacy={legacy.get('count')} compat={compat.get('count')} new={new.get('count')}"
            )
            for label in ("legacy", "compat", "new"):
                part = row.get(label) or {}
                if part.get("error"):
                    print(f"  {label} error: {part['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
