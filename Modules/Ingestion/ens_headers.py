#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Birkdale Process_ENS_Headers.

Birkdale forward a daily "Tss Details" / "Details for <date>" email whose body
carries the ENS header fields for the Primeline Express NI movement, as a
label/value block:

    DETAILS FOR 25.06.26
    Type of Movement
    RoRo Accompanied ICS2
    Identity number of transport
    IMO9244116#MV72WNH
    ...

This module extracts that block into a normalised CSV row (one row per ENS /
per day), keyed by DetailsDate + ICR number so the daily re-forwards can be
de-duplicated when ingested into the database later.

Modes:
  --eml <file...> / --eml-dir <dir>   parse local .eml files (no Graph needed)
  (Graph mailbox mode reuses graph_email helpers - see run_from_graph().)

CSV is appended idempotently: rows whose DedupKey already exists are skipped.
"""

from __future__ import annotations

import argparse
import csv
import email
import hashlib
import html
import re
from datetime import datetime, timezone
from email import policy
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
FALLBACK_ENS_DIR = r"\\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Integration_Layer\BKD\Inbound\ENS_Source"
PROCESS = "Process_ENS_Headers"

# Normalised label -> ENS API field (order defines CSV column order).
ENS_LABELS: list[tuple[str, str]] = [
    ("type of movement", "movement_type"),
    ("type of passive transport", "type_of_passive_transport"),
    ("identity number of transport", "identity_no_of_transport"),
    ("nationality of means of transport", "nationality_of_transport"),
    ("carrier eori", "carrier_eori"),
    ("transport document number icr number", "transport_document_number"),
    ("arrival date time", "arrival_date_time"),
    ("port of arrival", "arrival_port"),
    ("place s of loading", "place_of_loading"),
    ("is are the place s of acceptance same as place s of loading", "place_of_acceptance_same_as_loading"),
    ("place s of unloading", "place_of_unloading"),
    ("is are the place s of delivery same as place s of unloading", "place_of_delivery_same_as_unloading"),
    ("transport charges", "transport_charges"),
]
LABEL_TO_FIELD = dict(ENS_LABELS)
API_FIELDS = [field for _, field in ENS_LABELS]

CSV_COLUMNS = [
    "DedupKey", "DetailsDate", "SourceReceivedUtc", "SourceSender", "SourceSubject",
    "OriginalFrom", "OriginalSent",
    *API_FIELDS, "ParseStatus", "SourceFile",
]

# Subjects that identify a TSS-Details / ENS email (used to pre-filter the mailbox).
ENS_SUBJECT_HINTS = ("tss details", "details for")

STOP_WORDS = {"from", "sent", "to", "subject", "cc", "bcc"}


def is_stop_line(norm: str) -> bool:
    """True for the forwarded-header boundary (From:/Sent:/To:/Subject:/...) or the
    'Please email completed...' footer. Matches whole first words, so a value such
    as 'Tomorrow's Date' is NOT treated as a 'To:' line."""
    if not norm:
        return False
    if norm.startswith("please email completed"):
        return True
    return norm.split(" ", 1)[0] in STOP_WORDS


def normalize(text: str) -> str:
    text = html.unescape(text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def flatten_body(msg: email.message.Message) -> str:
    body = msg.get_body(preferencelist=("plain", "html"))
    if not body:
        return ""
    text = body.get_content()
    if body.get_content_type() == "text/html":
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def parse_details(body_text: str) -> dict[str, Any]:
    """Parse the FIRST (top / most-recent) DETAILS FOR block into ENS fields."""
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    start = next((i for i, ln in enumerate(lines) if normalize(ln).startswith("details for")), None)
    result: dict[str, Any] = {f: "" for f in API_FIELDS}
    result["DetailsDate"] = ""
    if start is None:
        result["ParseStatus"] = "no_details_block"
        return result

    # DetailsDate from "DETAILS FOR 25.06.26"
    m = re.search(r"details for\s+(.+)", lines[start], flags=re.I)
    result["DetailsDate"] = (m.group(1).strip() if m else "")

    window = lines[start + 1:]
    for i, line in enumerate(window):
        key = normalize(line)
        if is_stop_line(key):
            break
        field = LABEL_TO_FIELD.get(key)
        if not field:
            continue
        # value = next line that is not a parenthetical note, a known label, or a stop marker
        for cand in window[i + 1:i + 5]:
            cl = cand.strip()
            if cl.startswith("(") or normalize(cl) in LABEL_TO_FIELD:
                continue
            if is_stop_line(normalize(cl)):
                break
            result[field] = cl
            break

    found = sum(1 for f in API_FIELDS if result[f])
    result["ParseStatus"] = "ok" if found >= 8 else ("partial" if found else "empty")
    of, os_ = extract_forwarded(body_text)
    result["OriginalFrom"] = of
    result["OriginalSent"] = os_
    return result


def extract_forwarded(body_text: str) -> tuple[str, str]:
    """Pull the original Primeline sender + sent date from the forwarded header
    block (From:/Sent:) - additional identifiers carried in the email body."""
    orig_from = orig_sent = ""
    for line in body_text.splitlines():
        norm = normalize(line)
        if not orig_from and norm.startswith("from "):
            m = re.search(r"<([^>]+@[^>]+)>", line) or re.search(r"([\w.\-]+@[\w.\-]+)", line)
            orig_from = (m.group(1) if m else "").strip().lower()
        elif not orig_sent and norm.startswith("sent "):
            orig_sent = re.sub(r"(?i)^\s*sent\s*:\s*", "", line).strip()
        if orig_from and orig_sent:
            break
    return orig_from, orig_sent


def row_from_eml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    parsed = parse_details(flatten_body(msg))
    sender = str(msg.get("From") or "")
    addr = re.search(r"<([^>]+)>", sender)
    parsed["SourceSender"] = (addr.group(1) if addr else sender).strip().lower()
    parsed["SourceSubject"] = str(msg.get("Subject") or "")
    parsed["SourceReceivedUtc"] = _hdr_date(msg.get("Date"))
    parsed["SourceFile"] = path.name
    parsed["DedupKey"] = dedup_key(parsed)
    return parsed


def dedup_key(row: dict[str, Any]) -> str:
    icr = (row.get("transport_document_number") or "").strip()
    base = f"{row.get('DetailsDate','').strip()}|{icr}"
    if icr or row.get("DetailsDate"):
        return base
    # fall back to a content hash when neither date nor ICR parsed
    return "hash|" + hashlib.sha256("".join(str(row.get(f, "")) for f in API_FIELDS).encode()).hexdigest()[:16]


def _hdr_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


def write_csv(rows: list[dict[str, Any]], out_path: Path) -> tuple[int, int]:
    """Write a FRESH CSV (one per run). Duplicate DedupKeys WITHIN this batch are
    collapsed; cross-day de-duplication happens at DB ingest time. Returns
    (written, skipped_in_batch)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    written = skipped = 0
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            if row["DedupKey"] in seen:
                skipped += 1
                continue
            writer.writerow({c: row.get(c, "") for c in CSV_COLUMNS})
            seen.add(row["DedupKey"])
            written += 1
    return written, skipped


# --------------------------------------------------------------------------- #
# Graph mailbox mode - look for the TSS-Details emails directly in the mailbox
# --------------------------------------------------------------------------- #
def flatten_graph_body(body: Any) -> str:
    if isinstance(body, dict):
        content = body.get("content") or ""
        ct = (body.get("contentType") or "").lower()
    else:
        content, ct = str(body or ""), ""
    if ct == "html" or ("<" in content and ">" in content):
        content = re.sub(r"(?i)<br\s*/?>", "\n", content)
        content = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", content)
        content = re.sub(r"<[^>]+>", " ", content)
        content = html.unescape(content)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in content.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _graph_dt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


def row_from_graph(msg: dict[str, Any], sender: str) -> dict[str, Any]:
    parsed = parse_details(flatten_graph_body(msg.get("body")))
    parsed["SourceSender"] = sender
    parsed["SourceSubject"] = str(msg.get("subject") or "")
    parsed["SourceReceivedUtc"] = _graph_dt(msg.get("receivedDateTime"))
    parsed["SourceFile"] = str(msg.get("internetMessageId") or msg.get("id") or "")
    parsed["DedupKey"] = dedup_key(parsed)
    return parsed


def run_from_graph(client_code: str, ini_path: Path, out_dir_override: str | None, dry_run: bool) -> int:
    """Scan the mailbox for Birkdale TSS-Details emails, parse the ENS block, and
    write a timestamped CSV to the client ENS_Source folder. Logs to EXC/LOG."""
    from ingest import IngestionDb, load_db_config
    import graph_email as G

    db = IngestionDb.connect(load_db_config(ini_path), dry_run=dry_run)
    rows: list[dict[str, Any]] = []
    try:
        if not db.fetch_client(client_code):
            print(f"[ERROR] Unknown client {client_code}"); return 2
        db.open_execution(client_code, "INGESTING", PROCESS)
        db.log("START", f"{PROCESS} run (Transaction_ID={db.transaction_id})", detail={"dry_run": dry_run})

        params = db.fetch_parameters()
        paths = db.fetch_folder_paths(client_code)
        rules = db.fetch_email_rules(client_code)
        domain = next((r["SenderRule"] for r in rules if r.get("SenderRuleType") == "DOMAIN" and r.get("SenderRule")),
                      "birkdalesales.com").lower().lstrip("@")
        out_dir = Path(out_dir_override or paths.get("ENS_SOURCE") or FALLBACK_ENS_DIR)

        mailbox = params.get("GRAPH_MAILBOX") or ""
        tenant = params.get("GRAPH_TENANT_ID") or ""
        client_id = params.get("GRAPH_CLIENT_ID") or ""
        if not (mailbox and tenant and client_id) or tenant.startswith("<"):
            db.log("EMAIL", "Graph config incomplete (set GRAPH_TENANT_ID/CLIENT_ID/MAILBOX).", "WARN")
            db.finish_execution("ERROR", 0, 0, 1, "Graph config incomplete")
            return 1
        if dry_run:
            db.log("EMAIL", f"[dry-run] would scan {mailbox} for @{domain} TSS-Details mail -> {out_dir}")
            db.finish_execution("INGESTED", 0, 0, 0)
            return 0

        token = G.acquire_token(params.get("GRAPH_AUTHORITY", "https://login.microsoftonline.com/"),
                                tenant, client_id, G.resolve_client_secret(params),
                                params.get("GRAPH_SCOPE", "https://graph.microsoft.com/.default"))
        client = G.GraphClient(token)
        inbox_id = G.resolve_inbox_id(client, mailbox)
        # Scan the inbox AND all sub-folders (incl. Fusion_Processed/<client> where the
        # downloader may have moved the mail) - reading is harmless.
        folders = [{"id": inbox_id}] + G.scan_folders(client, mailbox, inbox_id, skip_names=set())
        for folder in folders:
            # LIGHT listing first (no body) - avoids pulling every message body and
            # hanging on a real mailbox. Body is fetched only for matched candidates.
            msgs = client.get_all(
                f"/users/{mailbox}/mailFolders/{folder['id']}/messages",
                {"$select": "id,subject,from,receivedDateTime,internetMessageId", "$top": 50})
            for msg in msgs:
                sender = (msg.get("from", {}).get("emailAddress", {}) or {}).get("address", "").lower()
                subject = (msg.get("subject") or "").lower()
                if not sender.endswith("@" + domain):
                    continue
                if not any(hint in subject for hint in ENS_SUBJECT_HINTS):
                    continue
                full = client.get(f"/users/{mailbox}/messages/{msg['id']}", {"$select": "body"})
                msg["body"] = full.get("body")
                row = row_from_graph(msg, sender)
                if row["ParseStatus"] == "no_details_block":
                    continue
                rows.append(row)
                db.log("ENS", f"{row['DedupKey']} ({row['ParseStatus']}) from {sender}")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_path = out_dir / f"ENS_Headers_{stamp}.csv"
        written, skipped = write_csv(rows, out_path)
        db.finish_execution("INGESTED", len(rows), written, 0)
        db.log("FINISH", f"Wrote {written} ENS row(s) ({skipped} in-batch dup) to {out_path}", "OK")
        print(f"{PROCESS}: {written} ENS row(s) written to {out_path} ({skipped} in-batch duplicate(s)).")
        return 0
    finally:
        db.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Birkdale Process_ENS_Headers - ENS details block -> CSV.")
    p.add_argument("--graph", action="store_true", help="Look for the TSS-Details emails in the mailbox (Graph)")
    p.add_argument("--client", default="BKD", help="Client code (default BKD)")
    p.add_argument("--ini", type=Path, default=DEFAULT_INI, help="Path to Fusion_Flow_QAS.ini")
    p.add_argument("--eml", nargs="*", type=Path, default=[], help="Offline: one or more .eml files")
    p.add_argument("--eml-dir", type=Path, help="Offline: folder of .eml files to parse")
    p.add_argument("--out-dir", help="Folder for the timestamped CSV (default: client ENS_Source from CFG)")
    p.add_argument("--out", type=Path, help="Explicit CSV path (overrides --out-dir + timestamp)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    # Graph mailbox mode (default operational path).
    if args.graph:
        return run_from_graph(args.client.strip().upper(), args.ini, args.out_dir, args.dry_run)

    # Offline .eml mode.
    files = list(args.eml)
    if args.eml_dir:
        files += sorted(args.eml_dir.glob("*.eml"))
    if not files:
        p.error("Provide --graph (mailbox) or --eml/--eml-dir (offline files)")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = args.out or (Path(args.out_dir or ".") / f"ENS_Headers_{stamp}.csv")
    rows = [row_from_eml(f) for f in files]
    written, skipped = write_csv(rows, out_path)
    for r in rows:
        print(f"  {r['SourceFile']}: {r['ParseStatus']}  key={r['DedupKey']}  ICR={r['transport_document_number']}")
    print(f"\n{out_path}: {written} written, {skipped} in-batch duplicate(s) skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
