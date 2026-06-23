"""Audit STG/TSS SDI goods for the source-goods duplication bug.

Root cause: when TSS goods items had no reliable item_number and all shared
the same description (common with TSS draft SDI goods), _match_source_goods
fell through to the description-match branch which returned source_goods[0]
for EVERY TSS goods item. All goods_ids in TSS then received the payload of
the first source goods line.

Detection signal: multiple STG.BKD_SDI_GoodsItems rows in the same stg_sdi_id
share the same source_stg_item_id but have distinct tss_goods_id values.

Output: JSON report with one entry per affected SUPDEC, classified as:

  TERMINAL  - TSS status CLOSED/COMPLETED/ACCEPTED/CLEARED.
              Local STG data is wrong but TSS is already accepted.
              Cannot safely repair via API. Requires operator review.

  TIR       - TSS status TRADER INPUT REQUIRED or AMENDMENT REQUIRED.
              Repairable: re-stage goods and resubmit via
              repair_trader_input_sdis.py --sup <SUP_REF>

  DRAFT     - TSS status DRAFT.
              Repairable: re-run sdi_autosubmit worker (fix is in place).

  OTHER     - Any other non-terminal TSS status (SUBMITTED, PROCESSING, etc.).
              Wait for final status then reassess.

Usage:
    python scripts/audit_sdi_goods_duplication.py
    python scripts/audit_sdi_goods_duplication.py --tenant BKD --output artifacts/sdi_dup_audit.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from app import create_app  # noqa: E402
from app.db import get_standalone_connection  # noqa: E402


TERMINAL_STATUSES = {"CLOSED", "COMPLETED", "ACCEPTED", "CLEARED"}
TIR_STATUSES = {"TRADER INPUT REQUIRED", "AMENDMENT REQUIRED"}
DRAFT_STATUSES = {"DRAFT"}


def _clean(value: Any) -> str:
    return str(value or "").strip().upper()


def _classify(status: str) -> str:
    s = _clean(status)
    if s in TERMINAL_STATUSES:
        return "TERMINAL"
    if s in TIR_STATUSES:
        return "TIR"
    if s in DRAFT_STATUSES:
        return "DRAFT"
    if not s:
        return "UNKNOWN"
    return "OTHER"


def _table_exists(cur: Any, schema: str, table: str) -> bool:
    cur.execute(
        "SELECT COUNT(1) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        [schema, table],
    )
    row = cur.fetchone()
    return bool(row and row[0])


def _column_exists(cur: Any, schema: str, table: str, column: str) -> bool:
    cur.execute(
        "SELECT COUNT(1) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?",
        [schema, table, column],
    )
    row = cur.fetchone()
    return bool(row and row[0])


def _inspect_schema(cur: Any) -> dict[str, bool]:
    checks = {
        "stg_goods": _table_exists(cur, "STG", "BKD_SDI_GoodsItems"),
        "stg_headers": _table_exists(cur, "STG", "BKD_SDI_Headers"),
        "tss_headers": _table_exists(cur, "TSS", "BKD_SDI_Headers"),
        "tss_goods": _table_exists(cur, "TSS", "BKD_SDI_GoodsItems"),
        "stg_source_goods": _table_exists(cur, "STG", "BKD_GoodsItems"),
        "has_source_stg_item_id": _column_exists(cur, "STG", "BKD_SDI_GoodsItems", "source_stg_item_id"),
        "has_tss_goods_id": _column_exists(cur, "STG", "BKD_SDI_GoodsItems", "tss_goods_id"),
        "has_item_seq_stg": _column_exists(cur, "STG", "BKD_SDI_GoodsItems", "item_seq"),
        "has_item_seq_src": _column_exists(cur, "STG", "BKD_GoodsItems", "item_seq"),
        "tss_headers_has_status": _column_exists(cur, "TSS", "BKD_SDI_Headers", "TssStatus"),
        "tss_goods_has_itemnumber": _column_exists(cur, "TSS", "BKD_SDI_GoodsItems", "ItemNumber"),
    }
    return checks


def _find_duplicated_sdi(cur: Any, tenant_code: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            g.stg_sdi_id,
            h.tss_sup_dec_number,
            COALESCE(
                NULLIF(LTRIM(RTRIM(tss_h.TssStatus)), ''),
                NULLIF(LTRIM(RTRIM(h.tss_status)), ''),
                NULLIF(LTRIM(RTRIM(h.sub_status)), '')
            ) AS effective_status,
            h.stg_consignment_id,
            h.tss_sfd_consignment_ref,
            h.transport_document_number,
            COUNT(g.stg_sdi_item_id)           AS total_goods,
            COUNT(DISTINCT g.source_stg_item_id) AS distinct_source_items,
            COUNT(DISTINCT g.tss_goods_id)       AS distinct_tss_goods,
            COUNT(DISTINCT g.goods_description)  AS distinct_descriptions
        FROM [STG].[BKD_SDI_GoodsItems] g
        JOIN [STG].[BKD_SDI_Headers] h
          ON h.stg_sdi_id = g.stg_sdi_id
         AND h.ClientCode = g.ClientCode
        LEFT JOIN [TSS].[BKD_SDI_Headers] tss_h
          ON tss_h.ClientCode = h.ClientCode
         AND tss_h.SupDecNumber = h.tss_sup_dec_number
        WHERE g.ClientCode = ?
          AND g.source_stg_item_id IS NOT NULL
        GROUP BY
            g.stg_sdi_id,
            h.tss_sup_dec_number,
            tss_h.TssStatus,
            h.tss_status,
            h.sub_status,
            h.stg_consignment_id,
            h.tss_sfd_consignment_ref,
            h.transport_document_number
        HAVING COUNT(g.stg_sdi_item_id) > 1
           AND COUNT(DISTINCT g.source_stg_item_id) < COUNT(g.stg_sdi_item_id)
        ORDER BY g.stg_sdi_id DESC
        """,
        [tenant_code],
    )
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _load_goods_detail(cur: Any, stg_sdi_id: int, tenant_code: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            g.stg_sdi_item_id,
            g.tss_goods_id,
            g.source_stg_item_id,
            g.item_seq,
            g.goods_description,
            g.commodity_code,
            g.sub_status,
            tss_g.ItemNumber       AS tss_item_number,
            src.item_seq           AS source_item_seq,
            src.goods_description  AS source_goods_description,
            src.commodity_code     AS source_commodity_code
        FROM [STG].[BKD_SDI_GoodsItems] g
        LEFT JOIN [TSS].[BKD_SDI_GoodsItems] tss_g
          ON tss_g.ClientCode = g.ClientCode
         AND tss_g.GoodsId = g.tss_goods_id
        LEFT JOIN [STG].[BKD_GoodsItems] src
          ON src.ClientCode = g.ClientCode
         AND src.stg_item_id = g.source_stg_item_id
        WHERE g.ClientCode = ?
          AND g.stg_sdi_id = ?
        ORDER BY COALESCE(tss_g.ItemNumber, g.item_seq, g.stg_sdi_item_id)
        """,
        [tenant_code, stg_sdi_id],
    )
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _detect_duplication_type(goods: list[dict[str, Any]]) -> str:
    """Classify how the duplication manifests."""
    source_ids = [g.get("source_stg_item_id") for g in goods if g.get("source_stg_item_id") is not None]
    if not source_ids:
        return "no_source_mapping"
    unique_source = set(source_ids)
    if len(unique_source) == 1:
        return "all_map_to_same_source"
    if len(unique_source) < len(source_ids):
        return "partial_duplicate_source"
    return "no_duplication"


def _repair_recommendation(classification: str, status: str) -> str:
    if classification == "TERMINAL":
        return (
            "SDI is terminal in TSS. Local STG goods data is incorrect but TSS is already accepted. "
            "No API repair possible. Review for customs compliance if required."
        )
    if classification == "TIR":
        return (
            "SDI is TRADER INPUT REQUIRED. "
            "Run: python scripts/repair_trader_input_sdis.py --sup <SUP_REF> "
            "This will re-stage goods with correct positional mapping and resubmit."
        )
    if classification == "DRAFT":
        return (
            "SDI is DRAFT. Re-run the autosubmit worker - the positional fix is in place. "
            "Run: python scripts/sdi_autosubmit.py --no-dry-run --submit"
        )
    if classification == "OTHER":
        return (
            f"SDI is {status}. Wait for TSS to settle to a final or TIR status then reassess. "
            "Do not attempt repair while in-flight."
        )
    return "Unknown status - manual investigation required."


def run(*, tenant_code: str = "BKD", output: Path | None = None) -> dict[str, Any]:
    conn = get_standalone_connection()
    cur = conn.cursor()
    try:
        schema_info = _inspect_schema(cur)

        missing = [key for key, ok in schema_info.items() if not ok]
        if "stg_goods" in missing or "stg_headers" in missing:
            return {
                "error": "Required tables STG.BKD_SDI_GoodsItems or STG.BKD_SDI_Headers are missing.",
                "schema_checks": schema_info,
            }

        if not schema_info.get("has_source_stg_item_id"):
            return {
                "error": "Column STG.BKD_SDI_GoodsItems.source_stg_item_id not found. "
                         "Cannot detect duplication without it.",
                "schema_checks": schema_info,
            }

        rows = _find_duplicated_sdi(cur, tenant_code)

        affected: list[dict[str, Any]] = []
        counts: dict[str, int] = {"TERMINAL": 0, "TIR": 0, "DRAFT": 0, "OTHER": 0, "UNKNOWN": 0}

        for row in rows:
            stg_sdi_id = row.get("stg_sdi_id")
            sup_ref = str(row.get("tss_sup_dec_number") or "")
            effective_status = str(row.get("effective_status") or "")
            classification = _classify(effective_status)
            counts[classification] = counts.get(classification, 0) + 1

            goods = []
            if stg_sdi_id:
                goods = _load_goods_detail(cur, stg_sdi_id, tenant_code)

            dup_type = _detect_duplication_type(goods)
            recommendation = _repair_recommendation(classification, effective_status)

            affected.append({
                "sup_ref": sup_ref,
                "stg_sdi_id": stg_sdi_id,
                "tss_sfd_ref": str(row.get("tss_sfd_consignment_ref") or ""),
                "transport_document_number": str(row.get("transport_document_number") or ""),
                "stg_consignment_id": row.get("stg_consignment_id"),
                "tss_status": effective_status,
                "classification": classification,
                "duplication_type": dup_type,
                "total_goods": row.get("total_goods"),
                "distinct_source_items": row.get("distinct_source_items"),
                "distinct_tss_goods": row.get("distinct_tss_goods"),
                "distinct_descriptions": row.get("distinct_descriptions"),
                "recommendation": recommendation,
                "goods_detail": [
                    {
                        "stg_sdi_item_id": g.get("stg_sdi_item_id"),
                        "tss_goods_id": g.get("tss_goods_id"),
                        "source_stg_item_id": g.get("source_stg_item_id"),
                        "item_seq": g.get("item_seq"),
                        "tss_item_number": g.get("tss_item_number"),
                        "goods_description": str(g.get("goods_description") or ""),
                        "sub_status": str(g.get("sub_status") or ""),
                        "source_item_seq": g.get("source_item_seq"),
                        "source_goods_description": str(g.get("source_goods_description") or ""),
                    }
                    for g in goods
                ],
            })

        result: dict[str, Any] = {
            "tenant_code": tenant_code,
            "total_affected_supdecs": len(affected),
            "counts_by_classification": counts,
            "schema_checks": schema_info,
            "affected": affected,
        }

    finally:
        cur.close()
        conn.close()

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, indent=2, default=str, ensure_ascii=True),
            encoding="utf-8",
        )
        print(f"Report written to {output}")
    else:
        print(json.dumps(result, indent=2, default=str, ensure_ascii=True))

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="BKD", help="Tenant code (default: BKD)")
    parser.add_argument("--output", default=None, help="Optional JSON output path.")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        run(
            tenant_code=args.tenant,
            output=Path(args.output) if args.output else None,
        )


if __name__ == "__main__":
    main()
