#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Birkdale raw loader (Load_BKD_Raw).

Loads the two Birkdale file types into their ING raw tables, then moves each
processed file into a 'Processed' sub-folder. No prompts - scheduler-friendly.

  ENS_Source\\ENS_Headers_*.csv          -> ING.BKD_Raw_ENS           (dedup on DedupKey)
  Sales_Order_files\\*.xlsx               -> ING.BKD_Raw_Sales_Orders  (verbatim row JSON)

Folders come from CFG.Folder_Paths (BKD: ENS_SOURCE, INBOUND, FAIL); connection
from Configuration/Fusion_Flow_QAS.ini. Every step logs to EXC.Execution / LOG.

Resilience: each file is loaded independently - a bad or locked file is logged,
rolled back and quarantined to the FAIL folder instead of aborting the run
(transient locks are retried first). Files in INBOUND that the loader does not
understand (not *.xlsx) are swept into a 'Skipped' sub-folder so they cannot
accumulate invisibly.

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
import time
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


def _move_to(path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        dest = dest_dir / f"{path.stem}_{datetime.now(timezone.utc):%Y%m%d%H%M%S}{path.suffix}"
    shutil.move(str(path), str(dest))
    return dest


def _move_processed(path: Path) -> Path:
    return _move_to(path, path.parent / "Processed")


def _retry_io(action, attempts: int = 3, delay: float = 2.0):
    """Run a file operation, retrying briefly on sharing violations (a file open
    in Excel / being scanned) so a transient lock doesn't fail the file."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except (PermissionError, OSError):
            if attempt == attempts:
                raise
            time.sleep(delay)


def _quarantine(db: IngestionDb, step: str, path: Path, fail_dir: str | None, error: Exception) -> None:
    """One bad file must not dam the pipeline: roll back its partial work, log the
    error, and move it to the FAIL folder. If even the move fails (still locked),
    leave it in place - it is retried on the next run."""
    try:
        db.conn.rollback()
    except Exception:  # noqa: BLE001
        pass
    db.log_error(step, f"{path.name}: {error}", type(error).__name__)
    try:
        dest = _move_to(path, Path(fail_dir) if fail_dir else path.parent / "Failed")
        db.log(step, f"{path.name}: quarantined to {dest.parent}", "WARN")
    except Exception as move_error:  # noqa: BLE001
        db.log(step, f"{path.name}: could not quarantine ({move_error}); left in place.", "WARN")


# --------------------------------------------------------------------------- #
# ENS CSV -> ING.BKD_Raw_ENS
# --------------------------------------------------------------------------- #
def _read_csv_rows(csv_path: Path) -> list[dict[str, Any]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_ens(db: IngestionDb, ens_dir: Path, fail_dir: str | None, dry_run: bool) -> dict[str, int]:
    stats = {"files": 0, "rows": 0, "skipped": 0, "failed": 0}
    files = sorted(ens_dir.glob("ENS_Headers_*.csv"))
    for csv_path in files:
        stats["files"] += 1
        try:
            rows = _retry_io(lambda: _read_csv_rows(csv_path))
            if dry_run:
                db.log("LOAD_ENS", f"[dry-run] {csv_path.name}: {len(rows)} row(s)")
                continue
            cur = db.conn.cursor()
            loaded = 0
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
                loaded += 1
            db.conn.commit()
            stats["rows"] += loaded
            db.log("LOAD_ENS", f"{csv_path.name}: {loaded} row(s) loaded; moving to Processed.")
            _retry_io(lambda: _move_processed(csv_path))
        except Exception as error:  # noqa: BLE001 - isolate the bad file, keep going
            stats["failed"] += 1
            _quarantine(db, "LOAD_ENS", csv_path, fail_dir, error)
    return stats


# --------------------------------------------------------------------------- #
# Sales Order xlsx -> ING.BKD_Raw_Sales_Orders
# --------------------------------------------------------------------------- #
def load_sales_orders(db: IngestionDb, so_dir: Path, fail_dir: str | None, dry_run: bool) -> dict[str, int]:
    stats = {"files": 0, "rows": 0, "failed": 0}
    files = sorted(p for p in so_dir.glob("*.xlsx") if not p.name.startswith("~$"))
    for xlsx_path in files:
        stats["files"] += 1
        try:
            headers, rows = xlsx_reader.read_xlsx_rows(_retry_io(xlsx_path.read_bytes))
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
            db.conn.commit()
            stats["rows"] += len(rows)
            db.log("LOAD_SO", f"{xlsx_path.name}: {len(rows)} row(s) loaded (FileDate={file_date}); moving to Processed.")
            _retry_io(lambda: _move_processed(xlsx_path))
        except Exception as error:  # noqa: BLE001 - isolate the bad file, keep going
            stats["failed"] += 1
            _quarantine(db, "LOAD_SO", xlsx_path, fail_dir, error)
    return stats


def sweep_inbound(db: IngestionDb, so_dir: Path, dry_run: bool) -> int:
    """Move files the loader does not understand (anything but *.xlsx) out of
    INBOUND into a 'Skipped' sub-folder, so unsupported attachments (csv/pdf/doc)
    downloaded by the acquire step do not accumulate invisibly."""
    moved = 0
    if not so_dir.is_dir():
        return moved
    for path in sorted(so_dir.iterdir()):
        if path.is_dir() or path.name.startswith("~$") or path.suffix.lower() == ".xlsx":
            continue
        if dry_run:
            db.log("SWEEP", f"[dry-run] would move {path.name} -> Skipped/", "WARN")
            continue
        try:
            _retry_io(lambda: _move_to(path, so_dir / "Skipped"))
            db.log("SWEEP", f"{path.name}: unsupported type (loader reads *.xlsx only); moved to Skipped/", "WARN")
            moved += 1
        except Exception as error:  # noqa: BLE001 - locked/odd file: leave for next run
            db.log("SWEEP", f"{path.name}: could not move to Skipped/ ({error}); left in place.", "WARN")
    return moved


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
        fail_dir = paths.get("FAIL")

        ens = {"files": 0, "rows": 0, "skipped": 0, "failed": 0}
        so = {"files": 0, "rows": 0, "failed": 0}
        swept = 0
        if not sales_only:
            ens = load_ens(db, ens_dir, fail_dir, dry_run)
        if not ens_only:
            so = load_sales_orders(db, so_dir, fail_dir, dry_run)
            swept = sweep_inbound(db, so_dir, dry_run)

        total = ens["rows"] + so["rows"]
        failed = ens["failed"] + so["failed"]
        db.finish_execution("INGESTED" if failed == 0 else "ERROR", total, total, failed)
        db.log("FINISH", f"ENS={ens} SalesOrders={so} swept={swept}", "OK" if failed == 0 else "ERROR")
        print(f"{PROCESS}: ENS {ens}; Sales Orders {so}; swept {swept}")
        return 0 if failed == 0 else 1
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
