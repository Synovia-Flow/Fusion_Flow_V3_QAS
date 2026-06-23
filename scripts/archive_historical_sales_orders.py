"""Backfill historical Sales Orders XLSX attachments into the file archive.

The current mailbox worker archives new Sales Orders workbooks as they arrive.
This script rehydrates older workbook attachments from Microsoft Graph using
the Graph message/attachment ids already stored in ING.BKD_Email* tables.

Dry-run is the default. Use --apply to write files and update ING archive paths.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional local/dev helper
    load_dotenv = None

if load_dotenv:
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

for dotted_key in (
    "GRAPH.TENANT_ID",
    "GRAPH.CLIENT_ID",
    "GRAPH.CLIENT_SECRET",
    "GRAPH.MAILBOX",
    "GRAPH.FOLDER",
    "GRAPH.PROCESSED_FOLDER",
):
    env_key = dotted_key.replace(".", "_")
    if not os.environ.get(env_key) and os.environ.get(dotted_key):
        os.environ[env_key] = os.environ[dotted_key]

from app.db import get_standalone_connection
from app.ingestion.defaults import resolve_graph_mail_settings
from app.ingestion.email_batch import is_sales_order_workbook_attachment
from app.ingestion.graph_mail import GraphMailClient
from scripts.pull_inbound_email import (
    _attachment_archive_dir,
    _parse_message_datetime,
    _sales_order_archive_path,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload or b"").hexdigest()


def _archive_path(message: dict[str, Any], attachment: dict[str, Any]) -> Path:
    filename = str(attachment.get("filename") or "attachment.xlsx")
    received_dt = _parse_message_datetime(message.get("received") or message.get("date"))
    return _sales_order_archive_path(_attachment_archive_dir(), filename, received_dt, attachment.get("bytes") or b"")


def _archive_attachment(message: dict[str, Any], attachment: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    dest = _archive_path(message, attachment)
    if not apply:
        return {"ok": True, "path": str(dest), "created": False, "dry_run": True}

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            return {"ok": True, "path": str(dest), "created": False}
        dest.write_bytes(attachment.get("bytes") or b"")
        return {"ok": True, "path": str(dest), "created": True}
    except Exception as exc:
        return {"ok": False, "path": str(dest), "error": str(exc)}


def _load_historical_attachments(cur: Any, *, limit: int | None, force: bool) -> list[dict[str, Any]]:
    top = f"TOP ({int(limit)})" if limit else ""
    force_filter = "" if force else "AND NULLIF(LTRIM(RTRIM(COALESCE(a.DownloadedPath, sfl.ArchivePath, ''))), '') IS NULL"
    cur.execute(
        f"""
        SELECT {top}
            a.EmailAttachmentId,
            a.GraphAttachmentId,
            a.OriginalName,
            a.SizeBytes,
            a.Sha256,
            a.DownloadedPath,
            a.SourceFileId,
            m.GraphMessageId,
            m.ReceivedAt,
            m.Subject,
            m.SenderEmail,
            sfl.FileId,
            sfl.FileName,
            sfl.FileSha256,
            sfl.ArchivePath
        FROM [ING].[BKD_EmailAttachment] a
        JOIN [ING].[BKD_EmailMessage] m
          ON m.EmailMessageId = a.EmailMessageId
        LEFT JOIN [ING].[BKD_SourceFileLog] sfl
          ON sfl.FileSha256 = a.Sha256
        WHERE a.ClientCode = 'BKD'
          AND LOWER(COALESCE(a.OriginalName, '')) LIKE '%.xlsx'
          AND COALESCE(a.Status, '') <> 'Skipped'
          AND NULLIF(LTRIM(RTRIM(COALESCE(m.GraphMessageId, ''))), '') IS NOT NULL
          {force_filter}
        ORDER BY COALESCE(m.ReceivedAt, a.DownloadedAt), a.EmailAttachmentId
        """,
    )
    columns = [column[0] for column in cur.description or []]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _find_attachment(message: dict[str, Any], record: dict[str, Any]) -> dict[str, Any] | None:
    attachments = message.get("attachments") or []
    wanted_id = str(record.get("GraphAttachmentId") or "")
    wanted_sha = str(record.get("Sha256") or "").lower()
    wanted_name = str(record.get("OriginalName") or "").lower()
    wanted_size = int(record.get("SizeBytes") or 0)

    for item in attachments:
        if wanted_id and str(item.get("attachment_id") or "") == wanted_id:
            return item

    for item in attachments:
        payload = item.get("bytes") or b""
        if wanted_sha and _sha256(payload).lower() == wanted_sha:
            return item

    for item in attachments:
        if (
            wanted_name
            and str(item.get("filename") or "").lower() == wanted_name
            and (not wanted_size or len(item.get("bytes") or b"") == wanted_size)
        ):
            return item
    return None


def _graph_folder_messages(client: GraphMailClient, folder_ref: str) -> list[dict[str, Any]]:
    folder_id = client._resolve_folder_reference(folder_ref)
    url = f"{client._user_root()}/mailFolders/{folder_id}/messages"
    params = {
        "$top": "100",
        "$select": "id,subject,receivedDateTime,from,internetMessageId,isRead,hasAttachments",
    }
    messages: list[dict[str, Any]] = []
    while url:
        response = client._request("GET", url, params=params)
        body = response.json()
        for item in body.get("value") or []:
            if not item.get("hasAttachments"):
                continue
            messages.append(item)
        url = body.get("@odata.nextLink") or ""
        params = None
    return messages


def _scan_graph_sales_orders(client: GraphMailClient, folders: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for folder_ref in folders:
        for message in _graph_folder_messages(client, folder_ref):
            message_id = str(message.get("id") or "")
            attachments = [item.as_upload() for item in client._list_attachments(message_id)]
            for attachment in attachments:
                if not is_sales_order_workbook_attachment(attachment):
                    continue
                key = (message_id, str(attachment.get("attachment_id") or attachment.get("filename") or ""))
                if key in seen:
                    continue
                seen.add(key)
                records.append(
                    {
                        "folder": folder_ref,
                        "message": {
                            "id": message_id,
                            "subject": message.get("subject") or "",
                            "received": message.get("receivedDateTime") or "",
                            "from": ((message.get("from") or {}).get("emailAddress") or {}).get("address", ""),
                            "message_id": message.get("internetMessageId") or "",
                        },
                        "attachment": attachment,
                    }
                )
    records.sort(key=lambda item: (item["message"].get("received") or "", item["attachment"].get("filename") or ""))
    return records


def _update_archive_paths(cur: Any, record: dict[str, Any], path: str, filename: str) -> None:
    cur.execute(
        """
        UPDATE [ING].[BKD_EmailAttachment]
        SET DownloadedName = ?,
            DownloadedPath = ?,
            Status = 'Saved',
            ErrorText = NULL,
            SourceFileId = COALESCE(SourceFileId, ?)
        WHERE EmailAttachmentId = ?
        """,
        [
            filename[:260],
            path[:500],
            record.get("FileId"),
            record["EmailAttachmentId"],
        ],
    )
    if record.get("FileId"):
        cur.execute(
            """
            UPDATE [ING].[BKD_SourceFileLog]
            SET ArchivedAt = COALESCE(ArchivedAt, SYSUTCDATETIME()),
                ArchivePath = ?
            WHERE FileId = ?
            """,
            [path[:500], record["FileId"]],
        )


def run_ing_backfill(*, client: GraphMailClient, tenant_code: str, limit: int | None, apply: bool, force: bool) -> int:
    del tenant_code
    conn = get_standalone_connection()
    cur = conn.cursor()
    try:
        records = _load_historical_attachments(cur, limit=limit, force=force)
        print(f"records={len(records)} apply={apply} archive_dir={_attachment_archive_dir()}")
        ok_count = 0
        failed_count = 0
        skipped_count = 0

        for record in records:
            label = f"{record['EmailAttachmentId']} {record.get('OriginalName') or ''} {record.get('ReceivedAt') or ''}"
            try:
                message = client.get_message_by_id(str(record.get("GraphMessageId") or ""))
                attachment = _find_attachment(message, record)
                if not attachment:
                    failed_count += 1
                    print(f"FAILED {label}: attachment not found in Graph message")
                    continue

                actual_sha = _sha256(attachment.get("bytes") or b"")
                expected_sha = str(record.get("Sha256") or "").lower()
                if expected_sha and actual_sha.lower() != expected_sha:
                    failed_count += 1
                    print(f"FAILED {label}: sha mismatch expected={expected_sha} actual={actual_sha}")
                    continue

                archive_result = _archive_attachment(message, attachment, apply=apply)
                if not archive_result.get("ok"):
                    failed_count += 1
                    print(f"FAILED {label}: {archive_result.get('error') or 'archive failed'}")
                    continue

                if apply:
                    _update_archive_paths(
                        cur,
                        record,
                        str(archive_result["path"]),
                        Path(str(archive_result["path"])).name,
                    )
                    conn.commit()
                ok_count += 1
                action = "WOULD_ARCHIVE" if not apply else ("ARCHIVED" if archive_result.get("created") else "ALREADY_ARCHIVED")
                print(f"{action} {label}: {archive_result['path']}")
            except Exception as exc:
                conn.rollback()
                failed_count += 1
                print(f"FAILED {label}: {exc}")

        if not apply:
            print("Dry-run only. Re-run with --apply to write files and update ING archive paths.")
        print(f"summary ok={ok_count} failed={failed_count} skipped={skipped_count}")
        return 1 if failed_count else 0
    finally:
        cur.close()
        conn.close()


def run_graph_scan(*, client: GraphMailClient, folders: list[str], limit: int | None, apply: bool) -> int:
    records = _scan_graph_sales_orders(client, folders)
    if limit:
        records = records[: int(limit)]
    print(f"graph_sales_order_records={len(records)} apply={apply} folders={','.join(folders)} archive_dir={_attachment_archive_dir()}")
    ok_count = 0
    failed_count = 0
    for record in records:
        message = record["message"]
        attachment = record["attachment"]
        label = f"{record['folder']} {message.get('received') or ''} {attachment.get('filename') or ''}"
        archive_result = _archive_attachment(message, attachment, apply=apply)
        if not archive_result.get("ok"):
            failed_count += 1
            print(f"FAILED {label}: {archive_result.get('error') or 'archive failed'}")
            continue
        ok_count += 1
        action = "WOULD_ARCHIVE" if not apply else ("ARCHIVED" if archive_result.get("created") else "ALREADY_ARCHIVED")
        print(f"{action} {label}: {archive_result['path']}")
    if not apply:
        print("Dry-run only. Re-run with --scan-graph --apply to write files.")
    print(f"summary ok={ok_count} failed={failed_count}")
    return 1 if failed_count else 0


def run(*, tenant_code: str, limit: int | None, apply: bool, force: bool, scan_graph: bool, folders: list[str]) -> int:
    settings = resolve_graph_mail_settings(tenant_code=tenant_code)
    client = GraphMailClient(settings)
    if not client.is_configured():
        missing = [
            name
            for name, value in (
                ("tenant_id", settings.tenant_id),
                ("client_id", settings.client_id),
                ("client_secret", settings.client_secret),
                ("mailbox", settings.mailbox),
            )
            if not value
        ]
        print(f"Graph settings incomplete: missing {', '.join(missing)}.")
        return 1

    if scan_graph:
        return run_graph_scan(client=client, folders=folders, limit=limit, apply=apply)
    return run_ing_backfill(client=client, tenant_code=tenant_code, limit=limit, apply=apply, force=force)


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive historical BKD Sales Orders attachments from Graph.")
    parser.add_argument("--tenant-code", default="BKD")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true", help="Write files and update ING archive paths.")
    parser.add_argument("--force", action="store_true", help="Include records that already have an archive/download path.")
    parser.add_argument("--scan-graph", action="store_true", help="Scan Graph folders directly instead of only ING-known attachments.")
    parser.add_argument(
        "--folder",
        action="append",
        dest="folders",
        default=None,
        help="Graph folder to scan with --scan-graph. Can be repeated. Defaults to Inbox.",
    )
    args = parser.parse_args()
    return run(
        tenant_code=args.tenant_code,
        limit=args.limit,
        apply=args.apply,
        force=args.force,
        scan_graph=args.scan_graph,
        folders=args.folders or ["Inbox"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
