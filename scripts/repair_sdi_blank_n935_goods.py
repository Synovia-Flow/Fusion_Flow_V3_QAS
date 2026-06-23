"""Repair SDI goods that contain corrupt nested document references in TSS.

The TSS API can accept a goods update while preserving an existing nested
document reference like ``N935::AC``. CDS later rejects the submit because the
AdditionalDocument has no ID. The public API also refuses to delete that nested
row because its unique reference is blank.

The same replacement path is used for SDI-denylisted residual documents such as
``1UKI``. This script repairs one or more SUP DECs by creating clean replacement
goods, verifying those goods do not contain known corrupt documents, then
deleting the old corrupt goods and updating the local STG goods IDs.

Default mode is dry-run. Use --apply to write to TSS/PRD.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.blueprints.supdec import routes as supdec_routes
from app.sdi_payloads import build_sdi_goods_update_payload_for_api_attempt
from app.tss_api import build_cfg_client


WRITE_ALLOWED_STATUSES = {"TRADER INPUT REQUIRED", "AMENDMENT REQUIRED"}
SDI_DOCUMENT_CODE_DENYLIST = {"1UKI"}

GOODS_READ_FIELDS = (
    "document_references",
    "goods_description",
    "commodity_code",
    "country_of_origin",
    "preference",
)


def _result_ok(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("http_status") and int(result["http_status"]) >= 400:
        return False
    if str(result.get("status") or "").strip().lower() in {"error", "failure", "failed"}:
        return False
    response = result.get("response")
    if isinstance(response, dict) and str(response.get("status") or "").strip().lower() in {"error", "failure", "failed"}:
        return False
    return True


def _result_message(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)
    response = result.get("response")
    if isinstance(response, dict):
        for key in ("process_message", "error_message", "message"):
            if response.get(key):
                return str(response[key])
    for key in ("process_message", "error_message", "message", "raw_response"):
        if result.get(key):
            return str(result[key])
    return str(result)


def _response_payload(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get("response", result.get("data", result))
    return result


def _response_goods_id(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    candidates = [result.get("reference"), result.get("goods_id"), result.get("id")]
    response = result.get("response")
    if isinstance(response, dict):
        candidates.extend(
            [
                response.get("reference"),
                response.get("goods_id"),
                response.get("goodsId"),
                response.get("id"),
                response.get("sys_id"),
            ]
        )
    for value in candidates:
        if value:
            return str(value)
    return ""


def _lookup_goods(api: Any, sup_ref: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in api.lookup_sdi_goods(sup_ref) or []:
        if not isinstance(item, dict):
            continue
        goods_id = supdec_routes._first_text(
            item.get("goods_id"),
            item.get("goodsId"),
            item.get("id"),
            item.get("sys_id"),
            item.get("reference"),
        )
        if goods_id:
            rows.append(_read_goods(api, goods_id))
    return rows


def _read_goods(api: Any, goods_id: str) -> dict[str, Any]:
    result = api._request(
        "GET",
        "goods",
        params={
            **api._act_as_params(),
            "reference": goods_id,
            "fields": ",".join(GOODS_READ_FIELDS),
        },
    )
    detail = _response_payload(result)
    documents = []
    for document in supdec_routes._tss_response_value(detail, "document_references") or []:
        if isinstance(document, dict):
            documents.append(
                {
                    "document_code": document.get("document_code"),
                    "document_reference": document.get("document_reference"),
                    "document_status": document.get("document_status"),
                    "document_reason": document.get("document_reason"),
                }
            )
    return {
        "goods_id": goods_id,
        "commodity_code": supdec_routes._tss_response_value(detail, "commodity_code"),
        "country_of_origin": supdec_routes._tss_response_value(detail, "country_of_origin"),
        "goods_description": supdec_routes._tss_response_value(detail, "goods_description"),
        "preference": supdec_routes._tss_response_value(detail, "preference"),
        "document_references": documents,
        "blank_n935": any(
            str(doc.get("document_code") or "").strip().upper() == "N935"
            and not str(doc.get("document_reference") or "").strip()
            for doc in documents
        ),
        "denylisted_documents": sorted(
            {
                str(doc.get("document_code") or "").strip().upper()
                for doc in documents
                if str(doc.get("document_code") or "").strip().upper() in SDI_DOCUMENT_CODE_DENYLIST
            }
        ),
        "n935_refs": [
            str(doc.get("document_reference") or "")
            for doc in documents
            if str(doc.get("document_code") or "").strip().upper() == "N935"
        ],
    }


def _load_sdi(sup_ref: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    matches = [
        row
        for row in supdec_routes._load_prd_sdi_headers("")
        if (row.get("sup_dec_number") or row.get("tss_sup_dec_number")) == sup_ref
    ]
    if not matches:
        raise RuntimeError(f"{sup_ref}: not found in STG.BKD_SDI_Headers")
    sd = supdec_routes._load_prd_sdi_header_by_id(matches[0]["staging_id"])
    goods = supdec_routes._load_prd_sdi_goods(sd["staging_id"])
    return sd, sorted(goods, key=lambda row: int(row.get("item_seq") or 0))


def _status_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _compact_text(value: Any, *, limit: int = 600) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def _official_sdi_status(api: Any, sup_ref: str) -> tuple[str, str]:
    result = api.read_sdi(sup_ref, fields=["status", "error_message"])
    detail = supdec_routes._tss_response_payload(result)
    status = supdec_routes._tss_response_value(detail, "status")
    error = supdec_routes._tss_response_value(detail, "error_message")
    return str(status or "").strip(), str(error or "").strip()


def _duplicate_transport_issue(sd: dict[str, Any]) -> str | None:
    duplicate_refs = supdec_routes._prd_sdi_has_active_transport_duplicate(sd)
    if not duplicate_refs:
        return None
    sup_ref = sd.get("sup_dec_number") or sd.get("tss_sup_dec_number") or "SDI"
    return (
        f"{sup_ref}: active duplicate SUP(s) with the same transport document "
        f"and same goods fingerprint: {', '.join(duplicate_refs)}"
    )


def _clean_create_payload(
    sd: dict[str, Any],
    item: dict[str, Any],
    sup_ref: str,
    *,
    current_tss_goods: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_record = dict(item)
    payload_record.setdefault("transport_document_number", sd.get("transport_document_number"))
    payload_record.setdefault("source_transport_document_number", sd.get("transport_document_number"))
    payload_record["invoice_number"] = sd.get("transport_document_number")
    if not payload_record.get("preference") and current_tss_goods and current_tss_goods.get("preference"):
        payload_record["preference"] = current_tss_goods["preference"]
    payload, _warnings = build_sdi_goods_update_payload_for_api_attempt(payload_record)
    payload.pop("goods_id", None)
    payload.pop("op_type", None)
    payload["op_type"] = "create"
    payload["consignment_number"] = sup_ref
    payload["goods_id"] = ""
    return payload


def _is_clean_replacement(row: dict[str, Any], item: dict[str, Any], transport_document: str) -> bool:
    if row.get("goods_id") == item.get("tss_goods_id") or row.get("blank_n935") or row.get("denylisted_documents"):
        return False
    if str(row.get("commodity_code") or "").replace(" ", "") != str(item.get("commodity_code") or "").replace(" ", ""):
        return False
    if str(row.get("country_of_origin") or "").upper() != str(item.get("country_of_origin") or "").upper():
        return False
    row_description = " ".join(str(row.get("goods_description") or "").upper().split())
    item_description = " ".join(str(item.get("goods_description") or "").upper().split())
    if row_description and item_description and row_description != item_description:
        return False
    return transport_document in set(row.get("n935_refs") or [])


def _corrupt_document_reasons(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if row.get("blank_n935"):
        reasons.append("blank_n935")
    for code in row.get("denylisted_documents") or []:
        reasons.append(f"denylisted_document_{code}")
    return reasons


def _repair_sup(api: Any, sup_ref: str, *, apply: bool, submit: bool, poll_seconds: list[int]) -> dict[str, Any]:
    sd, goods = _load_sdi(sup_ref)
    transport_document = str(sd.get("transport_document_number") or "").strip()
    official_status, official_error = _official_sdi_status(api, sup_ref)
    status_key = _status_key(official_status or sd.get("tss_status") or sd.get("sub_status"))
    current_goods = _lookup_goods(api, sup_ref)
    current_by_id = {row["goods_id"]: row for row in current_goods}

    targets = [
        item
        for item in goods
        if _corrupt_document_reasons(current_by_id.get(item.get("tss_goods_id"), {}))
    ]
    result: dict[str, Any] = {
        "sup_ref": sup_ref,
        "transport_document_number": transport_document,
        "targets": [
            {
                "stg_sdi_item_id": item.get("staging_id"),
                "item_seq": item.get("item_seq"),
                "old_goods_id": item.get("tss_goods_id"),
                "commodity_code": item.get("commodity_code"),
                "country_of_origin": item.get("country_of_origin"),
                "reasons": _corrupt_document_reasons(current_by_id.get(item.get("tss_goods_id"), {})),
            }
            for item in targets
        ],
        "created": [],
        "deleted": [],
        "local_updates": [],
        "submit": None,
        "polls": [],
        "official_status": official_status,
        "official_error": _compact_text(official_error),
    }
    if apply and status_key not in WRITE_ALLOWED_STATUSES:
        result["mode"] = "skipped_not_editable_status"
        result["reason"] = f"TSS status {official_status or status_key or 'unknown'} is not safe for blank N935 repair"
        if sd.get("staging_id"):
            try:
                result["sync"] = supdec_routes.sync_prd_sdi_from_tss(sd["staging_id"], api=api)
            except Exception as exc:
                result["sync_error"] = str(exc)
        return result

    mapping_issue = supdec_routes._prd_sdi_goods_mapping_issue(sd, goods)
    if apply and mapping_issue:
        result["mode"] = "skipped_goods_mapping_duplicate"
        result["reason"] = mapping_issue
        return result

    duplicate_issue = _duplicate_transport_issue(sd)
    if apply and duplicate_issue:
        result["mode"] = "skipped_duplicate_transport_document"
        result["reason"] = duplicate_issue
        return result

    if not targets or not apply:
        result["mode"] = "dry_run" if not apply else "no_targets"
        return result

    mapping: dict[int, str] = {}
    used_replacements: set[str] = set()

    def _map_local_item(item: dict[str, Any], new_goods_id: str, mode: str) -> None:
        with supdec_routes.db_cursor() as cursor:
            cursor.execute(
                """
                SELECT TOP 1 stg_sdi_item_id
                FROM [STG].[BKD_SDI_GoodsItems]
                WHERE ClientCode = ?
                  AND stg_sdi_id = ?
                  AND tss_goods_id = ?
                  AND stg_sdi_item_id <> ?
                """,
                [supdec_routes.S, sd["staging_id"], new_goods_id, item["staging_id"]],
            )
            duplicate = cursor.fetchone()
            if duplicate:
                cursor.execute(
                    """
                    UPDATE [STG].[BKD_SDI_GoodsItems]
                       SET sub_status = 'TSS_REMOVED',
                           sdi_validation_errors_json = NULL,
                           last_sub_status_change = SYSUTCDATETIME(),
                           updated_at = SYSUTCDATETIME()
                     WHERE ClientCode = ?
                       AND stg_sdi_item_id = ?
                    """,
                    [supdec_routes.S, item["staging_id"]],
                )
                update_mode = f"{mode}_marked_tss_removed_duplicate"
            else:
                cursor.execute(
                    """
                    UPDATE [STG].[BKD_SDI_GoodsItems]
                       SET tss_goods_id = ?,
                           sub_status = 'VALIDATED',
                           sdi_validation_errors_json = NULL,
                           sdi_ready_at = SYSUTCDATETIME(),
                           last_sub_status_change = SYSUTCDATETIME(),
                           updated_at = SYSUTCDATETIME()
                     WHERE ClientCode = ?
                       AND stg_sdi_item_id = ?
                    """,
                    [new_goods_id, supdec_routes.S, item["staging_id"]],
                )
                update_mode = mode
        result["local_updates"].append(
            {
                "stg_sdi_item_id": item["staging_id"],
                "item_seq": item.get("item_seq"),
                "new_goods_id": new_goods_id,
                "mode": update_mode,
            }
        )

    for item in targets:
        existing = [
            row
            for row in current_goods
            if row["goods_id"] not in used_replacements and _is_clean_replacement(row, item, transport_document)
        ]
        if existing:
            new_goods_id = existing[0]["goods_id"]
            used_replacements.add(new_goods_id)
        else:
            payload = _clean_create_payload(
                sd,
                item,
                sup_ref,
                current_tss_goods=current_by_id.get(item.get("tss_goods_id")),
            )
            create_result = api._request("POST", "goods", params=api._act_as_params(), payload=payload)
            new_goods_id = _response_goods_id(create_result)
            time.sleep(3)
            new_detail = _read_goods(api, new_goods_id) if new_goods_id else {}
            create_row = {
                "item_seq": item.get("item_seq"),
                "old_goods_id": item.get("tss_goods_id"),
                "new_goods_id": new_goods_id,
                "ok": _result_ok(create_result),
                "message": _result_message(create_result),
                "blank_n935": bool(new_detail.get("blank_n935")),
            }
            result["created"].append(create_row)
            replacement_reasons = _corrupt_document_reasons(new_detail)
            create_row["corrupt_document_reasons"] = replacement_reasons
            if not _result_ok(create_result) or not new_goods_id or replacement_reasons:
                if new_goods_id:
                    rollback = api._request(
                        "POST",
                        "goods",
                        params=api._act_as_params(),
                        payload={"op_type": "delete", "goods_id": new_goods_id},
                    )
                    create_row["rollback_message"] = _result_message(rollback)
                raise RuntimeError(f"{sup_ref}: replacement goods create failed: {create_row}")
            used_replacements.add(new_goods_id)

        old_goods_id = item.get("tss_goods_id")
        delete_result = api._request(
            "POST",
            "goods",
            params=api._act_as_params(),
            payload={"op_type": "delete", "goods_id": old_goods_id},
        )
        delete_row = {
            "item_seq": item.get("item_seq"),
            "old_goods_id": old_goods_id,
            "ok": _result_ok(delete_result),
            "message": _result_message(delete_result),
        }
        result["deleted"].append(delete_row)
        if not _result_ok(delete_result):
            raise RuntimeError(f"{sup_ref}: old goods delete failed: {delete_row}")
        time.sleep(1.5)
        mapping[int(item["staging_id"])] = new_goods_id
        _map_local_item(item, new_goods_id, "mapped_to_replacement")

    if submit:
        sd = supdec_routes._load_prd_sdi_header_by_id(sd["staging_id"])
        goods = supdec_routes._load_prd_sdi_goods(sd["staging_id"])
        summary, errors = supdec_routes._submit_single_prd_sdi_to_tss(sd, goods, api=api)
        result["submit"] = {"summary": summary, "errors": errors}
        for wait_seconds in poll_seconds:
            time.sleep(wait_seconds)
            read_result = api.read_sdi(sup_ref, fields=list(supdec_routes.PRD_SDI_SUBMIT_READ_FIELDS))
            detail = supdec_routes._tss_response_payload(read_result)
            result["polls"].append(
                {
                    "after_wait_s": wait_seconds,
                    "status": supdec_routes._tss_response_value(detail, "status"),
                    "error_message": supdec_routes._tss_response_value(detail, "error_message"),
                }
            )
        sync_result = supdec_routes.sync_prd_sdi_from_tss(sd["staging_id"], api=api)
        result["sync"] = sync_result
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sup", action="append", required=True, help="SUP reference to repair. Repeatable.")
    parser.add_argument("--apply", action="store_true", help="Write changes to TSS/PRD. Default is dry-run.")
    parser.add_argument("--submit", action="store_true", help="Submit each repaired SUP after repair.")
    parser.add_argument(
        "--poll",
        default="20,60,120",
        help="Comma-separated poll delays in seconds after submit. Default: 20,60,120.",
    )
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    poll_seconds = [int(part.strip()) for part in str(args.poll or "").split(",") if part.strip()]
    app = create_app()
    with app.app_context():
        api = build_cfg_client()
        for sup_ref in args.sup:
            try:
                result = _repair_sup(api, sup_ref, apply=args.apply, submit=args.submit, poll_seconds=poll_seconds)
            except Exception as exc:
                result = {"sup_ref": sup_ref, "mode": "error", "error": str(exc)}
            print(json.dumps(result, default=str, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
