"""Submit a small batch of PRD SDIs that are safe from active duplicates.

This script intentionally uses the same helper as the manual portal button so
the maintenance run follows the production UI path.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
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

if load_dotenv:
    load_dotenv(ROOT / ".env")

from app import create_app  # noqa: E402
from app.tss_api import build_cfg_client  # noqa: E402
from app.blueprints.supdec import routes as supdec_routes  # noqa: E402


CANCELLED_STATUSES = {
    "CANCELLED",
    "CANCELLED BY TSS",
    "CANCELED",
    "CANCELED BY TSS",
}

SKIP_TSS_STATUSES = {
    "CANCELLED",
    "CANCELED",
    "CLOSED",
    "COMPLETED",
    "PROCESSING",
    "SUBMITTED",
}

SUBMIT_LOCAL_STATUSES = {
    "DRAFT",
}

OFFICIAL_SUP_RE = re.compile(r"^SUP0{6,}\d+$")


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


def _status(row: dict[str, Any]) -> str:
    return _clean(_first_text(row.get("tss_status"), row.get("sub_status")))


def _local_sub_status(row: dict[str, Any]) -> str:
    return _clean(row.get("sub_status"))


def _sup(row: dict[str, Any]) -> str:
    return _first_text(row.get("sup_dec_number"), row.get("tss_sup_dec_number"))


def _has_official_sup_ref(row: dict[str, Any]) -> bool:
    return bool(OFFICIAL_SUP_RE.match(_sup(row)))


def _transport(row: dict[str, Any]) -> str:
    return _clean(row.get("transport_document_number"))


def _due_date(row: dict[str, Any]) -> str:
    value = row.get("tss_submission_due_date")
    if not value:
        return ""
    return str(value)[:10]


def _tss_status_from_read(result: Any) -> str:
    detail = supdec_routes._tss_response_payload(result)
    return _clean(_first_text(supdec_routes._tss_response_value(detail, "status")))


def _build_safe_queue(
    rows: list[dict[str, Any]],
    *,
    due_date: str | None = None,
) -> tuple[list[dict[str, Any]], set[str], dict[str, Any]]:
    active_by_transport: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if due_date and _due_date(row) != due_date:
            continue
        transport_doc = _transport(row)
        if not transport_doc:
            continue
        if _status(row) in CANCELLED_STATUSES:
            continue
        active_by_transport[transport_doc].append(row)

    duplicate_docs = {
        transport_doc
        for transport_doc, items in active_by_transport.items()
        if len({_sup(item) for item in items if _sup(item)}) > 1
    }

    queue = [
        row
        for row in rows
        if _status(row) in SUBMIT_LOCAL_STATUSES
        and _local_sub_status(row) not in {"SUBMITTED", "COMPLETED", "CLOSED"}
        and _sup(row)
        and _has_official_sup_ref(row)
        and _transport(row)
        and _transport(row) not in duplicate_docs
        and (not due_date or _due_date(row) == due_date)
    ]

    due_rows = [row for row in rows if not due_date or _due_date(row) == due_date]
    inventory = {
        "total_rows": len(rows),
        "due_date_filter": due_date or "",
        "due_rows": len(due_rows),
        "status_counts": dict(Counter(_status(row) or "UNKNOWN" for row in rows)),
        "due_status_counts": dict(Counter(_status(row) or "UNKNOWN" for row in due_rows)),
        "local_status_counts": dict(Counter(_local_sub_status(row) or "UNKNOWN" for row in rows)),
        "duplicate_transport_documents": sorted(duplicate_docs),
        "skipped_duplicate_count": sum(1 for row in due_rows if _transport(row) in duplicate_docs),
        "skipped_non_official_sup_count": sum(
            1
            for row in due_rows
            if _status(row) in SUBMIT_LOCAL_STATUSES and _sup(row) and not _has_official_sup_ref(row)
        ),
        "safe_queue_count": len(queue),
    }
    return queue, duplicate_docs, inventory


def _append_jsonl(path: Path | None, event: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, default=str, ensure_ascii=True) + "\n")


def run_batch(*, limit: int, sleep_seconds: float, output: Path | None, due_date: str | None = None) -> dict[str, Any]:
    app = create_app()
    with app.app_context():
        api = build_cfg_client()
        all_rows = supdec_routes._load_prd_sdi_headers("")
        queue, duplicate_docs, inventory = _build_safe_queue(all_rows, due_date=due_date)
        batch = queue[: max(0, limit)]
        events: list[dict[str, Any]] = []
        run_started_at = datetime.now(timezone.utc).isoformat()

        for index, queued in enumerate(batch, start=1):
            sid = queued.get("staging_id")
            sd = supdec_routes._load_prd_sdi_header_by_id(sid)
            if not sd:
                event = {
                    "stage": "not_found",
                    "staging_id": sid,
                    "queued_sup_ref": _sup(queued),
                }
                events.append(event)
                _append_jsonl(output, event)
                continue

            sup_ref = _sup(sd)
            transport_doc = _transport(sd)
            event = {
                "run_started_at": run_started_at,
                "index": index,
                "staging_id": sid,
                "sup_ref": sup_ref,
                "transport_document_number": transport_doc,
                "sfd_reference": sd.get("sfd_reference") or sd.get("tss_sfd_consignment_ref"),
                "local_status_before": _status(sd),
                "local_sub_status_before": _local_sub_status(sd),
            }

            if transport_doc in duplicate_docs:
                event.update({"stage": "skipped_duplicate", "action": "no_submit"})
                events.append(event)
                _append_jsonl(output, event)
                continue

            goods = supdec_routes._load_prd_sdi_goods(sid)
            mapping_issue = supdec_routes._prd_sdi_goods_mapping_issue(sd, goods)
            if mapping_issue:
                event.update(
                    {
                        "stage": "skipped_goods_mapping_duplicate",
                        "action": "no_submit",
                        "error": mapping_issue,
                        "goods_count": len(goods or []),
                    }
                )
                events.append(event)
                _append_jsonl(output, event)
                continue

            read_result = api.read_sdi(sup_ref, fields=list(supdec_routes.PRD_SDI_SUBMIT_READ_FIELDS))
            tss_status_before = _tss_status_from_read(read_result)
            event["tss_status_before"] = tss_status_before

            if tss_status_before in SKIP_TSS_STATUSES:
                sync_result = supdec_routes.sync_prd_sdi_from_tss(sid, api=api)
                event.update(
                    {
                        "stage": "skipped_tss_status",
                        "action": "sync_only",
                        "sync_result": sync_result,
                    }
                )
                events.append(event)
                _append_jsonl(output, event)
                time.sleep(max(0.0, sleep_seconds))
                continue

            summary, errors = supdec_routes._submit_single_prd_sdi_to_tss(sd, goods, api=api)
            fresh = supdec_routes._load_prd_sdi_header_by_id(sid) or {}
            event.update(
                {
                    "stage": "submitted_attempt",
                    "goods_count": len(goods or []),
                    "summary": summary,
                    "errors": errors,
                    "local_status_after": _status(fresh),
                    "tss_status_after_local": _status({"tss_status": fresh.get("tss_status")}),
                    "submitted": int(summary.get("submitted") or 0),
                    "blocked": int(summary.get("blocked") or 0),
                }
            )
            events.append(event)
            _append_jsonl(output, event)
            time.sleep(max(0.0, sleep_seconds))

        return {
            "run_started_at": run_started_at,
            "inventory": inventory,
            "batch_requested": limit,
            "batch_processed": len(events),
            "events": events,
            "counts": dict(Counter(event.get("stage", "unknown") for event in events)),
            "submitted": sum(int(event.get("submitted") or 0) for event in events),
            "blocked": sum(int(event.get("blocked") or 0) for event in events),
            "output": str(output) if output else None,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--output", default="artifacts/sdi_safe_submit_20260609.jsonl")
    parser.add_argument("--due-date", default="", help="Optional YYYY-MM-DD submission due date filter for submit queue.")
    args = parser.parse_args()

    result = run_batch(
        limit=args.limit,
        sleep_seconds=args.sleep_seconds,
        output=Path(args.output) if args.output else None,
        due_date=(args.due_date or "").strip() or None,
    )
    print(json.dumps(result, indent=2, default=str, ensure_ascii=True))


if __name__ == "__main__":
    main()
