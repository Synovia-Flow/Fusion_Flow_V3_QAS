"""Cancel PRD SDI/SUPDEC duplicates only when transport and goods match.

This is intentionally stricter than the duplicate transport guard:

* same transport_document_number
* same active SDI goods fingerprint
* same source DEC goods fingerprint when source goods are available
* cancellation target is still editable in TSS

Default mode is dry-run. Use --apply for real TSS cancel_sdi calls.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
except Exception:  # pragma: no cover
    load_dotenv = None

if load_dotenv:
    load_dotenv(ROOT / ".env")

from app import create_app  # noqa: E402
from app.blueprints.supdec import routes as supdec_routes  # noqa: E402
from app.tss_api import build_cfg_client  # noqa: E402


CANCELLED_STATUSES = {"CANCELLED", "CANCELED", "CANCELLED BY TSS", "CANCELED BY TSS"}
TERMINAL_KEEP_STATUSES = {"CLOSED", "COMPLETED", "CLEARED", "ACCEPTED"}
CANCELLABLE_STATUSES = {"DRAFT", "TRADER INPUT REQUIRED", "AMENDMENT REQUIRED"}
DO_NOT_CANCEL_STATUSES = TERMINAL_KEEP_STATUSES | {"PROCESSING", "SUBMITTED", "PENDING PAYMENT"}

STATUS_KEEP_RANK = {
    "CLOSED": 100,
    "COMPLETED": 100,
    "CLEARED": 100,
    "ACCEPTED": 100,
    "PENDING PAYMENT": 90,
    "PROCESSING": 80,
    "SUBMITTED": 70,
    "TRADER INPUT REQUIRED": 50,
    "AMENDMENT REQUIRED": 50,
    "DRAFT": 10,
}


def _clean(value: Any) -> str:
    return str(value or "").strip().upper()


def _first_text(*values: Any) -> str:
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _sup(row: dict[str, Any]) -> str:
    return _first_text(row.get("sup_dec_number"), row.get("tss_sup_dec_number"))


def _status(row: dict[str, Any]) -> str:
    return _clean(_first_text(row.get("tss_status"), row.get("sub_status"), row.get("status")))


def _transport(row: dict[str, Any]) -> str:
    return _clean(row.get("transport_document_number"))


def _due_date(row: dict[str, Any]) -> str:
    value = row.get("tss_submission_due_date")
    return str(value)[:10] if value else ""


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _norm_decimal(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(Decimal(str(value)).normalize())
    except (InvalidOperation, ValueError):
        return _norm_text(value)


def _goods_line_fingerprint(item: dict[str, Any], *, include_item_seq: bool = True) -> tuple[Any, ...]:
    parts: list[Any] = []
    if include_item_seq:
        parts.append(str(item.get("item_seq") or ""))
    parts.extend(
        [
            _norm_text(item.get("goods_description")),
            _norm_text(item.get("commodity_code")).replace(" ", ""),
            _norm_text(item.get("country_of_origin")),
            _norm_decimal(item.get("gross_mass_kg")),
            _norm_decimal(item.get("net_mass_kg")),
            _norm_decimal(item.get("number_of_packages")),
            _norm_text(item.get("type_of_packages")),
            _norm_decimal(item.get("item_invoice_amount") or item.get("customs_value") or item.get("line_amount_excl_vat")),
            _norm_text(item.get("item_invoice_currency")),
            _norm_decimal(item.get("supplementary_units")),
        ]
    )
    return tuple(parts)


def _sdi_goods_fingerprint(staging_id: int) -> tuple[tuple[Any, ...], ...]:
    goods = supdec_routes._load_prd_sdi_goods(staging_id)
    return tuple(sorted(_goods_line_fingerprint(item) for item in goods or []))


def _source_goods(stg_consignment_id: Any) -> list[dict[str, Any]]:
    if not stg_consignment_id:
        return []
    rows = supdec_routes.query_all(
        """
        SELECT
            item_seq,
            goods_description,
            commodity_code,
            country_of_origin,
            gross_mass_kg,
            net_mass_kg,
            number_of_packages,
            type_of_packages,
            item_invoice_amount,
            customs_value,
            line_amount_excl_vat,
            item_invoice_currency,
            supplementary_units
        FROM [STG].[BKD_GoodsItems]
        WHERE ClientCode = ?
          AND stg_consignment_id = ?
          AND UPPER(COALESCE(goods_stage, 'ENS')) <> 'SDI'
        ORDER BY COALESCE(item_seq, stg_item_id), stg_item_id
        """,
        [supdec_routes.S, stg_consignment_id],
    )
    return list(rows or [])


def _source_goods_fingerprint(stg_consignment_id: Any) -> tuple[tuple[Any, ...], ...]:
    return tuple(sorted(_goods_line_fingerprint(item) for item in _source_goods(stg_consignment_id)))


def _official_status(api: Any, sup_ref: str) -> tuple[str, str, str]:
    result = api.read_sdi(
        sup_ref,
        fields=["status", "error_message", "movement_reference_number", "transport_document_number"],
    )
    detail = supdec_routes._tss_response_payload(result)
    status = supdec_routes._tss_response_value(detail, "status")
    error = supdec_routes._tss_response_value(detail, "error_message")
    transport_doc = supdec_routes._tss_response_value(detail, "transport_document_number")
    return str(status or "").strip(), str(error or "").strip(), str(transport_doc or "").strip()


def _choose_keeper(items: list[dict[str, Any]]) -> dict[str, Any]:
    def sort_key(item: dict[str, Any]) -> tuple[int, str, int]:
        status = _status(item)
        sup_ref = _sup(item)
        staging_id = int(item.get("staging_id") or 0)
        return (-STATUS_KEEP_RANK.get(status, 0), sup_ref, staging_id)

    return sorted(items, key=sort_key)[0]


def _group_duplicates(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        transport_doc = _transport(row)
        if not transport_doc or _status(row) in CANCELLED_STATUSES:
            continue
        if not _sup(row):
            continue
        grouped[transport_doc].append(row)
    return {
        transport_doc: items
        for transport_doc, items in grouped.items()
        if len({_sup(item) for item in items}) > 1
    }


def _candidate_sets(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for transport_doc, items in sorted(groups.items()):
        enriched = []
        for row in items:
            staging_id = int(row.get("staging_id") or 0)
            sdi_fp = _sdi_goods_fingerprint(staging_id)
            source_fp = _source_goods_fingerprint(row.get("stg_consignment_id"))
            enriched.append(
                {
                    **row,
                    "goods_fingerprint": sdi_fp,
                    "source_fingerprint": source_fp,
                    "goods_count": len(sdi_fp),
                    "source_goods_count": len(source_fp),
                }
            )

        by_goods: dict[tuple[tuple[Any, ...], ...], list[dict[str, Any]]] = defaultdict(list)
        for item in enriched:
            if item["goods_fingerprint"]:
                by_goods[item["goods_fingerprint"]].append(item)

        for goods_fp, matching in by_goods.items():
            if len(matching) < 2:
                continue
            source_fps = {item["source_fingerprint"] for item in matching if item["source_fingerprint"]}
            if len(source_fps) != 1 or any(not item["source_fingerprint"] for item in matching):
                candidates.append(
                    {
                        "transport_document_number": transport_doc,
                        "stage": "skipped_source_goods_not_100pct_comparable",
                        "sup_refs": [_sup(item) for item in matching],
                        "goods_count": len(goods_fp),
                        "source_fingerprint_count": len(source_fps),
                    }
                )
                continue

            keeper = _choose_keeper(matching)
            for item in matching:
                if _sup(item) == _sup(keeper):
                    continue
                candidates.append(
                    {
                        "transport_document_number": transport_doc,
                        "stage": "candidate",
                        "keep_sup": _sup(keeper),
                        "keep_status": _status(keeper),
                        "cancel_sup": _sup(item),
                        "cancel_status": _status(item),
                        "cancel_staging_id": item.get("staging_id"),
                        "keep_staging_id": keeper.get("staging_id"),
                        "goods_count": len(goods_fp),
                        "source_goods_count": len(item["source_fingerprint"]),
                        "reason": "same transport_document_number, same SDI goods fingerprint, same source goods fingerprint",
                    }
                )
    return candidates


def _append_jsonl(path: Path | None, event: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, default=str, ensure_ascii=True) + "\n")


def _mark_cancelled(sd: dict[str, Any], result: Any) -> None:
    with supdec_routes.db_cursor() as cursor:
        supdec_routes._log_prd_sdi_tss_exchange(
            cursor,
            sd,
            call_type="CANCEL_SDI_DUPLICATE_GOODS_PRD",
            payload={"op_type": "cancel", "sup_dec_number": _sup(sd)},
            result=result,
        )
        supdec_routes._mark_prd_sdi_submit_state(cursor, sd, status="CANCELLED", errors=[])
        supdec_routes._mark_prd_tss_sdi_status(cursor, sd, "CANCELLED")
        goods_values = supdec_routes._sdi_existing_values(
            "STG",
            "BKD_SDI_GoodsItems",
            {
                "sub_status": "CANCELLED",
                "sdi_validation_errors_json": None,
                "last_sub_status_change": supdec_routes._sql_now,
                "updated_at": supdec_routes._sql_now,
            },
        )
        if goods_values:
            set_sql, params = supdec_routes._sdi_set_clause(goods_values)
            params.extend([supdec_routes.S, sd["staging_id"]])
            cursor.execute(
                f"""
                UPDATE [STG].[BKD_SDI_GoodsItems]
                   SET {set_sql}
                 WHERE ClientCode = ?
                   AND stg_sdi_id = ?
                """,
                params,
            )


def _mark_cancel_failed(sd: dict[str, Any], result: Any, message: str) -> None:
    with supdec_routes.db_cursor() as cursor:
        supdec_routes._log_prd_sdi_tss_exchange(
            cursor,
            sd,
            call_type="CANCEL_SDI_DUPLICATE_GOODS_PRD",
            payload={"op_type": "cancel", "sup_dec_number": _sup(sd)},
            result=result,
        )
        error = f"{_sup(sd)}: duplicate-goods cancel failed: {message}"
        supdec_routes._mark_prd_sdi_submit_state(cursor, sd, status="PENDING_REVIEW", errors=[error])


def run(
    *,
    apply: bool,
    output: Path | None,
    sleep_seconds: float,
    retry_failed: bool = False,
    due_date: str | None = None,
) -> dict[str, Any]:
    app = create_app()
    with app.app_context():
        api = build_cfg_client()
        rows = supdec_routes._load_prd_sdi_headers("")
        groups = _group_duplicates(rows)
        candidates = _candidate_sets(groups)
        if due_date:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.get("stage") != "candidate"
                or _due_date(supdec_routes._load_prd_sdi_header_by_id(candidate["cancel_staging_id"]) or {}) == due_date
                or _due_date(supdec_routes._load_prd_sdi_header_by_id(candidate["keep_staging_id"]) or {}) == due_date
            ]
        started_at = datetime.now(timezone.utc).isoformat()
        events: list[dict[str, Any]] = []

        for candidate in candidates:
            event = {"started_at": started_at, "apply": apply, **candidate}
            if candidate.get("stage") != "candidate":
                events.append(event)
                _append_jsonl(output, event)
                continue

            sd = supdec_routes._load_prd_sdi_header_by_id(candidate["cancel_staging_id"])
            keeper = supdec_routes._load_prd_sdi_header_by_id(candidate["keep_staging_id"])
            if not sd or not keeper:
                event.update({"stage": "skipped_missing_local_row"})
                events.append(event)
                _append_jsonl(output, event)
                continue

            previous_error = str(sd.get("auto_submit_error") or "")
            if not retry_failed and "duplicate-goods cancel failed" in previous_error:
                event.update(
                    {
                        "stage": "skipped_previous_cancel_failure",
                        "previous_error": previous_error[:600],
                    }
                )
                events.append(event)
                _append_jsonl(output, event)
                continue

            keep_status, _keep_error, keep_transport = _official_status(api, candidate["keep_sup"])
            cancel_status, cancel_error, cancel_transport = _official_status(api, candidate["cancel_sup"])
            event.update(
                {
                    "official_keep_status": keep_status,
                    "official_cancel_status": cancel_status,
                    "official_cancel_error": cancel_error[:600],
                    "official_keep_transport": keep_transport,
                    "official_cancel_transport": cancel_transport,
                }
            )

            if _clean(keep_transport) and _clean(keep_transport) != candidate["transport_document_number"]:
                event.update({"stage": "skipped_tss_keep_transport_mismatch"})
            elif _clean(cancel_transport) and _clean(cancel_transport) != candidate["transport_document_number"]:
                event.update({"stage": "skipped_tss_cancel_transport_mismatch"})
            elif _clean(cancel_status) not in CANCELLABLE_STATUSES:
                event.update({"stage": "skipped_cancel_status_not_cancellable"})
            elif _clean(keep_status) in CANCELLED_STATUSES:
                event.update({"stage": "skipped_keeper_cancelled_in_tss"})
            elif _clean(cancel_status) in DO_NOT_CANCEL_STATUSES:
                event.update({"stage": "skipped_cancel_status_terminal_or_processing"})
            elif not apply:
                event.update({"stage": "dry_run_would_cancel"})
            else:
                result = api.cancel_sdi(candidate["cancel_sup"])
                ok = supdec_routes._sdi_tss_result_ok(result)
                message = supdec_routes._sdi_tss_result_message(result)
                event.update(
                    {
                        "stage": "cancel_attempted",
                        "tss_cancel_ok": ok,
                        "tss_cancel_message": message,
                        "tss_cancel_status": result.get("status") if isinstance(result, dict) else None,
                        "tss_cancel_http_status": result.get("http_status") if isinstance(result, dict) else None,
                    }
                )
                if ok:
                    _mark_cancelled(sd, result)
                else:
                    _mark_cancel_failed(sd, result, message)

                read_status, read_error, _read_transport = _official_status(api, candidate["cancel_sup"])
                sync_result = supdec_routes.sync_prd_sdi_from_tss(sd["staging_id"], api=api)
                event.update(
                    {
                        "official_status_after": read_status,
                        "official_error_after": read_error[:600],
                        "sync_result": sync_result,
                    }
                )
                time.sleep(max(0.0, sleep_seconds))

            events.append(event)
            _append_jsonl(output, event)

        return {
            "started_at": started_at,
            "apply": apply,
            "due_date_filter": due_date or "",
            "duplicate_transport_groups": len(groups),
            "candidate_events": len(candidates),
            "counts": dict(Counter(event.get("stage", "unknown") for event in events)),
            "events": events,
            "output": str(output) if output else None,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Perform real TSS cancel_sdi calls.")
    parser.add_argument("--output", default="artifacts/sdi_duplicate_goods_cancel_20260609.jsonl")
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--retry-failed", action="store_true", help="Retry candidates that already have a TSS cancel failure recorded.")
    parser.add_argument("--due-date", default="", help="Optional YYYY-MM-DD filter; only duplicate candidates involving this due date are processed.")
    args = parser.parse_args()

    result = run(
        apply=args.apply,
        output=Path(args.output) if args.output else None,
        sleep_seconds=args.sleep_seconds,
        retry_failed=args.retry_failed,
        due_date=(args.due_date or "").strip() or None,
    )
    print(json.dumps(result, indent=2, default=str, ensure_ascii=True))


if __name__ == "__main__":
    main()
