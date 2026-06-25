#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Birkdale raw loader (Load_BKD_Raw).

Loads the two Birkdale file types into their ING raw tables, then moves each
processed file into a 'Processed' sub-folder. No prompts - scheduler-friendly.

  ENS_Source\\ENS_Headers_*.csv          -> ING.BKD_Raw_ENS           (dedup on DedupKey)
  Sales_Order_files\\*.xlsx               -> ING.BKD_Raw_Sales_Orders  (verbatim row JSON)

Folders come from CFG.Folder_Paths (BKD: ENS_SOURCE, INBOUND); connection from
Configuration/Fusion_Flow_QAS.ini. Every step logs to EXC.Execution / LOG.

Usage:
  python load_raw.py                 # load both, move processed files
  python load_raw.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ingest import IngestionDb, load_db_config, DEFAULT_INI
import xlsx_reader

CLIENT_CODE = "BKD"
PROCESS = "Load_BKD_Raw"

ENS_COLUMNS = [
    "DedupKey", "DetailsDate", "SourceReceivedUtc", "SourceSender", "SourceSubject",
    "OriginalFrom", "OriginalSent", "movement_type", "type_of_passive_transport",
    "identity_no_of_transport", "nationality_of_transport", "carrier_eori",
    "transport_document_number", "arrival_date_time", "arrival_port", "place_of_loading",
    "place_of_acceptance_same_as_loading", "place_of_unloading",
    "place_of_delivery_same_as_unloading", "transport_charges", "ParseStatus", "SourceFile",
]


def _file_date(name: str):
    m = re.match(r"(\d{8})", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _move_processed(path: Path) -> Path:
    dest_dir = path.parent / "Processed"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        dest = dest_dir / f"{path.stem}_{datetime.now(timezone.utc):%Y%m%d%H%M%S}{path.suffix}"
    shutil.move(str(path), str(dest))
    return dest


# --------------------------------------------------------------------------- #
# ENS CSV -> ING.BKD_Raw_ENS
# --------------------------------------------------------------------------- #
def load_ens(db: IngestionDb, ens_dir: Path, dry_run: bool) -> dict[str, int]:
    stats = {"files": 0, "rows": 0, "skipped": 0}
    files = sorted(ens_dir.glob("ENS_Headers_*.csv"))
    for csv_path in files:
        stats["files"] += 1
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if dry_run:
            db.log("LOAD_ENS", f"[dry-run] {csv_path.name}: {len(rows)} row(s)")
            continue
        cur = db.conn.cursor()
        for r in rows:
            key = (r.get("DedupKey") or "").strip()
            if not key:
                continue
            cur.execute("SELECT 1 FROM ING.BKD_Raw_ENS WHERE DedupKey = ?", key)
            if cur.fetchone():
                stats["skipped"] += 1
                continue
            received = (r.get("SourceReceivedUtc") or "").strip() or None
            cur.execute(
                "INSERT INTO ING.BKD_Raw_ENS (ExecutionID, TransactionID, " + ", ".join(ENS_COLUMNS) + ", SourceCsv) "
                "VALUES (?, ?, " + ", ".join("?" for _ in ENS_COLUMNS) + ", ?)",
                db.execution_id, db.transaction_id,
                *[(received if c == "SourceReceivedUtc" else (r.get(c) or None)) for c in ENS_COLUMNS],
                csv_path.name)
            stats["rows"] += 1
        db.conn.commit()
        db.log("LOAD_ENS", f"{csv_path.name}: loaded; moving to Processed.")
        _move_processed(csv_path)
    return stats


# --------------------------------------------------------------------------- #
# Sales Order xlsx -> ING.BKD_Raw_Sales_Orders
# --------------------------------------------------------------------------- #
def load_sales_orders(db: IngestionDb, so_dir: Path, dry_run: bool) -> dict[str, int]:
    stats = {"files": 0, "rows": 0}
    files = sorted(p for p in so_dir.glob("*.xlsx") if not p.name.startswith("~$"))
    for xlsx_path in files:
        stats["files"] += 1
        headers, rows = xlsx_reader.read_xlsx_rows(xlsx_path.read_bytes())
        if dry_run:
            db.log("LOAD_SO", f"[dry-run] {xlsx_path.name}: {len(rows)} row(s), {len(headers)} cols")
            continue
        file_date = _file_date(xlsx_path.name)
        cur = db.conn.cursor()
        # Idempotent re-load: clear any prior rows for this file.
        cur.execute("DELETE FROM ING.BKD_Raw_Sales_Orders WHERE SourceFile = ?", xlsx_path.name)
        for i, row in enumerate(rows, 1):
            payload = json.dumps(row, ensure_ascii=False, default=str)
            row_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            cur.execute(
                "INSERT INTO ING.BKD_Raw_Sales_Orders (ExecutionID, TransactionID, SourceFile, FileDate, "
                "SheetName, RowNumber, RowHash, PayloadJson) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                db.execution_id, db.transaction_id, xlsx_path.name, file_date, None, i, row_hash, payload)
            stats["rows"] += 1
        db.conn.commit()
        db.log("LOAD_SO", f"{xlsx_path.name}: {len(rows)} row(s) loaded (FileDate={file_date}); moving to Processed.")
        _move_processed(xlsx_path)
    return stats


def run(ini_path: Path, dry_run: bool, ens_only: bool = False, sales_only: bool = False) -> int:
    db = IngestionDb.connect(load_db_config(ini_path), dry_run=dry_run)
    try:
        if not db.fetch_client(CLIENT_CODE):
            print(f"[ERROR] Unknown client {CLIENT_CODE}"); return 2
        db.open_execution(CLIENT_CODE, "INGESTING", PROCESS)
        db.log("START", f"{PROCESS} (Transaction_ID={db.transaction_id})", detail={"dry_run": dry_run})

        paths = db.fetch_folder_paths(CLIENT_CODE)
        ens_dir = Path(paths.get("ENS_SOURCE", "."))
        so_dir = Path(paths.get("INBOUND", "."))

        ens = {"files": 0, "rows": 0, "skipped": 0}
        so = {"files": 0, "rows": 0}
        if not sales_only:
            ens = load_ens(db, ens_dir, dry_run)
        if not ens_only:
            so = load_sales_orders(db, so_dir, dry_run)

        total = ens["rows"] + so["rows"]
        db.finish_execution("INGESTED", total, total, 0)
        db.log("FINISH", f"ENS={ens} SalesOrders={so}", "OK")
        print(f"{PROCESS}: ENS {ens}; Sales Orders {so}")
        return 0
    except Exception as error:  # noqa: BLE001
        db.log_error("LOAD", str(error), type(error).__name__)
        db.finish_execution("ERROR", 0, 0, 1, str(error))
        raise
    finally:
        db.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Birkdale raw loader (Load_BKD_Raw).")
    p.add_argument("--ini", type=Path, default=DEFAULT_INI)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--ens-only", action="store_true")
    p.add_argument("--sales-only", action="store_true")
    args = p.parse_args()
    return run(args.ini, args.dry_run, args.ens_only, args.sales_only)


if __name__ == "__main__":
    raise SystemExit(main())
