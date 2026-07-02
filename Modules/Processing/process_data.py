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
def _config_from_env() -> dict[str, str] | None:
    """DB config from DB_* env vars (Render / container). Returns None when DB_SERVER
    isn't set, so local runs fall back to the .ini. Same var names the portal uses."""
    if not os.environ.get("DB_SERVER"):
        return None
    return {
        "server": os.environ.get("DB_SERVER", ""),
        "database": os.environ.get("DB_NAME", ""),
        "user": os.environ.get("DB_USER", ""),
        "password": os.environ.get("DB_PASSWORD", ""),
        "driver": os.environ.get("DB_DRIVER", "{ODBC Driver 18 for SQL Server}"),
        "encrypt": os.environ.get("DB_ENCRYPT", "yes"),
        "trust_server_certificate": os.environ.get("DB_TRUST", "no"),
    }


def load_db_config(ini_path: Path) -> dict[str, str]:
    """DB config from DB_* env (Render) if set, else the [database] section of the
    gitignored connection .ini (local)."""
    env = _config_from_env()
    if env:
        return env
    if not ini_path.exists():
        raise FileNotFoundError(
            f"Connection file not found: {ini_path} (and DB_SERVER not set). "
            f"Copy Fusion_Flow_QAS.example.ini to Fusion_Flow_QAS.ini and set the password, "
            f"or set the DB_* environment variables."
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

    def __init__(self, connection: Any, dry_run: bool = False,
                 overrides: dict[str, str] | None = None):
        self.conn = connection
        self.dry_run = dry_run
        # Per-run scope overrides consulted by fetch_parameter() before CFG, so a
        # caller can scope a single run (movement / mode) without mutating the shared
        # CFG.Application_Parameters that concurrent or scheduled runs also read.
        self.overrides: dict[str, str] = dict(overrides or {})
        self.execution_id: int | None = None
        self.transaction_id: str | None = None
        self.enhancement_count = 0
        self._col_cache: dict[str, list[str]] = {}

    @classmethod
    def connect(cls, db: dict[str, str], dry_run: bool = False,
                overrides: dict[str, str] | None = None) -> "ProcessingDb":
        import pyodbc  # lazy: only needed when actually talking to the DB
        conn = pyodbc.connect(build_connection_string(db), autocommit=False)
        return cls(conn, dry_run=dry_run, overrides=overrides)

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
        if key in self.overrides:                      # per-run scope wins over shared CFG
            v = self.overrides[key]
            return v if v is not None else default
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
    if file_date is None:
        rows = db._query(
            "SELECT LoadID, PayloadJson FROM ING.BKD_Raw_Sales_Orders "
            "WHERE Status = 'INGESTED' ORDER BY RowNumber")
    else:
        rows = db._query(
            "SELECT LoadID, PayloadJson FROM ING.BKD_Raw_Sales_Orders "
            "WHERE Status = 'INGESTED' AND FileDate = ? ORDER BY RowNumber", file_date)
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["PayloadJson"]) if r["PayloadJson"] else {}
        except (ValueError, TypeError):
            payload = {}
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

    # Skeleton consignment from Sales-Order consignment-level mapping (first row).
    cons: dict[str, Any] = {
        "ClientCode": client_code,
        "MovementKey": movement_key,
        "ConsignmentOrdinal": 1,
        "Status": "NORMALISED",
    }
    goods_items: list[dict[str, Any]] = []
    cref = _entity_ref(movement_key, cons=1)
    if so_rows:
        first = so_rows[0]
        for src_col, dest_col in SALES_ORDER_TO_CONSIGNMENT.items():
            value = normalise_text(first.get(src_col))
            # Many source aliases map to one dest; skip empties and never let a
            # later absent alias clobber a value an earlier alias already set.
            if value is None or _filled(cons.get(dest_col)):
                continue
            _set_field(db, cons, "Consignment", dest_col, cref, value,
                       "DP-FR-01:MAP_SO_CONSIGNMENT")

        # One goods item per Sales-Order line (ENS-context fields only - Q3).
        for idx, so in enumerate(so_rows, 1):
            gref = _entity_ref(movement_key, cons=1, goods=idx)
            goods: dict[str, Any] = {
                "ClientCode": client_code,
                "MovementKey": movement_key,
                "GoodsItemOrdinal": idx,
                "SourceSalesOrderLoadID": so.get("__LoadID"),
                "Status": "NORMALISED",
            }
            for src_col, dest_col in SALES_ORDER_TO_GOODS.items():
                if dest_col in ("country_of_origin", "country_of_preferential_origin",
                                "commodity_code"):
                    value = normalise_code(so.get(src_col))
                else:
                    value = normalise_text(so.get(src_col))
                # First non-empty alias wins (see consignment note above).
                if value is None or _filled(goods.get(dest_col)):
                    continue
                _set_field(db, goods, "Goods_Item", dest_col, gref, value,
                           "DP-FR-01:MAP_SO_GOODS")
            goods_items.append(goods)
    else:
        db.log("NORMALISE", f"{eref}: no Sales-Order lines resolved; "
               "attaching empty consignment skeleton (documented assumption - NOTE).",
               "WARN")

    cons["goods"] = goods_items
    return {"movement_key": movement_key, "header": header, "consignments": [cons]}


# =============================================================================
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

    # Apply BKD QAS hardcoded constants (Q4) at header level, with citations.
    # arrival_port (Rule 12) + transport_charges (Rule 11).
    for column in QAS_HEADER_FIELDS:
        if column in BKD_QAS_CONSTANTS:
            rule = QAS_RULE_CITATIONS.get(column, f"QAS:BKD ({column})")
            _set_field(db, header, "ENS_Header", column, eref, BKD_QAS_CONSTANTS[column], rule)

    for ci, cons in enumerate(movement["consignments"], 1):
        cons["Status"] = "ENRICHED"
        cref = _entity_ref(movement_key, cons=ci)
        # goods_domestic_status='D' single char (Rule 10), at consignment level.
        for column in QAS_CONSIGNMENT_FIELDS:
            if column in BKD_QAS_CONSTANTS:
                rule = QAS_RULE_CITATIONS.get(column, f"QAS:BKD ({column})")
                _set_field(db, cons, "Consignment", column, cref, BKD_QAS_CONSTANTS[column], rule)

        # BKD importer fallback (Rule 13): no importer EORI -> Birkdale is importer
        # AND consignor (literal XI379692092000).
        fallback = BKD_QAS_CONSTANTS.get(QAS_IMPORTER_FALLBACK_KEY)
        if fallback and not (cons.get("importer_eori") or "").strip():
            rule = QAS_RULE_CITATIONS.get("importer_eori_fallback",
                                          "QAS:BKD_IMPORTER_FALLBACK (Rule 13)")
            _set_field(db, cons, "Consignment", "importer_eori", cref, fallback, rule)
            _set_field(db, cons, "Consignment", "consignor_eori", cref, fallback, rule)

        # No goods-level QAS constants in the flat BKD set; goods enrichment is
        # choice-value resolution only (advanced in a later module). Mark status.
        for goods in cons.get("goods", []):
            goods["Status"] = "ENRICHED"

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
    for ci, cons in enumerate(consignments, 1):
        for field in CONSIGNMENT_ALWAYS_MANDATORY:
            if _missing(cons, field):
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

        # Conditional rules (each entry: predicate + required field, per §5).
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
    required field(s) are checked in `scope`. (Spec §5 conditional matrix.)
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
