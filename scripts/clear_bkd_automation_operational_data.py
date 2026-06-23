"""Clear BKD automation operational data while preserving master data.

Manual production reset helper. The script is deliberately allowlisted: it only
touches runtime ING/STG/TSS records and old operational BKD queues/logs. It does
not delete AppConfiguration, CompanyMaster, Partners, DocProductCatalog, or
product/CV/master-data tables.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional local convenience
    load_dotenv = None

if load_dotenv:
    load_dotenv(os.path.join(REPO_ROOT, ".env"))

from app.db import get_standalone_connection  # noqa: E402


CONFIRMATION = "DELETE_BKD_AUTOMATION_OPERATIONAL_DATA"
DEFAULT_EXPECTED_DB = "Fusion_TSS_Automation_PRD"


@dataclass(frozen=True)
class TargetTable:
    schema: str
    table: str
    order: int
    reset_identity: bool = True


TARGET_TABLES: tuple[TargetTable, ...] = (
    # STG runtime pipeline state, child tables first.
    TargetTable("STG", "BKD_SDI_GoodsItems", 5),
    TargetTable("STG", "BKD_GoodsItems", 10),
    TargetTable("STG", "BKD_SFD_Tracking", 20),
    TargetTable("STG", "BKD_SDI_Headers", 25),
    TargetTable("STG", "BKD_ENS_Consignments", 30),
    TargetTable("STG", "BKD_GMR_Movements", 50),
    TargetTable("STG", "BKD_IMMI_Tracking", 60),
    TargetTable("STG", "BKD_ENS_Headers", 70),
    # ING source files, emails, process trace, and imported order rows.
    TargetTable("ING", "BKD_ProcessLog", 100),
    TargetTable("ING", "BKD_EmailAttachment", 110),
    TargetTable("ING", "BKD_EmailMessage", 120),
    TargetTable("ING", "BKD_SalesOrderLine", 130),
    TargetTable("ING", "BKD_SourceFileLog", 170),
    # TSS mirrors, outbox/jobs, and API/notification logs.
    TargetTable("TSS", "BKD_SDI_GoodsItems", 195),
    TargetTable("TSS", "BKD_GoodsItems", 200),
    TargetTable("TSS", "BKD_SFD", 210),
    TargetTable("TSS", "BKD_ENS_Consignments", 220),
    TargetTable("TSS", "BKD_SDI_Headers", 230),
    TargetTable("TSS", "BKD_GMR_Movements", 240),
    TargetTable("TSS", "BKD_ENS_Headers", 250),
    TargetTable("TSS", "BKD_API_Outbox", 260),
    TargetTable("TSS", "BKD_JobRuns", 270),
    TargetTable("TSS", "BKD_API_Exchanges", 280),
    # Old BKD operational tables/queues if still present after cleanup migrations.
    TargetTable("BKD", "DocIngestValidation", 300),
    TargetTable("BKD", "DocIngestRecognition", 310),
    TargetTable("BKD", "DocIngestSummary", 320),
    TargetTable("BKD", "DocIngestLine", 330),
    TargetTable("BKD", "DocIngestHeader", 340),
    TargetTable("BKD", "DocIngestDocument", 350),
    TargetTable("BKD", "DocMappingRequest", 360),
    TargetTable("BKD", "MessageOutbox", 370),
    TargetTable("BKD", "JobRunLog", 380),
    TargetTable("BKD", "PollingTracker", 390),
    TargetTable("BKD", "ApiCallLog", 400),
    TargetTable("BKD", "SdiDeadlineAlerts", 410),
    TargetTable("BKD", "StagingGoodsItems", 420),
    TargetTable("BKD", "StagingConsignments", 430),
    TargetTable("BKD", "StagingEnsHeaders", 440),
    TargetTable("BKD", "StagingSupDecGoods", 450),
    TargetTable("BKD", "StagingSupDecHeaders", 460),
    TargetTable("BKD", "StagingDeclarations", 470),
    TargetTable("BKD", "StagingGmrs", 480),
    TargetTable("BKD", "Immis", 490),
    TargetTable("BKD", "Consignments", 500),
    TargetTable("BKD", "Sfds", 510),
    TargetTable("BKD", "EnsConsignments", 520),
    TargetTable("BKD", "EnsHeaders", 530),
)


def _q(schema: str, table: str) -> str:
    return f"[{schema}].[{table}]"


def _table_exists(cursor, target: TargetTable) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
        """,
        target.schema,
        target.table,
    )
    return cursor.fetchone() is not None


def _has_identity(cursor, target: TargetTable) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM sys.identity_columns ic
        JOIN sys.tables t ON t.object_id = ic.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
        """,
        target.schema,
        target.table,
    )
    return cursor.fetchone() is not None


def _row_count(cursor, target: TargetTable) -> int:
    cursor.execute(f"SELECT COUNT_BIG(*) FROM {_q(target.schema, target.table)}")
    return int(cursor.fetchone()[0] or 0)


def _db_name(cursor) -> str:
    cursor.execute("SELECT DB_NAME()")
    return str(cursor.fetchone()[0] or "")


def _collect_existing_targets(cursor) -> list[TargetTable]:
    existing: list[TargetTable] = []
    for target in sorted(TARGET_TABLES, key=lambda item: item.order):
        if _table_exists(cursor, target):
            existing.append(target)
    return existing


def _print_counts(cursor, targets: list[TargetTable]) -> int:
    total = 0
    for target in targets:
        count = _row_count(cursor, target)
        total += count
        print(f"{target.schema}.{target.table}: {count}")
    print(f"TOTAL operational rows selected for cleanup: {total}")
    return total


def _disable_constraints(cursor, targets: list[TargetTable]) -> None:
    for target in targets:
        cursor.execute(f"ALTER TABLE {_q(target.schema, target.table)} NOCHECK CONSTRAINT ALL")


def _enable_constraints(cursor, targets: list[TargetTable]) -> None:
    for target in reversed(targets):
        cursor.execute(f"ALTER TABLE {_q(target.schema, target.table)} WITH CHECK CHECK CONSTRAINT ALL")


def _delete_targets(cursor, targets: list[TargetTable]) -> None:
    for target in targets:
        cursor.execute(f"DELETE FROM {_q(target.schema, target.table)}")
        deleted = cursor.rowcount if cursor.rowcount is not None else -1
        print(f"Deleted {deleted} row(s) from {target.schema}.{target.table}")
        if target.reset_identity and _has_identity(cursor, target):
            cursor.execute(f"DBCC CHECKIDENT ('{target.schema}.{target.table}', RESEED, 0) WITH NO_INFOMSGS")
            print(f"Reseeded identity on {target.schema}.{target.table}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clear BKD operational automation rows while preserving master data.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete rows. Without this flag the script only prints counts.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Required with --apply. Exact value: {CONFIRMATION}",
    )
    parser.add_argument(
        "--expected-db",
        default=DEFAULT_EXPECTED_DB,
        help=f"Abort unless connected to this database. Default: {DEFAULT_EXPECTED_DB}",
    )
    parser.add_argument(
        "--database",
        default="",
        help=(
            "Force a database name using the split AZURE_SQL_* variables and ignore "
            "DB_CONN_STR/ODBC_CONNECTION_STRING for this run."
        ),
    )
    parser.add_argument(
        "--allow-any-db",
        action="store_true",
        help="Skip the database-name guard. Intended only for disposable local databases.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.apply and args.confirm != CONFIRMATION:
        print(f"Refusing to apply. Pass --confirm {CONFIRMATION!r}.")
        return 2
    if args.database:
        os.environ["DB_CONN_STR"] = ""
        os.environ["ODBC_CONNECTION_STRING"] = ""
        os.environ["AZURE_SQL_DATABASE"] = args.database
        os.environ["DB_NAME"] = args.database

    conn = get_standalone_connection()
    try:
        cursor = conn.cursor()
        database = _db_name(cursor)
        print(f"Connected database: {database}")
        if not args.allow_any_db and database.lower() != args.expected_db.lower():
            print(f"Refusing to run on {database!r}; expected {args.expected_db!r}.")
            return 3

        targets = _collect_existing_targets(cursor)
        print(f"Existing target tables: {len(targets)} / {len(TARGET_TABLES)}")
        _print_counts(cursor, targets)

        if not args.apply:
            print("DRY RUN ONLY: no rows deleted.")
            return 0

        print("Applying destructive operational-data cleanup...")
        _disable_constraints(cursor, targets)
        _delete_targets(cursor, targets)
        _enable_constraints(cursor, targets)
        conn.commit()
        print("Cleanup committed. Master/config/product/CV tables were not targeted.")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
