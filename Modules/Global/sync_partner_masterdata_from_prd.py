#!/usr/bin/env python3
"""Sync BKD partner masterdata from PRD BKD.Partners into CFG.Partner_Master.

The PRD table is treated as reference/masterdata, not source ingestion. Runtime
processing logs every partner-derived value as MASTERDATA in
EXC.Data_Processing_Enhancement.
"""

from __future__ import annotations

import argparse
import configparser
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
from typing import Any


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
MODULE = "REFERENCE_DATA"
PROCESS = "SYNC_PARTNER_MASTER"


def load_db_config(ini_path: Path) -> dict[str, str]:
    if not ini_path.exists():
        return {}
    cp = configparser.ConfigParser()
    cp.read(ini_path, encoding="utf-8")
    return {k.lower(): v for k, v in cp["database"].items()} if "database" in cp else {}


def read_env_conn(env_path: Path) -> str | None:
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("DB_CONN_STR="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def conn_str(db: dict[str, str]) -> str:
    yes = lambda v: str(v).lower() in ("yes", "true", "1")
    parts = [
        f"Driver={db.get('driver', '{ODBC Driver 17 for SQL Server}')}",
        f"Server={db['server']}",
        f"Database={db['database']}",
    ]
    parts += ([f"Uid={db['user']}", f"Pwd={db.get('password','')}"] if db.get("user")
              else ["Trusted_Connection=yes"])
    parts.append(f"Encrypt={'yes' if yes(db.get('encrypt','yes')) else 'no'}")
    parts.append(f"TrustServerCertificate={'yes' if yes(db.get('trust_server_certificate','no')) else 'no'}")
    return ";".join(parts) + ";"


def connection_string(ini_path: Path) -> str:
    return os.environ.get("DB_CONN_STR") or read_env_conn(DEFAULT_ENV_FILE) or conn_str(load_db_config(ini_path))


def with_database(connection: str, database: str) -> str:
    if re.search(r"(?i)(Database|Initial Catalog)=", connection):
        return re.sub(
            r"(?i)(Database|Initial Catalog)=[^;]*",
            lambda match: f"{match.group(1)}={database}",
            connection,
            count=1,
        )
    return connection.rstrip(";") + f";Database={database};"


def clean(value: Any, max_len: int | None = None) -> str | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    return text[:max_len] if max_len is not None and len(text) > max_len else text


def code(value: Any, max_len: int | None = None) -> str | None:
    text = clean(value, max_len)
    return text.upper() if text is not None else None


def bit(value: Any) -> int:
    text = str(value or "").strip().lower()
    return 0 if text in ("0", "false", "no", "n") else 1


def dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = clean(value)
    if text is None:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_name(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def row_hash(row: dict[str, Any]) -> str:
    parts = [f"{k}={'' if row.get(k) is None else row.get(k)}" for k in sorted(row)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def q(cur, sql: str, *params: Any) -> list[dict[str, Any]]:
    cur.execute(sql, *params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def open_execution(cur, client_code: str) -> int:
    env_rows = q(cur, "SELECT ParameterValue FROM CFG.Application_Parameters WHERE ParameterKey = 'DEFAULT_ENV' AND IsActive = 1")
    env_code = env_rows[0]["ParameterValue"] if env_rows and env_rows[0]["ParameterValue"] else "TST"
    cur.execute(
        "INSERT INTO EXC.Execution (EnvCode, ClientCode, ModuleName, ProcessName, RunMode, Status) "
        "OUTPUT INSERTED.ExecutionID VALUES (?, ?, ?, ?, ?, ?)",
        env_code, client_code, MODULE, PROCESS, "manual", "SYNCING",
    )
    return int(cur.fetchone()[0])


def finish_execution(cur, execution_id: int, status: str, found: int, processed: int, failed: int, error: str = "") -> None:
    cur.execute(
        "UPDATE EXC.Execution SET EndedAt = SYSUTCDATETIME(), Status = ?, "
        "ItemsFound = ?, ItemsProcessed = ?, ItemsFailed = ?, ErrorMessage = ? "
        "WHERE ExecutionID = ?",
        status, found, processed, failed, error or None, execution_id,
    )


def fetch_source_rows(cur, *, active_only: bool) -> list[dict[str, Any]]:
    active_filter = "WHERE active = 1" if active_only else ""
    return q(
        cur,
        f"""
        SELECT id, partner_type, partner_name, eori, eori_gb, vat_number, account_ref,
               address_line1, address_line2, city, county, postcode, country,
               contact_name, contact_email, contact_phone, env_code, source_system,
               source_record_id, active, notes, created_at, updated_at, source_loaded_at
        FROM BKD.Partners
        {active_filter}
        ORDER BY id
        """,
    )


def map_source(row: dict[str, Any], *, client_code: str, source_database: str) -> dict[str, Any] | None:
    partner_name = clean(row.get("partner_name"), 300)
    source_id = clean(row.get("id"), 80)
    if not partner_name or not source_id:
        return None
    mapped = {
        "ClientCode": client_code.upper()[:3],
        "SourceDatabase": source_database,
        "SourceSchema": "BKD",
        "SourceTable": "Partners",
        "SourceID": source_id,
        "PartnerType": clean(row.get("partner_type"), 40),
        "PartnerName": partner_name,
        "NormalizedPartnerName": normalize_name(partner_name)[:300],
        "EORI": code(row.get("eori"), 30),
        "EORIGB": code(row.get("eori_gb"), 30),
        "VATNumber": clean(row.get("vat_number"), 40),
        "AccountRef": clean(row.get("account_ref"), 80),
        "AddressLine1": clean(row.get("address_line1"), 300),
        "AddressLine2": clean(row.get("address_line2"), 300),
        "City": clean(row.get("city"), 120),
        "County": clean(row.get("county"), 120),
        "Postcode": code(row.get("postcode"), 30),
        "Country": code(row.get("country"), 2),
        "ContactName": clean(row.get("contact_name"), 200),
        "ContactEmail": clean(row.get("contact_email"), 320),
        "ContactPhone": clean(row.get("contact_phone"), 80),
        "EnvCode": clean(row.get("env_code"), 10),
        "SourceSystem": clean(row.get("source_system"), 128),
        "SourceRecordID": clean(row.get("source_record_id"), 80),
        "IsActive": bit(row.get("active")),
        "Notes": clean(row.get("notes"), 1000),
        "SourceCreatedAt": dt(row.get("created_at")),
        "SourceUpdatedAt": dt(row.get("updated_at")),
        "SourceLoadedAt": dt(row.get("source_loaded_at")),
    }
    mapped["RowHash"] = row_hash(mapped)
    return mapped


def upsert(cur, row: dict[str, Any]) -> str:
    existing = q(
        cur,
        "SELECT PartnerMasterID, RowHash FROM CFG.Partner_Master "
        "WHERE ClientCode = ? AND SourceTable = ? AND SourceID = ?",
        row["ClientCode"], row["SourceTable"], row["SourceID"],
    )
    cols = list(row)
    if existing:
        if existing[0]["RowHash"] == row["RowHash"]:
            return "unchanged"
        set_cols = [c for c in cols if c not in ("ClientCode", "SourceTable", "SourceID")]
        assignments = ", ".join(f"[{c}] = ?" for c in set_cols) + ", [UpdatedAt] = SYSUTCDATETIME()"
        cur.execute(
            f"UPDATE CFG.Partner_Master SET {assignments} WHERE PartnerMasterID = ?",
            *[row[c] for c in set_cols],
            existing[0]["PartnerMasterID"],
        )
        return "updated"

    col_list = ", ".join(f"[{c}]" for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    cur.execute(f"INSERT INTO CFG.Partner_Master ({col_list}) VALUES ({placeholders})", *[row[c] for c in cols])
    return "inserted"


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync BKD partners from PRD BKD.Partners into CFG.Partner_Master.")
    parser.add_argument("--client-code", default="BKD")
    parser.add_argument("--source-database", default="Fusion_TSS_Automation_PRD")
    parser.add_argument("--target-database", default="", help="Defaults to the DB in DB_CONN_STR / ini.")
    parser.add_argument("--ini", default=str(DEFAULT_INI))
    parser.add_argument("--include-inactive", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import pyodbc

    base_conn = connection_string(Path(args.ini))
    source_conn = pyodbc.connect(with_database(base_conn, args.source_database), autocommit=False)
    target_conn = pyodbc.connect(with_database(base_conn, args.target_database), autocommit=False) if args.target_database else pyodbc.connect(base_conn, autocommit=False)
    source_cur = source_conn.cursor()
    target_cur = target_conn.cursor()
    execution_id = None
    try:
        source_rows = fetch_source_rows(source_cur, active_only=not args.include_inactive)
        mapped_rows = [
            mapped for row in source_rows
            if (mapped := map_source(row, client_code=args.client_code, source_database=args.source_database))
        ]
        found = len(source_rows)
        if args.dry_run:
            print(f"[DRY-RUN] source_rows={found} mapped={len(mapped_rows)}")
            return 0

        execution_id = open_execution(target_cur, args.client_code.upper()[:3])
        stats = {"inserted": 0, "updated": 0, "unchanged": 0, "failed": 0}
        for row in mapped_rows:
            try:
                stats[upsert(target_cur, row)] += 1
            except Exception as exc:
                stats["failed"] += 1
                print(f"[WARN] partner source id {row.get('SourceID')} failed: {exc}")
        status = "COMPLETED" if stats["failed"] == 0 else "COMPLETED_WITH_WARNINGS"
        finish_execution(target_cur, execution_id, status, found, len(mapped_rows), stats["failed"])
        target_conn.commit()
        print(
            f"Partner master sync complete. ExecutionID={execution_id} "
            f"found={found} processed={len(mapped_rows)} failed={stats['failed']} "
            f"inserted={stats['inserted']} updated={stats['updated']} unchanged={stats['unchanged']}"
        )
        return 0 if stats["failed"] == 0 else 1
    except Exception as exc:
        target_conn.rollback()
        if execution_id is not None:
            finish_execution(target_cur, execution_id, "ERROR", 0, 0, 1, str(exc)[:1000])
            target_conn.commit()
        raise
    finally:
        source_cur.close()
        target_cur.close()
        source_conn.close()
        target_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
