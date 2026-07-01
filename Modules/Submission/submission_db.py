#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Module 3 (Submission) DB adapter.

Shared by promote_ens / submit_ens / mirror_ens. Provides the connection, the EXC
execution spine (one Execution per run, a Transaction row per movement, Errors),
the authoritative per-call log (API.Call), and generic upserts into STG / TSS.

Connection from Configuration/Fusion_Flow_QAS.ini [database]; all run behaviour
from CFG.Application_Parameters (no CLI).
"""

from __future__ import annotations

import configparser
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
MODULE = "SUBMISSION"

# TSS Declaration Header (ENS) create payload - the fields we send to /headers.
# Excludes declaration_number (returned), route (read-only/auto), the derived UTC
# datetime, and the Tss_* response fields.
ENS_PAYLOAD_FIELDS = [
    "op_type", "movement_type", "type_of_passive_transport", "identity_no_of_transport",
    "nationality_of_transport", "conveyance_ref", "arrival_date_time", "arrival_port",
    "place_of_loading", "place_of_unloading", "place_of_acceptance_same_as_loading",
    "place_of_acceptance", "place_of_delivery_same_as_unloading", "place_of_delivery",
    "seal_number", "transport_charges", "carrier_eori", "carrier_name",
    "carrier_street_number", "carrier_city", "carrier_postcode", "carrier_country",
    "haulier_eori",
]


def load_db_config(ini_path: Path) -> dict[str, str]:
    if not ini_path.exists():
        raise FileNotFoundError(
            f"Connection file not found: {ini_path}. Copy Fusion_Flow_QAS.example.ini "
            f"to Fusion_Flow_QAS.ini and set the password.")
    cp = configparser.ConfigParser()
    cp.read(ini_path, encoding="utf-8")
    if "database" not in cp:
        raise ValueError(f"No [database] section in {ini_path}")
    return {k.lower(): v for k, v in cp["database"].items()}


def _conn_str(db: dict[str, str]) -> str:
    parts = [
        f"Driver={db.get('driver', '{ODBC Driver 17 for SQL Server}')}",
        f"Server={db['server']}", f"Database={db['database']}",
    ]
    if db.get("user"):
        parts += [f"Uid={db['user']}", f"Pwd={db.get('password', '')}"]
    else:
        parts.append("Trusted_Connection=yes")
    yes = lambda v: str(v).lower() in ("yes", "true", "1")
    parts.append(f"Encrypt={'yes' if yes(db.get('encrypt', 'yes')) else 'no'}")
    parts.append(f"TrustServerCertificate={'yes' if yes(db.get('trust_server_certificate', 'no')) else 'no'}")
    return ";".join(parts) + ";"


class SubmissionDb:
    def __init__(self, conn: Any, dry_run: bool = False):
        self.conn = conn
        self.dry_run = dry_run
        self.execution_id: int | None = None
        self.transaction_id: str | None = None
        self.env_code: str = "TST"
        self.client_code: str | None = None
        self._cols: dict[str, list[str]] = {}

    @classmethod
    def connect(cls, db: dict[str, str], dry_run: bool = False) -> "SubmissionDb":
        import pyodbc
        return cls(pyodbc.connect(_conn_str(db), autocommit=False), dry_run=dry_run)

    # --- low level ----------------------------------------------------------
    def q(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(sql, *params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def exec(self, sql: str, *params: Any):
        cur = self.conn.cursor()
        cur.execute(sql, *params)
        return cur

    def commit(self) -> None:
        self.conn.commit()

    def introspect(self, schema: str, table: str) -> list[str]:
        key = f"{schema}.{table}".lower()
        if key not in self._cols:
            self._cols[key] = [r["COLUMN_NAME"] for r in self.q(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION", schema, table)]
        return self._cols[key]

    def param(self, key: str, default: str = "") -> str:
        rows = self.q("SELECT ParameterValue FROM CFG.Application_Parameters "
                      "WHERE ParameterKey = ? AND IsActive = 1", key)
        return rows[0]["ParameterValue"] if rows and rows[0]["ParameterValue"] is not None else default

    # --- EXC spine ----------------------------------------------------------
    def open_execution(self, process: str, client: str, env: str, run_mode: str) -> None:
        self.client_code, self.env_code = client, env
        cur = self.exec(
            "INSERT INTO EXC.Execution (EnvCode, ClientCode, ModuleName, ProcessName, RunMode, Status) "
            "OUTPUT INSERTED.ExecutionID, INSERTED.TransactionID VALUES (?, ?, ?, ?, ?, ?)",
            env, client, MODULE, process[:30], run_mode, process[:30])
        row = cur.fetchone()
        self.execution_id, self.transaction_id = int(row[0]), str(row[1])
        self.commit()

    def advance(self, process: str, status: str) -> None:
        if not self.execution_id:
            return
        self.exec("UPDATE EXC.Execution SET ProcessName = ?, Status = ? WHERE ExecutionID = ?",
                  process[:30], status[:30], self.execution_id)
        self.commit()

    def finish(self, status: str, found: int, processed: int, failed: int, err: str = "") -> None:
        if not self.execution_id:
            return
        self.exec("UPDATE EXC.Execution SET EndedAt = SYSUTCDATETIME(), Status = ?, "
                  "ItemsFound = ?, ItemsProcessed = ?, ItemsFailed = ?, ErrorMessage = ? WHERE ExecutionID = ?",
                  status[:30], found, processed, failed, (err or None), self.execution_id)
        self.commit()

    def transition(self, entity_type: str, entity_ref: str, process: str, status: str) -> None:
        print(f"[TXN] {entity_type} {entity_ref}: {process} -> {status}")
        if not self.execution_id:
            return
        self.exec("INSERT INTO EXC.[Transaction] (ExecutionID, TransactionID, ClientCode, "
                  "EntityType, EntityRef, ProcessName, Status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  self.execution_id, self.transaction_id, self.client_code,
                  entity_type[:40], entity_ref[:100], process[:30], status[:30])
        self.commit()

    def log(self, step: str, message: str, level: str = "INFO") -> None:
        print(f"[{level}] {step}: {message}")
        if not self.execution_id:
            return
        self.exec("INSERT INTO LOG.Process_Log (ExecutionID, TransactionID, ClientCode, ModuleName, "
                  "StepName, LogLevel, Message) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  self.execution_id, self.transaction_id, self.client_code, MODULE,
                  step[:100], level[:20], message[:2000])
        self.commit()

    def log_error(self, step: str, message: str, error_type: str = "", trace: str = "") -> None:
        print(f"[ERROR] {step}: {message}")
        if not self.execution_id:
            return
        self.exec("INSERT INTO LOG.Error_Log (ExecutionID, TransactionID, ClientCode, ModuleName, "
                  "StepName, ErrorType, Message, StackTrace) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  self.execution_id, self.transaction_id, self.client_code, MODULE,
                  step[:100], error_type[:100], message[:2000], trace or None)
        self.commit()

    # --- API.Call: the authoritative per-call log ---------------------------
    def log_call(self, *, process: str, resource: str, op_type: str, movement_key: str | None,
                 declaration_number: str | None, result: dict) -> None:
        """Persist one API call (request + response, dry-run flag, outcome)."""
        self.exec(
            "INSERT INTO API.Call (ExecutionID, TransactionID, ClientCode, EntityType, MovementKey, "
            "Declaration_Number, ModuleName, ProcessName, RouteCode, StepNo, ResourceName, OpType, "
            "EnvCode, HttpMethod, RequestUrl, RequestHeaders, RequestJson, ResponseHeaders, ResponseJson, "
            "StatusCode, Success, DurationMs, IsDryRun, ErrorMessage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self.execution_id, self.transaction_id, self.client_code, "ENS_HEADER", movement_key,
            declaration_number, MODULE, process[:30], "A", None, resource[:50], op_type[:20],
            self.env_code, (result.get("method") or "")[:10], (result.get("request_url") or "")[:1000],
            _js(result.get("request_headers")), _js(result.get("request_json")),
            _js(result.get("response_headers")), _clip(result.get("response_text"), 1_000_000),
            result.get("status_code"), 1 if result.get("ok") else 0, result.get("duration_ms"),
            1 if result.get("is_dry_run") else 0, _clip(result.get("error"), 2000))
        self.commit()

    # --- generic upsert (binds only real columns) ---------------------------
    def upsert(self, schema: str, table: str, obj: dict, key_cols: list[str], pk_col: str) -> int | None:
        real = set(self.introspect(schema, table))
        where = " AND ".join(f"[{k}] = ?" for k in key_cols)
        existing = self.q(f"SELECT {pk_col} AS pk FROM {schema}.{table} WHERE {where}",
                          *[obj.get(k) for k in key_cols])
        if existing:
            pk = existing[0]["pk"]
            setc = [c for c in obj if c in real and c != pk_col and c not in key_cols]
            if setc:
                assign = ", ".join(f"[{c}] = ?" for c in setc)
                if "UpdatedAt" in real:
                    assign += ", [UpdatedAt] = SYSUTCDATETIME()"
                self.exec(f"UPDATE {schema}.{table} SET {assign} WHERE {pk_col} = ?",
                          *[obj[c] for c in setc], pk)
            return int(pk)
        cols = [c for c in obj if c in real and c != pk_col]
        cur = self.exec(f"INSERT INTO {schema}.{table} ({', '.join('['+c+']' for c in cols)}) "
                        f"OUTPUT INSERTED.{pk_col} VALUES ({', '.join('?' for _ in cols)})",
                        *[obj[c] for c in cols])
        return int(cur.fetchone()[0])

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def _js(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v[:1_000_000]
    return json.dumps(v, default=str)[:1_000_000]


def _clip(v: Any, n: int) -> str | None:
    if v is None:
        return None
    s = v if isinstance(v, str) else str(v)
    return s[:n]


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
