#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Module 1: Ingestion (SKELETON).

Acquire inbound data for a client across its configured channels and land it
VERBATIM into the ING schema, opening exactly one EXC.Execution (Transaction_ID)
per run and writing provenance + dedup hashes.

This is a SKELETON. The per-route fetch/parse logic is intentionally stubbed:
each client/channel (SFTP / REST / EMAIL via Graph / FILE_DROP) differs, so the
concrete `discover()` / `fetch()` bodies are left as clearly-marked TODOs. The
orchestration, config loading, EXC/LOG wiring and verbatim-landing helpers are
real so a route can be implemented by filling one class.

Config sources (no hardcoded connection, no scattered settings):
  - DB connection : Configuration_Layer/Fusion_Flow_QAS.ini  -> [database]
  - Parameters    : CFG.Application_Parameters
  - Routing       : CFG.Clients, CFG.Email_Rules, CFG.Folder_Paths

Usage:
  python ingest.py --client BKD [--channel FILE_DROP] [--dry-run]
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows subprocess launchers: ASCII-safe stdout (Critical Rule 21).
os.environ.setdefault("NO_COLOR", "1")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INI = REPO_ROOT / "Configuration_Layer" / "Fusion_Flow_QAS.ini"

MODULE_NAME = "INGESTION"


# =============================================================================
# Config
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


# =============================================================================
# Database adapter (EXC spine + LOG + ING landing)
# =============================================================================
class IngestionDb:
    """Thin pyodbc adapter for the execution spine, logging and raw landing."""

    def __init__(self, connection: Any, dry_run: bool = False):
        self.conn = connection
        self.dry_run = dry_run
        self.execution_id: int | None = None
        self.transaction_id: str | None = None

    @classmethod
    def connect(cls, db: dict[str, str], dry_run: bool = False) -> "IngestionDb":
        import pyodbc  # lazy: only needed when actually talking to the DB
        conn = pyodbc.connect(build_connection_string(db), autocommit=False)
        return cls(conn, dry_run=dry_run)

    # --- reference reads ----------------------------------------------------
    def fetch_client(self, client_code: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT ClientCode, ClientName, SchemaName, IsActive, IsAgent "
            "FROM CFG.Clients WHERE ClientCode = ?", client_code)
        return rows[0] if rows else None

    def fetch_email_rules(self, client_code: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT Mailbox, SenderRuleType, SenderRule, AllowedFileTypes, IsActive "
            "FROM CFG.Email_Rules WHERE ClientCode = ? AND IsActive = 1", client_code)

    def fetch_folder_paths(self, client_code: str) -> dict[str, str]:
        rows = self._query(
            "SELECT PathType, PathValue FROM CFG.Folder_Paths "
            "WHERE ClientCode = ? AND IsActive = 1", client_code)
        return {r["PathType"]: r["PathValue"] for r in rows}

    def fetch_parameter(self, key: str, default: str = "") -> str:
        rows = self._query(
            "SELECT ParameterValue FROM CFG.Application_Parameters "
            "WHERE ParameterKey = ? AND IsActive = 1", key)
        return rows[0]["ParameterValue"] if rows else default

    # --- execution spine ----------------------------------------------------
    def open_execution(self, client_code: str, process: str, run_mode: str) -> None:
        if self.dry_run:
            self.transaction_id = "00000000-0000-0000-0000-000000000000"
            return
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO EXC.Execution (EnvCode, ClientCode, ModuleName, ProcessName, RunMode, Status) "
            "OUTPUT INSERTED.ExecutionID, INSERTED.TransactionID "
            "VALUES (?, ?, ?, ?, ?, ?)",
            self.fetch_parameter("DEFAULT_ENV", "TEST"), client_code, MODULE_NAME,
            process, run_mode, "INGESTING")
        row = cur.fetchone()
        self.execution_id, self.transaction_id = int(row[0]), str(row[1])
        self.conn.commit()

    def finish_execution(self, status: str, found: int, processed: int, failed: int, error: str = "") -> None:
        if self.dry_run or not self.execution_id:
            return
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE EXC.Execution SET EndedAt = SYSUTCDATETIME(), Status = ?, "
            "ItemsFound = ?, ItemsProcessed = ?, ItemsFailed = ?, ErrorMessage = ? "
            "WHERE ExecutionID = ?",
            status[:30], found, processed, failed, (error or None), self.execution_id)
        self.conn.commit()

    # --- logging ------------------------------------------------------------
    def log(self, step: str, message: str, level: str = "INFO", detail: dict | None = None) -> None:
        print(f"[{level}] {step}: {message}")
        if self.dry_run or not self.execution_id:
            return
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO LOG.Process_Log (ExecutionID, TransactionID, ModuleName, StepName, LogLevel, Message, DetailJson) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            self.execution_id, self.transaction_id, MODULE_NAME, step[:100], level[:20],
            message[:2000], json.dumps(detail) if detail else None)
        self.conn.commit()

    def log_error(self, step: str, message: str, error_type: str = "", trace: str = "") -> None:
        print(f"[ERROR] {step}: {message}", file=sys.stderr)
        if self.dry_run or not self.execution_id:
            return
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO LOG.Error_Log (ExecutionID, TransactionID, ModuleName, StepName, ErrorType, Message, StackTrace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            self.execution_id, self.transaction_id, MODULE_NAME, step[:100],
            error_type[:100], message[:2000], trace or None)
        self.conn.commit()

    # --- verbatim landing ---------------------------------------------------
    def land_inbound_file(self, client_code: str, channel: str, name: str, content: bytes,
                          source_path: str = "", sender: str = "", mailbox: str = "",
                          content_type: str = "") -> int | None:
        """Land one file verbatim. Returns FileID, or None on duplicate/dry-run."""
        file_hash = hashlib.sha256(content).hexdigest()
        if self.dry_run:
            self.log("LAND_FILE", f"[dry-run] would land {name} ({len(content)} bytes, {file_hash[:12]})")
            return None
        cur = self.conn.cursor()
        # Idempotent dedup: skip a file already landed for this client (Rule: hash natural key).
        cur.execute("SELECT FileID FROM ING.Inbound_File WHERE ClientCode = ? AND FileHash = ?",
                    client_code, file_hash)
        if cur.fetchone():
            self.log("DEDUP", f"Duplicate skipped: {name} ({file_hash[:12]})", "WARN")
            return None
        cur.execute(
            "INSERT INTO ING.Inbound_File (ExecutionID, TransactionID, ClientCode, SourceChannel, "
            "SourceName, SourcePath, Mailbox, Sender, ReceivedUtc, FileHash, SizeBytes, ContentType, Status) "
            "OUTPUT INSERTED.FileID "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME(), ?, ?, ?, 'INGESTED')",
            self.execution_id, self.transaction_id, client_code, channel, name[:500],
            source_path[:1000], (mailbox or None), (sender or None), file_hash, len(content),
            (content_type or None))
        file_id = int(cur.fetchone()[0])
        self.conn.commit()
        return file_id

    def land_raw_rows(self, file_id: int, client_code: str, rows: list[dict[str, Any]]) -> int:
        """Land parsed rows verbatim as JSON. Returns count landed."""
        if self.dry_run or not file_id:
            return 0
        cur = self.conn.cursor()
        for ordinal, row in enumerate(rows, 1):
            payload = json.dumps(row, ensure_ascii=False, default=str)
            row_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            cur.execute(
                "INSERT INTO ING.Raw_Record (FileID, ExecutionID, ClientCode, RowOrdinal, RowHash, PayloadJson) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                file_id, self.execution_id, client_code, ordinal, row_hash, payload)
        cur.execute("UPDATE ING.Inbound_File SET RowsLanded = ? WHERE FileID = ?", len(rows), file_id)
        self.conn.commit()
        return len(rows)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(sql, *params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# =============================================================================
# Channels - SKELETON. One class per route; fill discover()/fetch() per client.
# =============================================================================
class IngestChannel:
    """Base channel. A concrete route discovers artefacts then yields (name, bytes)."""

    name = "BASE"

    def __init__(self, db: IngestionDb, client: dict[str, Any], paths: dict[str, str],
                 email_rules: list[dict[str, Any]]):
        self.db = db
        self.client = client
        self.paths = paths
        self.email_rules = email_rules

    def is_enabled(self) -> bool:
        """Return True when this client has the channel configured. Override."""
        return False

    def discover(self) -> list[dict[str, Any]]:
        """Return a list of artefact descriptors to fetch. Override per route."""
        raise NotImplementedError(f"{self.name}.discover() not implemented")

    def fetch(self, artefact: dict[str, Any]) -> tuple[str, bytes]:
        """Return (filename, content_bytes) for one artefact. Override per route."""
        raise NotImplementedError(f"{self.name}.fetch() not implemented")

    def parse_rows(self, name: str, content: bytes) -> list[dict[str, Any]]:
        """Parse CSV/XLSX bytes into verbatim row dicts. Override/extend per route."""
        # TODO: route-specific parsing. CSV via csv module; XLSX via openpyxl/stdlib zip.
        return []


class FileDropChannel(IngestChannel):
    """Manual / scheduled file drop into the client INBOUND folder."""
    name = "FILE_DROP"

    def is_enabled(self) -> bool:
        return bool(self.paths.get("INBOUND"))

    def discover(self) -> list[dict[str, Any]]:
        # TODO: list allowed files in self.paths['INBOUND'] (filter by AllowedFileTypes).
        #       Return [{'path': <full path>, 'name': <filename>}, ...].
        raise NotImplementedError("FileDropChannel.discover() - implement folder scan")

    def fetch(self, artefact: dict[str, Any]) -> tuple[str, bytes]:
        # TODO: read bytes from artefact['path'].
        raise NotImplementedError("FileDropChannel.fetch() - implement file read")


class EmailGraphChannel(IngestChannel):
    """Microsoft Graph mailbox attachment harvest (CSV/XLSX)."""
    name = "EMAIL"

    def is_enabled(self) -> bool:
        return bool(self.email_rules)

    def discover(self) -> list[dict[str, Any]]:
        # TODO: auth to Graph; list messages matching CFG.Email_Rules sender rules;
        #       record provenance to ING.Source_Email; return attachment descriptors.
        raise NotImplementedError("EmailGraphChannel.discover() - implement Graph harvest")

    def fetch(self, artefact: dict[str, Any]) -> tuple[str, bytes]:
        raise NotImplementedError("EmailGraphChannel.fetch() - implement attachment download")


class SftpChannel(IngestChannel):
    """SFTP polling."""
    name = "SFTP"

    def discover(self) -> list[dict[str, Any]]:
        raise NotImplementedError("SftpChannel.discover() - implement SFTP listing")

    def fetch(self, artefact: dict[str, Any]) -> tuple[str, bytes]:
        raise NotImplementedError("SftpChannel.fetch() - implement SFTP get")


class RestChannel(IngestChannel):
    """REST API pull."""
    name = "REST"

    def discover(self) -> list[dict[str, Any]]:
        raise NotImplementedError("RestChannel.discover() - implement REST pull")

    def fetch(self, artefact: dict[str, Any]) -> tuple[str, bytes]:
        raise NotImplementedError("RestChannel.fetch() - implement REST fetch")


CHANNELS = [FileDropChannel, EmailGraphChannel, SftpChannel, RestChannel]


# =============================================================================
# Orchestration
# =============================================================================
def run(client_code: str, channel_filter: str | None, ini_path: Path, dry_run: bool) -> int:
    db_cfg = load_db_config(ini_path)
    db = IngestionDb.connect(db_cfg, dry_run=dry_run) if not dry_run else _connect_or_offline(db_cfg)

    found = processed = failed = 0
    try:
        client = db.fetch_client(client_code) if db else None
        if db and not client:
            print(f"[ERROR] Unknown client code: {client_code}", file=sys.stderr)
            return 2

        run_mode = "dry-run" if dry_run else "manual"
        if db:
            db.open_execution(client_code, "INGESTING", run_mode)
            db.log("START", f"Ingestion run for {client_code} (Transaction_ID={db.transaction_id})",
                   detail={"dry_run": dry_run, "channel_filter": channel_filter})

        paths = db.fetch_folder_paths(client_code) if db else {}
        rules = db.fetch_email_rules(client_code) if db else []

        for channel_cls in CHANNELS:
            channel = channel_cls(db, client or {}, paths, rules)
            if channel_filter and channel.name != channel_filter.upper():
                continue
            if not channel.is_enabled():
                continue
            db.log("CHANNEL", f"Channel {channel.name} enabled - SKELETON (not yet implemented)", "WARN")
            # TODO: when a route is implemented, replace this block with:
            #   for artefact in channel.discover():
            #       name, content = channel.fetch(artefact)
            #       file_id = db.land_inbound_file(client_code, channel.name, name, content, ...)
            #       rows = channel.parse_rows(name, content)
            #       processed += db.land_raw_rows(file_id, client_code, rows)

        status = "INGESTED" if failed == 0 else "ERROR"
        if db:
            db.finish_execution(status, found, processed, failed)
            db.log("FINISH", f"Run complete: found={found} processed={processed} failed={failed}")
        print(f"Ingestion summary [{client_code}]: found={found} processed={processed} failed={failed} status={status}")
        return 0 if failed == 0 else 1
    finally:
        if db:
            db.close()


def _connect_or_offline(db_cfg: dict[str, str]) -> "IngestionDb | None":
    """Dry-run: connect if possible, otherwise run offline (no DB writes)."""
    try:
        return IngestionDb.connect(db_cfg, dry_run=True)
    except Exception as error:
        print(f"[WARN] Dry-run offline (no DB): {error}")
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fusion Flow V3 QAS - Module 1 Ingestion (skeleton).")
    p.add_argument("--client", required=True, help="3-letter client code, e.g. BKD")
    p.add_argument("--channel", help="Only run this channel: FILE_DROP | EMAIL | SFTP | REST")
    p.add_argument("--ini", type=Path, default=DEFAULT_INI, help="Path to Fusion_Flow_QAS.ini")
    p.add_argument("--dry-run", action="store_true", help="Discover/report only; write nothing")
    return p


def main() -> int:
    args = build_parser().parse_args()
    return run(args.client.strip().upper(), args.channel, args.ini, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
