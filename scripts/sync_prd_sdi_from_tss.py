"""Reconcile PRD SDI/SUPDEC rows from the official TSS API.

Dry-run reads TSS and reports drift. Use --apply to persist the official TSS
header/goods state through the same sync helper used by the portal button.
"""

from __future__ import annotations

import argparse
from collections import Counter
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


OFFICIAL_SUP_RE = re.compile(r"^SUP0{6,}\d+$")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _status_key(value: Any) -> str:
    return _clean(value).replace("_", " ").upper()


def _sup(row: dict[str, Any]) -> str:
    return _clean(row.get("sup_dec_number") or row.get("tss_sup_dec_number"))


def _local_status(row: dict[str, Any]) -> str:
    return _status_key(row.get("sub_status") or row.get("status"))


def _tss_status(row: dict[str, Any]) -> str:
    return _status_key(row.get("tss_status"))


def _due_date(row: dict[str, Any]) -> str:
    value = row.get("tss_submission_due_date") or row.get("submission_due_date")
    return str(value)[:10] if value else ""


def _append_jsonl(path: Path | None, event: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, default=str, ensure_ascii=True) + "\n")


def _existing_output_sup_refs(path: Path | None) -> set[str]:
    if not path or not path.exists():
        return set()
    refs: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except Exception:
                continue
            sup_ref = _clean(event.get("sup_ref")).upper()
            if sup_ref:
                refs.add(sup_ref)
    return refs


def _official_read(api: Any, sup_ref: str) -> dict[str, Any]:
    result = api.read_sdi(sup_ref, fields=list(supdec_routes.PRD_SDI_SUBMIT_READ_FIELDS))
    detail = supdec_routes._tss_response_payload(result)
    status = supdec_routes._tss_response_value(detail, "status")
    error_message = supdec_routes._tss_response_value(detail, "error_message")
    return {
        "ok": supdec_routes._sdi_tss_result_ok(result),
        "status": _status_key(status),
        "error_message": _clean(error_message),
        "message": supdec_routes._sdi_tss_result_message(result),
        "result": result,
    }


def _select_rows(
    rows: list[dict[str, Any]],
    *,
    due_date: str,
    statuses: set[str],
    include_terminal: bool,
) -> list[dict[str, Any]]:
    terminal = {"CLOSED", "COMPLETED", "CANCELLED", "CANCELED"}
    selected = []
    for row in rows:
        sup_ref = _sup(row)
        if not OFFICIAL_SUP_RE.match(sup_ref):
            continue
        if due_date and _due_date(row) != due_date:
            continue
        effective_status = _tss_status(row) or _local_status(row)
        if statuses and effective_status not in statuses:
            continue
        if not include_terminal and effective_status in terminal:
            continue
        selected.append(row)
    return selected


def run_sync(
    *,
    apply: bool,
    due_date: str,
    statuses: set[str],
    include_terminal: bool,
    limit: int,
    sleep_seconds: float,
    output: Path | None,
    skip_sup_refs: set[str] | None = None,
) -> dict[str, Any]:
    app = create_app()
    with app.app_context():
        api = build_cfg_client()
        rows = supdec_routes._load_prd_sdi_headers("")
        selected = _select_rows(
            rows,
            due_date=due_date,
            statuses=statuses,
            include_terminal=include_terminal,
        )
        if skip_sup_refs:
            selected = [row for row in selected if _sup(row).upper() not in skip_sup_refs]
        if limit > 0:
            selected = selected[:limit]

        started_at = datetime.now(timezone.utc).isoformat()
        events: list[dict[str, Any]] = []
        for index, row in enumerate(selected, start=1):
            sid = row.get("staging_id")
            sup_ref = _sup(row)
            before_local = _local_status(row)
            before_tss = _tss_status(row)
            event = {
                "run_started_at": started_at,
                "index": index,
                "apply": apply,
                "staging_id": sid,
                "sup_ref": sup_ref,
                "transport_document_number": row.get("transport_document_number"),
                "sfd_reference": row.get("sfd_reference") or row.get("tss_sfd_consignment_ref"),
                "submission_due_date": _due_date(row),
                "local_sub_status_before": before_local,
                "local_tss_status_before": before_tss,
            }

            try:
                if apply:
                    sync_result = supdec_routes.sync_prd_sdi_from_tss(sid, api=api)
                    fresh = supdec_routes._load_prd_sdi_header_by_id(sid) or {}
                    event.update(
                        {
                            "stage": "synced" if sync_result.get("ok") else "sync_failed",
                            "sync_result": sync_result,
                            "official_status": _status_key(sync_result.get("tss_status")),
                            "local_sub_status_after": _local_status(fresh),
                            "local_tss_status_after": _tss_status(fresh),
                            "changed": (
                                before_local != _local_status(fresh)
                                or before_tss != _tss_status(fresh)
                            ),
                        }
                    )
                else:
                    official = _official_read(api, sup_ref)
                    event.update(
                        {
                            "stage": "read" if official["ok"] else "read_failed",
                            "official_status": official["status"],
                            "official_error_message": official["error_message"],
                            "official_message": official["message"],
                            "drift": (
                                bool(official["status"])
                                and official["status"] not in {before_local, before_tss}
                            ),
                        }
                    )
            except Exception as exc:  # pragma: no cover - operational guard
                event.update({"stage": "exception", "error": str(exc)})

            events.append(event)
            _append_jsonl(output, event)
            time.sleep(max(0.0, sleep_seconds))

        return {
            "run_started_at": started_at,
            "apply": apply,
            "due_date": due_date,
            "statuses": sorted(statuses),
            "include_terminal": include_terminal,
            "selected": len(selected),
            "counts": dict(Counter(event.get("stage") for event in events)),
            "status_counts": dict(Counter(event.get("official_status") or event.get("local_tss_status_after") or "UNKNOWN" for event in events)),
            "drift": sum(1 for event in events if event.get("drift")),
            "changed": sum(1 for event in events if event.get("changed")),
            "output": str(output) if output else None,
            "events": events,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Persist TSS state into STG/TSS mirrors.")
    parser.add_argument("--due-date", default="", help="Optional YYYY-MM-DD TSS submission due date filter.")
    parser.add_argument(
        "--status",
        action="append",
        default=[],
        help="Optional current local/TSS status filter. Can be repeated.",
    )
    parser.add_argument("--include-terminal", action="store_true", help="Also sync closed/cancelled rows.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum rows to process. 0 means all selected rows.")
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    parser.add_argument("--output", default="artifacts/sdi_tss_reconcile.jsonl")
    parser.add_argument("--skip-existing-output", action="store_true", help="Skip SUP refs already present in --output JSONL.")
    parser.add_argument("--summary-only", action="store_true", help="Print only run totals; JSONL still keeps row detail.")
    args = parser.parse_args()

    statuses = {_status_key(status) for status in args.status if _status_key(status)}
    output_path = Path(args.output) if args.output else None
    skip_sup_refs = _existing_output_sup_refs(output_path) if args.skip_existing_output else set()
    summary = run_sync(
        apply=args.apply,
        due_date=args.due_date.strip(),
        statuses=statuses,
        include_terminal=args.include_terminal,
        limit=max(0, args.limit),
        sleep_seconds=args.sleep_seconds,
        output=output_path,
        skip_sup_refs=skip_sup_refs,
    )
    if skip_sup_refs:
        summary["skipped_existing_output"] = len(skip_sup_refs)
    if args.summary_only:
        summary = {key: value for key, value in summary.items() if key != "events"}
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=True))


if __name__ == "__main__":
    main()
