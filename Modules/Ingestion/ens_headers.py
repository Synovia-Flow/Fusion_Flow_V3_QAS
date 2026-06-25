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
    *API_FIELDS, "ParseStatus", "SourceFile",
]

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
    return result


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
    """Append rows to the CSV, skipping DedupKeys already present. Returns (written, skipped)."""
    existing: set[str] = set()
    if out_path.exists():
        with out_path.open("r", encoding="utf-8-sig", newline="") as fh:
            existing = {r.get("DedupKey", "") for r in csv.DictReader(fh)}
    new_file = not out_path.exists()
    written = skipped = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if new_file:
            writer.writeheader()
        for row in rows:
            if row["DedupKey"] in existing:
                skipped += 1
                continue
            writer.writerow({c: row.get(c, "") for c in CSV_COLUMNS})
            existing.add(row["DedupKey"])
            written += 1
    return written, skipped


def main() -> int:
    p = argparse.ArgumentParser(description="Birkdale Process_ENS_Headers - ENS details block -> CSV.")
    p.add_argument("--eml", nargs="*", type=Path, default=[], help="One or more .eml files")
    p.add_argument("--eml-dir", type=Path, help="Folder of .eml files to parse")
    p.add_argument("--out", type=Path, default=Path("ENS_Headers.csv"), help="CSV output path (appended)")
    args = p.parse_args()

    files = list(args.eml)
    if args.eml_dir:
        files += sorted(args.eml_dir.glob("*.eml"))
    if not files:
        p.error("Provide --eml <file...> or --eml-dir <dir>")

    rows = [row_from_eml(f) for f in files]
    written, skipped = write_csv(rows, args.out)
    for r in rows:
        print(f"  {r['SourceFile']}: {r['ParseStatus']}  key={r['DedupKey']}  ICR={r['transport_document_number']}")
    print(f"\n{args.out}: {written} written, {skipped} duplicate(s) skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
