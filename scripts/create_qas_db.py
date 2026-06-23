"""Create Fusion_TSS_Automation_QAS as a copy of Fusion_TSS_Automation_PRD.

Uses Azure SQL's native CREATE DATABASE ... AS COPY OF command.
Runs against the master database on the same server.
Safe: does not touch PRD. Creates QAS as a new independent database.

Usage:
    python scripts/create_qas_db.py [--check]

    --check   Only check copy progress (do not start a new copy).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT, ".env"))
except Exception:
    pass

import pyodbc

SOURCE_DB = "Fusion_TSS_Automation_PRD"
TARGET_DB = "Fusion_TSS_Automation_QAS"


def _master_conn_str() -> str:
    """Derive a master-database connection string from the current env."""
    raw = os.environ.get("DB_CONN_STR") or os.environ.get("AZURE_SQL_CONNECTION_STRING") or ""

    if raw:
        # Replace the Database= value with master
        patched = re.sub(r"(?i)(Database|Initial\s+Catalog)\s*=\s*[^;]+", r"\1=master", raw)
        if patched == raw:
            patched = raw.rstrip(";") + ";Database=master"
        return patched

    # Build from individual env vars
    server   = os.environ.get("AZURE_SQL_SERVER") or os.environ.get("DB_SERVER", "")
    user     = os.environ.get("AZURE_SQL_USERNAME") or os.environ.get("DB_USER", "")
    password = os.environ.get("AZURE_SQL_PASSWORD") or os.environ.get("DB_PASSWORD", "")
    driver   = os.environ.get("ODBC_DRIVER") or os.environ.get("DB_DRIVER") or "ODBC Driver 17 for SQL Server"

    if not server or not user or not password:
        raise RuntimeError(
            "Cannot derive server credentials. Set DB_CONN_STR or "
            "AZURE_SQL_SERVER/AZURE_SQL_USERNAME/AZURE_SQL_PASSWORD in .env"
        )
    return (
        f"DRIVER={{{driver}}};SERVER={server};DATABASE=master;"
        f"UID={user};PWD={password};Encrypt=yes;TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )


def check_progress(cur) -> dict | None:
    """Query sys.dm_database_copies for copy state."""
    cur.execute("""
        SELECT
            d.name AS source_db,
            dc.partner_database AS target_db,
            dc.replication_state_desc,
            dc.percent_complete
        FROM sys.dm_database_copies dc
        JOIN sys.databases d ON d.database_id = dc.database_id
        WHERE dc.partner_database = ?
    """, [TARGET_DB])
    row = cur.fetchone()
    if row:
        return {
            "source": row[0],
            "target": row[1],
            "state": row[2],
            "pct": row[3],
        }
    return None


def target_exists(cur) -> bool:
    cur.execute("SELECT 1 FROM sys.databases WHERE name = ?", [TARGET_DB])
    return cur.fetchone() is not None


def start_copy(cur) -> None:
    sql = f"CREATE DATABASE [{TARGET_DB}] AS COPY OF [{SOURCE_DB}]"
    print(f"Executing: {sql}")
    cur.execute(sql)
    print("Copy initiated — Azure SQL is now cloning the database asynchronously.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", action="store_true", help="Check copy progress only.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    conn_str = _master_conn_str()

    print(f"Connecting to master on the same server as {SOURCE_DB}...")
    conn = pyodbc.connect(conn_str, autocommit=True, timeout=60)
    cur = conn.cursor()

    if args.check:
        prog = check_progress(cur)
        if prog:
            print(f"Copy in progress: {prog['pct']:.1f}% — state={prog['state']}")
        elif target_exists(cur):
            print(f"{TARGET_DB} exists and is fully copied (no active copy operation).")
        else:
            print(f"{TARGET_DB} does not exist yet and no copy is running.")
        conn.close()
        return 0

    # Check if already exists
    if target_exists(cur):
        print(f"{TARGET_DB} already exists.")
        prog = check_progress(cur)
        if prog:
            print(f"  Copy still in progress: {prog['pct']:.1f}% — state={prog['state']}")
        else:
            print("  Database is fully available.")
        conn.close()
        return 0

    # Check if copy already running
    prog = check_progress(cur)
    if prog:
        print(f"Copy already in progress: {prog['pct']:.1f}% — state={prog['state']}")
        conn.close()
        return 0

    start_copy(cur)

    # Poll for a few seconds to confirm it started
    print("\nWaiting 5s to confirm copy started...")
    time.sleep(5)
    prog = check_progress(cur)
    if prog:
        print(f"Confirmed: {prog['pct']:.1f}% — state={prog['state']}")
        print(f"\nRun with --check to monitor progress.")
        print(f"Azure SQL copies typically take 1-10 minutes depending on DB size.")
    elif target_exists(cur):
        print("DB already fully copied (very fast — small DB).")
    else:
        print("Copy command sent. Run --check in a minute to verify.")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
