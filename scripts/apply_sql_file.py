"""Apply one SQL file using the repository database connection.

This is intentionally narrower than ``manage.py db migrate``: it runs exactly
one file and prints the target database before executing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.db_connection import build_connection_string


def _batches(sql_text: str) -> list[str]:
    return [
        batch.strip()
        for batch in re.split(r"(?im)^\s*GO\s*$", sql_text or "")
        if batch.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply exactly one SQL file.")
    parser.add_argument("sql_file", help="Path to the SQL file to apply.")
    parser.add_argument("--execute", action="store_true", help="Actually execute the SQL.")
    parser.add_argument(
        "--verify-sdi-timestamps",
        action="store_true",
        help="Verify SDI timestamp columns after applying.",
    )
    args = parser.parse_args()

    sql_path = Path(args.sql_file).resolve()
    if not sql_path.exists():
        raise SystemExit(f"SQL file not found: {sql_path}")

    sql_text = sql_path.read_text(encoding="utf-8")
    batches = _batches(sql_text)
    if not batches:
        raise SystemExit(f"No executable SQL batches found: {sql_path}")

    load_dotenv()
    conn = pyodbc.connect(build_connection_string(timeout=30), autocommit=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DB_NAME()")
        database_name = cursor.fetchone()[0]
        print(f"database={database_name}")
        print(f"file={sql_path.name}")
        print(f"batches={len(batches)}")

        if args.execute:
            for idx, batch in enumerate(batches, start=1):
                cursor.execute(batch)
                while cursor.nextset():
                    pass
                print(f"applied_batch={idx}")
        else:
            print("dry_run=true")

        if args.verify_sdi_timestamps:
            cursor.execute(
                """
                SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = 'STG'
                  AND TABLE_NAME IN ('BKD_SDI_Headers', 'BKD_SDI_GoodsItems')
                  AND COLUMN_NAME IN ('created_at', 'updated_at')
                ORDER BY TABLE_NAME, COLUMN_NAME
                """
            )
            rows = cursor.fetchall()
            print(
                "columns="
                + ", ".join(
                    f"{row.TABLE_SCHEMA}.{row.TABLE_NAME}.{row.COLUMN_NAME}"
                    for row in rows
                )
            )
            cursor.execute(
                """
                SELECT
                    'STG.BKD_SDI_Headers' AS table_name,
                    COUNT(*) AS total_rows,
                    SUM(CASE WHEN updated_at IS NULL THEN 1 ELSE 0 END) AS null_updated_at
                FROM [STG].[BKD_SDI_Headers]
                UNION ALL
                SELECT
                    'STG.BKD_SDI_GoodsItems' AS table_name,
                    COUNT(*) AS total_rows,
                    SUM(CASE WHEN updated_at IS NULL THEN 1 ELSE 0 END) AS null_updated_at
                FROM [STG].[BKD_SDI_GoodsItems]
                """
            )
            for row in cursor.fetchall():
                print(
                    f"{row.table_name}: total_rows={row.total_rows}, "
                    f"null_updated_at={row.null_updated_at or 0}"
                )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
