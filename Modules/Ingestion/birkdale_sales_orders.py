#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Birkdale_Sales_Orders downloader (Script 1).

Identifies inbox emails from the Birkdale sender domain (@birkdalesales.com) and
downloads every relevant attachment (xlsx/xls/csv/pdf/doc/docx/txt - images
excluded). Each saved file is prefixed with the email date/time:

    <YYYYMMDD>_<HHMMSS>_<original filename>
    e.g. 20260507_134024_Sales_Orders_Synovia_1.xlsx

Files are written to the Birkdale INBOUND folder (CFG.Folder_Paths BKD INBOUND)
and landed verbatim into ING (Inbound_File) for dedup; every step is logged to
EXC.Execution / LOG. Config comes from CFG.Application_Parameters (GRAPH_*) and
the connection from Configuration/Fusion_Flow_QAS.ini.

Usage:
  python birkdale_sales_orders.py --dry-run
  python birkdale_sales_orders.py
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from ingest import IngestionDb, load_db_config, DEFAULT_INI
import graph_email as G

CLIENT_CODE = "BKD"
PROCESS = "Birkdale_Sales_Orders"
DEFAULT_SENDER_DOMAIN = "birkdalesales.com"
RELEVANT_EXTS = {".xlsx", ".xls", ".csv", ".pdf", ".doc", ".docx", ".txt"}


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "attachment").strip()
    return re.sub(r"\s+", " ", name)[:180] or "attachment"


def prefixed_name(received_iso: str | None, original: str) -> str:
    dt = G._parse_dt(received_iso)
    stamp = dt.strftime("%Y%m%d_%H%M%S") if dt else "00000000_000000"
    return f"{stamp}_{safe_name(original)}"


def unique_path(folder: Path, filename: str) -> Path:
    path = folder / filename
    counter = 2
    while path.exists():
        path = folder / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
        counter += 1
    return path


def run(ini_path: Path, dry_run: bool) -> int:
    db_cfg = load_db_config(ini_path)
    db = IngestionDb.connect(db_cfg, dry_run=dry_run)
    stats = {"emails": 0, "files": 0, "skipped": 0, "errors": 0}
    try:
        if not db.fetch_client(CLIENT_CODE):
            print(f"[ERROR] Unknown client {CLIENT_CODE}"); return 2
        db.open_execution(CLIENT_CODE, "INGESTING", PROCESS)
        db.log("START", f"{PROCESS} run (Transaction_ID={db.transaction_id})", detail={"dry_run": dry_run})

        params = db.fetch_parameters()
        paths = db.fetch_folder_paths(CLIENT_CODE)
        inbound = Path(paths.get("INBOUND", ".")) if not dry_run else None

        # Sender domain comes from CFG.Email_Rules (BKD) if present, else the default.
        rules = db.fetch_email_rules(CLIENT_CODE)
        domain = next((r["SenderRule"] for r in rules if r.get("SenderRuleType") == "DOMAIN" and r.get("SenderRule")),
                      DEFAULT_SENDER_DOMAIN).lower().lstrip("@")

        mailbox = params.get("GRAPH_MAILBOX") or ""
        tenant = params.get("GRAPH_TENANT_ID") or ""
        client_id = params.get("GRAPH_CLIENT_ID") or ""
        if not (mailbox and client_id and tenant) or tenant.startswith("<"):
            db.log("EMAIL", "Graph config incomplete (set GRAPH_TENANT_ID/CLIENT_ID/MAILBOX).", "WARN")
            db.finish_execution("ERROR", 0, 0, 1, "Graph config incomplete")
            return 1

        if dry_run:
            db.log("EMAIL", f"[dry-run] would scan {mailbox} for senders @{domain} and download {sorted(RELEVANT_EXTS)}.")
            db.finish_execution("INGESTED", 0, 0, 0)
            return 0

        token = G.acquire_token(params.get("GRAPH_AUTHORITY", "https://login.microsoftonline.com/"),
                                tenant, client_id, G.resolve_client_secret(params.get("GRAPH_CLIENT_SECRET_REF", "")),
                                params.get("GRAPH_SCOPE", "https://graph.microsoft.com/.default"))
        client = G.GraphClient(token)
        inbox_id = G.resolve_inbox_id(client, mailbox)
        processed_id = G.ensure_processed_folder(client, mailbox, inbox_id,
                                                 params.get("GRAPH_PROCESSED_FOLDER", "Fusion_Processed"))
        folders = [{"id": inbox_id}] + G.scan_folders(client, mailbox, inbox_id, processed_id)
        inbound.mkdir(parents=True, exist_ok=True)

        for folder in folders:
            msgs = client.get_all(
                f"/users/{mailbox}/mailFolders/{folder['id']}/messages",
                {"$select": "id,subject,from,receivedDateTime,hasAttachments", "$top": 50})
            for msg in msgs:
                sender = (msg.get("from", {}).get("emailAddress", {}) or {}).get("address", "").lower()
                if not sender.endswith("@" + domain):
                    continue
                stats["emails"] += 1
                try:
                    atts = client.get_all(f"/users/{mailbox}/messages/{msg['id']}/attachments",
                                          {"$select": "id,name,contentType,size,isInline,@odata.type"})
                    for att in atts:
                        if att.get("@odata.type") != "#microsoft.graph.fileAttachment" or att.get("isInline"):
                            continue
                        name = att.get("name") or "attachment"
                        if G._ext(name) not in RELEVANT_EXTS:
                            stats["skipped"] += 1
                            continue
                        content = client.get_bytes(
                            f"/users/{mailbox}/messages/{msg['id']}/attachments/{att['id']}/$value")
                        saved = prefixed_name(msg.get("receivedDateTime"), name)
                        dest = unique_path(inbound, saved)
                        dest.write_bytes(content)
                        file_id = db.land_inbound_file(CLIENT_CODE, "EMAIL", saved, content,
                                                       source_path=str(dest), sender=sender, mailbox=mailbox,
                                                       content_type=att.get("contentType", ""))
                        stats["files"] += 1
                        db.log("DOWNLOAD", f"Saved {dest.name} (file_id={file_id})")
                    client.post(f"/users/{mailbox}/messages/{msg['id']}/move", {"destinationId": processed_id})
                except Exception as error:  # noqa: BLE001
                    stats["errors"] += 1
                    db.log_error("DOWNLOAD", f"Message {str(msg.get('id'))[:12]}: {error}", "DownloadError")

        status = "INGESTED" if stats["errors"] == 0 else "ERROR"
        db.finish_execution(status, stats["emails"], stats["files"], stats["errors"])
        db.log("FINISH", f"{PROCESS} done: {stats}", "OK" if status == "INGESTED" else "ERROR")
        print(f"{PROCESS} summary: {stats}")
        return 0 if stats["errors"] == 0 else 1
    finally:
        db.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Birkdale_Sales_Orders downloader (Script 1).")
    p.add_argument("--ini", type=Path, default=DEFAULT_INI)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return run(args.ini, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
