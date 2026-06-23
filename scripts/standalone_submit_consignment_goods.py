"""
Standalone BKD submitter: Consignments + Consignment Goods.

This file is intentionally self-contained so it can be copied to another
machine without the Fusion Flow repo.

Requires:
    pip install pyodbc requests

Run:
    python standalone_submit_consignment_goods.py
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pyodbc
import requests


API_PATH = "/x_fhmrc_tss_api/v1/tss_api"
RATE_LIMIT_SECONDS = 0.3
TIMEOUT_SECONDS = 30
TARIC_SEPARATOR_RE = re.compile(r'[\s,;:/\\|_-]+')


def ask(name: str, default: str = "", secret: bool = False) -> str:
    label = f"{name} [{default}]: " if default else f"{name}: "
    value = getpass.getpass(label) if secret else input(label)
    value = (value or "").strip()
    return value or default


def yn(name: str, default: bool = False) -> bool:
    default_text = "yes" if default else "no"
    value = ask(name, default_text).strip().lower()
    return value in {"y", "yes", "1", "true", "on"}


def build_tss_api_url(base_url: str) -> str:
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith(API_PATH):
        return base_url
    return f"{base_url}{API_PATH}"


def format_tss_decimal(value, max_dp=2) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        dec = Decimal(text)
        quant = Decimal("1").scaleb(-max_dp)
        dec = dec.quantize(quant, rounding=ROUND_HALF_UP)
        out = format(dec.normalize(), "f")
    except (InvalidOperation, ValueError):
        return text
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return out or "0"


def normalise_taric_code(value) -> str:
    if value in (None, ""):
        return ""
    return TARIC_SEPARATOR_RE.sub("", str(value).strip())


def connect_sql() -> pyodbc.Connection:
    print("\nDatabase connection")
    driver = ask("ODBC driver", os.environ.get("DB_DRIVER", "ODBC Driver 18 for SQL Server"))
    server = ask("AZURE_SQL_SERVER", os.environ.get("AZURE_SQL_SERVER", ""))
    database = ask("AZURE_SQL_DATABASE", os.environ.get("AZURE_SQL_DATABASE", "Fusion_TSS"))
    username = ask("AZURE_SQL_USERNAME", os.environ.get("AZURE_SQL_USERNAME", ""))
    password = ask("AZURE_SQL_PASSWORD", os.environ.get("AZURE_SQL_PASSWORD", ""), secret=True)
    trust = ask("TrustServerCertificate yes/no", os.environ.get("TRUST_SERVER_CERTIFICATE", "yes")).lower()

    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        "Encrypt=yes;"
        f"TrustServerCertificate={'yes' if trust in {'y', 'yes', '1', 'true'} else 'no'};"
        "Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str, autocommit=False)


def load_app_config(conn: pyodbc.Connection, schema: str) -> dict[str, str]:
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT category, config_key, config_value
            FROM [{schema}].AppConfiguration
            WHERE category = 'TSS_API'
            """
        )
        rows = cur.fetchall()
        return {f"{r[0]}.{r[1]}": r[2] or "" for r in rows}
    except Exception as exc:
        print(f"Warning: could not read [{schema}].AppConfiguration: {exc}")
        return {}


def resolve_tss(conn: pyodbc.Connection, schema: str) -> dict[str, str]:
    app_cfg = load_app_config(conn, schema)
    mode = (app_cfg.get("TSS_API.ENVIRONMENT") or "").strip().lower()
    app_test_url = (app_cfg.get("TSS_API.TEST_URL") or "").strip()
    app_base_url = (app_cfg.get("TSS_API.BASE_URL") or "").strip()
    preferred_url = app_test_url if mode == "test" and app_test_url else app_base_url

    print("\nTSS API")
    print("If AppConfiguration has values, press Enter to accept them.")
    base_url = ask("TSS_API_BASE_URL", preferred_url or os.environ.get("TSS_API_BASE_URL", ""))
    username = ask("TSS_API_USERNAME", app_cfg.get("TSS_API.USERNAME") or os.environ.get("TSS_API_USERNAME", ""))
    password = ask("TSS_API_PASSWORD", app_cfg.get("TSS_API.PASSWORD") or os.environ.get("TSS_API_PASSWORD", ""), secret=True)
    act_as = ask("TSS_API_ACT_AS optional", app_cfg.get("TSS_API.ACT_AS") or os.environ.get("TSS_API_ACT_AS", ""))

    if not base_url or not username or not password:
        raise RuntimeError("TSS API base URL, username and password are required.")
    return {
        "api_url": build_tss_api_url(base_url),
        "username": username,
        "password": password,
        "act_as": act_as,
    }


def make_session(username: str, password: str) -> requests.Session:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
        }
    )
    return session


def post_tss(session: requests.Session, url: str, payload: dict, params: dict | None = None) -> dict:
    started = time.time()
    try:
        resp = session.post(url, params=params, json=payload, timeout=TIMEOUT_SECONDS)
        ms = int((time.time() - started) * 1000)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        result = body.get("result", body if isinstance(body, dict) else {})
        process_message = result.get("process_message") or ""
        error_message = result.get("error_message") or result.get("error_details") or ""
        reference = (
            result.get("reference")
            or result.get("declaration_number")
            or result.get("goods_id")
            or ""
        )
        success = resp.status_code == 200 and not str(process_message).upper().startswith("ERROR")
        return {
            "success": success,
            "http_status": resp.status_code,
            "reference": reference,
            "status": result.get("status") or ("ok" if success else "error"),
            "message": process_message or error_message or resp.text[:500],
            "duration_ms": ms,
            "raw": resp.text[:4000],
        }
    except Exception as exc:
        return {
            "success": False,
            "http_status": 0,
            "reference": "",
            "status": "error",
            "message": str(exc)[:500],
            "duration_ms": int((time.time() - started) * 1000),
            "raw": "",
        }
    finally:
        time.sleep(RATE_LIMIT_SECONDS)


def log_call(cur, schema: str, staging_id: int, call_type: str, result: dict, url: str, payload: dict) -> None:
    try:
        cur.execute(
            f"""
            INSERT INTO [{schema}].ApiCallLog
                (staging_id, call_type, http_method, url, request_payload,
                 http_status, response_status, response_message, response_json, duration_ms)
            VALUES (?, ?, 'POST', ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                staging_id,
                call_type,
                url[:500],
                json.dumps(payload, ensure_ascii=False)[:4000],
                result["http_status"],
                (result.get("status") or "")[:50],
                (result.get("message") or "")[:500],
                (result.get("raw") or "")[:4000],
                result["duration_ms"],
            ],
        )
    except Exception as exc:
        print(f"    warning: ApiCallLog failed for {call_type} #{staging_id}: {exc}")


def id_filter(column: str, ids: list[int]) -> tuple[str, list[int]]:
    if not ids:
        return "", []
    return f" AND {column} IN ({','.join('?' for _ in ids)})", ids


def parse_ids(text: str) -> list[int]:
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


def consignment_ready_for_submit(cur, schema: str, consignment_id: int) -> tuple[bool, int, int]:
    row = cur.execute(
        f"""
        SELECT
            COUNT(*) AS total_goods,
            SUM(CASE WHEN status IN ('CREATED', 'SYNCED', 'SUBMITTED') THEN 1 ELSE 0 END) AS ready_goods,
            SUM(CASE WHEN status IN ('PENDING', 'VALIDATED', 'FAILED', 'INVALID') THEN 1 ELSE 0 END) AS blocked_goods
        FROM [{schema}].StagingGoodsItems
        WHERE staging_cons_id = ?
        """,
        [consignment_id],
    ).fetchone()
    total = row[0] or 0
    ready = row[1] or 0
    blocked = row[2] or 0
    return total > 0 and ready == total and blocked == 0, total, blocked


def consignment_has_successful_submit(cur, schema: str, consignment_id: int) -> bool:
    try:
        row = cur.execute(
            f"""
            SELECT TOP 1 1
            FROM [{schema}].ApiCallLog
            WHERE staging_id = ?
              AND call_type = 'SUBMIT_CONSIGNMENT'
              AND http_status BETWEEN 200 AND 299
              AND (
                    UPPER(COALESCE(response_message, '')) = 'SUCCESS'
                 OR UPPER(COALESCE(response_status, '')) IN ('SUBMITTED', 'PROCESSING')
              )
            ORDER BY called_at DESC, id DESC
            """,
            [consignment_id],
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def local_status_after_consignment_update(already_submitted: bool) -> str:
    return "SUBMITTED" if already_submitted else "CREATED"


def build_consignment_payload(row: dict, *, op_type: str, ens_ref: str | None = None, dec_ref: str | None = None) -> dict:
    payload = {
        "op_type": op_type,
        "goods_description": row.get("goods_description"),
        "transport_document_number": row.get("transport_document_number"),
        "controlled_goods": row.get("controlled_goods") or "no",
        "consignor_eori": row.get("consignor_eori"),
        "consignee_eori": row.get("consignee_eori"),
        "importer_eori": row.get("importer_eori"),
        "exporter_eori": row.get("exporter_eori"),
        "consignor_name": row.get("consignor_name"),
        "consignee_name": row.get("consignee_name"),
        "importer_name": row.get("importer_name"),
        "exporter_name": row.get("exporter_name"),
        "buyer_same_as_importer": row.get("buyer_same_as_importer") or "yes",
        "seller_same_as_exporter": row.get("seller_same_as_exporter") or "yes",
    }
    if ens_ref:
        payload["declaration_number"] = ens_ref
    if dec_ref is not None:
        payload["consignment_number"] = dec_ref

    for field in [
        "trader_reference", "goods_domestic_status", "destination_country",
        "supervising_customs_office", "customs_warehouse_identifier",
        "ducr", "no_sfd_reason", "align_ukims", "use_importer_sde",
        "declaration_choice", "generate_SD", "container_indicator",
        "consignor_street_number", "consignor_city", "consignor_postcode",
        "consignor_country", "consignee_street_number", "consignee_city",
        "consignee_postcode", "consignee_country", "importer_street_number",
        "importer_city", "importer_postcode", "importer_country",
        "exporter_street_number", "exporter_city", "exporter_postcode",
        "exporter_country", "buyer_eori", "buyer_name",
        "buyer_street_and_number", "buyer_city", "buyer_postcode",
        "buyer_country", "seller_eori", "seller_name",
        "seller_street_and_number", "seller_city", "seller_postcode",
        "seller_country",
    ]:
        if field.startswith("buyer_") and payload.get("buyer_same_as_importer") == "yes":
            continue
        if field.startswith("seller_") and payload.get("seller_same_as_exporter") == "yes":
            continue
        if row.get(field):
            payload[field] = row[field]
    cleaned = {k: v for k, v in payload.items() if v not in (None, "")}
    if dec_ref is not None:
        cleaned["consignment_number"] = dec_ref
    return cleaned


def create_consignments(conn, schema: str, session: requests.Session, api_url: str, ids: list[int]) -> tuple[int, int]:
    cur = conn.cursor()
    where, params = id_filter("c.staging_id", ids)
    cur.execute(
        f"""
        SELECT c.staging_id, c.goods_description, c.transport_document_number,
               c.controlled_goods, c.goods_domestic_status, c.destination_country,
               c.consignor_eori, c.consignee_eori, c.importer_eori, c.exporter_eori,
               c.consignor_name, c.consignee_name, c.importer_name, c.exporter_name,
               c.consignor_street_number, c.consignor_city,
               c.consignor_postcode, c.consignor_country,
               c.consignee_street_number, c.consignee_city,
               c.consignee_postcode, c.consignee_country,
               c.importer_street_number, c.importer_city,
               c.importer_postcode, c.importer_country,
               c.exporter_street_number, c.exporter_city,
               c.exporter_postcode, c.exporter_country,
               c.buyer_same_as_importer, c.seller_same_as_exporter,
               c.buyer_eori, c.buyer_name, c.buyer_street_and_number,
               c.buyer_city, c.buyer_postcode, c.buyer_country,
               c.seller_eori, c.seller_name, c.seller_street_and_number,
               c.seller_city, c.seller_postcode, c.seller_country,
               c.trader_reference, c.supervising_customs_office,
               c.customs_warehouse_identifier, c.ducr, c.no_sfd_reason,
               c.align_ukims, c.use_importer_sde, c.declaration_choice,
               c.generate_SD, c.container_indicator, e.ens_reference
        FROM [{schema}].StagingConsignments c
        JOIN [{schema}].StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
        WHERE c.status = 'VALIDATED'
          AND c.dec_reference IS NULL
          AND e.ens_reference IS NOT NULL
          {where}
        ORDER BY c.staging_id
        """,
        params,
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    print(f"\nConsignments to create: {len(rows)}")

    ok = failed = 0
    url = f"{api_url}/consignments"
    for row in rows:
        sid = row["staging_id"]
        payload = build_consignment_payload(row, op_type="create", ens_ref=row["ens_reference"], dec_ref="")
        print(f"  #{sid}: POST /consignments...", end=" ")
        result = post_tss(session, url, payload)
        log_call(cur, schema, sid, "CREATE_CONSIGNMENT", result, url, payload)
        if result["success"]:
            cur.execute(
                f"""
                UPDATE [{schema}].StagingConsignments
                SET status='CREATED', dec_reference=?, tss_status=?,
                    error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                WHERE staging_id=?
                """,
                [result["reference"], result["status"], sid],
            )
            conn.commit()
            ok += 1
            print(f"OK -> {result['reference']}")
        else:
            cur.execute(
                f"""
                UPDATE [{schema}].StagingConsignments
                SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?
                """,
                [result["message"][:4000], sid],
            )
            conn.commit()
            failed += 1
            print(f"FAILED: {result['message'][:100]}")
    return ok, failed


def update_existing_consignments(conn, schema: str, session: requests.Session, api_url: str, ids: list[int]) -> tuple[int, int]:
    cur = conn.cursor()
    where, params = id_filter("c.staging_id", ids)
    cur.execute(
        f"""
        SELECT c.staging_id, c.dec_reference, c.goods_description, c.transport_document_number,
               c.controlled_goods, c.goods_domestic_status, c.destination_country,
               c.consignor_eori, c.consignee_eori, c.importer_eori, c.exporter_eori,
               c.consignor_name, c.consignee_name, c.importer_name, c.exporter_name,
               c.consignor_street_number, c.consignor_city,
               c.consignor_postcode, c.consignor_country,
               c.consignee_street_number, c.consignee_city,
               c.consignee_postcode, c.consignee_country,
               c.importer_street_number, c.importer_city,
               c.importer_postcode, c.importer_country,
               c.exporter_street_number, c.exporter_city,
               c.exporter_postcode, c.exporter_country,
               c.buyer_same_as_importer, c.seller_same_as_exporter,
               c.buyer_eori, c.buyer_name, c.buyer_street_and_number,
               c.buyer_city, c.buyer_postcode, c.buyer_country,
               c.seller_eori, c.seller_name, c.seller_street_and_number,
               c.seller_city, c.seller_postcode, c.seller_country,
               c.trader_reference, c.supervising_customs_office,
               c.customs_warehouse_identifier, c.ducr, c.no_sfd_reason,
               c.align_ukims, c.use_importer_sde, c.declaration_choice,
               c.generate_SD, c.container_indicator, c.tss_status
        FROM [{schema}].StagingConsignments c
        WHERE c.status = 'VALIDATED'
          AND c.dec_reference IS NOT NULL
          AND UPPER(REPLACE(COALESCE(c.tss_status, ''), '_', ' ')) IN (
              'TRADER INPUT REQUIRED', 'AMENDMENT REQUIRED', 'ERROR', 'DO NOT LOAD'
          )
          {where}
        ORDER BY c.staging_id
        """,
        params,
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    print(f"\nConsignments to update: {len(rows)}")

    ok = failed = 0
    url = f"{api_url}/consignments"
    for row in rows:
        sid = row["staging_id"]
        already_submitted = consignment_has_successful_submit(cur, schema, sid)
        payload = build_consignment_payload(row, op_type="update", dec_ref=row["dec_reference"])
        print(f"  #{sid}: POST /consignments update...", end=" ")
        result = post_tss(session, url, payload)
        log_call(cur, schema, sid, "UPDATE_CONSIGNMENT", result, url, payload)
        if result["success"]:
            cur.execute(
                f"""
                UPDATE [{schema}].StagingConsignments
                SET status=?, tss_status=?,
                    error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                WHERE staging_id=?
                """,
                [local_status_after_consignment_update(already_submitted), result["status"] or "UPDATED", sid],
            )
            conn.commit()
            ok += 1
            suffix = "already submitted; sync next" if already_submitted else "ready for submit"
            print(f"OK ({suffix})")
        else:
            cur.execute(
                f"""
                UPDATE [{schema}].StagingConsignments
                SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?
                """,
                [result["message"][:4000], sid],
            )
            conn.commit()
            failed += 1
            print(f"FAILED: {result['message'][:100]}")
    return ok, failed


def create_goods(conn, schema: str, session: requests.Session, api_url: str, consignment_ids: list[int], goods_ids: list[int]) -> tuple[int, int]:
    cur = conn.cursor()
    filters = []
    params = []
    where, where_params = id_filter("g.staging_cons_id", consignment_ids)
    filters.append(where)
    params.extend(where_params)
    where, where_params = id_filter("g.staging_id", goods_ids)
    filters.append(where)
    params.extend(where_params)
    cur.execute(
        f"""
        SELECT g.staging_id, g.goods_description, g.type_of_packages,
               g.number_of_packages, g.package_marks, g.gross_mass_kg, g.net_mass_kg,
               g.controlled_goods, g.controlled_goods_type, g.commodity_code,
               g.procedure_code, g.additional_procedure_code, g.country_of_origin,
               g.taric_code, g.item_invoice_amount, g.item_invoice_currency, c.dec_reference
        FROM [{schema}].StagingGoodsItems g
        JOIN [{schema}].StagingConsignments c ON c.staging_id = g.staging_cons_id
        WHERE g.status = 'VALIDATED'
          AND c.dec_reference IS NOT NULL
          {''.join(filters)}
        ORDER BY g.staging_id
        """,
        params,
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    print(f"\nGoods to create: {len(rows)}")

    ok = failed = 0
    url = f"{api_url}/goods"
    for row in rows:
        sid = row["staging_id"]
        payload = {
            "op_type": "create",
            "consignment_number": row["dec_reference"],
            "goods_id": "",
            "goods_description": row.get("goods_description"),
            "type_of_packages": row.get("type_of_packages"),
            "number_of_packages": str(row.get("number_of_packages") or 1),
            "package_marks": row.get("package_marks") or "ADDR",
            "gross_mass_kg": format_tss_decimal(row.get("gross_mass_kg") or 0),
            "net_mass_kg": format_tss_decimal(row.get("net_mass_kg")),
            "controlled_goods": row.get("controlled_goods"),
            "controlled_goods_type": row.get("controlled_goods_type"),
            "commodity_code": row.get("commodity_code"),
            "procedure_code": row.get("procedure_code"),
            "additional_procedure_code": row.get("additional_procedure_code"),
            "country_of_origin": row.get("country_of_origin"),
            "taric_code": normalise_taric_code(row.get("taric_code")),
            "item_invoice_amount": format_tss_decimal(row.get("item_invoice_amount")),
            "item_invoice_currency": row.get("item_invoice_currency"),
        }
        payload = {k: v for k, v in payload.items() if v not in (None, "")}
        print(f"  #{sid}: POST /goods...", end=" ")
        result = post_tss(session, url, payload)
        log_call(cur, schema, sid, "CREATE_GOODS", result, url, payload)
        if result["success"]:
            cur.execute(
                f"""
                UPDATE [{schema}].StagingGoodsItems
                SET status='CREATED', goods_id=?, tss_status=?,
                    error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                WHERE staging_id=?
                """,
                [result["reference"], result["status"], sid],
            )
            conn.commit()
            ok += 1
            print(f"OK -> {result['reference']}")
        else:
            cur.execute(
                f"""
                UPDATE [{schema}].StagingGoodsItems
                SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?
                """,
                [result["message"][:4000], sid],
            )
            conn.commit()
            failed += 1
            print(f"FAILED: {result['message'][:100]}")
    return ok, failed


def submit_consignments(conn, schema: str, session: requests.Session, api_url: str, ids: list[int]) -> tuple[int, int]:
    cur = conn.cursor()
    where, params = id_filter("c.staging_id", ids)
    cur.execute(
        f"""
        SELECT c.staging_id, c.dec_reference, c.tss_status
        FROM [{schema}].StagingConsignments c
        WHERE c.status IN ('CREATED', 'VALIDATED')
          AND c.dec_reference IS NOT NULL
          {where}
        ORDER BY c.staging_id
        """,
        params,
    )
    rows = cur.fetchall()
    print(f"\nConsignments to final submit/check: {len(rows)}")

    ok = failed = 0
    url = f"{api_url}/consignments"
    for sid, dec_ref, tss_status in rows:
        if consignment_has_successful_submit(cur, schema, sid):
            cur.execute(
                f"""
                UPDATE [{schema}].StagingConsignments
                SET status='SUBMITTED', error_message=NULL, updated_at=SYSUTCDATETIME()
                WHERE staging_id=? AND status='CREATED'
                """,
                [sid],
            )
            conn.commit()
            print(f"  #{sid}: skipped, DEC was already submitted successfully; run sync/status next")
            continue
        ready, total, blocked = consignment_ready_for_submit(cur, schema, sid)
        if not ready:
            print(f"  #{sid}: skipped, goods not ready ({total} total, {blocked} blocked)")
            continue
        if (tss_status or "").strip().upper().replace(" ", "_") not in {"", "DRAFT", "CREATED", "UPDATED"}:
            print(f"  #{sid}: skipped, TSS status is {tss_status}")
            continue
        payload = {"op_type": "submit", "consignment_number": dec_ref}
        print(f"  #{sid}: POST /consignments submit...", end=" ")
        result = post_tss(session, url, payload)
        log_call(cur, schema, sid, "SUBMIT_CONSIGNMENT", result, url, payload)
        if result["success"]:
            cur.execute(
                f"""
                UPDATE [{schema}].StagingConsignments
                SET status='SUBMITTED', tss_status=?,
                    error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                WHERE staging_id=?
                """,
                [result["status"] or "SUBMITTED", sid],
            )
            conn.commit()
            ok += 1
            print("OK")
        else:
            cur.execute(
                f"""
                UPDATE [{schema}].StagingConsignments
                SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?
                """,
                [result["message"][:4000], sid],
            )
            conn.commit()
            failed += 1
            print(f"FAILED: {result['message'][:100]}")
    return ok, failed


def main() -> int:
    print("Standalone Submitter - Consignment + Goods")
    print("=" * 48)
    schema = ask("Tenant schema", os.environ.get("TENANT_SCHEMA", "BKD"))
    consignment_ids = parse_ids(ask("Consignment staging IDs optional comma-separated", ""))
    goods_ids = parse_ids(ask("Goods staging IDs optional comma-separated", ""))
    skip_final_submit = yn("Skip final consignment submit? yes/no", default=False)

    conn = connect_sql()
    try:
        tss = resolve_tss(conn, schema)
        session = make_session(tss["username"], tss["password"])
        print(f"\nUsing API: {tss['api_url']}")

        ok1, fail1 = create_consignments(conn, schema, session, tss["api_url"], consignment_ids)
        ok_update, fail_update = update_existing_consignments(conn, schema, session, tss["api_url"], consignment_ids)
        ok2, fail2 = create_goods(conn, schema, session, tss["api_url"], consignment_ids, goods_ids)
        ok3 = fail3 = 0
        if not skip_final_submit:
            ok3, fail3 = submit_consignments(conn, schema, session, tss["api_url"], consignment_ids)

        print("\nDone")
        print(f"  Consignments create: OK={ok1} FAILED={fail1}")
        print(f"  Consignments update: OK={ok_update} FAILED={fail_update}")
        print(f"  Goods create:        OK={ok2} FAILED={fail2}")
        if not skip_final_submit:
            print(f"  Consignment submit:  OK={ok3} FAILED={fail3}")
        return 1 if (fail1 or fail_update or fail2 or fail3) else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
