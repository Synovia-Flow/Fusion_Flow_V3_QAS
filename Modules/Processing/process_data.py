#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Module 2: Data Processing (PRS) runner.

Transforms verbatim Module 1 rows (ING.BKD_Raw_ENS typed + ING.BKD_Raw_Sales_Orders
JSON) into validated canonical TSS-shaped objects in the PRS schema. Four ordered
stages run per logical movement:

    NORMALISE  -> NORMALISED   (EXC process NORMALISING)  - DP-FR-01
    ENRICH     -> ENRICHED     (EXC process ENRICHING)    - DP-FR-02/08
    CONSTRUCT  -> CONSTRUCTED  (EXC process CONSTRUCTING)  - DP-FR-03
    VALIDATE   -> VALIDATED |
                  REJECTED     (EXC process VALIDATING)    - DP-FR-04

Every field set/changed/cleared is logged to EXC.Data_Processing_Enhancement with
the rule label (DP-FR-06). Stage transitions write EXC.Transaction rows and advance
EXC.Execution.Status (DP-FR-07).

This mirrors the Ingestion DB-adapter pattern (IngestionDb): the DB connection is
read from Configuration/Fusion_Flow_QAS.ini [database]; ALL run behaviour is read
from CFG.Application_Parameters. No hardcoded secrets or connection strings.

No CLI (design decision): the scheduler simply runs `python process_data.py`.
Run behaviour is controlled entirely from CFG.Application_Parameters:
  PROCESSING_CLIENT            client code to process        (script default: BKD)
  PROCESSING_TRANSACTION_MODE  'latest' or an ExecutionID    (script default: latest)
  PROCESSING_DRY_RUN           1/true to process+report only (script default: 0)
The script-level constants below are the fallbacks used only when a parameter row
is absent.

Usage:
  python process_data.py        # behaviour from CFG.Application_Parameters
"""

from __future__ import annotations

import configparser
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows subprocess launchers: ASCII-safe stdout (Critical Rule 21).
os.environ.setdefault("NO_COLOR", "1")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"

MODULE_NAME = "DATA_PROCESSING"

# Run-control parameters live in CFG.Application_Parameters (no CLI). The keys
# below are read at run time; the script-level defaults are the fallbacks used
# only when a parameter row is missing.
PARAM_CLIENT = "PROCESSING_CLIENT"
PARAM_TRANSACTION_MODE = "PROCESSING_TRANSACTION_MODE"
PARAM_DRY_RUN = "PROCESSING_DRY_RUN"
DEFAULT_CLIENT = "BKD"
DEFAULT_TRANSACTION_MODE = "latest"
DEFAULT_DRY_RUN = False

# --------------------------------------------------------------------------- #
# Resilient sibling import (mirrors the Ingestion modules). mapping.py is being
# written in parallel; code STRICTLY to the locked import contract below.
# --------------------------------------------------------------------------- #
try:  # package context
    from . import mapping  # type: ignore
except Exception:  # pragma: no cover - script context fallback
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mapping  # type: ignore

# Locked symbols from mapping.py (do NOT redefine - import only).
ENS_CSV_TO_HEADER = mapping.ENS_CSV_TO_HEADER
SALES_ORDER_TO_GOODS = mapping.SALES_ORDER_TO_GOODS
SALES_ORDER_TO_CONSIGNMENT = mapping.SALES_ORDER_TO_CONSIGNMENT
BKD_QAS_CONSTANTS = mapping.BKD_QAS_CONSTANTS
QAS_RULE_CITATIONS = mapping.QAS_RULE_CITATIONS
MAX_GOODS_PER_CONSIGNMENT = mapping.MAX_GOODS_PER_CONSIGNMENT
ARRIVAL_MAX_FUTURE_DAYS = mapping.ARRIVAL_MAX_FUTURE_DAYS
HEADER_ALWAYS_MANDATORY = mapping.HEADER_ALWAYS_MANDATORY
CONSIGNMENT_ALWAYS_MANDATORY = mapping.CONSIGNMENT_ALWAYS_MANDATORY
GOODS_ALWAYS_MANDATORY = mapping.GOODS_ALWAYS_MANDATORY
MOVEMENT_TYPE_MANDATORY = mapping.MOVEMENT_TYPE_MANDATORY
CONDITIONAL_RULES = mapping.CONDITIONAL_RULES
MOVEMENT_TYPE_LABELS = mapping.MOVEMENT_TYPE_LABELS
normalise_text = mapping.normalise_text
normalise_code = mapping.normalise_code
to_yes_no = mapping.to_yes_no
normalise_datetime = mapping.normalise_datetime
parse_arrival_to_utc = mapping.parse_arrival_to_utc


# =============================================================================
# Config (identical shape to ingest.py)
# =============================================================================
def load_db_config(ini_path: Path) -> dict[str, str]:
    """Read the [database] section from the gitignored connection .ini."""
    if not ini_path.exists():
        raise FileNotFoundError(
            f"Connection file not found: {ini_path}. "
            f"Copy Fusion_Flow_QAS.example.ini to Fusion_Flow_QAS.ini and set the password."
        )
    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")
    if "database" not in parser:
        raise ValueError(f"No [database] section in {ini_path}")
    return {k.lower(): v for k, v in parser["database"].items()}


def build_connection_string(db: dict[str, str]) -> str:
    """Build an ODBC connection string from the .ini values."""
    parts = [
        f"Driver={db.get('driver', '{ODBC Driver 17 for SQL Server}')}",
        f"Server={db['server']}",
        f"Database={db['database']}",
    ]
    if db.get("user"):
        parts += [f"Uid={db['user']}", f"Pwd={db.get('password', '')}"]
    else:
        parts.append("Trusted_Connection=yes")
    parts.append(f"Encrypt={'yes' if db.get('encrypt', 'yes').lower() in ('yes', 'true', '1') else 'no'}")
    parts.append(
        f"TrustServerCertificate={'yes' if db.get('trust_server_certificate', 'no').lower() in ('yes', 'true', '1') else 'no'}"
    )
    return ";".join(parts) + ";"


def split_go_batches(script: str) -> list[str]:
    """Split a T-SQL script on standalone GO lines (house helper shape)."""
    batches: list[str] = []
    current: list[str] = []
    for line in script.splitlines():
        if line.strip().upper() == "GO":
            batch = "\n".join(current).strip()
            if batch:
                batches.append(batch)
            current = []
        else:
            current.append(line)
    tail = "\n".join(current).strip()
    if tail:
        batches.append(tail)
    return batches


# =============================================================================
# Database adapter (EXC spine + LOG + DPE + PRS), parallel to IngestionDb
# =============================================================================
class ProcessingDb:
    """Thin pyodbc adapter for the execution spine, logging, DPE and PRS writes."""

    def __init__(self, connection: Any, dry_run: bool = False):
        self.conn = connection
        self.dry_run = dry_run
        self.execution_id: int | None = None
        self.transaction_id: str | None = None
        self.enhancement_count = 0
        self._col_cache: dict[str, list[str]] = {}

    @classmethod
    def connect(cls, db: dict[str, str], dry_run: bool = False) -> "ProcessingDb":
        import pyodbc  # lazy: only needed when actually talking to the DB
        conn = pyodbc.connect(build_connection_string(db), autocommit=False)
        return cls(conn, dry_run=dry_run)

    # --- low-level helpers --------------------------------------------------
    def _query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(sql, *params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def execute_script(self, script: str) -> None:
        """Run a (possibly GO-delimited) T-SQL script, batch by batch."""
        if self.dry_run:
            return
        cur = self.conn.cursor()
        for batch in split_go_batches(script):
            cur.execute(batch)
        self.conn.commit()

    # --- reference reads ----------------------------------------------------
    def fetch_client(self, client_code: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT ClientCode, ClientName, SchemaName, IsActive, IsAgent "
            "FROM CFG.Clients WHERE ClientCode = ?", client_code)
        return rows[0] if rows else None

    def fetch_parameter(self, key: str, default: str = "") -> str:
        rows = self._query(
            "SELECT ParameterValue FROM CFG.Application_Parameters "
            "WHERE ParameterKey = ? AND IsActive = 1", key)
        return rows[0]["ParameterValue"] if rows and rows[0]["ParameterValue"] is not None else default

    def fetch_parameters(self) -> dict[str, str]:
        rows = self._query(
            "SELECT ParameterKey, ParameterValue FROM CFG.Application_Parameters WHERE IsActive = 1")
        return {r["ParameterKey"]: r["ParameterValue"] for r in rows}

    # --- INFORMATION_SCHEMA introspection (Rule 9) --------------------------
    def introspect_columns(self, schema: str, table: str) -> list[str]:
        """Return the real column names of schema.table (Rule 9). Cached."""
        key = f"{schema}.{table}".lower()
        if key in self._col_cache:
            return self._col_cache[key]
        rows = self._query(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
            schema, table)
        cols = [r["COLUMN_NAME"] for r in rows]
        self._col_cache[key] = cols
        return cols

    # --- execution spine ----------------------------------------------------
    def open_execution(self, module: str, process: str, client_code: str,
                       run_mode: str = "manual") -> None:
        if self.dry_run:
            self.transaction_id = "00000000-0000-0000-0000-000000000000"
            self.execution_id = None
            print(f"[dry-run] open_execution {module}/{process} for {client_code}")
            return
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO EXC.Execution (EnvCode, ClientCode, ModuleName, ProcessName, RunMode, Status) "
            "OUTPUT INSERTED.ExecutionID, INSERTED.TransactionID "
            "VALUES (?, ?, ?, ?, ?, ?)",
            self.fetch_parameter("DEFAULT_ENV", "TEST"), client_code, module,
            process, run_mode, "NORMALISING")
        row = cur.fetchone()
        self.execution_id, self.transaction_id = int(row[0]), str(row[1])
        self.conn.commit()

    def advance_execution(self, process: str, status: str) -> None:
        """Advance the open execution's ProcessName/Status to the next stage."""
        if self.dry_run or not self.execution_id:
            return
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE EXC.Execution SET ProcessName = ?, Status = ? WHERE ExecutionID = ?",
            process[:30], status[:30], self.execution_id)
        self.conn.commit()

    def finish_execution(self, status: str, found: int, processed: int, failed: int,
                         error: str = "") -> None:
        if self.dry_run or not self.execution_id:
            return
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE EXC.Execution SET EndedAt = SYSUTCDATETIME(), Status = ?, "
            "ItemsFound = ?, ItemsProcessed = ?, ItemsFailed = ?, ErrorMessage = ? "
            "WHERE ExecutionID = ?",
            status[:30], found, processed, failed, (error or None), self.execution_id)
        self.conn.commit()

    def log_transition(self, entity_type: str, entity_ref: str, process: str,
                       status: str) -> None:
        """Write one EXC.Transaction row recording a per-entity stage transition."""
        print(f"[TXN] {entity_type} {entity_ref}: {process} -> {status}")
        if self.dry_run or not self.execution_id:
            return
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO EXC.[Transaction] (ExecutionID, TransactionID, ClientCode, "
            "EntityType, EntityRef, ProcessName, Status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            self.execution_id, self.transaction_id, self._client_code,
            entity_type[:40], entity_ref[:100], process[:30], status[:30])
        self.conn.commit()

    # ClientCode threaded for transition / DPE rows.
    _client_code: str | None = None

    # --- logging ------------------------------------------------------------
    def log(self, step: str, message: str, level: str = "INFO", detail: dict | None = None) -> None:
        print(f"[{level}] {step}: {message}")
        if self.dry_run or not self.execution_id:
            return
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO LOG.Process_Log (ExecutionID, TransactionID, ClientCode, ModuleName, "
            "StepName, LogLevel, Message, DetailJson) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            self.execution_id, self.transaction_id, self._client_code, MODULE_NAME,
            step[:100], level[:20], message[:2000], json.dumps(detail) if detail else None)
        self.conn.commit()

    def log_error(self, step: str, message: str, error_type: str = "", trace: str = "") -> None:
        print(f"[ERROR] {step}: {message}", file=sys.stderr)
        if self.dry_run or not self.execution_id:
            return
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO LOG.Error_Log (ExecutionID, TransactionID, ClientCode, ModuleName, "
            "StepName, ErrorType, Message, StackTrace) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            self.execution_id, self.transaction_id, self._client_code, MODULE_NAME,
            step[:100], error_type[:100], message[:2000], trace or None)
        self.conn.commit()

    # --- enhancement audit (DP-FR-06) --------------------------------------
    @staticmethod
    def _coerce(value: Any) -> str | None:
        """NULL-safe coercion to str for DPE old/new comparison + storage."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return str(value)

    def log_enhancement(self, schema: str, table: str, column: str, entity_ref: str,
                        old: Any, new: Any, rule: str) -> bool:
        """Write ONE EXC.Data_Processing_Enhancement row ONLY when old != new.

        Returns True when a change was logged. Values are coerced to str and
        stored NULL-safe (NULL old = unset, NULL new = cleared).
        """
        old_s = self._coerce(old)
        new_s = self._coerce(new)
        if old_s == new_s:
            return False
        self.enhancement_count += 1
        if self.dry_run or not self.execution_id:
            print(f"[dry-run][DPE] {schema}.{table}.{column} ({entity_ref}): "
                  f"{old_s!r} -> {new_s!r} [{rule}]")
            return True
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO EXC.Data_Processing_Enhancement (ExecutionID, TransactionID, ClientCode, "
            "SchemaName, TableName, ColumnName, EntityRef, OldValue, NewValue, RuleApplied) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self.execution_id, self.transaction_id, self._client_code,
            schema[:128], table[:128], column[:128], entity_ref[:100],
            old_s, new_s, rule[:200])
        self.conn.commit()
        return True

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# =============================================================================
# Stage helpers shared across the pipeline
# =============================================================================
def _entity_ref(movement_key: str, cons: int | None = None, goods: int | None = None) -> str:
    """Build the DPE EntityRef path, e.g. MK=...|cons=1|goods=3."""
    ref = f"MK={movement_key}"
    if cons is not None:
        ref += f"|cons={cons}"
    if goods is not None:
        ref += f"|goods={goods}"
    return ref


def _filled(value: Any) -> bool:
    """True when value is a non-empty (non-blank) value already set on the object."""
    if value is None:
        return False
    return bool(str(value).strip()) if isinstance(value, str) else True


def _set_field(db: ProcessingDb, obj: dict[str, Any], table: str, column: str,
               entity_ref: str, new_value: Any, rule: str) -> None:
    """Set obj[column]=new_value, logging the change to DPE when it differs."""
    old_value = obj.get(column)
    if db.log_enhancement("PRS", table, column, entity_ref, old_value, new_value, rule):
        obj[column] = new_value

def _split_reference(value: Any, part_number: int, max_length: int) -> Any:
    """Suffix references for automatic >99 goods split parts without exceeding TSS field limits."""
    if part_number <= 1 or not _filled(value):
        return value
    text = str(value).strip()
    suffix = f"-{part_number:02d}"
    if text.endswith(suffix):
        return text[:max_length]
    prefix_len = max(1, max_length - len(suffix))
    return f"{text[:prefix_len]}{suffix}"


def _bkd_assumption_rule(field: str, label: str) -> str:
    citation = QAS_RULE_CITATIONS.get(field)
    suffix = f" ({citation})" if citation else ""
    return f"ASSUMPTION:{label}{suffix}"


def _normalise_source_key(value: str) -> str:
    """Normalise verbatim workbook headers into the mapping.py snake_case contract."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[_\-/]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", "_", text).strip("_")


def _normalise_sales_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep raw keys, add normalised aliases for PRS mapping."""
    out = dict(payload or {})
    for key, value in (payload or {}).items():
        normalised = _normalise_source_key(key)
        if normalised and normalised not in out:
            out[normalised] = value
    return out


def _normalise_sku(value: Any) -> str | None:
    text = normalise_text(value)
    return text.upper() if text is not None else None


def _normalise_decimal(value: Any, places: int = 2) -> Decimal | None:
    text = normalise_text(value)
    if text is None:
        return None
    try:
        quant = Decimal("1." + ("0" * places))
        return Decimal(str(text).replace(",", "")).quantize(quant, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _normalise_intish(value: Any) -> int | None:
    dec = _normalise_decimal(value, 0)
    if dec is None:
        return None
    return max(int(dec), 1)


def _format_tss_decimal(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal("1.00"), rounding=ROUND_HALF_UP)


def _weight_quantity(goods: dict[str, Any]) -> tuple[Decimal, str]:
    pieces = _normalise_decimal(goods.get("number_of_individual_pieces"), 0)
    if pieces is not None:
        return pieces, "QUANTITY_BASE"
    packages = _normalise_decimal(goods.get("number_of_packages"), 0)
    if packages is not None:
        return packages, "QUANTITY_PACKAGES_ASSUMPTION"
    return Decimal("1"), "QUANTITY_ONE_ASSUMPTION"


def _normalise_package_type(value: Any) -> str | None:
    text = normalise_text(value)
    if text is None:
        return None
    upper = text.upper()
    if "BOX" in upper:
        return "Boxes"
    if "PALLET" in upper:
        return "Pallets"
    return text


def _normalise_movement_type(value: Any) -> str | None:
    text = normalise_text(value)
    if text is None:
        return None
    low = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if low in {"3a", "roro accompanied ics2", "ro ro accompanied ics2"}:
        return "3a"
    if "roro" in low and "accompanied" in low and "unaccompanied" not in low:
        return "3a"
    return text


def _normalise_passive_transport(value: Any) -> str | None:
    text = normalise_text(value)
    if text is None:
        return None
    low = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if "truck" in low and "tautliner" in low and "25" in low and "removable" not in low:
        return "3103"
    return text


def _sales_order_document_ref(row: dict[str, Any], fallback: int) -> str:
    for key in ("transport_document_number", "document_no", "document_number", "trader_reference"):
        value = normalise_text(row.get(key))
        if value:
            return value
    return f"SO-ROW-{fallback:05d}"


def _group_sales_order_rows(so_rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    by_ref: dict[str, list[dict[str, Any]]] = {}
    for idx, row in enumerate(so_rows, 1):
        ref = _sales_order_document_ref(row, idx)
        if ref not in by_ref:
            by_ref[ref] = []
            groups.append(by_ref[ref])
        by_ref[ref].append(row)
    return groups


def _normalise_file_date(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    text = normalise_text(value)
    if text is None:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return value


def _is_sales_order_data_row(payload: dict[str, Any]) -> bool:
    return _filled(payload.get("document_no")) and (
        _filled(payload.get("no")) or _filled(payload.get("line_no"))
    )


# =============================================================================
# Source reads
# =============================================================================
def fetch_ens_rows(db: ProcessingDb, client_code: str, transaction_id: str | int) -> list[dict[str, Any]]:
    """Read the ENS source rows to process for this run.

    --transaction-id latest = the most recent INGESTED ENS rows for the client
    that have NOT yet produced a VALIDATED/REJECTED PRS header (not yet processed).
    An explicit numeric ExecutionID reprocesses that load's ENS rows idempotently.
    """
    if transaction_id == "latest":
        # Most recent ENS LoadIDs not yet represented by a PRS header for this client.
        return db._query(
            "SELECT e.* FROM ING.BKD_Raw_ENS e "
            "WHERE NOT EXISTS ("
            "    SELECT 1 FROM PRS.ENS_Header h "
            "    WHERE h.ClientCode = ? AND h.SourceEnsLoadID = e.LoadID "
            "      AND h.Status IN ('VALIDATED','REJECTED')"
            ") "
            "ORDER BY e.LoadID",
            client_code)
    # Explicit execution id: reprocess every ENS row landed by that execution.
    return db._query(
        "SELECT e.* FROM ING.BKD_Raw_ENS e WHERE e.ExecutionID = ? ORDER BY e.LoadID",
        int(transaction_id))


def fetch_sales_order_rows(db: ProcessingDb, file_date: Any) -> list[dict[str, Any]]:
    """Read INGESTED Sales Order rows for a movement's file-date, parsed from JSON."""
    file_date = _normalise_file_date(file_date)
    if file_date is None:
        rows = db._query(
            "SELECT LoadID, PayloadJson FROM ING.BKD_Raw_Sales_Orders "
            "WHERE Status = 'INGESTED' ORDER BY RowNumber")
    else:
        rows = db._query(
            "SELECT LoadID, PayloadJson FROM ING.BKD_Raw_Sales_Orders "
            "WHERE Status = 'INGESTED' AND FileDate = ? "
            "AND ExecutionID = ("
            "    SELECT MAX(ExecutionID) FROM ING.BKD_Raw_Sales_Orders "
            "    WHERE Status = 'INGESTED' AND FileDate = ?"
            ") ORDER BY RowNumber",
            file_date, file_date)
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["PayloadJson"]) if r["PayloadJson"] else {}
        except (ValueError, TypeError):
            payload = {}
        payload = _normalise_sales_order_payload(payload)
        if not _is_sales_order_data_row(payload):
            continue
        payload["__LoadID"] = r["LoadID"]
        out.append(payload)
    return out


# =============================================================================
# Stage 1: NORMALISE (DP-FR-01)
# =============================================================================
def normalise(db: ProcessingDb, ens_row: dict[str, Any], so_rows: list[dict[str, Any]],
              client_code: str, run_date: datetime) -> dict[str, Any]:
    """Map source rows to canonical PRS fields with standardised dates/codes/text.

    Returns a movement dict: {header, consignments:[{consignment, goods:[...]}], ...}
    MovementKey is derived from the ENS DedupKey (DetailsDate|ICR) per locked Q1/Q2.
    """
    dedup_key = (ens_row.get("DedupKey") or "").strip()
    movement_key = dedup_key  # locked decision: MovementKey == DedupKey
    eref = _entity_ref(movement_key)

    header: dict[str, Any] = {
        "ClientCode": client_code,
        "MovementKey": movement_key,
        "SourceEnsLoadID": ens_row.get("LoadID"),
        "Status": "NORMALISED",
    }

    # Map ENS source columns -> canonical header fields.
    for src_col, dest_col in ENS_CSV_TO_HEADER.items():
        raw = ens_row.get(src_col)
        if dest_col in ("nationality_of_transport", "carrier_country"):
            value = normalise_code(raw)
        elif dest_col == "movement_type":
            value = _normalise_movement_type(raw)
        elif dest_col == "type_of_passive_transport":
            value = _normalise_passive_transport(raw)
        else:
            value = normalise_text(raw)
        _set_field(db, header, "ENS_Header", dest_col, eref, value, "DP-FR-01:MAP_ENS")

    # Resolve / reformat arrival_date_time to strict DD/MM/YYYY HH:MM:SS (Rule 4 fmt).
    raw_arrival = ens_row.get("arrival_date_time")
    arrival_str = normalise_datetime(raw_arrival, now_utc=run_date)
    _set_field(db, header, "ENS_Header", "arrival_date_time", eref, arrival_str,
               "DP-FR-01:NORMALISE_DATETIME (Rule 4)")
    arrival_utc = parse_arrival_to_utc(arrival_str, now_utc=run_date)
    _set_field(db, header, "ENS_Header", "arrival_date_time_utc", eref,
               arrival_utc.isoformat() if arrival_utc else None,
               "DP-FR-01:ARRIVAL_UTC (Rule 4)")

    # Build one PRS consignment per Sales Order document. ING remains verbatim;
    # this is the PRS construction contract for BKD Sales Orders.
    if not so_rows:
        db.log(
            "NORMALISE",
            f"{eref}: no Sales-Order lines resolved; PRS header will be rejected without creating an orphan consignment.",
            "WARN",
        )
        return {"movement_key": movement_key, "header": header, "consignments": []}

    consignments: list[dict[str, Any]] = []
    for source_group_number, group_rows in enumerate(_group_sales_order_rows(so_rows), 1):
        first = group_rows[0]
        cref = _entity_ref(movement_key, cons=len(consignments) + 1)
        cons: dict[str, Any] = {
            "ClientCode": client_code,
            "MovementKey": movement_key,
            "ConsignmentOrdinal": len(consignments) + 1,
            "Status": "NORMALISED",
        }
        for src_col, dest_col in SALES_ORDER_TO_CONSIGNMENT.items():
            raw = first.get(src_col)
            if dest_col.endswith("_country") or dest_col == "destination_country":
                value = normalise_code(raw)
            elif dest_col in ("controlled_goods", "buyer_same_as_importer", "seller_same_as_exporter", "use_importer_sde"):
                value = to_yes_no(raw) or normalise_text(raw)
            else:
                value = normalise_text(raw)
            if value is None or _filled(cons.get(dest_col)):
                continue
            _set_field(db, cons, "Consignment", dest_col, cref, value,
                       "DP-FR-01:MAP_SO_CONSIGNMENT")

        doc_ref = _sales_order_document_ref(first, source_group_number)
        if doc_ref and not _filled(cons.get("transport_document_number")):
            _set_field(db, cons, "Consignment", "transport_document_number", cref, doc_ref,
                       "DP-FR-01:DERIVE_TRANSPORT_DOCUMENT_FROM_SALES_ORDER")
        if doc_ref and not _filled(cons.get("trader_reference")):
            _set_field(db, cons, "Consignment", "trader_reference", cref, doc_ref,
                       "DP-FR-01:DERIVE_TRADER_REFERENCE_FROM_SALES_ORDER")
        if doc_ref and not _filled(cons.get("consignment_number")):
            _set_field(db, cons, "Consignment", "consignment_number", cref, doc_ref[:40],
                       "DP-FR-01:DERIVE_CONSIGNMENT_NUMBER_FROM_SALES_ORDER")
        if doc_ref and not _filled(cons.get("goods_description")):
            _set_field(db, cons, "Consignment", "goods_description", cref,
                       f"Sales order goods {doc_ref}"[:254],
                       "ASSUMPTION:CONSIGNMENT_DESCRIPTION_FROM_DOCUMENT_REF")

        goods_items: list[dict[str, Any]] = []
        for idx, so in enumerate(group_rows, 1):
            gref = _entity_ref(movement_key, cons=len(consignments) + 1, goods=idx)
            goods: dict[str, Any] = {
                "ClientCode": client_code,
                "MovementKey": movement_key,
                "GoodsItemOrdinal": idx,
                "SourceSalesOrderLoadID": so.get("__LoadID"),
                "Status": "NORMALISED",
            }
            for src_col, dest_col in SALES_ORDER_TO_GOODS.items():
                raw = so.get(src_col)
                rule = "DP-FR-01:MAP_SO_GOODS"
                if dest_col in ("country_of_origin", "country_of_preferential_origin", "commodity_code"):
                    value = normalise_code(raw)
                elif dest_col == "type_of_packages":
                    value = _normalise_package_type(raw)
                    if value is not None and normalise_text(raw) != value:
                        rule = "ASSUMPTION:PACKAGE_TYPE_FROM_UOM"
                elif dest_col in ("number_of_packages", "number_of_individual_pieces"):
                    value = _normalise_intish(raw)
                elif dest_col in ("gross_mass_kg", "net_mass_kg", "item_invoice_amount"):
                    value = _normalise_decimal(raw, 2)
                else:
                    value = normalise_text(raw)
                if value is None or _filled(goods.get(dest_col)):
                    continue
                _set_field(db, goods, "Goods_Item", dest_col, gref, value, rule)

            sku = _normalise_sku(so.get("no") or so.get("item_no") or so.get("item_number"))
            if sku:
                goods["_source_sku"] = sku
            if sku and not _filled(goods.get("package_marks")):
                _set_field(db, goods, "Goods_Item", "package_marks", gref, sku[:140],
                           "DP-FR-01:PACKAGE_MARKS_FROM_ITEM_CODE")
            if sku and not _filled(goods.get("goods_description")):
                _set_field(db, goods, "Goods_Item", "goods_description", gref,
                           f"BKD item {sku}"[:255],
                           "ASSUMPTION:GOODS_DESCRIPTION_FROM_ITEM_CODE")
            gross = goods.get("gross_mass_kg")
            if gross is not None and not _filled(goods.get("net_mass_kg")):
                _set_field(db, goods, "Goods_Item", "net_mass_kg", gref, gross,
                           "ASSUMPTION:NET_MASS_EQUALS_GROSS_MASS")
            goods_items.append(goods)

        for part_number, start in enumerate(range(0, len(goods_items), MAX_GOODS_PER_CONSIGNMENT), 1):
            part_cons = dict(cons)
            part_ref = _entity_ref(movement_key, cons=len(consignments) + 1)
            part_cons["ConsignmentOrdinal"] = len(consignments) + 1
            part_cons["goods"] = goods_items[start:start + MAX_GOODS_PER_CONSIGNMENT]
            if part_number > 1:
                for field, max_length in (
                    ("consignment_number", 40),
                    ("trader_reference", 100),
                    ("transport_document_number", 35),
                ):
                    split_value = _split_reference(part_cons.get(field), part_number, max_length)
                    if split_value != part_cons.get(field):
                        _set_field(
                            db,
                            part_cons,
                            "Consignment",
                            field,
                            part_ref,
                            split_value,
                            "ASSUMPTION:SPLIT_REFERENCE (>99 goods per TSS consignment limit)",
                        )
            consignments.append(part_cons)

    return {"movement_key": movement_key, "header": header, "consignments": consignments}


def _apply_existing_header_submission_context(db: ProcessingDb, header: dict[str, Any], movement_key: str) -> None:
    rows = db._query(
        "SELECT TOP 1 declaration_number, carrier_name, carrier_street_number, "
        "carrier_city, carrier_postcode, carrier_country "
        "FROM PRS.BKD_ENS_Header_Submission "
        "WHERE ClientCode = ? AND MovementKey = ? ORDER BY SubmissionID DESC",
        db._client_code, movement_key,
    )
    if not rows:
        return
    eref = _entity_ref(movement_key)
    row = rows[0]
    for field in (
        "declaration_number", "carrier_name", "carrier_street_number",
        "carrier_city", "carrier_postcode", "carrier_country",
    ):
        value = row.get(field)
        if value is not None and not _filled(header.get(field)):
            _set_field(db, header, "ENS_Header", field, eref, value,
                       "SYNC:EXISTING_ENS_HEADER_SUBMISSION")


# =============================================================================

def fetch_product_master(db: ProcessingDb, client_code: str) -> dict[str, dict[str, Any]]:
    """Read active product masterdata keyed by SKU. Missing table = no enrichment."""
    try:
        exists = db._query("SELECT OBJECT_ID('CFG.Product_Master', 'U') AS ObjectID")
    except Exception:
        return {}
    if not exists or exists[0].get("ObjectID") is None:
        return {}

    rows = db._query(
        "SELECT ClientCode, SKU, ProductCode, ProductName, GoodsDescription, "
        "CommodityCode, CountryOfOrigin, PackageType, PackageMarks, ProcedureCode, "
        "AdditionalProcedureCode, ValuationMethod, ValuationIndicator, PreferenceCode, "
        "NiAdditionalInfoCode, NatureOfTransaction, CountryOfPreferentialOrigin, "
        "TaricCode, CusCode, NationalAdditionalCode, QuotaOrderNumber, ControlledGoodsType, "
        "GrossWeightKg, NetWeightKg, WeightSource, UnitValue, Currency, ControlledGoods, "
        "RequiresSupplementaryUnit "
        "FROM CFG.Product_Master "
        "WHERE IsActive = 1 AND ClientCode IN (?, 'ALL') "
        "ORDER BY CASE WHEN ClientCode = ? THEN 0 ELSE 1 END",
        client_code, client_code,
    )
    master: dict[str, dict[str, Any]] = {}
    for row in rows:
        sku = _normalise_sku(row.get("SKU"))
        if sku and sku not in master:
            master[sku] = row
    return master


def _set_goods_master_field(db: ProcessingDb, goods: dict[str, Any], field: str, value: Any,
                            gref: str, rule: str, *, replace_assumed: bool = False) -> None:
    if value is None or value == "":
        return
    current = goods.get(field)
    if not replace_assumed and _filled(current):
        return
    _set_field(db, goods, "Goods_Item", field, gref, value, rule)


def _yes_no_from_bit(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return "yes"
    if text in ("0", "false", "no"):
        return "no"
    return None


def _apply_product_master_enrichment(db: ProcessingDb, goods: dict[str, Any], product: dict[str, Any] | None,
                                     gref: str) -> bool:
    """Apply SKU masterdata to a goods line, logging every change as MASTERDATA."""
    if not product:
        return False
    goods["_product_master_matched"] = True

    description = normalise_text(product.get("GoodsDescription") or product.get("ProductName"))
    assumed_description = str(goods.get("goods_description") or "").startswith("BKD item ")
    _set_goods_master_field(
        db, goods, "goods_description", description, gref,
        "MASTERDATA:BKD_PRODUCT_MASTER_GOODS_DESCRIPTION",
        replace_assumed=assumed_description,
    )

    field_map = (
        ("CommodityCode", "commodity_code", normalise_code, "MASTERDATA:BKD_PRODUCT_MASTER_COMMODITY_CODE"),
        ("CountryOfOrigin", "country_of_origin", normalise_code, "MASTERDATA:BKD_PRODUCT_MASTER_COUNTRY_OF_ORIGIN"),
        ("PackageType", "type_of_packages", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_PACKAGE_TYPE"),
        ("PackageMarks", "package_marks", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_PACKAGE_MARKS"),
        ("ProcedureCode", "procedure_code", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_PROCEDURE_CODE"),
        ("AdditionalProcedureCode", "additional_procedure_code", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_ADDITIONAL_PROCEDURE_CODE"),
        ("ValuationMethod", "valuation_method", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_VALUATION_METHOD"),
        ("PreferenceCode", "preference", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_PREFERENCE"),
        ("NiAdditionalInfoCode", "ni_additional_information_codes", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_NI_ADDITIONAL_INFO"),
        ("TaricCode", "taric_code", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_TARIC_CODE"),
        ("CusCode", "cus_code", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_CUS_CODE"),
        ("NationalAdditionalCode", "national_additional_code", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_NATIONAL_ADDITIONAL_CODE"),
        ("QuotaOrderNumber", "quota_order_number", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_QUOTA_ORDER_NUMBER"),
        ("ControlledGoodsType", "controlled_goods_type", normalise_text, "MASTERDATA:BKD_PRODUCT_MASTER_CONTROLLED_GOODS_TYPE"),
    )
    for source, target, transform, rule in field_map:
        _set_goods_master_field(db, goods, target, transform(product.get(source)), gref, rule)

    controlled = _yes_no_from_bit(product.get("ControlledGoods"))
    if controlled is not None:
        goods["_controlled_goods_from_master"] = True
        _set_goods_master_field(
            db, goods, "controlled_goods", controlled, gref,
            "MASTERDATA:BKD_PRODUCT_MASTER_CONTROLLED_GOODS",
        )

    qty, qty_rule = _weight_quantity(goods)
    unit_gross = _normalise_decimal(product.get("GrossWeightKg"), 3)
    if unit_gross is not None and not _filled(goods.get("gross_mass_kg")):
        _set_field(
            db, goods, "Goods_Item", "gross_mass_kg", gref,
            _format_tss_decimal(unit_gross * qty),
            f"MASTERDATA:BKD_PRODUCT_MASTER_GROSS_WEIGHT_X_{qty_rule}",
        )

    unit_net = _normalise_decimal(product.get("NetWeightKg"), 3)
    if unit_net is not None and not _filled(goods.get("net_mass_kg")):
        _set_field(
            db, goods, "Goods_Item", "net_mass_kg", gref,
            _format_tss_decimal(unit_net * qty),
            f"MASTERDATA:BKD_PRODUCT_MASTER_NET_WEIGHT_X_{qty_rule}",
        )
    elif _filled(goods.get("gross_mass_kg")) and not _filled(goods.get("net_mass_kg")):
        _set_field(
            db, goods, "Goods_Item", "net_mass_kg", gref, goods.get("gross_mass_kg"),
            "ASSUMPTION:NET_MASS_EQUALS_GROSS_MASS",
        )

    return True


def fetch_partner_master(db: ProcessingDb, client_code: str) -> list[dict[str, Any]]:
    """Read active partner masterdata. Missing table = no enrichment."""
    try:
        exists = db._query("SELECT OBJECT_ID('CFG.Partner_Master', 'U') AS ObjectID")
    except Exception:
        return []
    if not exists or exists[0].get("ObjectID") is None:
        return []

    return db._query(
        "SELECT ClientCode, PartnerType, PartnerName, NormalizedPartnerName, EORI, EORIGB, "
        "AddressLine1, AddressLine2, City, County, Postcode, Country, EnvCode, SourceSystem, SourceRecordID "
        "FROM CFG.Partner_Master "
        "WHERE IsActive = 1 AND ClientCode IN (?, 'ALL') "
        "ORDER BY CASE WHEN ClientCode = ? THEN 0 ELSE 1 END, CASE WHEN PartnerName LIKE '%(%' THEN 1 ELSE 0 END, PartnerMasterID ASC",
        client_code, client_code,
    )


def _match_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def _compact_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _partner_address_blob(partner: dict[str, Any]) -> str:
    return _match_text(" ".join(
        str(partner.get(field) or "")
        for field in ("AddressLine1", "AddressLine2", "City", "County", "Postcode")
    ))


def _source_address_blob(cons: dict[str, Any], prefix: str) -> str:
    return _match_text(" ".join(
        str(cons.get(f"{prefix}_{field}") or "")
        for field in ("street_number", "city", "postcode")
    ))


def _partner_score(partner: dict[str, Any], *, party_type: str | None, name: Any,
                   street: Any, city: Any, postcode: Any, eori: Any) -> int:
    ptype = _match_text(partner.get("PartnerType"))
    wanted_type = _match_text(party_type)
    if wanted_type and ptype and wanted_type != ptype:
        return -100

    score = 0
    wanted_eori = _compact_text(eori)
    partner_eori = _compact_text(partner.get("EORI") or partner.get("EORIGB"))
    if wanted_eori and partner_eori and wanted_eori == partner_eori:
        score += 180

    wanted_name = _match_text(name)
    partner_name = _match_text(partner.get("NormalizedPartnerName") or partner.get("PartnerName"))
    if wanted_name and partner_name:
        if wanted_name == partner_name:
            score += 130
        elif wanted_name in partner_name or partner_name in wanted_name:
            score += 85
        else:
            wanted_tokens = {t for t in wanted_name.split() if len(t) > 1}
            partner_tokens = {t for t in partner_name.split() if len(t) > 1}
            overlap = len(wanted_tokens & partner_tokens)
            if overlap:
                score += min(overlap * 18, 70)

    wanted_street = _match_text(street)
    partner_blob = _partner_address_blob(partner)
    if wanted_street and partner_blob:
        if wanted_street in partner_blob:
            score += 55
        elif _compact_text(wanted_street) and _compact_text(wanted_street) in _compact_text(partner_blob):
            score += 40

    wanted_city = _match_text(city)
    partner_city = _match_text(partner.get("City"))
    if wanted_city and partner_city:
        if wanted_city == partner_city:
            score += 35
        elif wanted_city in partner_city or partner_city in wanted_city:
            score += 20

    wanted_postcode = _compact_text(postcode)
    partner_postcode = _compact_text(partner.get("Postcode"))
    if wanted_postcode and partner_postcode and wanted_postcode == partner_postcode:
        score += 45
    return score


def _best_partner_match(partners: list[dict[str, Any]], *, party_type: str | None,
                        name: Any, street: Any = None, city: Any = None,
                        postcode: Any = None, eori: Any = None) -> dict[str, Any] | None:
    best: tuple[int, dict[str, Any]] | None = None
    for partner in partners:
        score = _partner_score(
            partner,
            party_type=party_type,
            name=name,
            street=street,
            city=city,
            postcode=postcode,
            eori=eori,
        )
        if score < 110:
            continue
        if best is None or score > best[0]:
            best = (score, partner)
    return best[1] if best else None


PRS_CONSIGNMENT_TEXT_MAX = {
    "consignor_name": 35,
    "consignor_street_number": 35,
    "consignee_name": 35,
    "consignee_street_number": 35,
    "importer_name": 35,
    "importer_street_number": 35,
    "exporter_name": 35,
    "exporter_street_number": 35,
}


def _partner_name_for_prs(partner: dict[str, Any]) -> str | None:
    name = normalise_text(partner.get("PartnerName"))
    if _compact_text(partner.get("EORI") or partner.get("EORIGB")) == "XI379692092000":
        normalized = _match_text(name)
        if "BIRKDALE SALES" in normalized:
            return "Birkdale Sales Ltd"
    return name


def _partner_street(partner: dict[str, Any]) -> str | None:
    parts = [normalise_text(partner.get("AddressLine1")), normalise_text(partner.get("AddressLine2"))]
    parts = [part for part in parts if part]
    return " ".join(parts)[:300] if parts else None


def _fit_prs_consignment_field(field: str, value: Any) -> Any:
    if isinstance(value, str):
        max_len = PRS_CONSIGNMENT_TEXT_MAX.get(field)
        if max_len and len(value) > max_len:
            return value[:max_len].rstrip()
    return value


def _set_cons_partner_field(db: ProcessingDb, cons: dict[str, Any], field: str, value: Any,
                            cref: str, rule: str, *, replace: bool = False) -> None:
    if value is None or value == "":
        return
    value = _fit_prs_consignment_field(field, value)
    if _filled(cons.get(field)) and not replace:
        return
    _set_field(db, cons, "Consignment", field, cref, value, rule)


def _apply_party_partner(db: ProcessingDb, cons: dict[str, Any], partner: dict[str, Any] | None,
                         prefix: str, cref: str, rule_prefix: str, *, replace_source: bool = False) -> None:
    if not partner:
        return
    updates = {
        f"{prefix}_eori": normalise_text(partner.get("EORI") or partner.get("EORIGB")),
        f"{prefix}_name": _partner_name_for_prs(partner),
        f"{prefix}_street_number": _partner_street(partner),
        f"{prefix}_city": normalise_text(partner.get("City")),
        f"{prefix}_postcode": normalise_code(partner.get("Postcode")),
        f"{prefix}_country": normalise_code(partner.get("Country")),
    }
    for field, value in updates.items():
        replace = replace_source
        if field.endswith("_postcode"):
            replace = True
        _set_cons_partner_field(db, cons, field, value, cref, f"MASTERDATA:{rule_prefix}_{field.upper()}", replace=replace)


def _find_birkdale_partner(partners: list[dict[str, Any]], fallback_eori: str | None) -> dict[str, Any] | None:
    partner = _best_partner_match(
        partners,
        party_type=None,
        name="Birkdale Sales",
        eori=fallback_eori,
    )
    if partner:
        return partner
    for row in partners:
        if _compact_text(row.get("EORI") or row.get("EORIGB")) == _compact_text(fallback_eori):
            return row
    return None


def _apply_consignment_partner_master(db: ProcessingDb, cons: dict[str, Any], partners: list[dict[str, Any]],
                                      fallback_partner: dict[str, Any] | None, cref: str) -> None:
    consignee = _best_partner_match(
        partners,
        party_type="Consignee",
        name=cons.get("consignee_name"),
        street=cons.get("consignee_street_number"),
        city=cons.get("consignee_city"),
        postcode=cons.get("consignee_postcode"),
        eori=cons.get("consignee_eori"),
    )
    _apply_party_partner(
        db, cons, consignee, "consignee", cref,
        "BKD_PARTNER_MASTER_CONSIGNEE",
        replace_source=True,
    )

    if fallback_partner:
        fallback_eori = fallback_partner.get("EORI") or fallback_partner.get("EORIGB")
        for prefix, party_type, rule in (
            ("importer", "Importer", "BKD_PARTNER_MASTER_IMPORTER"),
            ("consignor", "Consignor", "BKD_PARTNER_MASTER_CONSIGNOR"),
            ("exporter", "Exporter", "BKD_PARTNER_MASTER_EXPORTER"),
        ):
            role_partner = _best_partner_match(
                partners,
                party_type=party_type,
                name="Birkdale Sales",
                eori=fallback_eori,
            ) or fallback_partner
            _apply_party_partner(db, cons, role_partner, prefix, cref, rule)

# Stage 2: ENRICH (DP-FR-02/08)
# =============================================================================
# BKD_QAS_CONSTANTS (mapping.py) is a FLAT dict keyed by TSS field name:
#   {arrival_port, transport_charges, goods_domestic_status, importer_eori_fallback}
# The maps below route each flat key onto the correct canonical object so the
# locked QAS rule set (Rules 10/11/12/13) is actually applied + logged to DPE.
QAS_HEADER_FIELDS = ("arrival_port", "transport_charges")        # Rule 12, Rule 11
QAS_CONSIGNMENT_FIELDS = ("goods_domestic_status",)              # Rule 10
QAS_IMPORTER_FALLBACK_KEY = "importer_eori_fallback"             # Rule 13


def _load_choice_cache(db: ProcessingDb) -> dict[str, set[str]]:
    """Resolve choice values from CFG.Choice_Value_Cache after introspecting its
    columns (Rule 9). Returns {ChoiceField: {valid ChoiceValue, ...}}."""
    cols = db.introspect_columns("CFG", "Choice_Value_Cache")
    # Bind only to columns that actually exist (Rule 9 - never assume names).
    field_col = "ChoiceField" if "ChoiceField" in cols else None
    value_col = "ChoiceValue" if "ChoiceValue" in cols else None
    cache: dict[str, set[str]] = {}
    if not (field_col and value_col):
        return cache
    active = " AND IsActive = 1" if "IsActive" in cols else ""
    rows = db._query(
        f"SELECT {field_col} AS f, {value_col} AS v FROM CFG.Choice_Value_Cache "
        f"WHERE 1=1{active}")
    for r in rows:
        cache.setdefault((r["f"] or "").strip(), set()).add((r["v"] or "").strip())
    return cache


def enrich(db: ProcessingDb, movement: dict[str, Any], choice_cache: dict[str, set[str]],
           client_code: str) -> None:
    """Apply BKD QAS constants (citing Critical Rule #) and resolve choice values."""
    movement_key = movement["movement_key"]
    header = movement["header"]
    eref = _entity_ref(movement_key)
    header["Status"] = "ENRICHED"
    product_master = fetch_product_master(db, client_code)
    partner_master = fetch_partner_master(db, client_code)
    birkdale_partner = _find_birkdale_partner(partner_master, BKD_QAS_CONSTANTS.get(QAS_IMPORTER_FALLBACK_KEY))

    _apply_existing_header_submission_context(db, header, movement_key)

    # Apply BKD QAS hardcoded constants (Q4) at header level, with citations.
    # arrival_port (Rule 12) + transport_charges (Rule 11).
    for column in QAS_HEADER_FIELDS:
        if column in BKD_QAS_CONSTANTS:
            rule = _bkd_assumption_rule(column, f"BKD_QAS_CONSTANT:{column}")
            _set_field(db, header, "ENS_Header", column, eref, BKD_QAS_CONSTANTS[column], rule)

    for ci, cons in enumerate(movement["consignments"], 1):
        cons["Status"] = "ENRICHED"
        cref = _entity_ref(movement_key, cons=ci)
        # goods_domestic_status='D' single char (Rule 10), at consignment level.
        for column in QAS_CONSIGNMENT_FIELDS:
            if column in BKD_QAS_CONSTANTS:
                rule = _bkd_assumption_rule(column, f"BKD_QAS_CONSTANT:{column}")
                _set_field(db, cons, "Consignment", column, cref, BKD_QAS_CONSTANTS[column], rule)

        # BKD importer fallback (Rule 13): no importer EORI -> Birkdale is importer
        # AND consignor (literal XI379692092000).
        fallback = BKD_QAS_CONSTANTS.get(QAS_IMPORTER_FALLBACK_KEY)
        if fallback and not (cons.get("importer_eori") or "").strip():
            rule = _bkd_assumption_rule("importer_eori_fallback", "BKD_IMPORTER_FALLBACK")
            _set_field(db, cons, "Consignment", "importer_eori", cref, fallback, rule)
            _set_field(db, cons, "Consignment", "consignor_eori", cref, fallback, rule)

        _apply_consignment_partner_master(db, cons, partner_master, birkdale_partner, cref)

        # Minimal BKD defaults needed for ENS consignment shape. These are logged
        # as assumptions, not hidden as source data. Consignee postcode is never
        # defaulted globally; it must come from source/masterdata or stay missing.
        for column, value, label in (
            ("destination_country", "GB", "BKD_DEFAULT_DESTINATION_COUNTRY_GB"),
            ("consignee_country", "GB", "BKD_DEFAULT_CONSIGNEE_COUNTRY_GB"),
            ("container_indicator", "0", "BKD_DEFAULT_NOT_CONTAINERISED"),
        ):
            if not _filled(cons.get(column)):
                _set_field(db, cons, "Consignment", column, cref, value, f"ASSUMPTION:{label}")

        if fallback and not (cons.get("exporter_eori") or "").strip():
            _set_field(db, cons, "Consignment", "exporter_eori", cref, fallback,
                       "ASSUMPTION:BKD_EXPORTER_EORI_FALLBACK")

        for goods_index, goods in enumerate(cons.get("goods", []), 1):
            gref = _entity_ref(movement_key, cons=ci, goods=goods_index)
            sku = _normalise_sku(goods.get("_source_sku") or goods.get("package_marks") or goods.get("goods_id"))
            _apply_product_master_enrichment(db, goods, product_master.get(sku or ""), gref)
            if not _filled(goods.get("controlled_goods")):
                _set_field(db, goods, "Goods_Item", "controlled_goods", gref,
                           "no", "ASSUMPTION:BKD_DEFAULT_CONTROLLED_GOODS_NO")
            goods["Status"] = "ENRICHED"

        goods_control = [str(g.get("controlled_goods") or "").strip().lower() for g in cons.get("goods", [])]
        if not _filled(cons.get("controlled_goods")):
            if "yes" in goods_control:
                _set_field(db, cons, "Consignment", "controlled_goods", cref, "yes",
                           "MASTERDATA:BKD_PRODUCT_MASTER_CONTROLLED_GOODS_ROLLUP")
            elif goods_control and all(g.get("_controlled_goods_from_master") for g in cons.get("goods", [])):
                _set_field(db, cons, "Consignment", "controlled_goods", cref, "no",
                           "MASTERDATA:BKD_PRODUCT_MASTER_CONTROLLED_GOODS_ROLLUP")
            else:
                _set_field(db, cons, "Consignment", "controlled_goods", cref, "no",
                           "ASSUMPTION:BKD_DEFAULT_CONTROLLED_GOODS_NO")

    # Stash the resolved choice cache on the movement for VALIDATE membership checks.
    movement["_choice_cache"] = choice_cache


# =============================================================================
# Stage 3: CONSTRUCT (DP-FR-03) - assemble + persist with upsert on MovementKey
# =============================================================================
def _upsert_row(db: ProcessingDb, schema: str, table: str, obj: dict[str, Any],
                key_cols: list[str], skip_cols: tuple[str, ...] = ()) -> int | None:
    """Idempotent upsert keyed on key_cols. Binds only to real columns (Rule 9).

    Returns the surrogate RowID of the affected row (when resolvable).
    """
    real_cols = set(db.introspect_columns(schema, table))
    payload = {c: v for c, v in obj.items()
               if c in real_cols and c not in skip_cols}
    # Always thread the execution spine when those columns exist.
    if "ExecutionID" in real_cols:
        payload["ExecutionID"] = db.execution_id
    if "TransactionID" in real_cols:
        payload["TransactionID"] = db.transaction_id

    if db.dry_run or not db.execution_id:
        return None

    pk_col = next((c for c in db.introspect_columns(schema, table) if c.endswith("RowID")), None)
    where = " AND ".join(f"[{c}] = ?" for c in key_cols)
    where_vals = [obj.get(c) for c in key_cols]
    existing = db._query(
        f"SELECT {pk_col} AS pk FROM {schema}.{table} WHERE {where}", *where_vals) if pk_col else []

    cur = db.conn.cursor()
    if existing:
        row_id = existing[0]["pk"]
        set_cols = [c for c in payload if c not in key_cols]
        if set_cols:
            assignments = ", ".join(f"[{c}] = ?" for c in set_cols)
            if "UpdatedAt" in real_cols:
                assignments += ", [UpdatedAt] = SYSUTCDATETIME()"
            cur.execute(
                f"UPDATE {schema}.{table} SET {assignments} WHERE {pk_col} = ?",
                *[payload[c] for c in set_cols], row_id)
            db.conn.commit()
        return int(row_id)

    cols = list(payload.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(f"[{c}]" for c in cols)
    output = f"OUTPUT INSERTED.{pk_col} " if pk_col else ""
    cur.execute(
        f"INSERT INTO {schema}.{table} ({col_list}) {output}VALUES ({placeholders})",
        *[payload[c] for c in cols])
    if pk_col:
        return int(cur.fetchone()[0])
    db.conn.commit()
    return None


def construct(db: ProcessingDb, movement: dict[str, Any]) -> None:
    """Persist 1 header -> many consignments -> <=99 goods, assigning ordinals,
    binding FKs, upserting on MovementKey for idempotency (DP-FR-03)."""
    movement_key = movement["movement_key"]
    header = movement["header"]
    header["Status"] = "CONSTRUCTED"

    header_row_id = _upsert_row(
        db, "PRS", "ENS_Header", header,
        key_cols=["ClientCode", "MovementKey"], skip_cols=("goods", "consignments"))
    movement["header_row_id"] = header_row_id

    for ci, cons in enumerate(movement["consignments"], 1):
        cons["ConsignmentOrdinal"] = ci
        cons["Status"] = "CONSTRUCTED"
        if header_row_id is not None:
            cons["EnsHeaderRowID"] = header_row_id
        cons_row_id = _upsert_row(
            db, "PRS", "Consignment", cons,
            key_cols=["EnsHeaderRowID", "ConsignmentOrdinal"], skip_cols=("goods",))
        cons["consignment_row_id"] = cons_row_id

        goods_list = cons.get("goods", [])
        if len(goods_list) > MAX_GOODS_PER_CONSIGNMENT:
            db.log("CONSTRUCT",
                   f"MK={movement_key} cons={ci}: {len(goods_list)} goods exceeds "
                   f"max {MAX_GOODS_PER_CONSIGNMENT}; flagged for VALIDATE rejection.",
                   "WARN")
        for gi, goods in enumerate(goods_list, 1):
            goods["GoodsItemOrdinal"] = gi
            goods["Status"] = "CONSTRUCTED"
            if cons_row_id is not None:
                goods["ConsignmentRowID"] = cons_row_id
            _upsert_row(
                db, "PRS", "Goods_Item", goods,
                key_cols=["ConsignmentRowID", "GoodsItemOrdinal"])


# =============================================================================
# Stage 4: VALIDATE (DP-FR-04)
# =============================================================================
def _missing(obj: dict[str, Any], field: str) -> bool:
    val = obj.get(field)
    return val is None or (isinstance(val, str) and not val.strip())


def _has_party_address(obj: dict[str, Any], prefix: str) -> bool:
    return all(
        not _missing(obj, f"{prefix}_{field}")
        for field in ("name", "street_number", "city", "postcode", "country")
    )


def validate(db: ProcessingDb, movement: dict[str, Any], run_date: datetime) -> tuple[str, str]:
    """Run mandatory/conditional + cross-cutting rules. Returns (status, reason)."""
    movement_key = movement["movement_key"]
    header = movement["header"]
    choice_cache: dict[str, set[str]] = movement.get("_choice_cache", {})
    reasons: list[str] = []
    eref = _entity_ref(movement_key)

    mtype = (header.get("movement_type") or "").strip()

    # Header always-mandatory.
    for field in HEADER_ALWAYS_MANDATORY:
        if _missing(header, field):
            reasons.append(f"header.{field} mandatory and missing")

    # movement_type-specific mandatory (includes 3a effectively-mandatory set, Rule 3).
    for field in MOVEMENT_TYPE_MANDATORY.get(mtype, []):
        if _missing(header, field):
            reasons.append(f"header.{field} mandatory for movement_type {mtype}")

    # Rule 4 arrival bounds: not in the past, <= ARRIVAL_MAX_FUTURE_DAYS ahead.
    arrival_utc = parse_arrival_to_utc(header.get("arrival_date_time"), now_utc=run_date)
    if arrival_utc is None:
        reasons.append("header.arrival_date_time unparseable (Rule 4)")
    else:
        now = run_date if run_date.tzinfo else run_date.replace(tzinfo=timezone.utc)
        if arrival_utc.tzinfo is None:
            arrival_utc = arrival_utc.replace(tzinfo=timezone.utc)
        delta_days = (arrival_utc - now).total_seconds() / 86400.0
        if delta_days < 0:
            reasons.append("header.arrival_date_time is in the past (Rule 4)")
        elif delta_days > ARRIVAL_MAX_FUTURE_DAYS:
            reasons.append(
                f"header.arrival_date_time more than {ARRIVAL_MAX_FUTURE_DAYS} days "
                "in the future (Rule 4)")

    # Choice membership for header-level choice fields present in the cache.
    if mtype and "movement_type" in choice_cache and mtype not in choice_cache["movement_type"]:
        reasons.append(f"movement_type '{mtype}' not in choice cache")

    consignments = movement.get("consignments", [])
    if not consignments:
        reasons.append("movement has no linked Sales Orders / consignments; no orphan consignment created")

    for ci, cons in enumerate(consignments, 1):
        for field in CONSIGNMENT_ALWAYS_MANDATORY:
            if field == "consignee_eori" and _has_party_address(cons, "consignee"):
                continue
            if _missing(cons, field):
                if field == "consignee_eori":
                    reasons.append(
                        f"cons[{ci}].consignee_eori or full consignee address mandatory and missing"
                    )
                else:
                    reasons.append(f"cons[{ci}].{field} mandatory and missing")

        goods_list = cons.get("goods", [])
        # >=1 goods per consignment; <=99 cardinality.
        if not goods_list:
            reasons.append(f"cons[{ci}] has no goods items")
        elif len(goods_list) > MAX_GOODS_PER_CONSIGNMENT:
            reasons.append(
                f"cons[{ci}] has {len(goods_list)} goods (max {MAX_GOODS_PER_CONSIGNMENT})")

        for gi, goods in enumerate(goods_list, 1):
            for field in GOODS_ALWAYS_MANDATORY:
                if _missing(goods, field):
                    reasons.append(f"cons[{ci}].goods[{gi}].{field} mandatory and missing")

        # Conditional rules (each entry: predicate + required field, per section 5).
        reasons.extend(_apply_conditional_rules(mtype, header, cons))

    status = "VALIDATED" if not reasons else "REJECTED"
    reason_text = "; ".join(reasons)[:2000]
    header["Status"] = status
    header["RejectReason"] = reason_text or None
    for cons in consignments:
        cons["Status"] = status
        for goods in cons.get("goods", []):
            goods["Status"] = status

    # Persist the resolved status/reason (Rule 9 - only to real columns).
    _persist_status(db, movement, status, reason_text)
    if status == "REJECTED":
        db.log_error("VALIDATE", f"MK={movement_key} REJECTED: {reason_text}", "VALIDATION")
    else:
        db.log("VALIDATE", f"MK={movement_key} VALIDATED", "OK")
    return status, reason_text


def _apply_conditional_rules(mtype: str, header: dict[str, Any],
                             cons: dict[str, Any]) -> list[str]:
    """Evaluate CONDITIONAL_RULES (mapping.py) against the constructed movement.

    Each rule is a dict: {when_field, when_equals|when_in, when_scope,
    require:[fields], scope, note}. The predicate is read from `when_scope`; the
    required field(s) are checked in `scope`. (Spec section 5 conditional matrix.)
    """
    reasons: list[str] = []
    goods_list = cons.get("goods", [])

    def _scope_objs(scope: str) -> list[tuple[str, dict[str, Any]]]:
        if scope == "header":
            return [("header", header)]
        if scope in ("consignment", "cons"):
            return [("consignment", cons)]
        if scope == "goods":
            return [(f"goods[{i}]", g) for i, g in enumerate(goods_list, 1)]
        return [("consignment", cons)]

    def _predicate_fires(when_scope: str, field: str, equals: Any, in_set: Any) -> bool:
        # Fires when ANY object in the predicate scope matches equals / in_set.
        for _, src in _scope_objs(when_scope):
            actual = src.get(field)
            actual_s = "" if actual is None else str(actual).strip()
            if equals is not None and actual_s.lower() == str(equals).strip().lower():
                return True
            if in_set is not None and actual_s in {str(x).strip() for x in in_set}:
                return True
        return False

    for rule in CONDITIONAL_RULES:
        if not isinstance(rule, dict):
            continue
        require = rule.get("require") or []
        if isinstance(require, str):
            require = [require]
        when_field = rule.get("when_field")
        if not when_field or not require:
            continue
        when_scope = rule.get("when_scope", rule.get("scope", "consignment"))
        if not _predicate_fires(when_scope, when_field,
                                rule.get("when_equals"), rule.get("when_in")):
            continue
        label = rule.get("note") or when_field
        scope = rule.get("scope", "consignment")
        for tag, obj in _scope_objs(scope):
            for field in require:
                if _missing(obj, field):
                    reasons.append(f"{tag}.{field} required: {label}")
    return reasons


def _persist_status(db: ProcessingDb, movement: dict[str, Any], status: str,
                    reason: str) -> None:
    """Write the final Status/RejectReason to the PRS rows (real columns only)."""
    if db.dry_run or not db.execution_id:
        return
    header_id = movement.get("header_row_id")
    if header_id is None:
        return
    real = set(db.introspect_columns("PRS", "ENS_Header"))
    cur = db.conn.cursor()
    sets = ["[Status] = ?"]
    vals: list[Any] = [status[:30]]
    if "RejectReason" in real:
        sets.append("[RejectReason] = ?")
        vals.append(reason or None)
    if "UpdatedAt" in real:
        sets.append("[UpdatedAt] = SYSUTCDATETIME()")
    cur.execute(f"UPDATE PRS.ENS_Header SET {', '.join(sets)} WHERE EnsHeaderRowID = ?",
                *vals, header_id)
    # Cascade status to children.
    for cons in movement["consignments"]:
        cons_id = cons.get("consignment_row_id")
        if cons_id is not None:
            cur.execute("UPDATE PRS.Consignment SET [Status] = ? WHERE ConsignmentRowID = ?",
                        status[:30], cons_id)
            cur.execute(
                "UPDATE PRS.Goods_Item SET [Status] = ? WHERE ConsignmentRowID = ?",
                status[:30], cons_id)
    db.conn.commit()


# =============================================================================
# Per-movement orchestration through all four stages
# =============================================================================
def process_movement(db: ProcessingDb, ens_row: dict[str, Any], choice_cache: dict[str, set[str]],
                     client_code: str, run_date: datetime) -> str:
    """Run NORMALISE -> ENRICH -> CONSTRUCT -> VALIDATE for one ENS movement.

    Writes an EXC.Transaction transition at each stage boundary (DP-FR-07).
    Returns the final status (VALIDATED / REJECTED).
    """
    movement_key = (ens_row.get("DedupKey") or "").strip()

    # Associate Sales-Order goods lines by the ENS movement's file-date (locked Q1/Q2:
    # shared business reference; fall back to all current-date lines for the open header).
    so_rows = fetch_sales_order_rows(db, ens_row.get("DetailsDate"))
    movement = normalise(db, ens_row, so_rows, client_code, run_date)
    db.log_transition("ENS_HEADER", _entity_ref(movement_key), "NORMALISING", "NORMALISED")

    enrich(db, movement, choice_cache, client_code)
    db.log_transition("ENS_HEADER", _entity_ref(movement_key), "ENRICHING", "ENRICHED")

    construct(db, movement)
    db.log_transition("ENS_HEADER", _entity_ref(movement_key), "CONSTRUCTING", "CONSTRUCTED")

    status, _reason = validate(db, movement, run_date)
    db.log_transition("ENS_HEADER", _entity_ref(movement_key), "VALIDATING", status)
    return status


# =============================================================================
# Orchestration / entry points (mirrors run_ingestion.py)
# =============================================================================
def _resolve_run_controls(db: ProcessingDb) -> tuple[str, str | int, bool]:
    """Read run controls from CFG.Application_Parameters (no CLI). Script-level
    constants are the fallbacks used only when a parameter row is absent."""
    client_code = (db.fetch_parameter(PARAM_CLIENT, DEFAULT_CLIENT)
                   or DEFAULT_CLIENT).strip().upper()
    txn_mode = (db.fetch_parameter(PARAM_TRANSACTION_MODE, DEFAULT_TRANSACTION_MODE)
                or DEFAULT_TRANSACTION_MODE).strip()
    transaction_id: str | int = ("latest" if txn_mode.lower() == "latest"
                                  else (int(txn_mode) if txn_mode.isdigit() else txn_mode))
    dry_default = "1" if DEFAULT_DRY_RUN else "0"
    dry_run = (db.fetch_parameter(PARAM_DRY_RUN, dry_default)
               or dry_default).strip().lower() in ("1", "true", "yes", "on")
    return client_code, transaction_id, dry_run


def run(ini_path: Path = DEFAULT_INI) -> int:
    """Scheduler entry point. Connects using Configuration/Fusion_Flow_QAS.ini and
    reads ALL run behaviour from CFG.Application_Parameters (no CLI)."""
    db_cfg = load_db_config(ini_path)
    # Connect first (reads only) so we can resolve the run controls from the DB;
    # dry_run gates writes and is applied once resolved.
    db = ProcessingDb.connect(db_cfg, dry_run=False)
    client_code, transaction_id, dry_run = _resolve_run_controls(db)
    db.dry_run = dry_run
    db._client_code = client_code

    found = processed = failed = 0
    try:
        if not db.fetch_client(client_code) and not dry_run:
            print(f"[ERROR] Unknown client code: {client_code}", file=sys.stderr)
            return 2

        run_mode = "dry-run" if dry_run else "manual"
        db.open_execution(MODULE_NAME, "NORMALISING", client_code, run_mode)
        db._client_code = client_code
        db.log("START", f"Data Processing run for {client_code} "
               f"(Transaction_ID={db.transaction_id}, transaction-id={transaction_id})",
               detail={"dry_run": dry_run, "transaction_id": str(transaction_id)})

        run_date = datetime.now(timezone.utc)
        choice_cache = _load_choice_cache(db)
        db.log("CHOICE_CACHE", f"Resolved {len(choice_cache)} choice field set(s) from cache.")

        ens_rows = fetch_ens_rows(db, client_code, transaction_id)
        found = len(ens_rows)
        db.log("SOURCE", f"{found} ENS movement(s) to process.")

        validated = rejected = 0
        for ens_row in ens_rows:
            mk = (ens_row.get("DedupKey") or "").strip()
            try:
                # advance execution status across the run as stages begin.
                db.advance_execution("NORMALISING", "NORMALISING")
                status = process_movement(db, ens_row, choice_cache, client_code, run_date)
                processed += 1
                if status == "VALIDATED":
                    validated += 1
                else:
                    rejected += 1
            except Exception as error:  # noqa: BLE001
                failed += 1
                db.log_error("PROCESS_MOVEMENT", f"MK={mk}: {error}",
                             type(error).__name__, traceback.format_exc())

        final = "VALIDATED" if failed == 0 else "ERROR"
        db.finish_execution(final, found, processed, failed)
        db.log("FINISH",
               f"Run complete: found={found} processed={processed} validated={validated} "
               f"rejected={rejected} failed={failed} enhancements={db.enhancement_count}",
               "OK")
        print(f"Data Processing summary [{client_code}]: found={found} processed={processed} "
              f"validated={validated} rejected={rejected} failed={failed} "
              f"enhancements={db.enhancement_count} status={final}")
        return 0 if failed == 0 else 1
    except Exception as error:  # noqa: BLE001
        db.log_error("RUN", str(error), type(error).__name__, traceback.format_exc())
        db.finish_execution("ERROR", found, processed, max(failed, 1), str(error))
        raise
    finally:
        db.close()


def main() -> int:
    """No CLI - the scheduler runs `python process_data.py`. The connection .ini
    path may be overridden via the FUSION_FLOW_INI environment variable; everything
    else comes from CFG.Application_Parameters."""
    ini_path = Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI)))
    return run(ini_path)


if __name__ == "__main__":
    raise SystemExit(main())
