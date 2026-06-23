#!/usr/bin/env python3
"""Import BKD unit weights from parsed commercial invoice PDFs.

Default mode is a dry run. Pass --execute to write missing weights into
BKD.DocProductCatalog. Existing non-zero catalog weights are not overwritten
unless --update-existing is supplied.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import sys
from typing import Iterable

import pyodbc
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingestion.parser import ParsedInvoice, ParsedInvoiceLine, parse_birkdale_invoice_file
from config.db_connection import build_connection_string


APP_SCHEMA = "BKD"


@dataclass
class PdfWeightSample:
    sku: str
    description: str
    commodity_code: str
    gross_unit_kg: Decimal
    net_unit_kg: Decimal
    source_file: str


@dataclass
class PdfWeightPayload:
    sku: str
    description: str
    commodity_code: str
    gross_weight_kg: Decimal
    net_weight_kg: Decimal
    sample_count: int
    source_files: list[str]


def q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def parse_decimal(value: object) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def quantize_kg(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def discover_pdfs(paths: Iterable[str]) -> list[Path]:
    pdfs: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            pdfs.extend(sorted(path.glob("*.pdf")))
        elif path.suffix.lower() == ".pdf":
            pdfs.append(path)
    return pdfs


def sample_from_line(line: ParsedInvoiceLine, source_file: str) -> PdfWeightSample | None:
    sku = (line.stock_code or "").strip().upper()
    commodity = (line.commodity_code or "").strip().replace(" ", "")
    qty = parse_decimal(line.quantity)
    gross = parse_decimal(line.gross_mass_kg)
    net = parse_decimal(line.net_mass_kg)
    if not sku or not commodity or qty is None or gross is None or net is None:
        return None
    if qty <= 0 or gross <= 0 or net <= 0 or net > gross:
        return None
    return PdfWeightSample(
        sku=sku,
        description=(line.description or sku).strip(),
        commodity_code=commodity,
        gross_unit_kg=gross / qty,
        net_unit_kg=net / qty,
        source_file=source_file,
    )


def build_weight_payloads(invoices: Iterable[ParsedInvoice]) -> tuple[list[PdfWeightPayload], list[str]]:
    buckets: dict[str, list[PdfWeightSample]] = {}
    skipped: list[str] = []
    for invoice in invoices:
        for line in invoice.lines:
            sample = sample_from_line(line, invoice.filename)
            if sample is None:
                skipped.append(f"{invoice.filename}/{line.stock_code or 'UNKNOWN'}")
                continue
            buckets.setdefault(sample.sku, []).append(sample)

    payloads: list[PdfWeightPayload] = []
    for sku, samples in sorted(buckets.items()):
        gross = sum((sample.gross_unit_kg for sample in samples), Decimal("0")) / len(samples)
        net = sum((sample.net_unit_kg for sample in samples), Decimal("0")) / len(samples)
        first = samples[0]
        payloads.append(
            PdfWeightPayload(
                sku=sku,
                description=first.description,
                commodity_code=first.commodity_code,
                gross_weight_kg=quantize_kg(gross),
                net_weight_kg=quantize_kg(net),
                sample_count=len(samples),
                source_files=sorted({sample.source_file for sample in samples}),
            )
        )
    return payloads, skipped


def parse_pdf_weights(paths: Iterable[Path]) -> tuple[list[PdfWeightPayload], list[str], list[str]]:
    invoices: list[ParsedInvoice] = []
    errors: list[str] = []
    for path in paths:
        try:
            invoices.append(parse_birkdale_invoice_file(path.read_bytes(), path.name))
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    payloads, skipped = build_weight_payloads(invoices)
    return payloads, skipped, errors


def connect(database: str) -> pyodbc.Connection:
    cfg = {"DATABASE": database}
    conn = pyodbc.connect(build_connection_string(cfg, timeout=10), autocommit=False)
    conn.timeout = 30
    return conn


def table_exists(conn: pyodbc.Connection, schema: str, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [schema, table],
    )
    return cur.fetchone() is not None


def column_names(conn: pyodbc.Connection, schema: str, table: str) -> set[str]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [schema, table],
    )
    return {str(row[0]) for row in cur.fetchall()}


def ensure_weight_columns(conn: pyodbc.Connection, *, execute: bool) -> list[str]:
    if not table_exists(conn, APP_SCHEMA, "DocProductCatalog"):
        raise RuntimeError("BKD.DocProductCatalog does not exist.")
    specs = {
        "gross_weight_kg": "DECIMAL(10,3) NULL",
        "net_weight_kg": "DECIMAL(10,3) NULL",
        "weight_source": "NVARCHAR(160) NULL",
        "weight_sample_count": "INT NULL",
    }
    existing = column_names(conn, APP_SCHEMA, "DocProductCatalog")
    missing = [name for name in specs if name not in existing]
    if execute:
        cur = conn.cursor()
        for name in missing:
            cur.execute(f"ALTER TABLE {q(APP_SCHEMA)}.DocProductCatalog ADD {q(name)} {specs[name]}")
    return missing


def existing_catalog(conn: pyodbc.Connection) -> dict[str, dict[str, object]]:
    cols = column_names(conn, APP_SCHEMA, "DocProductCatalog")
    select_cols = [
        "sku",
        "description",
        "commodity_code",
        "gross_weight_kg" if "gross_weight_kg" in cols else "NULL AS gross_weight_kg",
        "net_weight_kg" if "net_weight_kg" in cols else "NULL AS net_weight_kg",
    ]
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT {', '.join(select_cols)}
        FROM {q(APP_SCHEMA)}.DocProductCatalog
        WHERE customer_code = N'ALL'
        """
    )
    result = {}
    for row in cur.fetchall():
        result[str(row[0]).upper()] = {
            "description": row[1],
            "commodity_code": row[2],
            "gross_weight_kg": row[3],
            "net_weight_kg": row[4],
        }
    return result


def has_weight(row: dict[str, object] | None) -> bool:
    if not row:
        return False
    gross = parse_decimal(row.get("gross_weight_kg"))
    net = parse_decimal(row.get("net_weight_kg"))
    return gross is not None and gross > 0 and net is not None and net > 0


def upsert_weights(
    conn: pyodbc.Connection,
    payloads: list[PdfWeightPayload],
    *,
    execute: bool,
    update_existing: bool = False,
) -> dict[str, int]:
    ensure_weight_columns(conn, execute=execute)
    existing = existing_catalog(conn)
    stats = {
        "source": len(payloads),
        "inserted": 0,
        "updated_missing": 0,
        "skipped_existing_weight": 0,
    }
    cur = conn.cursor()
    cols = column_names(conn, APP_SCHEMA, "DocProductCatalog") if execute else set()
    for payload in payloads:
        current = existing.get(payload.sku)
        current_has_weight = has_weight(current)
        source = "BKD invoice PDFs: " + ", ".join(payload.source_files[:3])
        if len(source) > 160:
            source = source[:157] + "..."

        if current and current_has_weight and not update_existing:
            stats["skipped_existing_weight"] += 1
            continue
        if current:
            stats["updated_missing"] += 1
            if execute:
                assignments = [
                    "[description] = COALESCE(NULLIF([description], N''), ?)",
                    "[commodity_code] = COALESCE(NULLIF([commodity_code], N''), ?)",
                    "[gross_weight_kg] = ?",
                    "[net_weight_kg] = ?",
                    "[weight_source] = ?",
                    "[weight_sample_count] = ?",
                ]
                if "updated_at" in cols:
                    assignments.append("[updated_at] = SYSUTCDATETIME()")
                cur.execute(
                    f"""
                    UPDATE {q(APP_SCHEMA)}.DocProductCatalog
                       SET {', '.join(assignments)}
                     WHERE customer_code = N'ALL' AND sku = ?
                    """,
                    [
                        payload.description,
                        payload.commodity_code,
                        payload.gross_weight_kg,
                        payload.net_weight_kg,
                        source,
                        payload.sample_count,
                        payload.sku,
                    ],
                )
        else:
            stats["inserted"] += 1
            if execute:
                insert_cols = [
                    "customer_code",
                    "sku",
                    "product_code",
                    "description",
                    "commodity_code",
                    "gross_weight_kg",
                    "net_weight_kg",
                    "weight_source",
                    "weight_sample_count",
                    "controlled_goods",
                    "active",
                ]
                available_cols = [col for col in insert_cols if col in cols]
                row = {
                    "customer_code": "ALL",
                    "sku": payload.sku,
                    "product_code": payload.sku,
                    "description": payload.description,
                    "commodity_code": payload.commodity_code,
                    "gross_weight_kg": payload.gross_weight_kg,
                    "net_weight_kg": payload.net_weight_kg,
                    "weight_source": source,
                    "weight_sample_count": payload.sample_count,
                    "controlled_goods": 0,
                    "active": 1,
                }
                cur.execute(
                    f"""
                    INSERT INTO {q(APP_SCHEMA)}.DocProductCatalog
                        ({', '.join(q(col) for col in available_cols)})
                    VALUES ({', '.join('?' for _ in available_cols)})
                    """,
                    [row[col] for col in available_cols],
                )
    if execute:
        conn.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="PDF file or directory containing BKD invoice PDFs.")
    parser.add_argument("--database", default="Fusion_TSS", help="Target database name.")
    parser.add_argument("--execute", action="store_true", help="Write changes. Default is dry run.")
    parser.add_argument("--update-existing", action="store_true", help="Overwrite existing non-zero catalog weights.")
    parser.add_argument("--limit", type=int, default=20, help="Rows to print in the preview.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    pdfs = discover_pdfs(args.paths)
    if not pdfs:
        print("No PDF files found.")
        return 1

    payloads, skipped, errors = parse_pdf_weights(pdfs)
    print(f"PDFs: {len(pdfs)} parsed_payload_skus={len(payloads)} skipped_lines={len(skipped)} parse_errors={len(errors)}")
    for payload in payloads[: args.limit]:
        print(
            f"{payload.sku}: gross={payload.gross_weight_kg} net={payload.net_weight_kg} "
            f"samples={payload.sample_count} commodity={payload.commodity_code} {payload.description[:72]}"
        )
    if skipped[: args.limit]:
        print("Skipped line samples:")
        for item in skipped[: args.limit]:
            print(f"  {item}")
    if errors:
        print("Parse errors:")
        for item in errors:
            print(f"  {item}")

    conn = connect(args.database)
    try:
        stats = upsert_weights(
            conn,
            payloads,
            execute=args.execute,
            update_existing=args.update_existing,
        )
        mode = "EXECUTE" if args.execute else "DRY-RUN"
        print(
            f"{mode}: source={stats['source']} inserted={stats['inserted']} "
            f"updated_missing={stats['updated_missing']} "
            f"skipped_existing_weight={stats['skipped_existing_weight']}"
        )
        if not args.execute:
            conn.rollback()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
