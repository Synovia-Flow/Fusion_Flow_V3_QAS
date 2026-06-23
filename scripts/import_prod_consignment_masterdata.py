"""Import partner/company master data from BKD production CSV exports.

Supported input shapes:
  * No-header 68-column TSS consignment details export.
  * Headered customer CSV export with CustomerNo/CustomerName fields.

The script intentionally does not embed customer data; pass the CSV path at
runtime.

Usage:
    python scripts/import_prod_consignment_masterdata.py "C:\\path\\prod data consisgment details.csv" --tenant BKD
    python scripts/import_prod_consignment_masterdata.py "C:\\path\\bkd customers.csv" --tenant BKD
    python scripts/import_prod_consignment_masterdata.py "C:\\path\\file.csv" --dry-run
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.db_connection import build_connection_string


EXPECTED_FIELD_COUNT = 68
NULL_VALUES = {"", "NULL", "None", "none", "null", "undefined"}
UNSAFE_SAMPLE_EORIS = {"GB000000000000"}
TENANT_RE = re.compile(r"^[A-Z0-9]{3}$")
EORI_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{2,17}$")
CUSTOMER_HEADER_FIELDS = {"CustomerNo", "CustomerName", "Address1", "Postcode", "CountryRegionCode"}

PARTY_BLOCKS = {
    "Consignor": (27, 28, 29, 30, 31, 32),
    "Consignee": (33, 34, 35, 36, 37, 38),
    "Importer": (39, 40, 41, 42, 43, 44),
    "Exporter": (46, 47, 48, 49, 50, 51),
}


def clean(value):
    text = str(value or "").strip()
    return "" if text in NULL_VALUES else text


def safe_eori(value):
    text = re.sub(r"[\s-]+", "", clean(value).upper())
    if text in UNSAFE_SAMPLE_EORIS:
        return ""
    return text if EORI_RE.match(text) else ""


def normalize_tenant(value):
    tenant = str(value or "BKD").strip().upper()
    if not TENANT_RE.match(tenant):
        raise ValueError("Tenant/schema must be exactly 3 alphanumeric characters.")
    return tenant


def read_consignment_detail_rows(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    bad_rows = [idx for idx, row in enumerate(rows, start=1) if len(row) != EXPECTED_FIELD_COUNT]
    if bad_rows:
        raise ValueError(
            f"Expected {EXPECTED_FIELD_COUNT} columns. Bad rows: {', '.join(map(str, bad_rows[:10]))}"
        )
    return rows


def detect_input_shape(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        first_row = next(csv.reader(handle), [])
    if CUSTOMER_HEADER_FIELDS.issubset(set(first_row)):
        return "customers"
    return "consignment_details"


def read_customer_rows(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    missing = CUSTOMER_HEADER_FIELDS - set(rows[0].keys() if rows else [])
    if missing:
        raise ValueError(f"Customer CSV is missing required columns: {', '.join(sorted(missing))}")
    return rows


def extract_party(row, positions):
    eori_col, name_col, street_col, city_col, postcode_col, country_col = positions
    return {
        "eori": safe_eori(row[eori_col - 1]),
        "partner_name": clean(row[name_col - 1])[:200],
        "address_line1": clean(row[street_col - 1])[:200],
        "city": clean(row[city_col - 1])[:100],
        "postcode": clean(row[postcode_col - 1])[:20],
        "country": clean(row[country_col - 1]).upper()[:2],
    }


def extract_customer_partner(row):
    county = clean(row.get("County"))
    notes = "Loaded from BKD customer master"
    if county:
        notes = f"{notes}; County: {county}"
    return {
        "partner_type": "Consignee",
        "partner_name": clean(row.get("CustomerName"))[:200],
        "eori": safe_eori(row.get("EoriNumber")),
        "address_line1": clean(row.get("Address1"))[:200],
        "address_line2": clean(row.get("Address2"))[:200],
        "city": clean(row.get("City"))[:100],
        "postcode": clean(row.get("Postcode")).upper()[:20],
        "country": clean(row.get("CountryRegionCode")).upper()[:2],
        "contact_phone": clean(row.get("PhoneNo"))[:50],
        "account_ref": clean(row.get("CustomerNo"))[:50],
        "env_code": clean(row.get("EnvCode"))[:10],
        "source_system": "BKD_Customer",
        "source_record_id": clean(row.get("RecordId")),
        "source_file_id": clean(row.get("FileId")),
        "source_row_num": clean(row.get("SourceRowNum")),
        "county": county[:100],
        "source_loaded_at": clean(row.get("LoadedAt"))[:50],
        "notes": notes,
    }


def collect_parties(rows):
    parties = {}
    for row in rows:
        for role, positions in PARTY_BLOCKS.items():
            party = extract_party(row, positions)
            if not party["partner_name"]:
                continue
            key = (
                role,
                party["eori"] or "",
                party["partner_name"].upper(),
                party["postcode"].upper(),
                party["country"].upper(),
            )
            parties[key] = {"partner_type": role, **party}
    return list(parties.values())


def collect_customer_partners(rows):
    partners = {}
    for row in rows:
        party = extract_customer_partner(row)
        if not party["partner_name"]:
            continue
        key = party["account_ref"] or (
            party["eori"],
            party["partner_name"].upper(),
            party["postcode"],
        )
        partners[key] = party
    return list(partners.values())


def choose_company(parties):
    for role in ("Importer", "Consignor", "Exporter"):
        for party in parties:
            if party["partner_type"] == role and party.get("eori"):
                return party
    return None


def table_columns(cursor, schema, table):
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [schema, table],
    )
    return {row[0] for row in cursor.fetchall()}


def row_id(cursor, schema, table, where_sql, params):
    cursor.execute(f"SELECT TOP 1 id FROM [{schema}].[{table}] WHERE {where_sql} ORDER BY id", params)
    row = cursor.fetchone()
    return row[0] if row else None


def upsert_company(cursor, schema, company):
    if not company:
        return "skipped"
    columns = table_columns(cursor, schema, "CompanyMaster")
    available = {
        "company_name": company["partner_name"],
        "trading_name": company["partner_name"],
        "eori_xi": company["eori"] if company["eori"].startswith("XI") else None,
        "eori_gb": company["eori"] if company["eori"].startswith("GB") else None,
        "address_line1": company["address_line1"],
        "city": company["city"],
        "postcode": company["postcode"],
        "country": company["country"],
        "company_type": "Trader",
    }
    values = {key: value for key, value in available.items() if key in columns}

    existing_id = None
    if company["eori"] and "eori_xi" in columns:
        existing_id = row_id(cursor, schema, "CompanyMaster", "UPPER(eori_xi) = UPPER(?)", [company["eori"]])
    if not existing_id and company["eori"] and "eori_gb" in columns:
        existing_id = row_id(cursor, schema, "CompanyMaster", "UPPER(eori_gb) = UPPER(?)", [company["eori"]])

    if existing_id:
        set_sql = ", ".join(f"{key}=?" for key in values)
        params = list(values.values()) + [existing_id]
        cursor.execute(f"UPDATE [{schema}].[CompanyMaster] SET {set_sql} WHERE id=?", params)
        return "updated"

    insert_cols = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor.execute(
        f"INSERT INTO [{schema}].[CompanyMaster] ({insert_cols}) VALUES ({placeholders})",
        list(values.values()),
    )
    return "inserted"


def upsert_partner(cursor, schema, party):
    columns = table_columns(cursor, schema, "Partners")
    available = {
        "partner_type": party["partner_type"],
        "partner_name": party["partner_name"],
        "eori": party["eori"] or None,
        "address_line1": party["address_line1"],
        "address_line2": party.get("address_line2"),
        "city": party["city"],
        "postcode": party["postcode"],
        "country": party["country"],
        "contact_phone": party.get("contact_phone"),
        "account_ref": party.get("account_ref"),
        "env_code": party.get("env_code"),
        "source_system": party.get("source_system"),
        "source_record_id": int(party["source_record_id"]) if str(party.get("source_record_id") or "").isdigit() else None,
        "source_file_id": int(party["source_file_id"]) if str(party.get("source_file_id") or "").isdigit() else None,
        "source_row_num": int(party["source_row_num"]) if str(party.get("source_row_num") or "").isdigit() else None,
        "county": party.get("county"),
        "source_loaded_at": party.get("source_loaded_at"),
        "notes": party.get("notes"),
        "active": 1,
    }
    values = {key: value for key, value in available.items() if key in columns}

    existing_id = None
    has_account_ref_key = bool(party.get("account_ref") and "account_ref" in columns)
    if party.get("account_ref") and "account_ref" in columns:
        existing_id = row_id(
            cursor,
            schema,
            "Partners",
            "partner_type = ? AND account_ref = ?",
            [party["partner_type"], party["account_ref"]],
        )
    if not existing_id and not has_account_ref_key and party["eori"] and "eori" in columns:
        existing_id = row_id(
            cursor,
            schema,
            "Partners",
            "partner_type = ? AND UPPER(eori) = UPPER(?)",
            [party["partner_type"], party["eori"]],
        )
    if not existing_id and not has_account_ref_key:
        existing_id = row_id(
            cursor,
            schema,
            "Partners",
            "partner_type = ? AND UPPER(partner_name) = UPPER(?) AND ISNULL(postcode, '') = ?",
            [party["partner_type"], party["partner_name"], party["postcode"]],
        )

    if existing_id:
        set_values = {key: value for key, value in values.items() if key != "partner_type"}
        if "updated_at" in columns:
            set_values["updated_at"] = None
        set_sql = ", ".join(
            f"{key}=SYSUTCDATETIME()" if key == "updated_at" else f"{key}=?"
            for key in set_values
        )
        params = [value for key, value in set_values.items() if key != "updated_at"] + [existing_id]
        cursor.execute(f"UPDATE [{schema}].[Partners] SET {set_sql} WHERE id=?", params)
        return "updated"

    insert_cols = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor.execute(
        f"INSERT INTO [{schema}].[Partners] ({insert_cols}) VALUES ({placeholders})",
        list(values.values()),
    )
    return "inserted"


def import_masterdata(path, tenant="BKD", dry_run=False, source="auto"):
    schema = normalize_tenant(tenant)
    source_type = detect_input_shape(path) if source == "auto" else source
    if source_type == "customers":
        rows = read_customer_rows(path)
        parties = collect_customer_partners(rows)
        company = None
    elif source_type == "consignment_details":
        rows = read_consignment_detail_rows(path)
        parties = collect_parties(rows)
        company = choose_company(parties)
    else:
        raise ValueError("source must be auto, customers or consignment_details")

    summary = {
        "source": source_type,
        "rows": len(rows),
        "parties": len(parties),
        "company_candidate": bool(company),
        "company": "skipped",
        "partners_inserted": 0,
        "partners_updated": 0,
    }
    if dry_run:
        return summary

    load_dotenv()
    conn = pyodbc.connect(build_connection_string(timeout=30), autocommit=False)
    try:
        cursor = conn.cursor()
        summary["company"] = upsert_company(cursor, schema, company)
        for party in parties:
            result = upsert_partner(cursor, schema, party)
            if result == "inserted":
                summary["partners_inserted"] += 1
            elif result == "updated":
                summary["partners_updated"] += 1
        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", help="Path to the prod consignment details or BKD customer CSV")
    parser.add_argument("--tenant", default=os.environ.get("TENANT_CODE", "BKD"), help="Tenant/schema code")
    parser.add_argument(
        "--source",
        choices=["auto", "customers", "consignment_details"],
        default="auto",
        help="Input CSV shape. Defaults to auto-detect.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse and summarize without writing to SQL Server")
    args = parser.parse_args()

    summary = import_masterdata(args.csv_path, tenant=args.tenant, dry_run=args.dry_run, source=args.source)
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
