"""Copy retained BKD masterdata from Fusion_TSS to Fusion_TSS_Automation_PRD.

This bridge is intentionally narrow. It copies only the clean production
masterdata/config tables:

  BKD.AppConfiguration
  BKD.CompanyMaster
  BKD.Partners
  BKD.DocProductCatalog

It does not copy BKD.Staging*, old mirrors, old logs, or document-ingest tables.
By default it runs as a dry-run. Use --execute to apply changes.
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any

import pyodbc
from dotenv import load_dotenv


DEFAULT_SOURCE_DB = "Fusion_TSS"
DEFAULT_TARGET_DB = "Fusion_TSS_Automation_PRD"
SCHEMA = "BKD"
TABLES = ("AppConfiguration", "CompanyMaster", "Partners", "DocProductCatalog")


def q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def connection_string(database: str) -> str:
    load_dotenv(dotenv_path=Path(".env"))
    base = os.environ.get("DB_CONN_STR", "")
    if not base:
        raise RuntimeError("DB_CONN_STR is not configured")
    return re.sub(r"DATABASE=[^;]*;", f"DATABASE={database};", base, flags=re.I)


def connect(database: str) -> pyodbc.Connection:
    return pyodbc.connect(connection_string(database), autocommit=False)


def columns(conn: pyodbc.Connection, table: str) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.name
        FROM sys.columns c
        JOIN sys.tables t ON t.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
          AND c.is_identity = 0
          AND c.is_computed = 0
        ORDER BY c.column_id
        """,
        [SCHEMA, table],
    )
    return [row[0] for row in cur.fetchall()]


def table_exists(conn: pyodbc.Connection, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*)
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
        """,
        [SCHEMA, table],
    )
    return cur.fetchone()[0] == 1


def fetch_rows(conn: pyodbc.Connection, table: str, cols: list[str]) -> list[dict[str, Any]]:
    if not cols:
        return []
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(q(c) for c in cols)} FROM {q(SCHEMA)}.{q(table)}")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def clean(value: Any) -> str:
    return str(value or "").strip()


def has_value(value: Any) -> bool:
    return clean(value) != ""


def first_value(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if has_value(value):
            return value
    return None


def row_keys(table: str, row: dict[str, Any], target_cols: set[str]) -> list[tuple[str, tuple[Any, ...]]]:
    if table == "AppConfiguration":
        return [("category_key", (clean(row.get("category")).upper(), clean(row.get("config_key")).upper()))]

    if table == "CompanyMaster":
        keys: list[tuple[str, tuple[Any, ...]]] = []
        for col in ("eori_xi", "eori_gb"):
            value = row.get(col)
            if col in target_cols and has_value(value):
                keys.append((col, (clean(value).upper(),)))
        if "company_name" in target_cols and has_value(row.get("company_name")):
            keys.append(("company_name", (clean(row["company_name"]).upper(),)))
        return keys

    if table == "Partners":
        partner_type = row.get("partner_type")
        account_ref = row.get("account_ref")
        if "account_ref" in target_cols and has_value(partner_type) and has_value(account_ref):
            return [("partner_account", (clean(partner_type).upper(), clean(account_ref).upper()))]
        eori = first_value(row, "eori", "eori_gb")
        if has_value(partner_type) and has_value(eori):
            if "eori" in target_cols:
                return [("partner_eori", (clean(partner_type).upper(), clean(eori).upper()))]
        if has_value(partner_type) and has_value(row.get("partner_name")):
            return [("partner_name", (clean(partner_type).upper(), clean(row["partner_name"]).upper()))]

    if table == "DocProductCatalog":
        customer_code = row.get("customer_code")
        sku = row.get("sku")
        if has_value(customer_code) and has_value(sku):
            return [("catalog_customer_sku", (clean(customer_code).upper(), clean(sku).upper()))]
        if has_value(sku):
            return [("catalog_sku", (clean(sku).upper(),))]
        for col in ("product_code", "barcode"):
            if col in target_cols and has_value(row.get(col)):
                return [(f"catalog_{col}", (clean(row[col]).upper(),))]

    return []


def existing_index(target_rows: list[dict[str, Any]], table: str, target_cols: set[str]) -> dict[tuple[str, tuple[Any, ...]], int]:
    index: dict[tuple[str, tuple[Any, ...]], int] = {}
    for row in target_rows:
        row_id = row.get("id")
        if row_id is None:
            continue
        for key in row_keys(table, row, target_cols):
            if all(has_value(v) for v in key[1]):
                index.setdefault(key, int(row_id))
    return index


def find_existing_id(index: dict[tuple[str, tuple[Any, ...]], int], keys: list[tuple[str, tuple[Any, ...]]]) -> int | None:
    for key in keys:
        row_id = index.get(key)
        if row_id is not None:
            return row_id
    return None


def should_copy_config_value(existing: bool, overwrite_config_values: bool) -> bool:
    return (not existing) or overwrite_config_values


def execute_many_with_duplicate_skip(cur: pyodbc.Cursor, sql: str, payloads: list[list[Any]]) -> None:
    """Run a batch, falling back to row-by-row if SQL Server reports duplicates."""
    try:
        cur.executemany(sql, payloads)
        return
    except pyodbc.IntegrityError:
        pass

    for payload in payloads:
        try:
            cur.execute(sql, payload)
        except pyodbc.IntegrityError as exc:
            message = str(exc)
            if "2627" in message or "2601" in message or "UNIQUE KEY" in message:
                continue
            raise


def migrate_table(
    source: pyodbc.Connection,
    target: pyodbc.Connection,
    table: str,
    *,
    execute: bool,
    overwrite_config_values: bool,
) -> dict[str, int | str]:
    if not table_exists(source, table):
        return {"table": table, "status": "source_missing", "source": 0, "inserted": 0, "updated": 0, "skipped": 0}
    if not table_exists(target, table):
        return {"table": table, "status": "target_missing", "source": 0, "inserted": 0, "updated": 0, "skipped": 0}

    source_cols = columns(source, table)
    target_cols = columns(target, table)
    target_cols_with_id = ["id"] + [c for c in target_cols if c != "id"]
    common = [c for c in source_cols if c in set(target_cols) and c != "id"]
    rows = fetch_rows(source, table, common)
    target_rows = fetch_rows(target, table, target_cols_with_id)

    inserted = updated = skipped = 0
    target_col_set = set(target_cols)
    target_index = existing_index(target_rows, table, target_col_set)
    insert_batches: dict[tuple[str, ...], list[list[Any]]] = {}
    update_batches: dict[tuple[str, ...], list[list[Any]]] = {}

    for row in rows:
        keys = row_keys(table, row, target_col_set)
        row_id = find_existing_id(target_index, keys)
        if row_id is not None and row_id < 0:
            skipped += 1
            continue
        is_existing = row_id is not None

        if is_existing:
            key_columns = {
                "category",
                "config_key",
                "company_name",
                "eori_xi",
                "eori_gb",
                "partner_type",
                "account_ref",
                "eori",
                "partner_name",
                "customer_code",
                "sku",
                "product_code",
                "barcode",
            }
            update_cols = [
                c for c in common
                if c not in {"id", "created_at"}
                and c not in key_columns
            ]
            if table == "AppConfiguration" and not should_copy_config_value(True, overwrite_config_values):
                update_cols = [c for c in update_cols if c != "config_value"]
            if not update_cols:
                skipped += 1
                continue
            if execute:
                update_batches.setdefault(tuple(update_cols), []).append(
                    [row.get(c) for c in update_cols] + [row_id]
                )
            updated += 1
            continue

        insert_cols = [c for c in common if c != "id"]
        if table == "AppConfiguration" and not should_copy_config_value(False, overwrite_config_values):
            pass
        if not keys:
            skipped += 1
            continue
        if execute:
            insert_batches.setdefault(tuple(insert_cols), []).append([row.get(c) for c in insert_cols])
        fake_id = -inserted - 1
        for key in keys:
            target_index.setdefault(key, fake_id)
        inserted += 1

    if execute:
        cur = target.cursor()
        cur.fast_executemany = True
        for insert_cols, payloads in insert_batches.items():
            if not payloads:
                continue
            sql = (
                f"INSERT INTO {q(SCHEMA)}.{q(table)} "
                f"({', '.join(q(c) for c in insert_cols)}) "
                f"VALUES ({', '.join('?' for _ in insert_cols)})"
            )
            execute_many_with_duplicate_skip(cur, sql, payloads)
        for update_cols, payloads in update_batches.items():
            if not payloads:
                continue
            sql = (
                f"UPDATE {q(SCHEMA)}.{q(table)} "
                f"SET {', '.join(q(c) + ' = ?' for c in update_cols)} "
                "WHERE [id] = ?"
            )
            cur.executemany(sql, payloads)

    return {
        "table": table,
        "status": "ok",
        "source": len(rows),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=DEFAULT_SOURCE_DB)
    parser.add_argument("--target-db", default=DEFAULT_TARGET_DB)
    parser.add_argument("--execute", action="store_true", help="Apply changes. Default is dry-run.")
    parser.add_argument(
        "--overwrite-config-values",
        action="store_true",
        help="Overwrite AppConfiguration config_value on existing target rows.",
    )
    parser.add_argument("--tables", nargs="*", default=list(TABLES), choices=TABLES)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(
        f"BKD masterdata bridge: {args.source_db} -> {args.target_db} "
        f"({'EXECUTE' if args.execute else 'DRY-RUN'})"
    , flush=True)

    with connect(args.source_db) as source, connect(args.target_db) as target:
        results = []
        for table in args.tables:
            results.append(
                migrate_table(
                    source,
                    target,
                    table,
                    execute=args.execute,
                    overwrite_config_values=args.overwrite_config_values,
                )
            )
        if args.execute:
            target.commit()
        else:
            target.rollback()

    for result in results:
        print(
            f"{result['table']}: {result['status']} "
            f"source={result['source']} inserted={result['inserted']} "
            f"updated={result['updated']} skipped={result['skipped']}"
            ,
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
