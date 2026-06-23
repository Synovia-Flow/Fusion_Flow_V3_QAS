"""Import missing BKD consignments from Fusion_TSS into automation PRD.

The production sync cannot discover old consignments from another database by
itself; it needs STG rows in the current database as anchors. This bridge copies
only consignments that are not already in the current STG model, brings their
associated ENS header and source goods rows, then optionally runs the normal
tenant sync so TSS mirrors ENS, goods, SFD and SDI/SUP data from the API.

Default mode is dry-run. Use --execute to write, and --skip-sync if you want to
import only without running the general sync afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyodbc
from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DB = "Fusion_TSS"
DEFAULT_TARGET_DB = "Fusion_TSS_Automation_PRD"
DEFAULT_TENANT_CODE = "BKD"
SOURCE_TAG = "FusionTSS_Backfill"
IMPORTED_LOCAL_STATUS = "IMPORTED"

sys.path.insert(0, str(PROJECT))

from config.db_connection import build_connection_string as build_env_connection_string  # noqa: E402


class SqlNow:
    pass


SQL_NOW = SqlNow()


@dataclass(frozen=True)
class SourceLayout:
    name: str
    schema: str
    headers_table: str
    consignments_table: str
    goods_table: str
    header_id: str
    consignment_id: str
    goods_id: str
    consignment_header_fk: str
    goods_consignment_fk: str
    has_client_code: bool


CLEAN_LAYOUT = SourceLayout(
    name="clean STG",
    schema="STG",
    headers_table="BKD_ENS_Headers",
    consignments_table="BKD_ENS_Consignments",
    goods_table="BKD_GoodsItems",
    header_id="stg_header_id",
    consignment_id="stg_consignment_id",
    goods_id="stg_item_id",
    consignment_header_fk="stg_header_id",
    goods_consignment_fk="stg_consignment_id",
    has_client_code=True,
)

LEGACY_LAYOUT = SourceLayout(
    name="legacy BKD.Staging",
    schema="BKD",
    headers_table="StagingEnsHeaders",
    consignments_table="StagingConsignments",
    goods_table="StagingGoodsItems",
    header_id="staging_id",
    consignment_id="staging_id",
    goods_id="staging_id",
    consignment_header_fk="staging_ens_id",
    goods_consignment_fk="staging_cons_id",
    has_client_code=False,
)


HEADER_MAP = {
    "ClientCode": ("ClientCode",),
    "sub_status": ("sub_status", "status"),
    "source": ("source",),
    "label": ("label",),
    "tss_ens_header_ref": ("tss_ens_header_ref", "ens_reference", "declaration_number"),
    "movement_type": ("movement_type",),
    "type_of_passive_transport": ("type_of_passive_transport",),
    "identity_no_of_transport": ("identity_no_of_transport", "identity_no_transport"),
    "nationality_of_transport": ("nationality_of_transport",),
    "conveyance_ref": ("conveyance_ref", "vehicle_registration"),
    "arrival_date_time": ("arrival_date_time",),
    "arrival_port": ("arrival_port", "port_of_arrival"),
    "place_of_loading": ("place_of_loading",),
    "place_of_unloading": ("place_of_unloading",),
    "transport_charges": ("transport_charges",),
    "carrier_eori": ("carrier_eori",),
    "carrier_name": ("carrier_name",),
    "carrier_street_number": ("carrier_street_number",),
    "carrier_city": ("carrier_city",),
    "carrier_postcode": ("carrier_postcode",),
    "carrier_country": ("carrier_country",),
    "haulier_eori": ("haulier_eori",),
    "validation_errors_json": ("validation_errors_json",),
}

CONSIGNMENT_MAP = {
    "ClientCode": ("ClientCode",),
    "sub_status": ("sub_status", "status"),
    "source": ("source",),
    "stg_header_id": (),
    "tss_consignment_ref": ("tss_consignment_ref", "dec_reference", "consignment_reference"),
    "tss_ens_header_ref": ("tss_ens_header_ref", "ens_reference"),
    "goods_description": ("goods_description",),
    "trader_reference": ("trader_reference",),
    "transport_document_number": ("transport_document_number",),
    "controlled_goods": ("controlled_goods",),
    "goods_domestic_status": ("goods_domestic_status",),
    "destination_country": ("destination_country",),
    "ducr": ("ducr",),
    "align_ukims": ("align_ukims",),
    "use_importer_sde": ("use_importer_sde",),
    "declaration_choice": ("declaration_choice",),
    "generate_SD": ("generate_SD", "generate_sd"),
    "container_indicator": ("container_indicator",),
    "buyer_same_as_importer": ("buyer_same_as_importer",),
    "seller_same_as_exporter": ("seller_same_as_exporter",),
    "no_sfd_reason": ("no_sfd_reason",),
    "consignor_eori": ("consignor_eori",),
    "consignor_name": ("consignor_name",),
    "consignor_street_number": ("consignor_street_number",),
    "consignor_city": ("consignor_city",),
    "consignor_postcode": ("consignor_postcode",),
    "consignor_country": ("consignor_country",),
    "consignee_eori": ("consignee_eori",),
    "consignee_name": ("consignee_name",),
    "consignee_street_number": ("consignee_street_number",),
    "consignee_city": ("consignee_city",),
    "consignee_postcode": ("consignee_postcode",),
    "consignee_country": ("consignee_country",),
    "importer_eori": ("importer_eori",),
    "importer_name": ("importer_name",),
    "importer_street_number": ("importer_street_number",),
    "importer_city": ("importer_city",),
    "importer_postcode": ("importer_postcode",),
    "importer_country": ("importer_country",),
    "exporter_eori": ("exporter_eori",),
    "metadata_json": ("metadata_json",),
    "error_message": ("error_message",),
}

GOODS_MAP = {
    "ClientCode": ("ClientCode",),
    "sub_status": ("sub_status", "status"),
    "source": ("source",),
    "stg_consignment_id": (),
    "goods_stage": ("goods_stage",),
    "tss_hex_id": ("tss_hex_id", "goods_id", "tss_goods_id", "tss_goods_id_ens"),
    "tss_consignment_ref": ("tss_consignment_ref",),
    "item_seq": ("item_seq", "item_number"),
    "goods_description": ("goods_description", "description"),
    "commodity_code": ("commodity_code",),
    "gross_mass_kg": ("gross_mass_kg", "gross_weight_kg"),
    "net_mass_kg": ("net_mass_kg", "net_weight_kg"),
    "number_of_packages": ("number_of_packages",),
    "number_of_individual_pieces": ("number_of_individual_pieces",),
    "type_of_packages": ("type_of_packages", "type_of_package"),
    "package_marks": ("package_marks",),
    "equipment_number": ("equipment_number",),
    "procedure_code": ("procedure_code",),
    "additional_procedure_code": ("additional_procedure_code",),
    "controlled_goods": ("controlled_goods",),
    "controlled_goods_type": ("controlled_goods_type",),
    "country_of_origin": ("country_of_origin",),
    "item_invoice_amount": ("item_invoice_amount", "line_amount_excl_vat"),
    "line_amount_excl_vat": ("line_amount_excl_vat",),
    "source_amount": ("source_amount",),
    "unit_price_excl_vat": ("unit_price_excl_vat",),
    "item_invoice_currency": ("item_invoice_currency",),
    "customs_value": ("customs_value",),
    "valuation_method": ("valuation_method",),
    "statistical_value": ("statistical_value",),
    "nature_of_transaction": ("nature_of_transaction",),
    "preference": ("preference",),
    "ni_additional_information_codes": ("ni_additional_information_codes",),
    "sku": ("sku", "No.", "No"),
    "error_message": ("error_message",),
}


def q(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def qualified(schema: str, table: str) -> str:
    return f"{q(schema)}.{q(table)}"


def clean(value: Any) -> str:
    return str(value or "").strip()


def has_text(value: Any) -> bool:
    return bool(clean(value))


def first_value(row: dict[str, Any] | None, *names: str) -> Any:
    if not row:
        return None
    lower_map = {str(k).lower(): k for k in row}
    for name in names:
        actual = lower_map.get(str(name).lower())
        if actual is None:
            continue
        value = row.get(actual)
        if value not in (None, ""):
            text = str(value).strip() if isinstance(value, str) else value
            if text not in (None, ""):
                return value
    return None


def status_value(row: dict[str, Any] | None, default: str = "IMPORTED") -> str:
    value = clean(first_value(row, "sub_status", "status", "tss_status"))
    return value or default


def connection_string(database: str) -> str:
    load_dotenv(PROJECT / ".env")
    raw = os.environ.get("DB_CONN_STR") or os.environ.get("ODBC_CONNECTION_STRING", "")
    raw = raw.strip()
    if raw:
        return connection_string_for_database(raw, database)
    return build_env_connection_string({"AZURE_SQL_DATABASE": database}, timeout=30)


def connection_string_for_database(raw: str, database: str) -> str:
    value = clean(raw)
    if not value:
        return f"DATABASE={database};"
    if not value.endswith(";"):
        value += ";"
    if re.search(r"(^|;)DATABASE=", value, flags=re.I):
        return re.sub(r"(^|;)DATABASE=[^;]*", rf"\1DATABASE={database}", value, flags=re.I)
    if re.search(r"(^|;)Initial Catalog=", value, flags=re.I):
        return re.sub(r"(^|;)Initial Catalog=[^;]*", rf"\1Initial Catalog={database}", value, flags=re.I)
    return value + f"DATABASE={database};"


def connect(database: str) -> pyodbc.Connection:
    conn = pyodbc.connect(connection_string(database), autocommit=False)
    conn.timeout = 60
    return conn


def table_exists(conn: pyodbc.Connection, schema: str, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*)
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
        """,
        [schema, table],
    )
    return bool(cur.fetchone()[0])


def table_columns(conn: pyodbc.Connection, schema: str, table: str) -> dict[str, str]:
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
        [schema, table],
    )
    return {str(row[0]).lower(): str(row[0]) for row in cur.fetchall()}


def source_layout_exists(conn: pyodbc.Connection, layout: SourceLayout) -> bool:
    return (
        table_exists(conn, CLEAN_LAYOUT.schema, CLEAN_LAYOUT.headers_table)
        and table_exists(conn, CLEAN_LAYOUT.schema, CLEAN_LAYOUT.consignments_table)
    ) if layout is CLEAN_LAYOUT else (
        table_exists(conn, LEGACY_LAYOUT.schema, LEGACY_LAYOUT.headers_table)
        and table_exists(conn, LEGACY_LAYOUT.schema, LEGACY_LAYOUT.consignments_table)
    )


def source_layout_count(conn: pyodbc.Connection, layout: SourceLayout, client_code: str) -> int:
    if not source_layout_exists(conn, layout):
        return 0
    columns = table_columns(conn, layout.schema, layout.consignments_table)
    where_sql = ""
    params: list[Any] = []
    if layout.has_client_code and "clientcode" in columns:
        where_sql = "WHERE [ClientCode] = ?"
        params.append(client_code)
    cur = conn.cursor()
    cur.execute(
        f"SELECT COUNT(*) FROM {qualified(layout.schema, layout.consignments_table)} {where_sql}",
        params,
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def detect_source_layout(conn: pyodbc.Connection, *, requested: str = "auto", client_code: str = DEFAULT_TENANT_CODE) -> SourceLayout:
    requested = clean(requested).lower() or "auto"
    layouts = {
        "clean": CLEAN_LAYOUT,
        "stg": CLEAN_LAYOUT,
        "legacy": LEGACY_LAYOUT,
        "bkd": LEGACY_LAYOUT,
    }
    if requested in layouts:
        layout = layouts[requested]
        if not source_layout_exists(conn, layout):
            raise RuntimeError(f"Requested source layout {requested!r} does not exist in source DB")
        return layout
    if requested != "auto":
        raise ValueError("--source-layout must be auto, clean or legacy")

    clean_count = source_layout_count(conn, CLEAN_LAYOUT, client_code)
    legacy_count = source_layout_count(conn, LEGACY_LAYOUT, client_code)
    if clean_count > 0:
        return CLEAN_LAYOUT
    if legacy_count > 0:
        return LEGACY_LAYOUT
    if source_layout_exists(conn, CLEAN_LAYOUT):
        return CLEAN_LAYOUT
    if source_layout_exists(conn, LEGACY_LAYOUT):
        return LEGACY_LAYOUT
    raise RuntimeError(
        "Source DB has neither STG.BKD_ENS_* nor BKD.StagingEnsHeaders/StagingConsignments"
    )


def row_as_dict(cursor: pyodbc.Cursor, row: Any) -> dict[str, Any]:
    names = [col[0] for col in cursor.description or []]
    return dict(zip(names, row)) if row else {}


def fetch_by_id(
    conn: pyodbc.Connection,
    layout: SourceLayout,
    table: str,
    id_column: str,
    row_id: Any,
) -> dict[str, Any]:
    if row_id in (None, ""):
        return {}
    cur = conn.cursor()
    cur.execute(
        f"SELECT * FROM {qualified(layout.schema, table)} WHERE {q(id_column)} = ?",
        [row_id],
    )
    return row_as_dict(cur, cur.fetchone())


def fetch_goods(
    conn: pyodbc.Connection,
    layout: SourceLayout,
    source_consignment_id: Any,
) -> list[dict[str, Any]]:
    if not table_exists(conn, layout.schema, layout.goods_table):
        return []
    cur = conn.cursor()
    order_column = (
        "item_seq"
        if "item_seq" in table_columns(conn, layout.schema, layout.goods_table)
        else "item_number"
        if "item_number" in table_columns(conn, layout.schema, layout.goods_table)
        else layout.goods_id
    )
    cur.execute(
        f"""
        SELECT *
        FROM {qualified(layout.schema, layout.goods_table)}
        WHERE {q(layout.goods_consignment_fk)} = ?
        ORDER BY {q(order_column)}, {q(layout.goods_id)}
        """,
        [source_consignment_id],
    )
    return [row_as_dict(cur, row) for row in cur.fetchall()]


def source_consignment_ref(row: dict[str, Any]) -> str:
    return clean(first_value(row, "tss_consignment_ref", "dec_reference", "consignment_reference"))


def source_header_ref(header: dict[str, Any], consignment: dict[str, Any]) -> str:
    return clean(
        first_value(header, "tss_ens_header_ref", "ens_reference", "declaration_number")
        or first_value(consignment, "tss_ens_header_ref", "ens_reference")
    )


def consignment_keys(row: dict[str, Any]) -> list[tuple[str, str]]:
    dec_ref = source_consignment_ref(row)
    if dec_ref:
        return [("dec", dec_ref.upper())]
    keys: list[tuple[str, str]] = []
    trader = clean(first_value(row, "trader_reference"))
    transport = clean(first_value(row, "transport_document_number"))
    if trader:
        keys.append(("trader", trader.upper()))
    if transport:
        keys.append(("transport", transport.upper()))
    return keys


def target_existing_keys(conn: pyodbc.Connection, client_code: str) -> set[tuple[str, str]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT tss_consignment_ref, trader_reference, transport_document_number
        FROM [STG].[BKD_ENS_Consignments]
        WHERE ClientCode = ?
        """,
        [client_code],
    )
    keys: set[tuple[str, str]] = set()
    for dec_ref, trader, transport in cur.fetchall():
        if has_text(dec_ref):
            keys.add(("dec", clean(dec_ref).upper()))
        if has_text(trader):
            keys.add(("trader", clean(trader).upper()))
        if has_text(transport):
            keys.add(("transport", clean(transport).upper()))
    return keys


def is_existing_consignment(row: dict[str, Any], existing: set[tuple[str, str]]) -> bool:
    keys = consignment_keys(row)
    return bool(keys and any(key in existing for key in keys))


def source_candidates(
    conn: pyodbc.Connection,
    layout: SourceLayout,
    *,
    client_code: str,
    limit: int,
    refs: list[str],
) -> list[dict[str, Any]]:
    columns = table_columns(conn, layout.schema, layout.consignments_table)
    where_parts = []
    params: list[Any] = []
    if layout.has_client_code and "clientcode" in columns:
        where_parts.append("[ClientCode] = ?")
        params.append(client_code)
    if refs:
        ref_col = columns.get("tss_consignment_ref") or columns.get("dec_reference")
        if not ref_col:
            raise RuntimeError("Cannot filter by ref; source consignment table has no DEC/reference column")
        where_parts.append(f"UPPER({q(ref_col)}) IN ({', '.join('?' for _ in refs)})")
        params.extend([ref.upper() for ref in refs])

    order_col = (
        columns.get("updated_at")
        or columns.get("created_at")
        or columns.get(layout.consignment_id.lower())
        or layout.consignment_id
    )
    where_sql = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    order_terms = [q(order_col)]
    if order_col.lower() != layout.consignment_id.lower():
        order_terms.append(q(layout.consignment_id))

    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT TOP (?) *
        FROM {qualified(layout.schema, layout.consignments_table)}
        {where_sql}
        ORDER BY {', '.join(f'{term} DESC' for term in order_terms)}
        """,
        [max(1, int(limit)), *params],
    )
    return [row_as_dict(cur, row) for row in cur.fetchall()]


def target_header_id(conn: pyodbc.Connection, client_code: str, header_ref: str) -> int | None:
    if not header_ref:
        return None
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP 1 stg_header_id
        FROM [STG].[BKD_ENS_Headers]
        WHERE ClientCode = ?
          AND UPPER(LTRIM(RTRIM(COALESCE(tss_ens_header_ref, '')))) = UPPER(?)
        ORDER BY stg_header_id DESC
        """,
        [client_code, header_ref],
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def mapped_payload(
    mapping: dict[str, tuple[str, ...]],
    row: dict[str, Any],
    *,
    client_code: str,
    source_label: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for target_col, source_cols in mapping.items():
        if target_col == "ClientCode":
            payload[target_col] = client_code
        elif target_col == "source":
            payload[target_col] = source_label
        elif target_col == "sub_status":
            payload[target_col] = status_value(row)
        elif source_cols:
            payload[target_col] = first_value(row, *source_cols)
    if extra:
        payload.update(extra)
    return payload


def metadata(source_db: str, layout: SourceLayout, source_row: dict[str, Any]) -> str:
    return json.dumps(
        {
            "source_db": source_db,
            "source_schema": layout.schema,
            "source_table": layout.consignments_table,
            "source_consignment_id": source_row.get(layout.consignment_id),
            "imported_by": Path(__file__).name,
        },
        ensure_ascii=True,
        default=str,
    )


def insert_row(
    conn: pyodbc.Connection,
    schema: str,
    table: str,
    identity_col: str,
    values: dict[str, Any],
) -> int:
    target_cols = table_columns(conn, schema, table)
    insert_items = [
        (target_cols[key.lower()], value)
        for key, value in values.items()
        if key.lower() in target_cols and key.lower() != identity_col.lower()
    ]
    if not insert_items:
        raise RuntimeError(f"No insertable columns for {schema}.{table}")
    cols = [name for name, _ in insert_items]
    placeholders = ["SYSUTCDATETIME()" if value is SQL_NOW else "?" for _, value in insert_items]
    params = [value for _, value in insert_items if value is not SQL_NOW]
    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT INTO {qualified(schema, table)} ({', '.join(q(c) for c in cols)})
        OUTPUT INSERTED.{q(identity_col)}
        VALUES ({', '.join(placeholders)})
        """,
        params,
    )
    return int(cur.fetchone()[0])


def import_header(
    target: pyodbc.Connection,
    *,
    client_code: str,
    source_db: str,
    layout: SourceLayout,
    header: dict[str, Any],
    consignment: dict[str, Any],
) -> int:
    header_ref = source_header_ref(header, consignment)
    existing_id = target_header_id(target, client_code, header_ref)
    if existing_id:
        return existing_id

    label = clean(first_value(header, "label")) or (f"Imported {header_ref}" if header_ref else "Imported ENS header")
    payload = mapped_payload(
        HEADER_MAP,
        header,
        client_code=client_code,
        source_label=SOURCE_TAG,
        extra={
            "sub_status": IMPORTED_LOCAL_STATUS,
            "label": label,
            "tss_ens_header_ref": header_ref or None,
            "stg_created_at": first_value(header, "stg_created_at", "created_at") or SQL_NOW,
            "updated_at": SQL_NOW,
            "last_sub_status_change": SQL_NOW,
        },
    )
    return insert_row(target, "STG", "BKD_ENS_Headers", "stg_header_id", payload)


def import_consignment(
    target: pyodbc.Connection,
    *,
    client_code: str,
    source_db: str,
    layout: SourceLayout,
    header_id: int,
    header: dict[str, Any],
    consignment: dict[str, Any],
) -> int:
    dec_ref = source_consignment_ref(consignment)
    header_ref = source_header_ref(header, consignment)
    payload = mapped_payload(
        CONSIGNMENT_MAP,
        consignment,
        client_code=client_code,
        source_label=SOURCE_TAG,
        extra={
            "sub_status": IMPORTED_LOCAL_STATUS,
            "stg_header_id": header_id,
            "tss_consignment_ref": dec_ref or None,
            "tss_ens_header_ref": header_ref or None,
            "metadata_json": metadata(source_db, layout, consignment),
            "last_sub_status_change": SQL_NOW,
            "updated_at": SQL_NOW,
            "stg_created_at": first_value(consignment, "stg_created_at", "created_at") or SQL_NOW,
        },
    )
    return insert_row(target, "STG", "BKD_ENS_Consignments", "stg_consignment_id", payload)


def import_goods(
    target: pyodbc.Connection,
    *,
    client_code: str,
    target_consignment_id: int,
    dec_ref: str,
    goods: list[dict[str, Any]],
) -> int:
    inserted = 0
    for item in goods:
        payload = mapped_payload(
            GOODS_MAP,
            item,
            client_code=client_code,
            source_label=SOURCE_TAG,
            extra={
                "stg_consignment_id": target_consignment_id,
                "goods_stage": clean(first_value(item, "goods_stage")) or "ENS",
                "tss_consignment_ref": dec_ref or first_value(item, "tss_consignment_ref"),
                "last_sub_status_change": SQL_NOW,
                "updated_at": SQL_NOW,
                "stg_created_at": first_value(item, "stg_created_at", "created_at") or SQL_NOW,
            },
        )
        insert_row(target, "STG", "BKD_GoodsItems", "stg_item_id", payload)
        inserted += 1
    return inserted


def run_general_sync(target_db: str, tenant_code: str, steps: str | None, *, sync_limit: int | None = None) -> int:
    load_dotenv(PROJECT / ".env")
    env = os.environ.copy()
    env["AZURE_SQL_DATABASE"] = target_db
    env["TENANT_CODE"] = tenant_code
    env["CLIENT_CODE"] = tenant_code
    env["FUSION_PRD_ENS_SYNC_MIN_AGE_MINUTES"] = "0"
    if sync_limit:
        env["FUSION_PRD_ENS_SYNC_LIMIT"] = str(max(1, int(sync_limit)))
    if env.get("DB_CONN_STR"):
        env["DB_CONN_STR"] = connection_string_for_database(env["DB_CONN_STR"], target_db)
    command = [
        sys.executable,
        str(PROJECT / "scripts" / "run_tenant_syncs.py"),
        "--tenants",
        tenant_code,
    ]
    if steps:
        command.extend(["--steps", steps])
    print("Running general sync:", " ".join(command), flush=True)
    return subprocess.run(command, cwd=PROJECT, env=env).returncode


def import_missing(args: argparse.Namespace) -> dict[str, int | str]:
    client_code = clean(args.tenant_code).upper() or DEFAULT_TENANT_CODE
    refs = [clean(ref).upper() for ref in args.refs if clean(ref)]

    with connect(args.source_db) as source, connect(args.target_db) as target:
        layout = detect_source_layout(source, requested=args.source_layout, client_code=client_code)
        existing = target_existing_keys(target, client_code)
        candidates = source_candidates(
            source,
            layout,
            client_code=client_code,
            limit=args.limit,
            refs=refs,
        )

        summary = {
            "source_layout": layout.name,
            "scanned": len(candidates),
            "missing": 0,
            "headers_inserted": 0,
            "consignments_inserted": 0,
            "goods_inserted": 0,
            "skipped_existing": 0,
            "skipped_no_key": 0,
            "missing_sync_anchor": 0,
        }
        header_cache: dict[Any, int] = {}

        for source_cons in candidates:
            keys = consignment_keys(source_cons)
            if not keys:
                summary["skipped_no_key"] += 1
                continue
            if is_existing_consignment(source_cons, existing):
                summary["skipped_existing"] += 1
                continue

            summary["missing"] += 1
            source_header_id = source_cons.get(layout.consignment_header_fk)
            header = fetch_by_id(source, layout, layout.headers_table, layout.header_id, source_header_id)
            if not source_header_ref(header, source_cons):
                summary["missing_sync_anchor"] += 1
            if not args.execute:
                continue

            header_cache_key = source_header_id or source_header_ref({}, source_cons) or source_cons.get(layout.consignment_id)
            if header_cache_key in header_cache:
                target_header = header_cache[header_cache_key]
            else:
                before = target_header_id(target, client_code, source_header_ref(header, source_cons))
                target_header = import_header(
                    target,
                    client_code=client_code,
                    source_db=args.source_db,
                    layout=layout,
                    header=header,
                    consignment=source_cons,
                )
                if before is None:
                    summary["headers_inserted"] += 1
                header_cache[header_cache_key] = target_header

            target_cons = import_consignment(
                target,
                client_code=client_code,
                source_db=args.source_db,
                layout=layout,
                header_id=target_header,
                header=header,
                consignment=source_cons,
            )
            summary["consignments_inserted"] += 1
            for key in keys:
                existing.add(key)

            source_goods = fetch_goods(source, layout, source_cons.get(layout.consignment_id))
            summary["goods_inserted"] += import_goods(
                target,
                client_code=client_code,
                target_consignment_id=target_cons,
                dec_ref=source_consignment_ref(source_cons),
                goods=source_goods,
            )

        if args.execute:
            target.commit()
        else:
            target.rollback()

    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=DEFAULT_SOURCE_DB)
    parser.add_argument("--target-db", default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--source-layout",
        choices=("auto", "clean", "legacy"),
        default="auto",
        help="Source schema layout. auto chooses a non-empty STG layout, then legacy BKD.Staging*.",
    )
    parser.add_argument(
        "--tenant-code",
        default=os.environ.get("TENANT_CODE") or os.environ.get("CLIENT_CODE") or DEFAULT_TENANT_CODE,
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--ref",
        dest="refs",
        action="append",
        default=[],
        help="Specific DEC/tss_consignment_ref to import. Can be repeated.",
    )
    parser.add_argument("--execute", action="store_true", help="Apply imports. Default is dry-run.")
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Do not run scripts/run_tenant_syncs.py after a successful --execute import.",
    )
    parser.add_argument(
        "--sync-steps",
        default="all",
        help="Steps passed to run_tenant_syncs.py. Default: all.",
    )
    parser.add_argument(
        "--sync-limit",
        type=int,
        default=None,
        help="Override FUSION_PRD_ENS_SYNC_LIMIT for the sync after import. Defaults to --limit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(
        f"Import missing consignments: {args.source_db} -> {args.target_db} "
        f"tenant={args.tenant_code} mode={mode}",
        flush=True,
    )
    summary = import_missing(args)
    print(json.dumps(summary, indent=2, sort_keys=True, default=str), flush=True)

    if not args.execute:
        print("Dry-run only. Re-run with --execute to import rows.", flush=True)
        return 0

    if args.skip_sync or int(summary.get("consignments_inserted", 0)) == 0:
        return 0

    return run_general_sync(
        args.target_db,
        clean(args.tenant_code).upper(),
        args.sync_steps,
        sync_limit=args.sync_limit or args.limit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
