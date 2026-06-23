"""Import historical SDI document candidates into BKD masterdata.

This loads the API-derived CSV produced by map_historical_supdec_api.py into
BKD.DocProductCatalogDocuments. Imported rows are active and auto-applied; they
do not introduce any local submit blockers. TSS remains the validator.
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional local dependency
    load_dotenv = None

from app.db import get_standalone_connection  # noqa: E402


DEFAULT_INPUT = ROOT / "artifacts" / "sdi_historical_api_mapping" / "document_candidates_by_commodity.csv"
SOURCE = "TSS_CLOSED_HISTORY_API"


def main() -> int:
    parser = argparse.ArgumentParser(description="Import historical SDI document candidates into BKD masterdata.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="document_candidates_by_commodity.csv path.")
    parser.add_argument("--tenant", default="BKD", help="Tenant/customer code to stamp on imported rows.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this flag, only prints counts.")
    parser.add_argument("--limit", type=int, default=None, help="Optional row limit for controlled runs.")
    args = parser.parse_args()

    if load_dotenv:
        load_dotenv(ROOT / ".env")

    input_path = Path(args.input)
    rows = _load_rows(input_path)
    if args.limit:
        rows = rows[: max(0, args.limit)]

    rows = [row for row in rows if _candidate_is_usable(row)]
    print(f"Loaded {len(rows)} usable document candidates from {input_path}")

    if not args.apply:
        _print_preview(rows)
        print("Dry run only. Re-run with --apply to write BKD.DocProductCatalogDocuments.")
        return 0

    conn = get_standalone_connection()
    cur = conn.cursor()
    try:
        _ensure_target_table_ready(cur)
        columns = _table_columns(cur, "BKD", "DocProductCatalogDocuments")
        inserted = 0
        updated = 0
        for row in rows:
            action = _upsert_candidate(cur, columns, row, tenant=args.tenant)
            inserted += 1 if action == "inserted" else 0
            updated += 1 if action == "updated" else 0
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    print(f"Imported SDI document candidates: inserted={inserted} updated={updated}")
    return 0


def _load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _candidate_is_usable(row: dict[str, Any]) -> bool:
    code = _text(row.get("document_code")).upper()
    return bool(code and code != "N935" and _text(row.get("commodity_code")))


def _print_preview(rows: list[dict[str, Any]]) -> None:
    by_code: dict[str, int] = {}
    for row in rows:
        code = _text(row.get("document_code")).upper()
        by_code[code] = by_code.get(code, 0) + 1
    for code, count in sorted(by_code.items(), key=lambda item: (-item[1], item[0]))[:20]:
        print(f"{code}: {count}")


def _ensure_target_table_ready(cur: Any) -> None:
    cur.execute(
        """
        IF OBJECT_ID(N'[BKD].[DocProductCatalogDocuments]', 'U') IS NULL
            THROW 51097, 'BKD.DocProductCatalogDocuments missing. Run migration 087/097 first.', 1;

        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'commodity_code') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [commodity_code] NVARCHAR(10) NULL;
        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'country_of_origin') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [country_of_origin] NVARCHAR(2) NULL;
        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'source') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [source] NVARCHAR(80) NULL;
        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'source_sup_dec_number') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [source_sup_dec_number] NVARCHAR(80) NULL;
        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'source_tss_status') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [source_tss_status] NVARCHAR(80) NULL;
        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'evidence_count') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [evidence_count] INT NULL;
        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'confirmed_by_history') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [confirmed_by_history] BIT NULL;
        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'requires_compliance_review') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [requires_compliance_review] BIT NULL;
        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'auto_apply_to_sdi') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [auto_apply_to_sdi] BIT NULL;
        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'notes') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [notes] NVARCHAR(1000) NULL;
        IF COL_LENGTH(N'BKD.DocProductCatalogDocuments', N'last_seen_at') IS NULL
            ALTER TABLE [BKD].[DocProductCatalogDocuments] ADD [last_seen_at] DATETIME2(7) NULL;
        """
    )


def _upsert_candidate(cur: Any, columns: dict[str, str], row: dict[str, Any], *, tenant: str) -> str:
    values = {
        "customer_code": tenant,
        "commodity_code": _text(row.get("commodity_code")).replace(" ", ""),
        "country_of_origin": _text(row.get("country_of_origin")).upper(),
        "document_code": _text(row.get("document_code")).upper(),
        "document_status": _none_if_blank(row.get("document_status")),
        "document_reference": _none_if_blank(row.get("document_reference")),
        "document_part": _none_if_blank(row.get("document_part")),
        "document_reason": _none_if_blank(row.get("document_reason")),
        "source": SOURCE,
        "source_sup_dec_number": _first_sup_ref(row.get("sample_sup_refs")),
        "source_tss_status": "Closed",
        "evidence_count": _as_int(row.get("goods_count"), default=1),
        "confirmed_by_history": 1,
        "requires_compliance_review": 0,
        "auto_apply_to_sdi": 1,
        "notes": _notes(row),
        "active": 1,
    }

    key = {
        "customer_code": values["customer_code"],
        "commodity_code": values["commodity_code"],
        "country_of_origin": values["country_of_origin"],
        "document_code": values["document_code"],
        "document_status": values["document_status"],
        "document_reference": values["document_reference"],
        "document_part": values["document_part"],
    }
    filtered_key = {name: value for name, value in key.items() if name in columns}
    where_sql = " AND ".join(f"COALESCE([{columns[name]}], '') = COALESCE(?, '')" for name in filtered_key)
    key_params = list(filtered_key.values())

    cur.execute(f"SELECT TOP 1 [{columns['id']}] FROM [BKD].[DocProductCatalogDocuments] WHERE {where_sql}", key_params)
    existing = cur.fetchone()
    if existing:
        update_values = {
            name: value
            for name, value in values.items()
            if name in columns and name not in {"customer_code", "commodity_code", "country_of_origin", "document_code"}
        }
        update_values["updated_at"] = _sql_now()
        update_values["last_seen_at"] = _sql_now()
        set_sql, params = _set_clause(update_values, columns)
        params.extend(key_params)
        cur.execute(f"UPDATE [BKD].[DocProductCatalogDocuments] SET {set_sql} WHERE {where_sql}", params)
        return "updated"

    insert_values = {name: value for name, value in values.items() if name in columns}
    if "created_at" in columns:
        insert_values["created_at"] = _sql_now()
    if "updated_at" in columns:
        insert_values["updated_at"] = _sql_now()
    if "last_seen_at" in columns:
        insert_values["last_seen_at"] = _sql_now()
    col_sql = ", ".join(f"[{columns[name]}]" for name in insert_values)
    placeholders = ", ".join("SYSUTCDATETIME()" if value is _SQL_NOW else "?" for value in insert_values.values())
    params = [value for value in insert_values.values() if value is not _SQL_NOW]
    cur.execute(f"INSERT INTO [BKD].[DocProductCatalogDocuments] ({col_sql}) VALUES ({placeholders})", params)
    return "inserted"


def _table_columns(cur: Any, schema: str, table: str) -> dict[str, str]:
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [schema, table],
    )
    return {str(row[0]).lower(): str(row[0]) for row in cur.fetchall()}


def _set_clause(values: dict[str, Any], columns: dict[str, str]) -> tuple[str, list[Any]]:
    parts = []
    params: list[Any] = []
    for name, value in values.items():
        if name not in columns:
            continue
        if value is _SQL_NOW:
            parts.append(f"[{columns[name]}] = SYSUTCDATETIME()")
        else:
            parts.append(f"[{columns[name]}] = ?")
            params.append(value)
    return ", ".join(parts), params


def _notes(row: dict[str, Any]) -> str:
    sup_count = _text(row.get("sup_count"))
    samples = _text(row.get("sample_sup_refs"))
    generated = datetime.now(UTC).date().isoformat()
    text = (
        f"Auto-applied from TSS closed SUP DEC API history on {generated}; "
        f"evidence_sup_count={sup_count or '0'}; sample_sup_refs={samples[:700]}"
    )
    return text[:1000]


def _first_sup_ref(value: Any) -> str | None:
    refs = [part.strip() for part in _text(value).split("|") if part.strip()]
    return refs[0] if refs else None


def _none_if_blank(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _as_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(float(_text(value)))
    except Exception:
        return default


def _text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


class _SqlNow:
    pass


_SQL_NOW = _SqlNow()


def _sql_now() -> _SqlNow:
    return _SQL_NOW


if __name__ == "__main__":
    raise SystemExit(main())
