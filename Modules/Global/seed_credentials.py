#!/usr/bin/env python3
"""Fusion Flow V3 QAS - seed TSS credentials & environments into the database.

Run as part of deploy. Reads the credential matrix from the gitignored
Configuration\\tss_credentials.json and upserts it (including passwords) into
CFG.TSS_Credential, and the (non-secret) environments into CFG.TSS_Environment.

This is how "everything lives in the database" without committing any password
to git: the secrets come from the local JSON at deploy time, not from a
committed SQL seed. The tables' DDL lives in 009_cfg_tss_credentials_environments.sql
(and is also ensured here so the seeder is self-sufficient on a fresh DB).

Connection comes from Configuration\\Fusion_Flow_QAS.ini ([database]).

Usage:
  python seed_credentials.py            # ensure tables + upsert envs + creds
  python seed_credentials.py --dry-run  # show what would be seeded; no writes
"""

from __future__ import annotations

import argparse
import configparser
import json
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
CRED_FILE = REPO_ROOT / "Configuration" / "tss_credentials.json"
ENV_FILE = REPO_ROOT / ".env"


def _read_env_db_name() -> str:
    """Parse DATABASE=<name> from DB_CONN_STR in .env. Falls back to 'Fusion_Flow_V3_QAS'."""
    if not ENV_FILE.exists():
        return "Fusion_Flow_V3_QAS"
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("DB_CONN_STR"):
            m = re.search(r"DATABASE=([^;\"]+)", line, re.IGNORECASE)
            if m:
                return m.group(1).strip()
    return "Fusion_Flow_V3_QAS"


def _build_environments() -> list[dict]:
    db_name = _read_env_db_name()
    return [
        {"EnvCode": "PRD", "EnvName": "Production", "BaseUrl": "https://api.tradersupportservice.co.uk/api",
         "Description": "Live HMRC submissions. Use only after go-live approval. Outbound email active.",
         "DatabaseName": "Fusion_TSS_PRD", "Notes": "Live HMRC submissions", "IsActive": 1},
        {"EnvCode": "TST", "EnvName": "Test", "BaseUrl": "https://api.tsstestenv.co.uk/api",
         "Description": "Integrated to HMRC CDS Trader Dress Rehearsal. Outbound email disabled.",
         "DatabaseName": db_name, "Notes": "CDS Trader Dress Rehearsal", "IsActive": 1},
    ]

ENSURE_DDL = """
IF SCHEMA_ID('CFG') IS NULL EXEC('CREATE SCHEMA CFG');
IF OBJECT_ID('CFG.TSS_Environment','U') IS NULL
CREATE TABLE CFG.TSS_Environment (
    EnvCode varchar(10) NOT NULL CONSTRAINT PK_CFG_TSS_Environment PRIMARY KEY,
    EnvName nvarchar(50) NOT NULL, BaseUrl nvarchar(200) NOT NULL, Description nvarchar(500) NULL,
    DatabaseName nvarchar(128) NULL, Notes nvarchar(500) NULL,
    IsActive bit NOT NULL DEFAULT(1), CreatedAt datetime2(3) NOT NULL DEFAULT(SYSUTCDATETIME()));
IF OBJECT_ID('CFG.TSS_Credential','U') IS NULL
CREATE TABLE CFG.TSS_Credential (
    ClientCode char(3) NOT NULL, EnvCode varchar(10) NOT NULL, TssUsername varchar(64) NOT NULL,
    TssPassword nvarchar(256) NULL, IsActive bit NOT NULL DEFAULT(1), LastVerified datetime2(7) NULL,
    LastStatus varchar(10) NULL, HttpStatus int NULL, UpdatedAt datetime2(3) NOT NULL DEFAULT(SYSUTCDATETIME()),
    CONSTRAINT PK_CFG_TSS_Credential PRIMARY KEY (ClientCode, EnvCode));
"""


def load_db_config(ini_path: Path) -> dict[str, str]:
    if not ini_path.exists():
        raise SystemExit(f"Missing connection file: {ini_path}")
    cp = configparser.ConfigParser(); cp.read(ini_path, encoding="utf-8")
    if "database" not in cp:
        raise SystemExit(f"No [database] section in {ini_path}")
    return {k.lower(): v for k, v in cp["database"].items()}


def conn_str(db: dict[str, str]) -> str:
    yes = lambda v: str(v).lower() in ("yes", "true", "1")
    parts = [f"Driver={db.get('driver', '{ODBC Driver 17 for SQL Server}')}",
             f"Server={db['server']}", f"Database={db['database']}"]
    parts += ([f"Uid={db['user']}", f"Pwd={db.get('password','')}"] if db.get("user") else ["Trusted_Connection=yes"])
    parts.append(f"Encrypt={'yes' if yes(db.get('encrypt','yes')) else 'no'}")
    parts.append(f"TrustServerCertificate={'yes' if yes(db.get('trust_server_certificate','no')) else 'no'}")
    return ";".join(parts) + ";"


def load_creds() -> list[dict[str, Any]]:
    if not CRED_FILE.exists():
        raise SystemExit(f"Missing {CRED_FILE}. Create it from Modules/Global/tss_credentials.example.json.")
    data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    return data.get("credentials", data if isinstance(data, list) else [])


def upsert(cur, table: str, keys: list[str], cols: dict[str, Any], touch: str | None = None) -> None:
    """Update matching row(s) else insert. `touch`, if given, is a column set to
    SYSUTCDATETIME() on update (e.g. UpdatedAt)."""
    set_cols = [c for c in cols if c not in keys]
    set_clause = ", ".join(f"{c} = ?" for c in set_cols) + (f", {touch} = SYSUTCDATETIME()" if touch else "")
    where = " AND ".join(f"{k} = ?" for k in keys)
    cur.execute(f"UPDATE {table} SET {set_clause} WHERE {where}",
                *[cols[c] for c in set_cols], *[cols[k] for k in keys])
    if cur.rowcount == 0:
        names = list(cols)
        cur.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                    *[cols[n] for n in names])


def main() -> int:
    p = argparse.ArgumentParser(description="Seed TSS credentials + environments into the database.")
    p.add_argument("--ini", type=Path, default=DEFAULT_INI)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    environments = _build_environments()
    creds = load_creds()
    print(f"Environments: {len(environments)}; Credentials from JSON: {len(creds)}")
    if args.dry_run:
        for c in creds:
            print(f"  would upsert {c.get('client_code')}/{c.get('env_code')} user={c.get('username')} "
                  f"pwd={'set' if c.get('password') else 'MISSING'} active={c.get('active')}")
        return 0

    import pyodbc
    conn = pyodbc.connect(conn_str(load_db_config(args.ini)), autocommit=False)
    try:
        cur = conn.cursor()
        for stmt in [s for s in ENSURE_DDL.split("\n\n") if s.strip()]:
            cur.execute(stmt)
        conn.commit()

        for env in environments:
            upsert(cur, "CFG.TSS_Environment", ["EnvCode"], dict(env))
        for c in creds:
            upsert(cur, "CFG.TSS_Credential", ["ClientCode", "EnvCode"], {
                "ClientCode": c.get("client_code"), "EnvCode": c.get("env_code"),
                "TssUsername": c.get("username"), "TssPassword": c.get("password"),
                "IsActive": 1 if str(c.get("active")).lower() in ("1", "true", "yes") else 0}, touch="UpdatedAt")
        conn.commit()
        print(f"Seeded {len(environments)} environment(s) and {len(creds)} credential(s) into CFG.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
