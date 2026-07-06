#!/usr/bin/env python3
"""Load product/item masterdata CSV into CFG.Product_Master.

The CSV is treated as configuration/masterdata, not source ingestion. Runtime
processing logs every product-derived value as MASTERDATA in
EXC.Data_Processing_Enhancement.
"""

from __future__ import annotations

import argparse
import configparser
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import os
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
MODULE = "REFERENCE_DATA"
PROCESS = "LOAD_PRODUCT_MASTER"


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


def clean(value: Any, max_len: int | None = None) -> str | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    if max_len is not None and len(text) > max_len:
        return text[:max_len]
    return text


def code(value: Any, max_len: int | None = None) -> str | None:
    text = clean(value, max_len)
    return text.upper() if text is not None else None


def dec(value: Any, scale: int = 3) -> Decimal | None:
    text = clean(value)
    if text is None:
        return None
    try:
        quant = Decimal("1." + ("0" * scale))
        return Decimal(text.replace(",", "")).quantize(quant)
    except (InvalidOperation, ValueError):
        return None


def intish(value: Any) -> int | None:
    value_dec = dec(value, 0)
    return int(value_dec) if value_dec is not None else None


def bit(value: Any) -> int | None:
    text = clean(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in ("1", "true", "yes", "y"):
        return 1
    if lowered in ("0", "false", "no", "n"):
        return 0
    return None


def dt(value: Any) -> datetime | None:
    text = clean(value)
    if text is None:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def row_hash(row: dict[str, Any]) -> str:
    parts = [f"{k}={'' if row.get(k) is None else row.get(k)}" for k in sorted(row)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def map_row(raw: dict[str, str], client_code: str) -> dict[str, Any] | None:
    sku = code(raw.get("sku") or raw.get("product_code"), 100)
    if not sku:
        return None
    mapped = {
        "ClientCode": client_code.upper()[:3],
        "CustomerCode": clean(raw.get("customer_code"), 30),
        "SourceTable": clean(raw.get("source_table"), 128),
        "SourceID": clean(raw.get("id"), 80),
        "SKU": sku,
        "ProductCode": code(raw.get("product_code"), 100),
        "Barcode": clean(raw.get("barcode"), 100),
        "ProductName": clean(raw.get("product_name"), 500),
        "GoodsDescription": clean(raw.get("goods_description"), 500),
        "CommodityCode": code(raw.get("commodity_code"), 10),
        "CountryOfOrigin": code(raw.get("country_of_origin"), 2),
        "PackageType": clean(raw.get("package_type"), 40),
        "PackageMarks": clean(raw.get("package_marks"), 140),
        "ProcedureCode": code(raw.get("procedure_code"), 4),
        "AdditionalProcedureCode": code(raw.get("additional_procedure_code"), 3),
        "ValuationMethod": clean(raw.get("valuation_method"), 2),
        "ValuationIndicator": clean(raw.get("valuation_indicator"), 4),
        "PreferenceCode": code(raw.get("preference_code"), 4),
        "NiAdditionalInfoCode": clean(raw.get("ni_additional_info_code"), 40),
        "NatureOfTransaction": clean(raw.get("nature_of_transaction"), 20),
        "CountryOfPreferentialOrigin": code(raw.get("country_of_preferential_origin"), 2),
        "TaricCode": clean(raw.get("taric_code"), 20),
        "CusCode": clean(raw.get("cus_code"), 8),
        "NationalAdditionalCode": clean(raw.get("national_additional_code"), 4),
        "QuotaOrderNumber": clean(raw.get("quota_order_number"), 6),
        "ControlledGoodsType": clean(raw.get("controlled_goods_type"), 40),
        "SdiNotes": clean(raw.get("sdi_notes"), 1000),
        "GrossWeightKg": dec(raw.get("gross_weight_kg"), 3),
        "NetWeightKg": dec(raw.get("net_weight_kg"), 3),
        "WeightSource": clean(raw.get("weight_source"), 200),
        "WeightSampleCount": intish(raw.get("weight_sample_count")),
        "UnitValue": dec(raw.get("unit_value"), 4),
        "Currency": clean(raw.get("currency"), 8),
        "StatisticalUnit": clean(raw.get("statistical_unit"), 50),
        "ControlledGoods": bit(raw.get("controlled_goods")),
        "RequiresSupplementaryUnit": bit(raw.get("requires_supplementary_unit")),
        "IsActive": bit(raw.get("is_active")),
        "Notes": clean(raw.get("notes"), 1000),
        "SourceCreatedAt": dt(raw.get("created_at")),
        "SourceUpdatedAt": dt(raw.get("updated_at")),
    }
    if mapped["IsActive"] is None:
        mapped["IsActive"] = 1
    mapped["RowHash"] = row_hash(mapped)
    return mapped


def q(cur, sql: str, *params: Any) -> list[dict[str, Any]]:
    cur.execute(sql, *params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def open_execution(cur, client_code: str) -> tuple[int, str]:
    env_rows = q(cur, "SELECT ParameterValue FROM CFG.Application_Parameters WHERE ParameterKey = 'DEFAULT_ENV' AND IsActive = 1")
    env_code = env_rows[0]["ParameterValue"] if env_rows and env_rows[0]["ParameterValue"] else "TST"
    cur.execute(
        "INSERT INTO EXC.Execution (EnvCode, ClientCode, ModuleName, ProcessName, RunMode, Status) "
        "OUTPUT INSERTED.ExecutionID, INSERTED.TransactionID VALUES (?, ?, ?, ?, ?, ?)",
        env_code, client_code, MODULE, PROCESS, "manual", "SYNCING",
    )
    row = cur.fetchone()
    return int(row[0]), str(row[1])


def finish_execution(cur, execution_id: int, status: str, found: int, processed: int, failed: int, error: str = "") -> None:
    cur.execute(
        "UPDATE EXC.Execution SET EndedAt = SYSUTCDATETIME(), Status = ?, "
        "ItemsFound = ?, ItemsProcessed = ?, ItemsFailed = ?, ErrorMessage = ? "
        "WHERE ExecutionID = ?",
        status, found, processed, failed, error or None, execution_id,
    )


def upsert(cur, row: dict[str, Any]) -> str:
    existing = q(
        cur,
        "SELECT ProductMasterID, RowHash FROM CFG.Product_Master WHERE ClientCode = ? AND SKU = ?",
        row["ClientCode"], row["SKU"],
    )
    cols = list(row)
    if existing:
        if existing[0]["RowHash"] == row["RowHash"]:
            return "unchanged"
        set_cols = [c for c in cols if c not in ("ClientCode", "SKU")]
        assignments = ", ".join(f"[{c}] = ?" for c in set_cols) + ", [UpdatedAt] = SYSUTCDATETIME()"
        cur.execute(
            f"UPDATE CFG.Product_Master SET {assignments} WHERE ProductMasterID = ?",
            *[row[c] for c in set_cols],
            existing[0]["ProductMasterID"],
        )
        return "updated"

    col_list = ", ".join(f"[{c}]" for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    cur.execute(
        f"INSERT INTO CFG.Product_Master ({col_list}) VALUES ({placeholders})",
        *[row[c] for c in cols],
    )
    return "inserted"


def main() -> int:
    parser = argparse.ArgumentParser(description="Load product/item masterdata into CFG.Product_Master.")
    parser.add_argument("--csv", required=True, help="Path to product masterdata CSV.")
    parser.add_argument("--client-code", default="BKD", help="Fusion client code to own these rows.")
    parser.add_argument("--ini", default=str(DEFAULT_INI), help="Path to Fusion_Flow_QAS.ini.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report without writing.")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    import pyodbc

    conn = pyodbc.connect(connection_string(Path(args.ini)), autocommit=False)
    cur = conn.cursor()
    execution_id: int | None = None
    found = processed = failed = inserted = updated = unchanged = 0
    try:
        if not args.dry_run:
            execution_id, _ = open_execution(cur, args.client_code.upper()[:3])
            conn.commit()

        rows_by_sku: dict[str, dict[str, Any]] = {}
        with csv_path.open(newline="", encoding="utf-8-sig") as handle:
            for raw in csv.DictReader(handle):
                found += 1
                row = map_row(raw, args.client_code)
                if row is None:
                    failed += 1
                    continue
                rows_by_sku[row["SKU"]] = row

        for row in rows_by_sku.values():
            if args.dry_run:
                processed += 1
                continue
            outcome = upsert(cur, row)
            inserted += 1 if outcome == "inserted" else 0
            updated += 1 if outcome == "updated" else 0
            unchanged += 1 if outcome == "unchanged" else 0
            processed += 1
            if processed % 500 == 0:
                conn.commit()

        if not args.dry_run and execution_id is not None:
            finish_execution(cur, execution_id, "COMPLETED", found, processed, failed)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if execution_id is not None:
            finish_execution(cur, execution_id, "ERROR", found, processed, failed, str(exc)[:2000])
            conn.commit()
        raise
    finally:
        cur.close()
        conn.close()

    print(
        f"Product master load: found={found} processed={processed} failed={failed} "
        f"inserted={inserted} updated={updated} unchanged={unchanged} dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
