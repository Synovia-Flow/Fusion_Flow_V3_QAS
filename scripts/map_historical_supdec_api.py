"""Read historical SUP DECs from TSS and extract SDI masterdata candidates.

This script is intentionally read-only. It calls:

    GET supplementary_declarations?reference=SUP...
    GET goods?sup_dec_number=SUP...

Outputs are resumable and written under artifacts/sdi_historical_api_mapping
by default.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import UTC, datetime
import json
from pathlib import Path
import re
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

from app.ingestion.sdi_autosubmit import SDI_READ_FIELDS  # noqa: E402
from app.sdi_payloads import SDI_GOODS_NESTED_FIELDS, SDI_GOODS_OPTIONAL_FIELDS, SDI_GOODS_REQUIRED_FIELDS  # noqa: E402
from app.tss_api import build_cfg_client  # noqa: E402


DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "sdi_historical_api_mapping"
DEFAULT_INPUT = Path(r"E:\Downloads\sn_customerservice_supplementary_declaration (1).csv")

TERMINAL_STATUSES = {"ACCEPTED", "CLEARED", "CLOSED", "COMPLETED"}
GOODS_READ_FIELDS = tuple(dict.fromkeys((
    "goods_id",
    "item_number",
    "goods_description",
    *(
        source_name
        for source_names in SDI_GOODS_REQUIRED_FIELDS.values()
        for source_name in source_names
    ),
    *(
        source_name
        for source_names in SDI_GOODS_OPTIONAL_FIELDS.values()
        for source_name in source_names
    ),
    *(
        source_name
        for source_names in SDI_GOODS_NESTED_FIELDS.values()
        for source_name in source_names
    ),
    "document_code",
    "document_status",
    "document_reference",
    "countryOfOrigin",
    "commodityCode",
    "documentReferences",
    "additionalInformation",
)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Map historical SUP DEC data from TSS API.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="CSV export with a number column containing SUP refs.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for JSONL/CSV outputs.")
    parser.add_argument("--limit", type=int, default=None, help="Optional max number of SUP refs to process.")
    parser.add_argument("--sleep", type=float, default=0.15, help="Delay between SUP refs, in seconds.")
    parser.add_argument("--statuses", default="", help="Comma-separated CSV states to include. Empty means all.")
    parser.add_argument("--closed-only", action="store_true", help="Only process CSV rows with state=Closed.")
    parser.add_argument("--force", action="store_true", help="Re-fetch SUP refs already present in raw output.")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if load_dotenv:
        load_dotenv(ROOT / ".env")

    rows = _load_input_rows(input_path)
    rows = _filter_rows(rows, statuses=args.statuses, closed_only=args.closed_only)
    if args.limit:
        rows = rows[: max(0, args.limit)]

    raw_path = output_dir / "supdec_api_raw.jsonl"
    if args.force and raw_path.exists():
        raw_path.unlink()
    processed = set() if args.force else _processed_refs(raw_path)

    client = _build_client()
    started_at = _utc_now()
    print(f"Started {started_at}. Input rows={len(rows)} already_cached={len(processed)}")

    raw_file = raw_path.open("a", encoding="utf-8", newline="\n")
    try:
        for idx, row in enumerate(rows, start=1):
            sup_ref = _text(row.get("number"))
            if not sup_ref:
                continue
            if sup_ref in processed:
                if idx % 100 == 0:
                    print(f"{idx}/{len(rows)} skipped cached up to {sup_ref}")
                continue

            record = _fetch_supdec(client, row)
            raw_file.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
            raw_file.flush()
            processed.add(sup_ref)

            status = _text(_pick(record.get("detail") or {}, "status", "state")) or record.get("csv_state")
            goods_count = len(record.get("goods") or [])
            error = _text(record.get("error"))
            marker = f" error={error[:80]}" if error else ""
            print(f"{idx}/{len(rows)} {sup_ref} status={status or '-'} goods={goods_count}{marker}")
            if args.sleep > 0:
                time.sleep(args.sleep)
    finally:
        raw_file.close()

    records = _read_jsonl(raw_path)
    _write_outputs(records, output_dir)
    print(f"Done. Outputs written to {output_dir}")
    return 0


def _build_client() -> Any:
    try:
        return build_cfg_client()
    except RuntimeError as exc:
        if "Working outside of application context" not in str(exc):
            raise
        from app import create_app

        app = create_app()
        ctx = app.app_context()
        ctx.push()
        return build_cfg_client()


def _load_input_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _filter_rows(rows: list[dict[str, Any]], *, statuses: str, closed_only: bool) -> list[dict[str, Any]]:
    if closed_only:
        wanted = {"CLOSED"}
    else:
        wanted = {_normalise_status(value) for value in statuses.split(",") if value.strip()}
    if not wanted:
        return rows
    return [row for row in rows if _normalise_status(row.get("state")) in wanted]


def _processed_refs(path: Path) -> set[str]:
    if not path.exists():
        return set()
    refs = set()
    for record in _read_jsonl(path):
        ref = _text(record.get("sup_ref"))
        if ref:
            refs.add(ref)
    return refs


def _fetch_supdec(client: Any, csv_row: dict[str, Any]) -> dict[str, Any]:
    sup_ref = _text(csv_row.get("number"))
    record: dict[str, Any] = {
        "sup_ref": sup_ref,
        "csv_state": _text(csv_row.get("state")),
        "csv_parent": _text(csv_row.get("parent")),
        "csv_transport_document_reference": _text(csv_row.get("u_transport_document_reference")),
        "csv_trader_reference": _text(csv_row.get("u_trader_reference")),
        "csv_importer_eori": _text(csv_row.get("u_importer_eori")),
        "csv_exporter_eori": _text(csv_row.get("u_exporter_eori")),
        "csv_arrival_date_time": _text(csv_row.get("u_arrival_date_time")),
        "csv_submission_due_date": _text(csv_row.get("u_submission_due_date")),
        "fetched_at": _utc_now(),
        "detail": {},
        "goods": [],
    }

    try:
        detail_result = client.read_sdi(sup_ref, fields=list(SDI_READ_FIELDS))
        record["detail_result_status"] = _text(detail_result.get("status")) if isinstance(detail_result, dict) else ""
        record["detail"] = _unwrap_response(detail_result)
    except Exception as exc:
        record["detail_error"] = str(exc)

    try:
        goods = _lookup_sdi_goods_with_fields(client, sup_ref)
        record["goods"] = [item for item in goods or [] if isinstance(item, dict)]
    except Exception as exc:
        record["goods_error"] = str(exc)

    if record.get("detail_error") or record.get("goods_error"):
        record["error"] = "; ".join(
            value for value in (_text(record.get("detail_error")), _text(record.get("goods_error"))) if value
        )
    return record


def _lookup_sdi_goods_with_fields(client: Any, sup_ref: str) -> list[dict[str, Any]]:
    lookup_items = client.lookup_sdi_goods(sup_ref) or []
    full_items: list[dict[str, Any]] = []
    for item in lookup_items:
        if not isinstance(item, dict):
            continue
        goods_id = _text(_pick(item, "goods_id", "goodsId", "reference", "id", "sys_id"))
        if not goods_id or not hasattr(client, "read_goods"):
            full_items.append(item)
            continue
        try:
            detail = _unwrap_response(client.read_goods(goods_id, list(GOODS_READ_FIELDS)))
        except Exception as exc:
            merged = dict(item)
            merged["read_goods_error"] = str(exc)
            full_items.append(merged)
            continue
        merged = dict(item)
        merged.update(detail or {})
        merged.setdefault("goods_id", goods_id)
        full_items.append(merged)
    return full_items


def _write_outputs(records: list[dict[str, Any]], output_dir: Path) -> None:
    sup_rows = []
    goods_rows = []
    doc_counter: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    field_counter: dict[tuple[str, str, str, str, str], Counter[str]] = defaultdict(Counter)

    for record in records:
        detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
        goods = record.get("goods") if isinstance(record.get("goods"), list) else []
        tss_status = _text(_pick(detail, "status", "state"))
        is_terminal = _normalise_status(tss_status or record.get("csv_state")) in TERMINAL_STATUSES
        error_message = _text(_pick(detail, "error_message", "errorMessage", "process_message", "processMessage"))
        all_doc_codes: set[str] = set()

        for item_index, item in enumerate(goods, start=1):
            documents = _extract_documents(item)
            doc_codes = sorted({_text(doc.get("document_code")).upper() for doc in documents if _text(doc.get("document_code"))})
            all_doc_codes.update(doc_codes)

            commodity = _text(_pick(item, "commodity_code", "commodityCode")).replace(" ", "")
            origin = _text(_pick(item, "country_of_origin", "countryOfOrigin"))
            goods_description = _text(_pick(item, "goods_description", "goodsDescription"))
            item_number = _text(_pick(item, "item_number", "itemNumber", "goods_item_number", "goodsItemNumber")) or str(item_index)
            goods_id = _text(_pick(item, "goods_id", "goodsId", "id", "sys_id"))
            key_base = (commodity, origin)

            goods_rows.append({
                "sup_ref": record.get("sup_ref"),
                "csv_state": record.get("csv_state"),
                "tss_status": tss_status,
                "parent_dec": record.get("csv_parent") or _text(_pick(detail, "sfd_number", "sfdNumber")),
                "transport_reference": record.get("csv_transport_document_reference")
                or _text(_pick(detail, "transport_document_number", "transportDocumentNumber")),
                "goods_id": goods_id,
                "item_number": item_number,
                "commodity_code": commodity,
                "country_of_origin": origin,
                "goods_description": goods_description,
                "procedure_code": _text(_pick(item, "procedure_code", "procedureCode")),
                "additional_procedure_code": _text(_pick(item, "additional_procedure_code", "additionalProcedureCode")),
                "valuation_method": _text(_pick(item, "valuation_method", "valuationMethod")),
                "valuation_indicator": _text(_pick(item, "valuation_indicator", "valuationIndicator")),
                "nature_of_transaction": _text(_pick(item, "nature_of_transaction", "natureOfTransaction")),
                "preference": _text(_pick(item, "preference", "preference_code", "preferenceCode")),
                "ni_additional_information_codes": _text(_pick(item, "ni_additional_information_codes", "niAdditionalInformationCodes")),
                "document_codes": "|".join(doc_codes),
                "documents_json": json.dumps(documents, ensure_ascii=True, default=str),
            })

            if is_terminal:
                for field_name in (
                    "procedure_code",
                    "additional_procedure_code",
                    "valuation_method",
                    "valuation_indicator",
                    "nature_of_transaction",
                    "preference",
                    "ni_additional_information_codes",
                ):
                    value = _text(goods_rows[-1].get(field_name))
                    if value:
                        field_counter[(*key_base, field_name, value, "")][record["sup_ref"]] += 1

            for doc in documents:
                doc_code = _text(doc.get("document_code")).upper()
                if not is_terminal or not doc_code or doc_code == "N935":
                    continue
                doc_key = (
                    commodity,
                    origin,
                    doc_code,
                    _text(doc.get("document_status")),
                    _text(doc.get("document_reference")),
                )
                entry = doc_counter.setdefault(
                    doc_key,
                    {
                        "commodity_code": commodity,
                        "country_of_origin": origin,
                        "document_code": doc_code,
                        "document_status": _text(doc.get("document_status")),
                        "document_reference": _text(doc.get("document_reference")),
                        "document_part": _text(doc.get("document_part")),
                        "document_reason": _text(doc.get("document_reason")),
                        "sup_refs": set(),
                        "goods_count": 0,
                        "sample_descriptions": [],
                    },
                )
                entry["sup_refs"].add(record["sup_ref"])
                entry["goods_count"] += 1
                if goods_description and len(entry["sample_descriptions"]) < 5:
                    entry["sample_descriptions"].append(goods_description)

        sup_rows.append({
            "sup_ref": record.get("sup_ref"),
            "csv_state": record.get("csv_state"),
            "tss_status": tss_status,
            "parent_dec": record.get("csv_parent") or _text(_pick(detail, "sfd_number", "sfdNumber")),
            "transport_reference": record.get("csv_transport_document_reference")
            or _text(_pick(detail, "transport_document_number", "transportDocumentNumber")),
            "arrival_date_time": record.get("csv_arrival_date_time") or _text(_pick(detail, "arrival_date_time", "arrivalDateTime")),
            "submission_due_date": record.get("csv_submission_due_date") or _text(_pick(detail, "submission_due_date", "submissionDueDate")),
            "error_message": error_message,
            "goods_count": len(goods),
            "document_codes": "|".join(sorted(all_doc_codes)),
            "detail_error": record.get("detail_error", ""),
            "goods_error": record.get("goods_error", ""),
        })

    doc_rows = []
    for entry in doc_counter.values():
        sup_refs = sorted(entry.pop("sup_refs"))
        doc_rows.append({
            **entry,
            "sup_count": len(sup_refs),
            "sample_sup_refs": "|".join(sup_refs[:10]),
            "sample_descriptions": " | ".join(entry.get("sample_descriptions") or []),
        })
    doc_rows.sort(key=lambda row: (-int(row.get("sup_count") or 0), row.get("commodity_code") or "", row.get("document_code") or ""))

    field_rows = []
    for (commodity, origin, field_name, value, _), refs in field_counter.items():
        field_rows.append({
            "commodity_code": commodity,
            "country_of_origin": origin,
            "field_name": field_name,
            "value": value,
            "sup_count": len(refs),
            "sample_sup_refs": "|".join(sorted(refs)[:10]),
        })
    field_rows.sort(key=lambda row: (-int(row.get("sup_count") or 0), row.get("commodity_code") or "", row.get("field_name") or ""))

    missing_doc_rows = _build_missing_document_suggestions(goods_rows, sup_rows, doc_rows)

    _write_csv(output_dir / "supdec_api_summary.csv", sup_rows)
    _write_csv(output_dir / "goods_api_summary.csv", goods_rows)
    _write_csv(output_dir / "document_candidates_by_commodity.csv", doc_rows)
    _write_csv(output_dir / "field_candidates_by_commodity.csv", field_rows)
    _write_csv(output_dir / "missing_document_suggestions.csv", missing_doc_rows)
    _write_json(output_dir / "run_summary.json", {
        "generated_at": _utc_now(),
        "sup_count": len(sup_rows),
        "goods_count": len(goods_rows),
        "document_candidate_count": len(doc_rows),
        "field_candidate_count": len(field_rows),
        "missing_document_suggestion_count": len(missing_doc_rows),
        "terminal_sup_count": sum(1 for row in sup_rows if _normalise_status(row.get("tss_status") or row.get("csv_state")) in TERMINAL_STATUSES),
        "states": Counter(_normalise_status(row.get("tss_status") or row.get("csv_state")) for row in sup_rows),
    })


def _build_missing_document_suggestions(
    goods_rows: list[dict[str, Any]],
    sup_rows: list[dict[str, Any]],
    doc_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in doc_rows:
        commodity = _text(row.get("commodity_code"))
        origin = _text(row.get("country_of_origin"))
        if commodity:
            candidates[(commodity, origin)].append(row)
            candidates[(commodity, "")].append(row)

    sup_errors: dict[str, str] = {}
    error_item_numbers: dict[str, set[str]] = {}
    for row in sup_rows:
        sup_ref = _text(row.get("sup_ref"))
        error_message = _text(row.get("error_message"))
        if not sup_ref or not error_message:
            continue
        sup_errors[sup_ref] = error_message
        error_item_numbers[sup_ref] = _parse_error_item_numbers(error_message)

    suggestions = []
    seen = set()
    for goods in goods_rows:
        sup_ref = _text(goods.get("sup_ref"))
        if sup_ref not in sup_errors:
            continue
        status = _normalise_status(goods.get("tss_status") or goods.get("csv_state"))
        if status in TERMINAL_STATUSES:
            continue

        item_number = _text(goods.get("item_number"))
        focused_items = error_item_numbers.get(sup_ref) or set()
        if focused_items and item_number and item_number not in focused_items:
            continue

        commodity = _text(goods.get("commodity_code"))
        origin = _text(goods.get("country_of_origin"))
        current_codes = {
            value.strip().upper()
            for value in _text(goods.get("document_codes")).split("|")
            if value.strip()
        }
        for candidate in candidates.get((commodity, origin), []) or candidates.get((commodity, ""), []):
            code = _text(candidate.get("document_code")).upper()
            if not code or code == "N935" or code in current_codes:
                continue
            key = (sup_ref, item_number, commodity, origin, code, _text(candidate.get("document_reference")))
            if key in seen:
                continue
            seen.add(key)
            suggestions.append({
                "sup_ref": sup_ref,
                "tss_status": goods.get("tss_status"),
                "item_number": item_number,
                "goods_id": goods.get("goods_id"),
                "commodity_code": commodity,
                "country_of_origin": origin,
                "goods_description": goods.get("goods_description"),
                "current_document_codes": goods.get("document_codes"),
                "suggested_document_code": code,
                "suggested_document_reference": candidate.get("document_reference"),
                "suggested_document_status": candidate.get("document_status"),
                "suggested_document_reason": candidate.get("document_reason"),
                "evidence_goods_count": candidate.get("goods_count"),
                "evidence_sup_count": candidate.get("sup_count"),
                "evidence_sup_refs": candidate.get("sample_sup_refs"),
                "tss_error_item_numbers": "|".join(sorted(error_item_numbers.get(sup_ref) or set(), key=_as_int_for_sort)),
                "tss_error_message": sup_errors[sup_ref],
            })

    suggestions.sort(key=lambda row: (
        row.get("sup_ref") or "",
        _as_int_for_sort(row.get("item_number")),
        row.get("commodity_code") or "",
        row.get("suggested_document_code") or "",
    ))
    return suggestions


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row}) if rows else ["empty"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=True, indent=2, default=_json_default)
        handle.write("\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
    return rows


def _extract_documents(item: dict[str, Any]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for key, value in item.items():
        lower_key = str(key).lower()
        if "document" not in lower_key:
            continue
        documents.extend(_normalise_document_items(value))

    flat_code = _text(_pick(item, "document_code", "documentCode"))
    if flat_code:
        documents.append({
            "document_code": flat_code,
            "document_status": _text(_pick(item, "document_status", "documentStatus")),
            "document_reference": _text(_pick(item, "document_reference", "documentReference")),
        })

    normalised: list[dict[str, Any]] = []
    seen = set()
    for doc in documents:
        doc = _normalise_document(doc)
        if not doc.get("document_code"):
            continue
        key = tuple(str(doc.get(name, "")).strip().upper() for name in sorted(doc))
        if key in seen:
            continue
        seen.add(key)
        normalised.append(doc)
    return normalised


def _normalise_document_items(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except ValueError:
            return []
        return _normalise_document_items(parsed)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _normalise_document(doc: dict[str, Any]) -> dict[str, Any]:
    result = {
        "op_type": _text(_pick(doc, "op_type", "opType")) or "create",
        "document_code": _text(_pick(doc, "document_code", "documentCode", "code")).upper(),
        "document_status": _text(_pick(doc, "document_status", "documentStatus", "status")),
        "document_reference": _text(_pick(doc, "document_reference", "documentReference", "reference")),
        "document_part": _text(_pick(doc, "document_part", "documentPart", "part")),
        "document_reason": _text(_pick(doc, "document_reason", "documentReason", "reason")),
    }
    return {key: value for key, value in result.items() if value}


def _unwrap_response(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    payload = result.get("response", result.get("data", result))
    while isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    return payload if isinstance(payload, dict) else {}


def _pick(record: dict[str, Any], *names: str) -> Any:
    if not isinstance(record, dict):
        return None
    for name in names:
        if name in record and record[name] not in (None, ""):
            value = record[name]
            if isinstance(value, dict):
                for nested_key in ("value", "display_value", "displayValue", "label", "name"):
                    nested = value.get(nested_key)
                    if nested not in (None, ""):
                        return nested
                continue
            return value
    return None


def _normalise_status(value: Any) -> str:
    return _text(value).strip().upper()


def _parse_error_item_numbers(message: Any) -> set[str]:
    return {match.lstrip("0") or "0" for match in re.findall(r"Goods Item Number:\s*(\d+)", _text(message), re.I)}


def _as_int_for_sort(value: Any) -> int:
    try:
        return int(_text(value))
    except Exception:
        return 999999


def _text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, set, tuple, Counter)):
        return json.dumps(value, ensure_ascii=True, default=_json_default)
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Counter):
        return dict(value)
    return str(value)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
