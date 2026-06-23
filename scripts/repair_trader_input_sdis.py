"""Repair and resubmit PRD SDIs currently in Trader Input Required.

This runner is deliberately conservative:

* It reads the official TSS status/error before each action.
* It applies only data repairs that are either proven by closed history or
  directly derivable from the staged source line.
* It submits through the same helper used by the manual portal button.
* It writes a JSONL audit trail under artifacts/.

It does not cancel SDIs, create SUP references, or bypass TSS validation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional local dependency
    load_dotenv = None

if load_dotenv:
    load_dotenv(ROOT / ".env")

from app import create_app  # noqa: E402
from app.db import get_standalone_connection  # noqa: E402
from app.tss_api import build_cfg_client  # noqa: E402
from app.blueprints.supdec import routes as supdec_routes  # noqa: E402
from scripts.repair_sdi_blank_n935_goods import _repair_sup as _repair_corrupt_goods  # noqa: E402


TIR_STATUSES = {"TRADER INPUT REQUIRED", "AMENDMENT REQUIRED"}
FINAL_STATUSES = {
    "CLOSED",
    "COMPLETED",
    "CLEARED",
    "ACCEPTED",
    "CANCELLED",
    "CANCELED",
}

TERMINAL_OR_IN_FLIGHT = {
    *FINAL_STATUSES,
    "PROCESSING",
    "PENDING PAYMENT",
    "SUBMITTED",
}

DRAFT_STATUSES = {"DRAFT"}

PROVEN_TARIC_BY_COMMODITY_ORIGIN = {
    # Learned from closed TSS history for 7318129090/CN.
    ("7318129090", "CN"): "899L",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _status_key(value: Any) -> str:
    return _clean(value).upper()


def _sup(row: dict[str, Any]) -> str:
    return _clean(row.get("sup_dec_number") or row.get("tss_sup_dec_number"))


def _append_jsonl(path: Path | None, event: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, default=str, ensure_ascii=True) + "\n")


def _official_read(api: Any, sup_ref: str) -> dict[str, Any]:
    result = api.read_sdi(sup_ref, fields=list(supdec_routes.PRD_SDI_SUBMIT_READ_FIELDS))
    detail = supdec_routes._tss_response_payload(result)
    return {
        "status": supdec_routes._tss_response_value(detail, "status"),
        "error_message": supdec_routes._tss_response_value(detail, "error_message"),
        "mrn": (
            supdec_routes._tss_response_value(detail, "movement_reference_number")
            or supdec_routes._tss_response_value(detail, "mrn")
        ),
        "raw": result,
    }


def _current_tir_rows(sup_refs: list[str] | None = None) -> list[dict[str, Any]]:
    rows = supdec_routes._load_prd_sdi_headers("")
    selected = []
    wanted = {ref.upper() for ref in sup_refs or []}
    for row in rows:
        sup_ref = _sup(row)
        if wanted and sup_ref.upper() not in wanted:
            continue
        status = _status_key(row.get("tss_status") or row.get("sub_status"))
        if status in TIR_STATUSES or wanted:
            selected.append(row)
    return selected


def _apply_local_repairs(
    staging_id: int,
    official_error: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    conn = get_standalone_connection()
    cur = conn.cursor()
    try:
        for (commodity_code, origin), taric_code in PROVEN_TARIC_BY_COMMODITY_ORIGIN.items():
            cur.execute(
                """
                UPDATE [STG].[BKD_SDI_GoodsItems]
                   SET taric_code = ?,
                       updated_at = SYSUTCDATETIME()
                 WHERE ClientCode = 'BKD'
                   AND stg_sdi_id = ?
                   AND REPLACE(COALESCE(commodity_code, ''), ' ', '') = ?
                   AND UPPER(COALESCE(country_of_origin, '')) = ?
                   AND NULLIF(LTRIM(RTRIM(COALESCE(taric_code, ''))), '') IS NULL
                """,
                [taric_code, staging_id, commodity_code, origin],
            )
            counts[f"taric_{commodity_code}_{origin}"] = max(cur.rowcount, 0)

        if "Missing Supplementary Unit" in official_error:
            cur.execute(
                """
                UPDATE [STG].[BKD_SDI_GoodsItems]
                   SET supplementary_units = TRY_CONVERT(DECIMAL(16,3), number_of_individual_pieces),
                       updated_at = SYSUTCDATETIME()
                 WHERE ClientCode = 'BKD'
                   AND stg_sdi_id = ?
                   AND NULLIF(LTRIM(RTRIM(COALESCE(CONVERT(NVARCHAR(80), supplementary_units), ''))), '') IS NULL
                   AND NULLIF(TRY_CONVERT(DECIMAL(16,3), number_of_individual_pieces), 0) IS NOT NULL
                """,
                [staging_id],
            )
            counts["supplementary_units_from_pieces"] = max(cur.rowcount, 0)

        if "Missing Item Value" in official_error:
            cur.execute(
                """
                UPDATE [STG].[BKD_SDI_GoodsItems]
                   SET item_invoice_amount = COALESCE(
                           NULLIF(TRY_CONVERT(DECIMAL(16,2), line_amount_excl_vat), 0),
                           NULLIF(TRY_CONVERT(DECIMAL(16,2), source_amount), 0),
                           NULLIF(TRY_CONVERT(DECIMAL(16,2), customs_value), 0),
                           NULLIF(
                               TRY_CONVERT(DECIMAL(16,2), unit_price_excl_vat)
                               * TRY_CONVERT(DECIMAL(16,2), number_of_individual_pieces),
                               0
                           )
                       ),
                       customs_value = COALESCE(
                           NULLIF(TRY_CONVERT(DECIMAL(16,2), customs_value), 0),
                           NULLIF(TRY_CONVERT(DECIMAL(16,2), line_amount_excl_vat), 0),
                           NULLIF(TRY_CONVERT(DECIMAL(16,2), source_amount), 0),
                           NULLIF(
                               TRY_CONVERT(DECIMAL(16,2), unit_price_excl_vat)
                               * TRY_CONVERT(DECIMAL(16,2), number_of_individual_pieces),
                               0
                           )
                       ),
                       updated_at = SYSUTCDATETIME()
                 WHERE ClientCode = 'BKD'
                   AND stg_sdi_id = ?
                   AND COALESCE(
                           NULLIF(TRY_CONVERT(DECIMAL(16,2), item_invoice_amount), 0),
                           NULLIF(TRY_CONVERT(DECIMAL(16,2), customs_value), 0)
                       ) IS NULL
                   AND COALESCE(
                           NULLIF(TRY_CONVERT(DECIMAL(16,2), line_amount_excl_vat), 0),
                           NULLIF(TRY_CONVERT(DECIMAL(16,2), source_amount), 0),
                           NULLIF(
                               TRY_CONVERT(DECIMAL(16,2), unit_price_excl_vat)
                               * TRY_CONVERT(DECIMAL(16,2), number_of_individual_pieces),
                               0
                           )
                       ) IS NOT NULL
                """,
                [staging_id],
            )
            counts["item_value_from_source"] = max(cur.rowcount, 0)

        if "Missing Document Code" in official_error or "CDS12068" in official_error:
            counts["historical_documents"] = _apply_historical_documents(cur, staging_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    return counts


def _apply_historical_documents(cur: Any, staging_id: int) -> int:
    cur.execute(
        """
        SELECT
            stg_sdi_item_id,
            REPLACE(COALESCE(commodity_code, ''), ' ', '') AS commodity_code,
            UPPER(COALESCE(country_of_origin, '')) AS country_of_origin,
            document_references_json
        FROM [STG].[BKD_SDI_GoodsItems]
        WHERE ClientCode = 'BKD'
          AND stg_sdi_id = ?
        """,
        [staging_id],
    )
    goods_rows = [
        {
            "stg_sdi_item_id": row[0],
            "commodity_code": _clean(row[1]),
            "country_of_origin": _clean(row[2]).upper(),
            "document_references_json": row[3],
        }
        for row in cur.fetchall()
    ]
    combos = sorted(
        {
            (row["commodity_code"], row["country_of_origin"])
            for row in goods_rows
            if row["commodity_code"] and row["country_of_origin"]
        }
    )
    if not combos:
        return 0

    documents_by_combo: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for commodity_code, origin in combos:
        cur.execute(
            """
            SELECT
                document_code,
                document_status,
                document_reference,
                document_part,
                document_reason,
                date_of_validity,
                issuing_authority,
                amount,
                currency,
                measurement_unit,
                quantity
            FROM [BKD].[DocProductCatalogDocuments]
            WHERE active = 1
              AND COALESCE(auto_apply_to_sdi, 1) = 1
              AND COALESCE(requires_compliance_review, 0) = 0
              AND REPLACE(COALESCE(commodity_code, ''), ' ', '') = ?
              AND UPPER(COALESCE(country_of_origin, '')) = ?
              AND UPPER(LTRIM(RTRIM(COALESCE(document_code, '')))) NOT IN ('', 'N935', '1UKI')
            ORDER BY COALESCE(evidence_count, 1) DESC, id
            """,
            [commodity_code, origin],
        )
        docs = []
        for row in cur.fetchall():
            document = {
                "op_type": "create",
                "document_code": _clean(row[0]).upper(),
            }
            for idx, key in enumerate(
                (
                    "document_status",
                    "document_reference",
                    "document_part",
                    "document_reason",
                    "date_of_validity",
                    "issuing_authority",
                    "amount",
                    "currency",
                    "measurement_unit",
                    "quantity",
                ),
                start=1,
            ):
                value = row[idx]
                if value not in (None, ""):
                    document[key] = value
            docs.append(document)
        documents_by_combo[(commodity_code, origin)] = docs

    updated = 0
    for goods_row in goods_rows:
        defaults = documents_by_combo.get((goods_row["commodity_code"], goods_row["country_of_origin"])) or []
        if not defaults:
            continue
        existing = _load_document_json(goods_row.get("document_references_json"))
        merged = _merge_documents(existing, defaults)
        if merged == existing:
            continue
        cur.execute(
            """
            UPDATE [STG].[BKD_SDI_GoodsItems]
               SET document_references_json = ?,
                   updated_at = SYSUTCDATETIME()
             WHERE ClientCode = 'BKD'
               AND stg_sdi_item_id = ?
            """,
            [json.dumps(merged, default=str, ensure_ascii=True), goods_row["stg_sdi_item_id"]],
        )
        updated += max(cur.rowcount, 0)
    return updated


def _load_document_json(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _document_key(document: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _clean(document.get("document_code")).upper(),
        _clean(document.get("document_reference")).upper(),
        _clean(document.get("document_status")).upper(),
        _clean(document.get("document_reason")).upper(),
    )


def _merge_documents(existing: list[dict[str, Any]], defaults: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Deduplicate by (code, reference) — prefer non-blank status over blank.
    # existing entries take precedence; defaults fill in missing codes only.
    by_code_ref: dict[tuple[str, str], dict[str, Any]] = {}
    for document in [*existing, *defaults]:
        code = _clean(document.get("document_code")).upper()
        if not code or code in {"1UKI"}:
            continue
        clean_doc = dict(document)
        clean_doc["document_code"] = code
        clean_doc.setdefault("op_type", "create")
        ref = _clean(document.get("document_reference")).upper()
        slot_key = (code, ref)
        prev = by_code_ref.get(slot_key)
        if prev is None:
            by_code_ref[slot_key] = clean_doc
        else:
            # Keep existing but upgrade blank status from defaults
            prev_status = _clean(prev.get("document_status"))
            new_status = _clean(clean_doc.get("document_status"))
            if not prev_status and new_status:
                prev["document_status"] = clean_doc["document_status"]
    return list(by_code_ref.values())


def _poll(api: Any, sup_ref: str, waits: list[int]) -> list[dict[str, Any]]:
    polls = []
    for wait_seconds in waits:
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        read = _official_read(api, sup_ref)
        polls.append(
            {
                "after_wait_s": wait_seconds,
                "status": read["status"],
                "error_message": read["error_message"],
                "mrn": read["mrn"],
            }
        )
        if _status_key(read["status"]) in FINAL_STATUSES and not _clean(read["error_message"]):
            break
    return polls


def run(
    *,
    sup_refs: list[str] | None,
    limit: int,
    output: Path | None,
    poll_waits: list[int],
) -> dict[str, Any]:
    app = create_app()
    with app.app_context():
        api = build_cfg_client()
        rows = _current_tir_rows(sup_refs)
        rows = rows[: max(0, limit)] if limit else rows
        events = []
        started_at = datetime.now(timezone.utc).isoformat()

        for index, queued in enumerate(rows, start=1):
            sid = queued.get("staging_id")
            sup_ref = _sup(queued)
            event: dict[str, Any] = {
                "started_at": started_at,
                "index": index,
                "staging_id": sid,
                "sup_ref": sup_ref,
                "transport_document_number": queued.get("transport_document_number"),
            }
            try:
                before = _official_read(api, sup_ref)
                event["official_before"] = {
                    "status": before["status"],
                    "error_message": before["error_message"],
                    "mrn": before["mrn"],
                }
                before_status = _status_key(before["status"])
                if before_status in TERMINAL_OR_IN_FLIGHT:
                    event["stage"] = "sync_only"
                    event["sync"] = supdec_routes.sync_prd_sdi_from_tss(sid, api=api)
                    events.append(event)
                    _append_jsonl(output, event)
                    continue

                if before_status in DRAFT_STATUSES:
                    event["stage"] = "skipped_draft_not_submittable"
                    event["action"] = "sync_only"
                    event["message"] = "TSS rejected direct submit from Draft with invalid op_type; wait for TSS to require trader input or use the official draft transition path."
                    event["sync"] = supdec_routes.sync_prd_sdi_from_tss(sid, api=api)
                    events.append(event)
                    _append_jsonl(output, event)
                    continue

                sd = supdec_routes._load_prd_sdi_header_by_id(sid)
                goods = supdec_routes._load_prd_sdi_goods(sid)
                mapping_issue = supdec_routes._prd_sdi_goods_mapping_issue(sd, goods)
                if mapping_issue:
                    event["stage"] = "skipped_goods_mapping_duplicate"
                    event["action"] = "no_submit"
                    event["error"] = mapping_issue
                    event["goods_count"] = len(goods or [])
                    events.append(event)
                    _append_jsonl(output, event)
                    continue

                if "CDS12068" in _clean(before["error_message"]) or "AdditionalDocument" in _clean(before["error_message"]):
                    event["corrupt_goods_repair"] = _repair_corrupt_goods(
                        api,
                        sup_ref,
                        apply=True,
                        submit=False,
                        poll_seconds=[],
                    )

                event["repairs"] = _apply_local_repairs(sid, _clean(before["error_message"]))
                if (
                    "Missing Item Value" in _clean(before["error_message"])
                    and not event["repairs"].get("item_value_from_source")
                ):
                    event["stage"] = "skipped_missing_real_item_value"
                    event["action"] = "no_submit"
                    event["message"] = "TSS requires an item value, but Fusion has no non-zero source/invoice/customs value to send."
                    event["sync"] = supdec_routes.sync_prd_sdi_from_tss(sid, api=api)
                    events.append(event)
                    _append_jsonl(output, event)
                    continue
                if (
                    "Missing Supplementary Unit" in _clean(before["error_message"])
                    and not event["repairs"].get("supplementary_units_from_pieces")
                ):
                    event["stage"] = "skipped_missing_real_supplementary_units"
                    event["action"] = "no_submit"
                    event["message"] = "TSS requires supplementary units, but Fusion has no non-zero number_of_individual_pieces to send."
                    event["sync"] = supdec_routes.sync_prd_sdi_from_tss(sid, api=api)
                    events.append(event)
                    _append_jsonl(output, event)
                    continue
                sd = supdec_routes._load_prd_sdi_header_by_id(sid)
                goods = supdec_routes._load_prd_sdi_goods(sid)
                summary, errors = supdec_routes._submit_single_prd_sdi_to_tss(sd, goods, api=api)
                event["submit"] = {"summary": summary, "errors": errors}
                event["polls"] = _poll(api, sup_ref, poll_waits)
                event["sync"] = supdec_routes.sync_prd_sdi_from_tss(sid, api=api)
                event["stage"] = "submitted_attempt"
            except Exception as exc:
                event["stage"] = "exception"
                event["error"] = str(exc)

            events.append(event)
            _append_jsonl(output, event)

        return {
            "started_at": started_at,
            "requested_sup_refs": sup_refs or [],
            "processed": len(events),
            "counts": dict(Counter(event.get("stage") for event in events)),
            "final_status_counts": dict(
                Counter(
                    _status_key(
                        (event.get("sync") or {}).get("tss_status")
                        or ((event.get("polls") or [{}])[-1]).get("status")
                        or (event.get("official_before") or {}).get("status")
                    )
                    or "UNKNOWN"
                    for event in events
                )
            ),
            "events": events,
            "output": str(output) if output else None,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sup", action="append", default=None, help="Specific SUP ref. Repeatable.")
    parser.add_argument("--limit", type=int, default=5, help="Max SUPs to process. 0 means all selected.")
    parser.add_argument("--poll", default="20,60", help="Comma-separated waits after submit, seconds.")
    parser.add_argument("--output", default="artifacts/sdi_tir_repair_20260609.jsonl")
    args = parser.parse_args()

    waits = [int(part.strip()) for part in str(args.poll or "").split(",") if part.strip()]
    result = run(
        sup_refs=args.sup,
        limit=args.limit,
        output=Path(args.output) if args.output else None,
        poll_waits=waits,
    )
    print(json.dumps(result, indent=2, default=str, ensure_ascii=True))


if __name__ == "__main__":
    main()
