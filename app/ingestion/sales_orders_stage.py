"""Stage parsed sales-order batches into production STG tables.

Anti-duplicate keys (per tenant + env):
    ENS:        conveyance_ref + arrival_date_time + identity_no_of_transport
    Consignment: ens_id + document_no
    Goods:       document_no + line_no + sku

Uses MERGE WITH (HOLDLOCK) so two concurrent runs cannot insert the same row.
All writes go through staging tables only; nothing calls TSS here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.ingestion.excel_sales_orders import (
    EmailMetadata,
    ParsedConsignment,
    ParsedGoodsLine,
    ParsedSalesOrders,
    resolve_product_master,
)
from app.pipeline_validation import normalise_decimal_scale, normalise_package_type, strict_masterdata_validation_enabled
from app.tss_text import tss_safe_text_suggestion

logger = logging.getLogger(__name__)


@dataclass
class StagedSalesOrdersResult:
    ens_staging_id: int | None = None
    ens_inserted: bool = False
    consignments: list[dict] = field(default_factory=list)  # {staging_id, document_no, inserted, goods: [...]}
    diff_flags: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_review: bool = True


BKD_SALES_ORDER_PRODUCT_FALLBACKS: dict[str, dict[str, Any]] = {
    # Birkdale post supports. Master data remains authoritative; this only
    # prevents known BKD Sales Orders SKUs from staging without a commodity code
    # when the product catalogue has not yet been seeded in an environment.
    "B2621003": {
        "description": 'FENCEMATE SWIFT CLAMP BOLT DWN POST SUPP | 4"X4" 100X100 E-BROW',
        "goods_description": 'FENCEMATE SWIFT CLAMP BOLT DWN POST SUPP | 4"X4" 100X100 E-BROW',
        "commodity_code": "8302100090",
    },
    "B2620753": {
        "description": 'FENCEMATE SWIFT CLAMP BOLT DOWN POST SUPP | 3"X3" 75X75 E-BROW',
        "goods_description": 'FENCEMATE SWIFT CLAMP BOLT DOWN POST SUPP | 3"X3" 75X75 E-BROW',
        "commodity_code": "8302100090",
    },
}


def _builtin_sales_order_product_fallback(sku: str) -> dict[str, Any] | None:
    fallback = BKD_SALES_ORDER_PRODUCT_FALLBACKS.get((sku or "").strip().upper())
    return dict(fallback) if fallback else None


def _non_declarable_sales_order_goods_reason(line: ParsedGoodsLine) -> str:
    """Return why a Sales Order line should not become customs goods."""

    raw = line.raw or {}
    sku = str(line.sku or "").strip().upper()
    description_bits = [
        raw.get("goods_description"),
        raw.get("description"),
        raw.get("item_description"),
        raw.get("line_description"),
    ]
    description = " ".join(str(value or "") for value in description_bits).upper()

    if sku == "BAV25":
        return "Amazon voucher"
    if "AMAZON" in description and "VOUCHER" in description:
        return "Amazon voucher"
    return ""


def _sales_order_line_description(line: ParsedGoodsLine) -> str:
    raw = line.raw or {}
    return _goods_description_text(
        raw.get("goods_description"),
        raw.get("description"),
        raw.get("item_description"),
        raw.get("line_description"),
        line.sku,
    )


def _excluded_sales_order_goods(consignment: ParsedConsignment) -> list[dict[str, Any]]:
    excluded = []
    for line in consignment.goods:
        reason = _non_declarable_sales_order_goods_reason(line)
        if not reason:
            continue
        excluded.append({
            "line_no": line.line_no,
            "sku": line.sku,
            "description": _sales_order_line_description(line),
            "reason": reason,
            "action": "excluded_from_customs_goods",
            "sent_to_tss": False,
        })
    return excluded


def _consignment_metadata_json(c: ParsedConsignment) -> str:
    metadata: dict[str, Any] = {}
    note = _consignment_metadata_note(c)
    if note:
        metadata["source_note"] = note
    excluded = _excluded_sales_order_goods(c)
    if excluded:
        metadata["excluded_goods"] = excluded
    return json.dumps(metadata, ensure_ascii=True) if metadata else ""


def stage_sales_orders_batch(
    cursor,
    *,
    tenant_code: str,
    schema: str,
    master_schema: str,
    env_code: str,
    email_meta: EmailMetadata,
    parsed: ParsedSalesOrders,
    defaults,
    received_at: datetime,
    auto_create_if_clean: bool = False,
    existing_ens_staging_id: int | None = None,
    create_header_from_email_body: bool = False,
) -> StagedSalesOrdersResult:
    """Stage email + Excel batch. Returns staged ids + review flags.

    Liberal staging policy: poor email-body parsing never blocks consignment
    creation. Excel is the source of truth for orders; the ENS row is created
    as a draft (PENDING_REVIEW) and the operator fills the missing transport
    metadata before submit. Email-driven Sales Orders can also preserve the ENS
    header from the carrier body when workbook rows fail to parse.
    """
    result = StagedSalesOrdersResult()

    if not parsed.consignments:
        result.blockers.append("No consignments parsed from Excel")
    if not existing_ens_staging_id:
        result.warnings.extend(email_meta.parse_warnings)
    result.warnings.extend(parsed.parse_warnings)

    # ── Compare email metadata to tenant defaults — produce diff_flags ──
    if not existing_ens_staging_id:
        result.diff_flags.extend(_diff_email_vs_defaults(email_meta, defaults))

    # ── Soft validations: missing transport metadata is a warning, not a blocker ──
    if not existing_ens_staging_id:
        result.warnings.extend(_transport_metadata_warnings(email_meta, defaults))

    # ── ENS upsert (always proceeds if Excel had consignments) ──
    can_create_header = bool(parsed.consignments)
    if create_header_from_email_body and _email_header_metadata_present(email_meta):
        can_create_header = True

    if existing_ens_staging_id:
        result.ens_staging_id = int(existing_ens_staging_id)
        result.ens_inserted = False
    elif can_create_header:
        ens_id, ens_inserted = _upsert_ens_header(
            cursor, schema, env_code, email_meta, defaults, received_at,
        )
        result.ens_staging_id = ens_id
        result.ens_inserted = ens_inserted

    # ── Consignments + goods ──
    if result.ens_staging_id:
        for cons in parsed.consignments:
            try:
                cons_summary = _stage_one_consignment(
                    cursor, schema, master_schema, env_code,
                    ens_staging_id=result.ens_staging_id,
                    consignment=cons,
                    defaults=defaults,
                    tenant_code=tenant_code,
                )
            except Exception as exc:
                logger.exception("Sales Orders consignment staging failed for %s", cons.document_no)
                cons_summary = _failed_consignment_summary(cons, exc)
            result.consignments.append(cons_summary)
            # Goods-level blockers (missing product master) downgrade to warnings
            # so the consignment row still exists for operator review/fix.
            result.warnings.extend(cons_summary.get("blockers", []))
            result.warnings.extend(cons_summary.get("warnings", []))
            result.blockers.extend(cons_summary.get("hard_blockers", []))

    # ── Decide review state ──
    # Hard blockers always force review. Warnings/diff_flags are informational
    # only — when auto_create_if_clean is active the operator chose to submit
    # regardless, so we proceed and let them review TSS feedback instead.
    if result.blockers:
        result.needs_review = True
    else:
        result.needs_review = not auto_create_if_clean
    if result.ens_staging_id and not result.needs_review:
        _mark_sales_orders_ready(cursor, schema, result)

    return result


# Step 05 - ENS header creation (docs/README.md)
def stage_sales_orders_details_header_stg(
    cursor,
    *,
    tenant_code: str,
    env_code: str,
    email_meta: EmailMetadata,
    defaults,
    received_at: datetime,
    source: str = "EXCEL_SALES_ORDERS_DETAILS",
    overwrite: bool = True,
) -> tuple[int, bool]:
    """Create/update the production STG ENS header from a details email."""

    return _upsert_ens_header_stg(
        cursor,
        tenant_code=tenant_code,
        env_code=env_code,
        email_meta=email_meta,
        defaults=defaults,
        received_at=received_at,
        source=source,
        overwrite=overwrite,
    )


# Step 08 - consignments + goods creation (docs/README.md)
def stage_sales_orders_batch_stg(
    cursor,
    *,
    tenant_code: str,
    schema: str,
    master_schema: str,
    env_code: str,
    email_meta: EmailMetadata,
    parsed: ParsedSalesOrders,
    defaults,
    received_at: datetime,
    auto_create_if_clean: bool = False,
    existing_ens_staging_id: int | None = None,
    create_header_from_email_body: bool = False,
    ing_line_ids: dict[tuple[str, int | None, str], int] | None = None,
) -> StagedSalesOrdersResult:
    """Stage Sales Orders into the production STG.BKD_* contract."""

    result = StagedSalesOrdersResult()

    if not parsed.consignments:
        result.blockers.append("No consignments parsed from Excel")
    if not existing_ens_staging_id:
        result.warnings.extend(email_meta.parse_warnings)
        result.diff_flags.extend(_diff_email_vs_defaults(email_meta, defaults))
        result.warnings.extend(_transport_metadata_warnings(email_meta, defaults))
    result.warnings.extend(parsed.parse_warnings)

    can_create_header = bool(parsed.consignments)
    if create_header_from_email_body and _email_header_metadata_present(email_meta):
        can_create_header = True

    if existing_ens_staging_id:
        result.ens_staging_id = int(existing_ens_staging_id)
        result.ens_inserted = False
    elif can_create_header:
        ens_id, ens_inserted = _upsert_ens_header_stg(
            cursor,
            tenant_code=tenant_code,
            env_code=env_code,
            email_meta=email_meta,
            defaults=defaults,
            received_at=received_at,
        )
        result.ens_staging_id = ens_id
        result.ens_inserted = ens_inserted

    if result.ens_staging_id:
        for cons in parsed.consignments:
            try:
                cons_summary = _stage_one_consignment_stg(
                    cursor,
                    master_schema=master_schema,
                    env_code=env_code,
                    ens_staging_id=result.ens_staging_id,
                    consignment=cons,
                    defaults=defaults,
                    tenant_code=tenant_code,
                    ing_line_ids=ing_line_ids or {},
                )
            except Exception as exc:
                logger.exception("Sales Orders STG consignment staging failed for %s", cons.document_no)
                cons_summary = _failed_consignment_summary(cons, exc)
            result.consignments.append(cons_summary)
            result.warnings.extend(cons_summary.get("blockers", []))
            result.warnings.extend(cons_summary.get("warnings", []))
            result.blockers.extend(cons_summary.get("hard_blockers", []))

    result.needs_review = bool(result.blockers) or not auto_create_if_clean
    if result.ens_staging_id and not result.needs_review:
        _mark_sales_orders_ready_stg(cursor, result)

    return result


def record_sales_orders_source_file(
    cursor,
    *,
    env_code: str,
    filename: str,
    xlsx_bytes: bytes,
    parsed: ParsedSalesOrders,
) -> int:
    """Record the inbound workbook in ING before normalized STG writes."""

    payload = xlsx_bytes or b""
    sha256 = hashlib.sha256(payload).hexdigest()
    record_count = sum(len(cons.goods) for cons in parsed.consignments)
    cursor.execute(
        """
        SELECT TOP 1 FileId
        FROM [ING].[BKD_SourceFileLog]
        WHERE EnvCode = ? AND FileSha256 = ?
        ORDER BY FileId DESC
        """,
        [env_code, sha256],
    )
    existing = cursor.fetchone()
    if existing:
        return int(existing[0])

    cursor.execute(
        """
        INSERT INTO [ING].[BKD_SourceFileLog]
            (EnvCode, FileKind, FileName, FilePath, FileSizeBytes,
             FileSha256, RecordCount)
        OUTPUT INSERTED.FileId
        VALUES (?, 'SALES_ORDERS_XLSX', ?, ?, ?, ?, ?)
        """,
        [
            env_code,
            (filename or "Sales Orders.xlsx")[:260],
            f"mailbox://{filename or 'Sales Orders.xlsx'}"[:500],
            len(payload),
            sha256,
            record_count,
        ],
    )
    return int(cursor.fetchone()[0])


def record_sales_orders_ing_lines(
    cursor,
    *,
    env_code: str,
    file_id: int,
    parsed: ParsedSalesOrders,
) -> dict[tuple[str, int | None, str], int]:
    """Persist parsed Sales Orders rows into ING and return STG link IDs."""

    line_ids: dict[tuple[str, int | None, str], int] = {}
    source_row = 0
    for consignment in parsed.consignments:
        for line in consignment.goods:
            source_row += 1
            key = _ing_line_key(consignment.document_no, line.line_no, line.sku)
            cursor.execute(
                """
                SELECT TOP 1 RecordId
                FROM [ING].[BKD_SalesOrderLine]
                WHERE EnvCode = ?
                  AND FileId = ?
                  AND ISNULL(DocumentNo, '') = ?
                  AND ISNULL(LineNumber, 0) = ?
                  AND ISNULL(ItemNo, '') = ?
                ORDER BY RecordId DESC
                """,
                [env_code, file_id, consignment.document_no or "", line.line_no or 0, line.sku or ""],
            )
            existing = cursor.fetchone()
            if existing:
                line_ids[key] = int(existing[0])
                continue

            cursor.execute(
                """
                INSERT INTO [ING].[BKD_SalesOrderLine]
                    (EnvCode, FileId, SourceRowNum, SellToCustomerNo,
                     ShipToName, ShipToAddress1, ShipToAddress2, ShipToCity,
                     ShipToCounty, ShipToPhoneNo, ShipToEmail, DocumentNo,
                     ItemNo, LineNumber, Quantity, QuantityBase, Amount,
                     LineAmountExclVat, UnitPriceExclVat, QtyPerUom,
                     UnitOfMeasureCode)
                OUTPUT INSERTED.RecordId
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    env_code,
                    file_id,
                    source_row,
                    consignment.sell_to_customer_no or None,
                    consignment.ship_to_name or None,
                    consignment.ship_to_address or None,
                    consignment.ship_to_address_2 or None,
                    consignment.ship_to_city or None,
                    consignment.ship_to_county or None,
                    consignment.ship_to_phone or None,
                    consignment.ship_to_email or None,
                    consignment.document_no or None,
                    line.sku or None,
                    line.line_no,
                    line.quantity,
                    line.quantity_base,
                    line.amount if line.amount is not None else line.line_amount_excl_vat,
                    line.line_amount_excl_vat,
                    line.unit_price_excl_vat,
                    line.qty_per_uom,
                    line.uom_code or None,
                ],
            )
            line_ids[key] = int(cursor.fetchone()[0])
    return line_ids


def _ing_line_key(document_no: str, line_no: int | None, sku: str) -> tuple[str, int | None, str]:
    return (document_no or "", line_no, sku or "")


# ── Diffing helpers ────────────────────────────────────────────────────


def _email_header_metadata_present(email_meta: EmailMetadata) -> bool:
    return any(
        bool(str(value or "").strip())
        for value in (
            email_meta.raw_block,
            email_meta.conveyance_ref,
            email_meta.arrival_date_time,
            email_meta.identity_no_of_transport,
            email_meta.arrival_port,
            email_meta.place_of_loading,
            email_meta.place_of_unloading,
        )
    )


def _exception_message(exc: Exception) -> str:
    raw = str(exc).strip() or exc.__class__.__name__
    lowered = raw.lower()
    if "string or binary data would be truncated" in lowered:
        column_match = re.search(r"column '([^']+)'", raw, flags=re.IGNORECASE)
        value_match = re.search(r"truncated value: '([^']*)'", raw, flags=re.IGNORECASE)
        column = (column_match.group(1) if column_match else "a field").strip()
        value = (value_match.group(1) if value_match else "").strip()
        if column.lower() == "type_of_packages":
            suffix = f" Source value: {value}." if value else ""
            return (
                "Invalid package type from the source line. Delivery/service charge "
                "rows are skipped automatically; real goods must use a TSS package "
                f"code such as PK or BG.{suffix}"
            )
        return f"{column} is too long for the production staging table. Please shorten it and retry."
    return raw


def _failed_consignment_summary(consignment: ParsedConsignment, exc: Exception) -> dict:
    message = f"{consignment.document_no or '(unknown document)'}: consignment staging failed - {_exception_message(exc)}"
    return {
        "document_no": consignment.document_no,
        "staging_id": None,
        "inserted": False,
        "goods": [],
        "blockers": [message],
        "hard_blockers": [message],
        "warnings": [],
    }


def _failed_goods_summary(consignment: ParsedConsignment, line: ParsedGoodsLine, exc: Exception) -> dict:
    message = (
        f"{consignment.document_no or '(unknown document)'}/{line.sku or line.line_no}: "
        f"goods staging failed - {_exception_message(exc)}"
    )
    return {
        "document_no": consignment.document_no,
        "line_no": line.line_no,
        "sku": line.sku,
        "staging_id": None,
        "inserted": False,
        "blockers": [message],
        "hard_blockers": [message],
        "warnings": [],
    }


def _mark_sales_orders_ready(cursor, schema: str, result: StagedSalesOrdersResult) -> None:
    """Move clean auto-created rows out of review-only status."""
    ens_id = result.ens_staging_id
    if not ens_id:
        return

    def update(table: str, id_column: str, ids: list[int], status: str = "PENDING") -> None:
        if not ids:
            return
        cols = _columns(cursor, schema, table)
        assignments = []
        params: list[Any] = []
        if "status" in cols:
            assignments.append("[status] = ?")
            params.append(status)
        if "tss_status" in cols:
            assignments.append("[tss_status] = ?")
            params.append(status)
        if "updated_at" in cols:
            assignments.append("updated_at = SYSUTCDATETIME()")
        if not assignments or id_column.lower() not in cols:
            return
        placeholders = ", ".join("?" for _ in ids)
        cursor.execute(
            f"UPDATE [{schema}].{table} SET {', '.join(assignments)} WHERE [{id_column}] IN ({placeholders})",
            params + ids,
        )

    cons_ids = [int(c["staging_id"]) for c in result.consignments if c.get("staging_id")]
    goods_ids = [
        int(g["staging_id"])
        for c in result.consignments
        for g in c.get("goods", [])
        if g.get("staging_id")
    ]
    update("StagingEnsHeaders", "staging_id", [int(ens_id)])
    update("StagingConsignments", "staging_id", cons_ids)
    update("StagingGoodsItems", "staging_id", goods_ids)


def _diff_email_vs_defaults(email_meta: EmailMetadata, defaults) -> list[str]:
    """Compare key fields parsed from email vs tenant INGEST_AUTO defaults.

    Each diff returned as 'field: parsed=X default=Y' so the review UI shows it.
    """
    if not (email_meta.raw_block or "").strip():
        return []

    flags: list[str] = []

    def cmp(field_name: str, parsed_val, default_val):
        p = (parsed_val or "").strip() if isinstance(parsed_val, str) else parsed_val
        d = (default_val or "").strip() if isinstance(default_val, str) else default_val
        if not p or not d:
            return
        if str(p) != str(d):
            flags.append(f"{field_name}: parsed={p!r} default={d!r}")

    cmp("movement_type", email_meta.movement_type, defaults.movement_type)
    cmp("nationality_of_transport", email_meta.nationality_of_transport, defaults.nationality_of_transport)
    cmp("carrier_eori", email_meta.carrier_eori, defaults.carrier_eori)
    cmp("arrival_port", email_meta.arrival_port, defaults.arrival_port)
    cmp("place_of_loading", email_meta.place_of_loading, defaults.place_of_loading)
    cmp("place_of_unloading", email_meta.place_of_unloading, defaults.place_of_unloading)
    cmp("transport_charges", email_meta.transport_charges, defaults.transport_charges)
    return flags


# ── ENS header upsert ──────────────────────────────────────────────────


def _transport_metadata_warnings(email_meta: EmailMetadata, defaults) -> list[str]:
    warnings: list[str] = []
    movement_type = email_meta.movement_type or defaults.movement_type
    missing_transport_fields: list[str] = []
    if not email_meta.movement_type:
        missing_transport_fields.append("movement_type")
    if not email_meta.conveyance_ref:
        missing_transport_fields.append("conveyance_ref (ICR)")
    if not email_meta.arrival_date_time:
        missing_transport_fields.append("arrival_date_time")
    if not email_meta.identity_no_of_transport:
        missing_transport_fields.append("identity_no_of_transport")
    if not email_meta.arrival_port:
        missing_transport_fields.append("arrival_port")
    if not email_meta.place_of_loading:
        missing_transport_fields.append("place_of_loading")
    if not email_meta.place_of_unloading:
        missing_transport_fields.append("place_of_unloading")
    if not email_meta.transport_charges:
        missing_transport_fields.append("transport_charges")
    if movement_type == "3a":
        if not email_meta.type_of_passive_transport:
            missing_transport_fields.append("type_of_passive_transport")
        if not email_meta.place_of_acceptance_same_as_loading:
            missing_transport_fields.append("place_of_acceptance_same_as_loading")
        if not email_meta.place_of_delivery_same_as_unloading:
            missing_transport_fields.append("place_of_delivery_same_as_unloading")
    if missing_transport_fields:
        warnings.append(
            "Email body did not provide: " + ", ".join(missing_transport_fields)
            + " - ENS staged as draft, operator must complete before submit"
        )
    return warnings


def _upsert_ens_header(
    cursor, schema: str, env_code: str,
    email_meta: EmailMetadata, defaults, received_at: datetime,
    *, source: str = "EXCEL_SALES_ORDERS", overwrite: bool = False,
) -> tuple[int, bool]:
    """Upsert the ENS row using the best natural key available in this schema.

    Returns (staging_id, inserted_bool).
    """
    cols = _columns(cursor, schema, "StagingEnsHeaders")
    arrival = email_meta.arrival_date_time or _resolve_default_arrival(defaults, received_at)

    # When the email body cannot be parsed (no carrier block, malformed forward,
    # missing fields), synthesize a unique-but-stable conveyance_ref so the
    # natural key still works and the operator sees a clear DRAFT label to fix.
    parsed_conveyance = (email_meta.conveyance_ref or "").strip()
    conveyance_ref = parsed_conveyance or _synthetic_conveyance_ref(received_at)
    is_draft_ref = not parsed_conveyance

    label_prefix = "DRAFT " if is_draft_ref else ""

    payload: dict[str, Any] = {
        "env_code": env_code,
        "movement_type": email_meta.movement_type or defaults.movement_type,
        "type_of_passive_transport": email_meta.type_of_passive_transport or "",
        "identity_no_of_transport": email_meta.identity_no_of_transport or defaults.identity_no_of_transport,
        "nationality_of_transport": email_meta.nationality_of_transport or defaults.nationality_of_transport,
        "conveyance_ref": conveyance_ref,
        "arrival_date_time": arrival,
        "arrival_port": email_meta.arrival_port or defaults.arrival_port,
        "place_of_loading": email_meta.place_of_loading or defaults.place_of_loading,
        "place_of_acceptance_same_as_loading": email_meta.place_of_acceptance_same_as_loading,
        "place_of_acceptance": email_meta.place_of_acceptance,
        "place_of_unloading": email_meta.place_of_unloading or defaults.place_of_unloading,
        "place_of_delivery_same_as_unloading": email_meta.place_of_delivery_same_as_unloading,
        "place_of_delivery": email_meta.place_of_delivery,
        "transport_charges": email_meta.transport_charges or defaults.transport_charges,
        "carrier_eori": email_meta.carrier_eori or defaults.carrier_eori or "",
        "carrier_name": email_meta.carrier_name or defaults.carrier_name or "",
        "carrier_street_number": email_meta.carrier_street_number or defaults.carrier_street_number or "",
        "carrier_city": email_meta.carrier_city or defaults.carrier_city or "",
        "carrier_postcode": email_meta.carrier_postcode or defaults.carrier_postcode or "",
        "carrier_country": email_meta.carrier_country or defaults.carrier_country or "",
        "haulier_eori": email_meta.haulier_eori or defaults.haulier_eori or "",
        "status": "PENDING_REVIEW",
        "tss_status": "PENDING_REVIEW",
        "source": source,
        "label": f"{label_prefix}Sales-order batch {conveyance_ref}",
    }
    if is_draft_ref:
        payload["error_message"] = (
            "Email body did not provide carrier transport metadata. "
            "Operator must complete conveyance_ref/ICR, identity_no_of_transport, "
            "and arrival_date_time before submit."
        )

    # Preferred natural key = conveyance_ref + arrival_date_time + identity.
    # Older BKD schemas may not have all optional transport columns yet, so the
    # lookup degrades to the stable generated label instead of referencing a
    # missing column.
    ck_conv = conveyance_ref
    ck_arrival = arrival
    ck_identity = email_meta.identity_no_of_transport or ""
    existing = _find_existing_ens_header(
        cursor, schema, cols, payload,
        conveyance_ref=ck_conv,
        arrival=ck_arrival,
        identity=ck_identity,
    )

    if existing:
        # UPDATE only blank-able fields by default; pasted DETAILS can opt into
        # an overwrite because the operator is deliberately refreshing metadata.
        while True:
            values = _payload_values_for_columns(payload, cols)
            if not values:
                raise RuntimeError("StagingEnsHeaders has no usable columns for sales-order ingest")
            if overwrite:
                sets = ", ".join(f"[{k}] = ?" for k, _ in values)
            else:
                sets = ", ".join(
                    f"[{k}] = COALESCE(NULLIF([{k}], ''), ?)" if isinstance(v, str) else f"[{k}] = COALESCE([{k}], ?)"
                    for k, v in values
                )
            params = [v for _, v in values] + [existing[0]]
            if "updated_at" in cols:
                sets += ", updated_at = SYSUTCDATETIME()"
            try:
                cursor.execute(
                    f"UPDATE [{schema}].StagingEnsHeaders SET {sets} WHERE staging_id = ?",
                    params,
                )
                break
            except Exception as exc:
                if not _drop_invalid_optional_column(cols, exc):
                    raise
        return existing[0], False

    while True:
        values = _payload_values_for_columns(payload, cols)
        if not values:
            raise RuntimeError("StagingEnsHeaders has no usable columns for sales-order ingest")

        insert_cols = [k for k, _ in values]
        placeholders = ["?"] * len(values)
        if "created_at" in cols:
            insert_cols.append("created_at")
            placeholders.append("SYSUTCDATETIME()")
        if "updated_at" in cols:
            insert_cols.append("updated_at")
            placeholders.append("SYSUTCDATETIME()")

        try:
            cursor.execute(
                f"INSERT INTO [{schema}].StagingEnsHeaders ({', '.join(f'[{c}]' for c in insert_cols)}) "
                f"OUTPUT INSERTED.staging_id VALUES ({', '.join(placeholders)})",
                [v for _, v in values],
            )
            return int(cursor.fetchone()[0]), True
        except Exception as exc:
            if not _drop_invalid_optional_column(cols, exc):
                raise


def _find_existing_ens_header(
    cursor,
    schema: str,
    cols: set[str],
    payload: dict[str, Any],
    *,
    conveyance_ref: str,
    arrival: datetime,
    identity: str,
):
    while True:
        lookup_where, lookup_params = _ens_lookup_where(
            cols, payload,
            conveyance_ref=conveyance_ref,
            arrival=arrival,
            identity=identity,
        )
        if not lookup_where:
            return None
        try:
            cursor.execute(
                f"""
                SELECT TOP 1 staging_id FROM [{schema}].StagingEnsHeaders
                WHERE {lookup_where}
                """,
                lookup_params,
            )
            return cursor.fetchone()
        except Exception as exc:
            if not _drop_invalid_optional_column(cols, exc):
                raise


def _payload_values_for_columns(payload: dict[str, Any], cols: set[str]) -> list[tuple[str, Any]]:
    aliases = {
        "place_of_acceptance_same_as_loading": ("place_of_acceptance_same",),
        "place_of_delivery_same_as_unloading": ("place_of_delivery_same",),
    }
    values: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for key, value in payload.items():
        if value in (None, ""):
            continue
        for column in (key, *aliases.get(key, ())):
            lowered = column.lower()
            if lowered in seen or lowered not in cols:
                continue
            values.append((column, value))
            seen.add(lowered)
    return values


def _invalid_column_name(exc: Exception) -> str | None:
    match = re.search(r"Invalid column name '([^']+)'", str(exc), re.IGNORECASE)
    return match.group(1).lower() if match else None


def _drop_invalid_optional_column(cols: set[str], exc: Exception) -> bool:
    column = _invalid_column_name(exc)
    if column and column in cols:
        cols.discard(column)
        return True
    return False


def _ens_lookup_where(
    cols: set[str],
    payload: dict[str, Any],
    *,
    conveyance_ref: str,
    arrival: datetime,
    identity: str,
) -> tuple[str, list[Any]]:
    """Build a schema-aware ENS staging lookup without optional missing columns."""
    predicates: list[str] = []
    params: list[Any] = []

    def add_eq(column: str, value: Any) -> None:
        if column in cols and value not in (None, ""):
            predicates.append(f"[{column}] = ?")
            params.append(value)

    add_eq("env_code", payload.get("env_code"))
    add_eq("conveyance_ref", conveyance_ref)
    add_eq("arrival_date_time", arrival)
    if "identity_no_of_transport" in cols:
        predicates.append("ISNULL([identity_no_of_transport], '') = ?")
        params.append(identity or "")

    if "conveyance_ref" not in cols and "label" in cols:
        add_eq("label", payload.get("label"))

    return " AND ".join(predicates), params


def _resolve_default_arrival(defaults, received_at: datetime) -> datetime:
    try:
        return datetime.strptime(defaults.build_arrival_datetime(now=received_at), "%d/%m/%Y %H:%M:%S")
    except (AttributeError, ValueError, TypeError):
        return received_at


def _synthetic_conveyance_ref(received_at: datetime) -> str:
    """Stable placeholder ref for ENS rows where the email body had no ICR.

    Format: DRAFT-YYYYMMDDHHMM. Two batches in the same minute resolve to the
    same row (idempotent), which is the desired anti-dup behavior. The operator
    edits this ref to the real ICR before submit.
    """
    return f"DRAFT-{received_at.strftime('%Y%m%d%H%M')}"


def _tss_datetime_text(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M:%S")
    return str(value or "").strip()


def _upsert_ens_header_stg(
    cursor,
    *,
    tenant_code: str,
    env_code: str,
    email_meta: EmailMetadata,
    defaults,
    received_at: datetime,
    source: str = "EXCEL_SALES_ORDERS",
    overwrite: bool = False,
) -> tuple[int, bool]:
    cols = _columns(cursor, "STG", "BKD_ENS_Headers")
    arrival = email_meta.arrival_date_time or _resolve_default_arrival(defaults, received_at)
    arrival_text = _tss_datetime_text(arrival)
    parsed_conveyance = (email_meta.conveyance_ref or "").strip()
    conveyance_ref = parsed_conveyance or _synthetic_conveyance_ref(received_at)
    is_draft_ref = not parsed_conveyance
    label_prefix = "DRAFT " if is_draft_ref else ""

    payload: dict[str, Any] = {
        "ClientCode": tenant_code,
        "sub_status": "PENDING",
        "source": source,
        "label": f"{label_prefix}Sales-order batch {conveyance_ref}",
        "movement_type": email_meta.movement_type or defaults.movement_type,
        "type_of_passive_transport": email_meta.type_of_passive_transport or "",
        "identity_no_of_transport": email_meta.identity_no_of_transport or defaults.identity_no_of_transport,
        "nationality_of_transport": email_meta.nationality_of_transport or defaults.nationality_of_transport,
        "conveyance_ref": conveyance_ref,
        "arrival_date_time": arrival_text,
        "arrival_port": email_meta.arrival_port or defaults.arrival_port,
        "place_of_loading": email_meta.place_of_loading or defaults.place_of_loading,
        "place_of_acceptance_same_as_loading": email_meta.place_of_acceptance_same_as_loading,
        "place_of_acceptance": email_meta.place_of_acceptance,
        "place_of_unloading": email_meta.place_of_unloading or defaults.place_of_unloading,
        "place_of_delivery_same_as_unloading": email_meta.place_of_delivery_same_as_unloading,
        "place_of_delivery": email_meta.place_of_delivery,
        "transport_charges": email_meta.transport_charges or defaults.transport_charges,
        "carrier_eori": email_meta.carrier_eori or defaults.carrier_eori or "",
        "carrier_name": email_meta.carrier_name or defaults.carrier_name or "",
        "carrier_street_number": email_meta.carrier_street_number or defaults.carrier_street_number or "",
        "carrier_city": email_meta.carrier_city or defaults.carrier_city or "",
        "carrier_postcode": email_meta.carrier_postcode or defaults.carrier_postcode or "",
        "carrier_country": email_meta.carrier_country or defaults.carrier_country or "",
        "haulier_eori": email_meta.haulier_eori or defaults.haulier_eori or "",
    }

    missing_for_operator: list[str] = []
    if is_draft_ref:
        missing_for_operator.append("conveyance_ref")
    if not payload.get("identity_no_of_transport"):
        missing_for_operator.append("identity_no_of_transport")
    if not payload.get("arrival_date_time"):
        missing_for_operator.append("arrival_date_time")

    if missing_for_operator:
        payload["validation_errors_json"] = json.dumps(
            {
                "missing": missing_for_operator,
                "message": "Email body did not provide complete carrier transport metadata.",
            },
            ensure_ascii=True,
        )

    existing = _find_existing_ens_header_stg(
        cursor,
        cols,
        tenant_code=tenant_code,
        source=source,
        conveyance_ref=conveyance_ref,
        arrival_text=arrival_text,
        identity=email_meta.identity_no_of_transport or "",
    )

    if existing:
        existing_id = int(existing[0])
        existing_tss_ref = str(existing[1] if len(existing) > 1 else "").strip()
        if existing_tss_ref:
            return existing_id, False
        values = _payload_values_for_columns(payload, cols)
        sets = _coalescing_sets(values, overwrite=overwrite)
        if "last_sub_status_change" in cols:
            sets.append("last_sub_status_change = SYSUTCDATETIME()")
        if "updated_at" in cols:
            sets.append("updated_at = SYSUTCDATETIME()")
        if "validation_errors_json" in cols and "validation_errors_json" not in payload:
            sets.append("[validation_errors_json] = NULL")
        if sets:
            cursor.execute(
                f"UPDATE [STG].[BKD_ENS_Headers] SET {', '.join(sets)} WHERE stg_header_id = ?",
                [v for _, v in values] + [existing_id],
            )
        return existing_id, False

    values = _payload_values_for_columns(payload, cols)
    if not values:
        raise RuntimeError("STG.BKD_ENS_Headers has no usable columns for sales-order ingest")
    insert_cols = [k for k, _ in values]
    placeholders = ["?"] * len(values)
    if "last_sub_status_change" in cols:
        insert_cols.append("last_sub_status_change")
        placeholders.append("SYSUTCDATETIME()")
    cursor.execute(
        f"INSERT INTO [STG].[BKD_ENS_Headers] ({', '.join(f'[{c}]' for c in insert_cols)}) "
        f"OUTPUT INSERTED.stg_header_id VALUES ({', '.join(placeholders)})",
        [v for _, v in values],
    )
    return int(cursor.fetchone()[0]), True


def _find_existing_ens_header_stg(
    cursor,
    cols: set[str],
    *,
    tenant_code: str,
    source: str,
    conveyance_ref: str,
    arrival_text: str,
    identity: str,
):
    select_cols = ["stg_header_id"]
    if "tss_ens_header_ref" in cols:
        select_cols.append("tss_ens_header_ref")
    predicates = ["ClientCode = ?", "conveyance_ref = ?"]
    params: list[Any] = [tenant_code, conveyance_ref]
    if "source" in cols:
        predicates.append("source = ?")
        params.append(source)
    if "arrival_date_time" in cols:
        predicates.append("arrival_date_time = ?")
        params.append(arrival_text)
    if "identity_no_of_transport" in cols:
        predicates.append("ISNULL(identity_no_of_transport, '') = ?")
        params.append(identity or "")
    cursor.execute(
        f"""
        SELECT TOP 1 {', '.join(select_cols)} FROM [STG].[BKD_ENS_Headers]
        WHERE {' AND '.join(predicates)}
        ORDER BY stg_header_id DESC
        """,
        params,
    )
    exact = cursor.fetchone()
    if exact:
        return exact

    if source != "EXCEL_SALES_ORDERS_DETAILS" or "source" not in cols:
        return None

    # DETAILS FOR emails are retried while the parser is being improved or when
    # the Graph caller times out. Reuse the open draft instead of creating a new
    # ENS each time one extracted field changes.
    fallback_predicates = ["ClientCode = ?", "COALESCE(source, '') = ?"]
    fallback_params: list[Any] = [tenant_code, source]
    if "sub_status" in cols:
        fallback_predicates.append(
            "UPPER(COALESCE(sub_status, '')) NOT IN ('CANCELLED', 'DELETED', 'SUBMITTED', 'COMPLETED')"
        )
    if "tss_ens_header_ref" in cols:
        fallback_predicates.append(
            "NULLIF(LTRIM(RTRIM(COALESCE(tss_ens_header_ref, ''))), '') IS NULL"
        )
    if "stg_created_at" in cols:
        fallback_predicates.append("stg_created_at >= DATEADD(hour, -36, SYSUTCDATETIME())")

    order_expr = "stg_header_id"
    if "updated_at" in cols and "stg_created_at" in cols:
        order_expr = "COALESCE(updated_at, stg_created_at)"
    elif "stg_created_at" in cols:
        order_expr = "stg_created_at"
    elif "updated_at" in cols:
        order_expr = "updated_at"

    cursor.execute(
        f"""
        SELECT TOP 1 {', '.join(select_cols)} FROM [STG].[BKD_ENS_Headers]
        WHERE {' AND '.join(fallback_predicates)}
        ORDER BY {order_expr} DESC, stg_header_id DESC
        """,
        fallback_params,
    )
    return cursor.fetchone()


def _coalescing_sets(values: list[tuple[str, Any]], *, overwrite: bool = False) -> list[str]:
    if overwrite:
        return [f"[{key}] = ?" for key, _ in values]
    return [
        f"[{key}] = COALESCE(NULLIF([{key}], ''), ?)" if isinstance(value, str)
        else f"[{key}] = COALESCE([{key}], ?)"
        for key, value in values
    ]


def _mark_sales_orders_ready_stg(cursor, result: StagedSalesOrdersResult) -> None:
    ids = {
        "BKD_ENS_Headers": ("stg_header_id", [result.ens_staging_id]),
        "BKD_ENS_Consignments": (
            "stg_consignment_id",
            [c.get("staging_id") for c in result.consignments if c.get("staging_id")],
        ),
        "BKD_GoodsItems": (
            "stg_item_id",
            [
                g.get("staging_id")
                for c in result.consignments
                for g in c.get("goods", [])
                if g.get("staging_id")
            ],
        ),
    }
    for table, (id_column, table_ids) in ids.items():
        clean_ids = [int(value) for value in table_ids if value]
        if not clean_ids:
            continue
        cols = _columns(cursor, "STG", table)
        assignments = []
        params: list[Any] = []
        if "sub_status" in cols:
            assignments.append("[sub_status] = ?")
            params.append("VALIDATED")
        if "validated_at" in cols:
            assignments.append("validated_at = SYSUTCDATETIME()")
        if "last_sub_status_change" in cols:
            assignments.append("last_sub_status_change = SYSUTCDATETIME()")
        if not assignments:
            continue
        placeholders = ", ".join("?" for _ in clean_ids)
        cursor.execute(
            f"UPDATE [STG].[{table}] SET {', '.join(assignments)} WHERE [{id_column}] IN ({placeholders})",
            params + clean_ids,
        )


# ── Consignment + goods upsert ─────────────────────────────────────────


def _first_goods_raw(consignment: ParsedConsignment) -> dict:
    for line in consignment.goods:
        if line.raw:
            return line.raw
    return {}


def _raw_text(raw: dict, key: str) -> str:
    value = raw.get(key)
    if value in (None, ""):
        return ""
    return str(value).strip()


def _product_text(product: dict, *keys: str) -> str:
    for key in keys:
        value = product.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _goods_description_text(*values) -> str:
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return tss_safe_text_suggestion(text)
    return ""


def _default_text(defaults, key: str) -> str:
    return str(getattr(defaults, key, "") or "").strip()


def _default_export_party_name(defaults) -> str:
    return _default_text(defaults, "supplier_name") or _default_text(defaults, "importer_name")


def _yes_no_text(value, default: str = "yes") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"yes", "y", "true", "1", "on"}:
        return "yes"
    if text in {"no", "n", "false", "0", "off"}:
        return "no"
    return text


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _tenant_demo_mode_enabled(tenant_code: str) -> bool:
    try:
        from app import config_store

        environment = str(
            config_store.get("TSS_API", "ENVIRONMENT", fallback="", tenant_code=tenant_code) or ""
        ).strip().lower()
        legacy_demo = config_store.get("DEMO", "ENABLED", fallback="", tenant_code=tenant_code)
        return environment == "demo" or _truthy(legacy_demo)
    except Exception:
        return (
            str(os.environ.get("TSS_API_ENVIRONMENT", "") or "").strip().lower() == "demo"
            or _truthy(os.environ.get("DEMO_ENABLED"))
        )


def _warn_missing_customer_masterdata(tenant_code: str) -> bool:
    return (
        strict_masterdata_validation_enabled(tenant_code=tenant_code)
        and not _tenant_demo_mode_enabled(tenant_code)
    )


def _consignment_goods_description(consignment: ParsedConsignment) -> str:
    descriptions: list[str] = []
    for line in consignment.goods:
        raw = line.raw or {}
        desc = _goods_description_text(_raw_text(raw, "goods_description"), _raw_text(raw, "description"), line.sku)
        if desc and desc not in descriptions:
            descriptions.append(desc)
        if len(" | ".join(descriptions)) >= 254:
            break
    return " | ".join(descriptions)[:254]


def _package_type_text(value, default: str = "") -> str:
    normalised = normalise_package_type(value)
    return normalised or default


def _positive_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalise_tss_weight(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(normalise_decimal_scale(value, scale=2))
    except (TypeError, ValueError):
        return None


def _uom_code_multiplier(value) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    matches = re.findall(r"\d+(?:\.\d+)?", text)
    if not matches:
        return None
    return _positive_float(matches[-1])


def _line_base_quantity(line: ParsedGoodsLine) -> float:
    quantity_base = _positive_float(line.quantity_base)
    if quantity_base is not None:
        return quantity_base

    quantity = _positive_float(line.quantity) or 1.0
    multiplier = _positive_float(line.qty_per_uom) or _uom_code_multiplier(line.uom_code) or 1.0
    return quantity * multiplier


def _raw_float(raw: dict, key: str) -> float | None:
    value = raw.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_select(cols: set[str], column: str, alias: str | None = None) -> str:
    output = alias or column
    if column.lower() in cols:
        return f"[{column}] AS [{output}]"
    return f"CAST(NULL AS NVARCHAR(400)) AS [{output}]"


def _sales_order_customer_master(cursor, schema: str, customer_no: str) -> dict[str, Any]:
    """Resolve Sales Header - Sell-to Customer No. against local masterdata."""
    customer_no = (customer_no or "").strip()
    if not customer_no:
        return {}

    try:
        partner_cols = _columns(cursor, schema, "Partners")
        if "account_ref" in partner_cols:
            active_clause = "AND active = 1" if "active" in partner_cols else ""
            cursor.execute(
                f"""
                SELECT TOP 1
                    {_optional_select(partner_cols, 'partner_name', 'name')},
                    {_optional_select(partner_cols, 'eori')},
                    {_optional_select(partner_cols, 'eori_gb')},
                    {_optional_select(partner_cols, 'address_line1')},
                    {_optional_select(partner_cols, 'address_line2')},
                    {_optional_select(partner_cols, 'city')},
                    {_optional_select(partner_cols, 'postcode')},
                    {_optional_select(partner_cols, 'country')},
                    {_optional_select(partner_cols, 'contact_email')},
                    {_optional_select(partner_cols, 'contact_phone')}
                FROM [{schema}].Partners
                WHERE UPPER(account_ref) = UPPER(?)
                  {active_clause}
                ORDER BY CASE WHEN partner_type = 'Consignee' THEN 0 ELSE 1 END, id
                """,
                [customer_no],
            )
            row = cursor.fetchone()
            if row:
                return {
                    "source": f"{schema}.Partners",
                    "name": row[0],
                    "eori": row[1] or row[2],
                    "address_line1": row[3],
                    "address_line2": row[4],
                    "city": row[5],
                    "postcode": row[6],
                    "country": row[7],
                    "contact_email": row[8],
                    "contact_phone": row[9],
                }
    except Exception:
        logger.debug("Sales Orders customer lookup failed in Partners", exc_info=True)

    try:
        data_cols = _columns(cursor, "DATA", "BKD_Customers")
        if "customerno" in data_cols:
            cursor.execute(
                f"""
                SELECT TOP 1
                    {_optional_select(data_cols, 'Name', 'name')},
                    {_optional_select(data_cols, 'EORINumber', 'eori')},
                    {_optional_select(data_cols, 'Address', 'address_line1')},
                    {_optional_select(data_cols, 'Address2', 'address_line2')},
                    {_optional_select(data_cols, 'City', 'city')},
                    {_optional_select(data_cols, 'PostCode', 'postcode')},
                    {_optional_select(data_cols, 'CountryRegionCode', 'country')}
                FROM [DATA].BKD_Customers
                WHERE UPPER(CustomerNo) = UPPER(?)
                """,
                [customer_no],
            )
            row = cursor.fetchone()
            if row:
                return {
                    "source": "DATA.BKD_Customers",
                    "name": row[0],
                    "eori": row[1],
                    "address_line1": row[2],
                    "address_line2": row[3],
                    "city": row[4],
                    "postcode": row[5],
                    "country": row[6],
                }
    except Exception:
        logger.debug("Sales Orders customer lookup failed in DATA.BKD_Customers", exc_info=True)

    return {}


def _stage_one_consignment(
    cursor, schema: str, master_schema: str, env_code: str,
    *, ens_staging_id: int, consignment: ParsedConsignment,
    defaults, tenant_code: str,
) -> dict:
    summary: dict[str, Any] = {
        "document_no": consignment.document_no,
        "staging_id": None,
        "inserted": False,
        "goods": [],
        "blockers": [],
        "hard_blockers": [],
        "warnings": [],
    }

    cols = _columns(cursor, schema, "StagingConsignments")
    raw = _first_goods_raw(consignment)
    customer_master = _sales_order_customer_master(cursor, master_schema, consignment.sell_to_customer_no)
    customer_eori = str(customer_master.get("eori") or "").strip()
    customer_name = str(customer_master.get("name") or "").strip()
    if consignment.sell_to_customer_no and not customer_master and _warn_missing_customer_masterdata(tenant_code):
        summary["warnings"].append(
            f"{consignment.document_no}/{consignment.sell_to_customer_no}: customer masterdata not found - strict masterdata review required"
        )

    consignee_name = _raw_text(raw, "consignee_name") or customer_name or consignment.ship_to_name
    consignee_street = _raw_text(raw, "consignee_street_number") or _ship_street(consignment) or customer_master.get("address_line1") or ""
    consignee_city = _raw_text(raw, "consignee_city") or consignment.ship_to_city or customer_master.get("city") or ""
    consignee_postcode = (
        _raw_text(raw, "consignee_postcode")
        or getattr(consignment, "ship_to_postcode", "")
        or customer_master.get("postcode")
        or ""
    )
    consignee_country = (
        _raw_text(raw, "consignee_country")
        or getattr(consignment, "ship_to_country", "")
        or customer_master.get("country")
        or "GB"
    )
    no_sfd_reason = _raw_text(raw, "no_sfd_reason")
    generate_sd = _yes_no_text(_raw_text(raw, "generate_SD"), default="no")
    use_sfd_path = not no_sfd_reason

    default_importer_eori = _default_text(defaults, "importer_eori")
    default_importer_name = _default_text(defaults, "importer_name")
    default_importer_street = _default_text(defaults, "importer_street_number")
    default_importer_city = _default_text(defaults, "importer_city")
    default_importer_postcode = _default_text(defaults, "importer_postcode")
    default_importer_country = _default_text(defaults, "importer_country")
    default_export_party_name = _default_export_party_name(defaults)

    if use_sfd_path and default_importer_eori:
        importer_eori = default_importer_eori
        importer_name = default_importer_name or _raw_text(raw, "importer_name") or consignee_name
        importer_street = default_importer_street or _raw_text(raw, "importer_street_number") or consignee_street
        importer_city = default_importer_city or _raw_text(raw, "importer_city") or consignee_city
        importer_postcode = default_importer_postcode or _raw_text(raw, "importer_postcode") or consignee_postcode
        importer_country = default_importer_country or _raw_text(raw, "importer_country") or _raw_text(raw, "destination_country") or consignee_country
    else:
        importer_eori = _raw_text(raw, "importer_eori") or customer_eori
        importer_name = _raw_text(raw, "importer_name") or consignee_name
        importer_street = _raw_text(raw, "importer_street_number") or consignee_street
        importer_city = _raw_text(raw, "importer_city") or consignee_city
        importer_postcode = _raw_text(raw, "importer_postcode") or consignee_postcode
        importer_country = _raw_text(raw, "importer_country") or _raw_text(raw, "destination_country") or consignee_country

    buyer_same_as_importer = _yes_no_text(_raw_text(raw, "buyer_same_as_importer"), default="yes")
    seller_same_as_exporter = _yes_no_text(_raw_text(raw, "seller_same_as_exporter"), default="yes")

    payload: dict[str, Any] = {
        "staging_ens_id": ens_staging_id,
        "label": f"{consignment.document_no} — {consignment.ship_to_name}",
        "goods_description": _raw_text(raw, "consignment_goods_description") or _consignment_goods_description(consignment),
        "trader_reference": consignment.document_no,
        "transport_document_number": _raw_text(raw, "transport_document_number") or consignment.document_no,
        "consignee_eori": _raw_text(raw, "consignee_eori") or customer_eori,
        "consignee_name": consignee_name,
        "consignee_street_number": consignee_street,
        "consignee_city": consignee_city,
        "consignee_postcode": consignee_postcode,
        "consignee_country": consignee_country,
        "consignee_contact_email": consignment.ship_to_email or customer_master.get("contact_email") or None,
        "importer_eori": importer_eori,
        "importer_name": importer_name,
        "importer_street_number": importer_street,
        "importer_city": importer_city,
        "importer_postcode": importer_postcode,
        "importer_country": importer_country,
        "consignor_eori": _raw_text(raw, "consignor_eori") or defaults.consignor_eori or "",
        "consignor_name": _raw_text(raw, "consignor_name") or default_export_party_name,
        "consignor_street_number": _raw_text(raw, "consignor_street_number"),
        "consignor_city": _raw_text(raw, "consignor_city"),
        "consignor_postcode": _raw_text(raw, "consignor_postcode"),
        "consignor_country": _raw_text(raw, "consignor_country"),
        "exporter_eori": _raw_text(raw, "exporter_eori") or defaults.exporter_eori or defaults.consignor_eori or "",
        "exporter_name": _raw_text(raw, "exporter_name") or default_export_party_name,
        "exporter_street_number": _raw_text(raw, "exporter_street_number"),
        "exporter_city": _raw_text(raw, "exporter_city"),
        "exporter_postcode": _raw_text(raw, "exporter_postcode"),
        "exporter_country": _raw_text(raw, "exporter_country"),
        "destination_country": _raw_text(raw, "destination_country") or "GB",
        "controlled_goods": _raw_text(raw, "controlled_goods") or defaults.controlled_goods,
        "goods_domestic_status": _raw_text(raw, "goods_domestic_status") or "D",
        "container_indicator": _raw_text(raw, "container_indicator") or defaults.container_indicator,
        "no_sfd_reason": no_sfd_reason,
        "generate_SD": generate_sd,
        "declaration_choice": _raw_text(raw, "declaration_choice"),
        "ducr": _raw_text(raw, "ducr"),
        "align_ukims": _raw_text(raw, "align_ukims"),
        "use_importer_sde": _raw_text(raw, "use_importer_sde"),
        "buyer_same_as_importer": buyer_same_as_importer,
        "buyer_eori": "" if buyer_same_as_importer == "yes" else _raw_text(raw, "buyer_eori"),
        "buyer_name": "" if buyer_same_as_importer == "yes" else _raw_text(raw, "buyer_name"),
        "buyer_street_and_number": "" if buyer_same_as_importer == "yes" else _raw_text(raw, "buyer_street_and_number"),
        "buyer_city": "" if buyer_same_as_importer == "yes" else _raw_text(raw, "buyer_city"),
        "buyer_postcode": "" if buyer_same_as_importer == "yes" else _raw_text(raw, "buyer_postcode"),
        "buyer_country": "" if buyer_same_as_importer == "yes" else _raw_text(raw, "buyer_country"),
        "seller_same_as_exporter": seller_same_as_exporter,
        "seller_eori": "" if seller_same_as_exporter == "yes" else _raw_text(raw, "seller_eori"),
        "seller_name": "" if seller_same_as_exporter == "yes" else _raw_text(raw, "seller_name"),
        "seller_street_and_number": "" if seller_same_as_exporter == "yes" else _raw_text(raw, "seller_street_and_number"),
        "seller_city": "" if seller_same_as_exporter == "yes" else _raw_text(raw, "seller_city"),
        "seller_postcode": "" if seller_same_as_exporter == "yes" else _raw_text(raw, "seller_postcode"),
        "seller_country": "" if seller_same_as_exporter == "yes" else _raw_text(raw, "seller_country"),
        "supervising_customs_office": _raw_text(raw, "supervising_customs_office"),
        "customs_warehouse_identifier": _raw_text(raw, "customs_warehouse_identifier"),
        "status": "PENDING_REVIEW",
        "tss_status": "PENDING_REVIEW",
        "source": "EXCEL_SALES_ORDERS",
        "internal_notes": _consignment_metadata_note(consignment),
        "metadata_json": _consignment_metadata_json(consignment),
    }
    if payload.get("no_sfd_reason"):
        payload["generate_SD"] = "no"
    if "staging_ens_id" not in cols:
        raise RuntimeError("StagingConsignments is missing staging_ens_id; cannot link consignments to ENS headers")
    if "trader_reference" not in cols:
        raise RuntimeError("StagingConsignments is missing trader_reference; cannot de-duplicate uploaded consignments")
    values = _payload_values_for_columns(payload, cols)

    existing = _find_existing_consignment(cursor, schema, cols, ens_staging_id, consignment.document_no)
    if existing:
        sets = ", ".join(
            f"[{k}] = COALESCE(NULLIF([{k}], ''), ?)" if isinstance(v, str) else f"[{k}] = COALESCE([{k}], ?)"
            for k, v in values
        )
        params = [v for _, v in values] + [existing[0]]
        if "updated_at" in cols:
            sets += ", updated_at = SYSUTCDATETIME()"
        cursor.execute(
            f"UPDATE [{schema}].StagingConsignments SET {sets} WHERE staging_id = ?",
            params,
        )
        cons_id = int(existing[0])
        summary["inserted"] = False
    else:
        insert_cols = [k for k, _ in values]
        placeholders = ["?"] * len(values)
        if "created_at" in cols:
            insert_cols.append("created_at"); placeholders.append("SYSUTCDATETIME()")
        if "updated_at" in cols:
            insert_cols.append("updated_at"); placeholders.append("SYSUTCDATETIME()")
        cursor.execute(
            f"INSERT INTO [{schema}].StagingConsignments ({', '.join(f'[{c}]' for c in insert_cols)}) "
            f"OUTPUT INSERTED.staging_id VALUES ({', '.join(placeholders)})",
            [v for _, v in values],
        )
        cons_id = int(cursor.fetchone()[0])
        summary["inserted"] = True
    summary["staging_id"] = cons_id

    # Stage goods items
    for line in consignment.goods:
        skip_reason = _non_declarable_sales_order_goods_reason(line)
        if skip_reason:
            summary["warnings"].append(
                f"{consignment.document_no}/{line.sku or line.line_no}: skipped non-declarable line ({skip_reason})"
            )
            continue
        try:
            goods_summary = _stage_one_goods(
                cursor, schema, master_schema, env_code,
                consignment=consignment, line=line, cons_staging_id=cons_id, defaults=defaults,
            )
        except Exception as exc:
            logger.exception(
                "Sales Orders goods staging failed for %s/%s",
                consignment.document_no,
                line.sku or line.line_no,
            )
            goods_summary = _failed_goods_summary(consignment, line, exc)
        summary["goods"].append(goods_summary)
        summary["blockers"].extend(goods_summary.get("blockers", []))
        summary["hard_blockers"].extend(goods_summary.get("hard_blockers", []))
        summary["warnings"].extend(goods_summary.get("warnings", []))

    return summary


def _stage_one_consignment_stg(
    cursor,
    *,
    master_schema: str,
    env_code: str,
    ens_staging_id: int,
    consignment: ParsedConsignment,
    defaults,
    tenant_code: str,
    ing_line_ids: dict[tuple[str, int | None, str], int],
) -> dict:
    summary: dict[str, Any] = {
        "document_no": consignment.document_no,
        "staging_id": None,
        "inserted": False,
        "goods": [],
        "blockers": [],
        "hard_blockers": [],
        "warnings": [],
    }

    cols = _columns(cursor, "STG", "BKD_ENS_Consignments")
    raw = _first_goods_raw(consignment)
    customer_master = _sales_order_customer_master(cursor, master_schema, consignment.sell_to_customer_no)
    customer_eori = str(customer_master.get("eori") or "").strip()
    customer_name = str(customer_master.get("name") or "").strip()
    if consignment.sell_to_customer_no and not customer_master and _warn_missing_customer_masterdata(tenant_code):
        summary["warnings"].append(
            f"{consignment.document_no}/{consignment.sell_to_customer_no}: customer masterdata not found - strict masterdata review required"
        )

    consignee_name = _raw_text(raw, "consignee_name") or customer_name or consignment.ship_to_name
    consignee_street = _raw_text(raw, "consignee_street_number") or _ship_street(consignment) or customer_master.get("address_line1") or ""
    consignee_city = _raw_text(raw, "consignee_city") or consignment.ship_to_city or customer_master.get("city") or ""
    consignee_postcode = (
        _raw_text(raw, "consignee_postcode")
        or getattr(consignment, "ship_to_postcode", "")
        or customer_master.get("postcode")
        or ""
    )
    consignee_country = (
        _raw_text(raw, "consignee_country")
        or getattr(consignment, "ship_to_country", "")
        or customer_master.get("country")
        or "GB"
    )
    no_sfd_reason = _raw_text(raw, "no_sfd_reason")
    generate_sd = "no" if no_sfd_reason else _yes_no_text(_raw_text(raw, "generate_SD"), default="no")
    default_export_party_name = _default_export_party_name(defaults)

    importer_eori = _raw_text(raw, "importer_eori") or _default_text(defaults, "importer_eori") or customer_eori
    importer_name = _raw_text(raw, "importer_name") or _default_text(defaults, "importer_name") or consignee_name
    importer_street = _raw_text(raw, "importer_street_number") or _default_text(defaults, "importer_street_number") or consignee_street
    importer_city = _raw_text(raw, "importer_city") or _default_text(defaults, "importer_city") or consignee_city
    importer_postcode = _raw_text(raw, "importer_postcode") or _default_text(defaults, "importer_postcode") or consignee_postcode
    importer_country = (
        _raw_text(raw, "importer_country")
        or _default_text(defaults, "importer_country")
        or _raw_text(raw, "destination_country")
        or consignee_country
    )

    payload: dict[str, Any] = {
        "ClientCode": tenant_code,
        "stg_header_id": ens_staging_id,
        "sub_status": "PENDING",
        "source": "EXCEL_SALES_ORDERS",
        "goods_description": _raw_text(raw, "consignment_goods_description") or _consignment_goods_description(consignment),
        "trader_reference": consignment.document_no,
        "transport_document_number": _raw_text(raw, "transport_document_number") or consignment.document_no,
        "controlled_goods": _raw_text(raw, "controlled_goods") or defaults.controlled_goods,
        "goods_domestic_status": _raw_text(raw, "goods_domestic_status") or "D",
        "destination_country": _raw_text(raw, "destination_country") or "GB",
        "ducr": _raw_text(raw, "ducr"),
        "align_ukims": _raw_text(raw, "align_ukims"),
        "use_importer_sde": _raw_text(raw, "use_importer_sde"),
        "declaration_choice": _raw_text(raw, "declaration_choice"),
        "generate_SD": generate_sd,
        "container_indicator": _raw_text(raw, "container_indicator") or defaults.container_indicator,
        "buyer_same_as_importer": _yes_no_text(_raw_text(raw, "buyer_same_as_importer"), default="yes"),
        "seller_same_as_exporter": _yes_no_text(_raw_text(raw, "seller_same_as_exporter"), default="yes"),
        "no_sfd_reason": no_sfd_reason,
        "consignor_eori": _raw_text(raw, "consignor_eori") or defaults.consignor_eori or "",
        "consignor_name": _raw_text(raw, "consignor_name") or default_export_party_name,
        "consignor_street_number": _raw_text(raw, "consignor_street_number"),
        "consignor_city": _raw_text(raw, "consignor_city"),
        "consignor_postcode": _raw_text(raw, "consignor_postcode"),
        "consignor_country": _raw_text(raw, "consignor_country"),
        "consignee_eori": _raw_text(raw, "consignee_eori") or customer_eori,
        "consignee_name": consignee_name,
        "consignee_street_number": consignee_street,
        "consignee_city": consignee_city,
        "consignee_postcode": consignee_postcode,
        "consignee_country": consignee_country,
        "consignee_contact_email": consignment.ship_to_email or customer_master.get("contact_email") or "",
        "importer_eori": importer_eori,
        "importer_name": importer_name,
        "importer_street_number": importer_street,
        "importer_city": importer_city,
        "importer_postcode": importer_postcode,
        "importer_country": importer_country,
        "exporter_eori": _raw_text(raw, "exporter_eori") or defaults.exporter_eori or defaults.consignor_eori or "",
        "exporter_name": _raw_text(raw, "exporter_name") or default_export_party_name,
        "metadata_json": _consignment_metadata_json(consignment),
    }
    values = _payload_values_for_columns(payload, cols)
    existing = _find_existing_consignment_stg(cursor, ens_staging_id, consignment.document_no)

    if existing:
        sets = _coalescing_sets(values)
        if "last_sub_status_change" in cols:
            sets.append("last_sub_status_change = SYSUTCDATETIME()")
        if "updated_at" in cols:
            sets.append("updated_at = SYSUTCDATETIME()")
        if sets:
            cursor.execute(
                f"UPDATE [STG].[BKD_ENS_Consignments] SET {', '.join(sets)} WHERE stg_consignment_id = ?",
                [v for _, v in values] + [int(existing[0])],
            )
        cons_id = int(existing[0])
        summary["inserted"] = False
    else:
        insert_cols = [k for k, _ in values]
        placeholders = ["?"] * len(values)
        if "last_sub_status_change" in cols:
            insert_cols.append("last_sub_status_change")
            placeholders.append("SYSUTCDATETIME()")
        cursor.execute(
            f"INSERT INTO [STG].[BKD_ENS_Consignments] ({', '.join(f'[{c}]' for c in insert_cols)}) "
            f"OUTPUT INSERTED.stg_consignment_id VALUES ({', '.join(placeholders)})",
            [v for _, v in values],
        )
        cons_id = int(cursor.fetchone()[0])
        summary["inserted"] = True
    summary["staging_id"] = cons_id

    for line in consignment.goods:
        skip_reason = _non_declarable_sales_order_goods_reason(line)
        if skip_reason:
            summary["warnings"].append(
                f"{consignment.document_no}/{line.sku or line.line_no}: skipped non-declarable line ({skip_reason})"
            )
            continue
        try:
            goods_summary = _stage_one_goods_stg(
                cursor,
                master_schema=master_schema,
                consignment=consignment,
                line=line,
                cons_staging_id=cons_id,
                defaults=defaults,
                tenant_code=tenant_code,
                ing_line_ids=ing_line_ids or {},
            )
        except Exception as exc:
            logger.exception("Sales Orders STG goods staging failed for %s/%s", consignment.document_no, line.sku or line.line_no)
            goods_summary = _failed_goods_summary(consignment, line, exc)
        summary["goods"].append(goods_summary)
        summary["blockers"].extend(goods_summary.get("blockers", []))
        summary["hard_blockers"].extend(goods_summary.get("hard_blockers", []))
        summary["warnings"].extend(goods_summary.get("warnings", []))

    return summary


def _find_existing_consignment_stg(cursor, ens_staging_id: int, document_no: str):
    cursor.execute(
        """
        SELECT TOP 1 stg_consignment_id FROM [STG].[BKD_ENS_Consignments]
        WHERE stg_header_id = ? AND trader_reference = ?
        ORDER BY stg_consignment_id DESC
        """,
        [ens_staging_id, document_no],
    )
    return cursor.fetchone()


def _find_existing_consignment(cursor, schema: str, cols: set[str], ens_staging_id: int, document_no: str):
    cursor.execute(
        f"""
        SELECT TOP 1 staging_id FROM [{schema}].StagingConsignments
        WHERE staging_ens_id = ? AND trader_reference = ?
        """,
        [ens_staging_id, document_no],
    )
    existing = cursor.fetchone()
    if existing:
        return existing

    predicates = ["staging_ens_id IS NULL", "trader_reference = ?"]
    params: list[Any] = [document_no]
    if "source" in cols:
        predicates.append("(source = ? OR source IS NULL OR source = '')")
        params.append("EXCEL_SALES_ORDERS")

    cursor.execute(
        f"""
        SELECT TOP 1 staging_id FROM [{schema}].StagingConsignments
        WHERE {' AND '.join(predicates)}
        ORDER BY staging_id DESC
        """,
        params,
    )
    return cursor.fetchone()


def _stage_one_goods(
    cursor, schema: str, master_schema: str, env_code: str,
    *, consignment: ParsedConsignment, line: ParsedGoodsLine,
    cons_staging_id: int, defaults,
) -> dict:
    summary: dict[str, Any] = {
        "document_no": consignment.document_no,
        "line_no": line.line_no,
        "sku": line.sku,
        "staging_id": None,
        "inserted": False,
        "blockers": [],
        "warnings": [],
    }
    raw = line.raw or {}
    product = (
        resolve_product_master(cursor, master_schema, consignment.sell_to_customer_no, line.sku)
        or _builtin_sales_order_product_fallback(line.sku)
    )
    _try_learn_doc_product_catalog_unit_price(
        cursor,
        master_schema,
        consignment.sell_to_customer_no,
        line.sku,
        line.unit_price_excl_vat,
    )
    missing_master = not product
    product = product or {}
    effective_commodity = _raw_text(raw, "commodity_code") or _product_text(product, "commodity_code")
    template_is_customs_ready = bool(
        effective_commodity
        and _raw_text(raw, "goods_description")
        and _raw_float(raw, "gross_mass_kg") is not None
    )

    pending_notes: list[str] = []
    if missing_master and not template_is_customs_ready:
        pending_notes.append("no product master match")
        summary["warnings"].append(f"{consignment.document_no}/{line.sku}: no product master match — staged as draft")
    if not effective_commodity:
        pending_notes.append("missing commodity_code")
        summary["warnings"].append(f"{consignment.document_no}/{line.sku}: missing commodity_code — staged as draft")

    cols = _columns(cursor, schema, "StagingGoodsItems")

    # Supplementary units only when product master flags it
    requires_supp = bool(product.get("requires_supplementary_unit") or product.get("statistical_unit"))
    supp_units = _line_base_quantity(line) if requires_supp else None
    gross_mass_kg = _raw_float(raw, "gross_mass_kg")
    if gross_mass_kg is None:
        gross_mass_kg = _compute_weight(product, line, "gross")
    gross_mass_kg = _normalise_tss_weight(gross_mass_kg)
    net_mass_kg = _raw_float(raw, "net_mass_kg")
    if net_mass_kg is None:
        net_mass_kg = _compute_weight(product, line, "net")
    net_mass_kg = _normalise_tss_weight(net_mass_kg)
    missing_weight_fields = []
    if gross_mass_kg is None or gross_mass_kg <= 0:
        missing_weight_fields.append("gross")
        gross_mass_kg = 0.0
    if net_mass_kg is None or net_mass_kg <= 0:
        missing_weight_fields.append("net")
        net_mass_kg = 0.0
    if missing_weight_fields:
        missing_weight_label = "/".join(missing_weight_fields)
        pending_notes.append(f"missing {missing_weight_label} weight")
        summary["warnings"].append(
            f"{consignment.document_no}/{line.sku}: missing {missing_weight_label} weight - staged as draft"
        )

    payload: dict[str, Any] = {
        "staging_cons_id": cons_staging_id,
        "consignment_number": "",  # filled when DEC ref allocated by TSS
        "item_number": line.line_no,
        "sku": line.sku,
        "goods_description": _goods_description_text(
            _raw_text(raw, "goods_description"),
            _product_text(product, "description", "goods_description"),
            line.sku,
        ),
        "commodity_code": _raw_text(raw, "commodity_code") or _product_text(product, "commodity_code"),
        "taric_code": _raw_text(raw, "taric_code") or _product_text(product, "taric_code"),
        "country_of_origin": _raw_text(raw, "country_of_origin") or _product_text(product, "country_of_origin") or defaults.country_of_origin,
        "type_of_packages": _package_type_text(
            _raw_text(raw, "type_of_packages")
            or _product_text(product, "package_type")
            or line.uom_code,
            defaults.package_type,
        ),
        "package_marks": _raw_text(raw, "package_marks") or consignment.ship_to_name[:140] or "ADDR",
        "number_of_packages": int(_raw_float(raw, "number_of_packages") or line.quantity or 1),
        "number_of_individual_pieces": _raw_float(raw, "number_of_individual_pieces"),
        "supplementary_units": supp_units,
        "gross_mass_kg": gross_mass_kg,
        "net_mass_kg": net_mass_kg,
        "procedure_code": _raw_text(raw, "procedure_code") or _product_text(product, "procedure_code") or defaults.procedure_code,
        "additional_procedure_code": (
            _raw_text(raw, "additional_procedure_code")
            or _product_text(product, "additional_procedure_code")
            or defaults.additional_procedure_code
        ),
        "cus_code": _raw_text(raw, "cus_code") or _product_text(product, "cus_code"),
        "national_additional_code": _raw_text(raw, "national_additional_code") or _product_text(product, "national_additional_code"),
        "ni_additional_information_codes": (
            _raw_text(raw, "ni_additional_information_codes")
            or _product_text(product, "ni_additional_information_codes", "ni_additional_info_code")
        ),
        "country_of_preferential_origin": (
            _raw_text(raw, "country_of_preferential_origin")
            or _product_text(product, "country_of_preferential_origin")
        ),
        "valuation_method": _raw_text(raw, "valuation_method") or _product_text(product, "valuation_method") or defaults.valuation_method,
        "valuation_indicator": _raw_text(raw, "valuation_indicator") or _product_text(product, "valuation_indicator"),
        "preference": _raw_text(raw, "preference") or _product_text(product, "preference_code", "preference"),
        "invoice_number": _raw_text(raw, "invoice_number"),
        "nature_of_transaction": _raw_text(raw, "nature_of_transaction") or _product_text(product, "nature_of_transaction"),
        "statistical_value": _raw_float(raw, "statistical_value"),
        "quota_order_number": _raw_text(raw, "quota_order_number") or _product_text(product, "quota_order_number"),
        "item_invoice_amount": line.line_amount_excl_vat,
        "line_amount_excl_vat": line.line_amount_excl_vat,
        "source_amount": line.amount if line.amount is not None else _raw_float(raw, "amount"),
        "unit_price_excl_vat": line.unit_price_excl_vat,
        "item_invoice_currency": _raw_text(raw, "item_invoice_currency") or defaults.invoice_currency,
        "tax_type": _raw_text(raw, "tax_type"),
        "tax_base_unit": _raw_text(raw, "tax_base_unit"),
        "tax_base_quantity": _raw_float(raw, "tax_base_quantity"),
        "payable_tax_amount": _raw_float(raw, "payable_tax_amount"),
        "payable_tax_currency": _raw_text(raw, "payable_tax_currency"),
        "controlled_goods": _raw_text(raw, "controlled_goods") or defaults.controlled_goods,
        "controlled_goods_type": _raw_text(raw, "controlled_goods_type") or _product_text(product, "controlled_goods_type"),
        "status": "PENDING_REVIEW",
        "tss_status": "PENDING_REVIEW",
        "source": "EXCEL_SALES_ORDERS",
        "error_message": ("Pending: " + "; ".join(pending_notes)) if pending_notes else None,
    }
    values = [(k, v) for k, v in payload.items() if k.lower() in cols and v not in (None, "")]

    # Preferred natural key: cons + line_no + sku. The sku column is additive in
    # some older tenant schemas, so fall back to cons + line_no when needed.
    lookup_where, lookup_params = _goods_lookup_where(cols, cons_staging_id, line)
    existing = None
    if lookup_where:
        cursor.execute(
            f"""
            SELECT TOP 1 staging_id FROM [{schema}].StagingGoodsItems
            WHERE {lookup_where}
            """,
            lookup_params,
        )
        existing = cursor.fetchone()
    if existing:
        sets = ", ".join(
            f"[{k}] = COALESCE(NULLIF([{k}], ''), ?)" if isinstance(v, str) else f"[{k}] = COALESCE([{k}], ?)"
            for k, v in values
        )
        params = [v for _, v in values] + [existing[0]]
        if "updated_at" in cols:
            sets += ", updated_at = SYSUTCDATETIME()"
        cursor.execute(
            f"UPDATE [{schema}].StagingGoodsItems SET {sets} WHERE staging_id = ?",
            params,
        )
        summary["staging_id"] = int(existing[0])
        summary["inserted"] = False
        return summary

    insert_cols = [k for k, _ in values]
    placeholders = ["?"] * len(values)
    if "created_at" in cols:
        insert_cols.append("created_at"); placeholders.append("SYSUTCDATETIME()")
    if "updated_at" in cols:
        insert_cols.append("updated_at"); placeholders.append("SYSUTCDATETIME()")
    cursor.execute(
        f"INSERT INTO [{schema}].StagingGoodsItems ({', '.join(f'[{c}]' for c in insert_cols)}) "
        f"OUTPUT INSERTED.staging_id VALUES ({', '.join(placeholders)})",
        [v for _, v in values],
    )
    summary["staging_id"] = int(cursor.fetchone()[0])
    summary["inserted"] = True
    return summary


def _stage_one_goods_stg(
    cursor,
    *,
    master_schema: str,
    consignment: ParsedConsignment,
    line: ParsedGoodsLine,
    cons_staging_id: int,
    defaults,
    tenant_code: str,
    ing_line_ids: dict[tuple[str, int | None, str], int],
) -> dict:
    summary: dict[str, Any] = {
        "document_no": consignment.document_no,
        "line_no": line.line_no,
        "sku": line.sku,
        "staging_id": None,
        "inserted": False,
        "blockers": [],
        "warnings": [],
    }
    raw = line.raw or {}
    product = (
        resolve_product_master(cursor, master_schema, consignment.sell_to_customer_no, line.sku)
        or _builtin_sales_order_product_fallback(line.sku)
    )
    _try_learn_doc_product_catalog_unit_price(
        cursor,
        master_schema,
        consignment.sell_to_customer_no,
        line.sku,
        line.unit_price_excl_vat,
    )
    missing_master = not product
    product = product or {}
    effective_commodity = _raw_text(raw, "commodity_code") or _product_text(product, "commodity_code")
    template_is_customs_ready = bool(
        effective_commodity
        and _raw_text(raw, "goods_description")
        and _raw_float(raw, "gross_mass_kg") is not None
    )

    pending_notes: list[str] = []
    if missing_master and not template_is_customs_ready:
        pending_notes.append("no product master match")
        summary["warnings"].append(f"{consignment.document_no}/{line.sku}: no product master match - staged as draft")
    if not effective_commodity:
        pending_notes.append("missing commodity_code")
        summary["warnings"].append(f"{consignment.document_no}/{line.sku}: missing commodity_code - staged as draft")

    gross_mass_kg = _raw_float(raw, "gross_mass_kg")
    if gross_mass_kg is None:
        gross_mass_kg = _compute_weight(product, line, "gross")
    gross_mass_kg = _normalise_tss_weight(gross_mass_kg)
    net_mass_kg = _raw_float(raw, "net_mass_kg")
    if net_mass_kg is None:
        net_mass_kg = _compute_weight(product, line, "net")
    net_mass_kg = _normalise_tss_weight(net_mass_kg)
    missing_weight_fields = []
    if gross_mass_kg is None or gross_mass_kg <= 0:
        missing_weight_fields.append("gross")
        gross_mass_kg = 0.0
    if net_mass_kg is None or net_mass_kg <= 0:
        missing_weight_fields.append("net")
        net_mass_kg = 0.0
    if missing_weight_fields:
        missing_weight_label = "/".join(missing_weight_fields)
        pending_notes.append(f"missing {missing_weight_label} weight")
        summary["warnings"].append(
            f"{consignment.document_no}/{line.sku}: missing {missing_weight_label} weight - staged as draft"
        )

    requires_supp = bool(product.get("requires_supplementary_unit") or product.get("statistical_unit"))
    base_quantity = _line_base_quantity(line)
    cols = _columns(cursor, "STG", "BKD_GoodsItems")
    payload: dict[str, Any] = {
        "ClientCode": tenant_code,
        "stg_consignment_id": cons_staging_id,
        "sub_status": "PENDING",
        "ing_item_id": ing_line_ids.get(_ing_line_key(consignment.document_no, line.line_no, line.sku)),
        "source": "EXCEL_SALES_ORDERS",
        "goods_stage": "ENS",
        "item_seq": line.line_no,
        "sku": line.sku,
        "goods_description": _goods_description_text(
            _raw_text(raw, "goods_description"),
            _product_text(product, "description", "goods_description"),
            line.sku,
        ),
        "commodity_code": effective_commodity,
        "gross_mass_kg": gross_mass_kg,
        "net_mass_kg": net_mass_kg,
        "number_of_packages": int(_raw_float(raw, "number_of_packages") or line.quantity or 1),
        "number_of_individual_pieces": _raw_float(raw, "number_of_individual_pieces"),
        "type_of_packages": _package_type_text(
            _raw_text(raw, "type_of_packages")
            or _product_text(product, "package_type")
            or line.uom_code,
            defaults.package_type,
        ),
        "package_marks": _raw_text(raw, "package_marks") or consignment.ship_to_name[:140] or "ADDR",
        "procedure_code": _raw_text(raw, "procedure_code") or _product_text(product, "procedure_code") or defaults.procedure_code,
        "additional_procedure_code": (
            _raw_text(raw, "additional_procedure_code")
            or _product_text(product, "additional_procedure_code")
            or defaults.additional_procedure_code
        ),
        "controlled_goods": _raw_text(raw, "controlled_goods") or defaults.controlled_goods,
        "controlled_goods_type": _raw_text(raw, "controlled_goods_type") or _product_text(product, "controlled_goods_type"),
        "country_of_origin": _raw_text(raw, "country_of_origin") or _product_text(product, "country_of_origin") or defaults.country_of_origin,
        "item_invoice_amount": line.line_amount_excl_vat,
        "line_amount_excl_vat": line.line_amount_excl_vat,
        "source_amount": line.amount if line.amount is not None else _raw_float(raw, "amount"),
        "unit_price_excl_vat": line.unit_price_excl_vat,
        "item_invoice_currency": _raw_text(raw, "item_invoice_currency") or defaults.invoice_currency,
        "customs_value": _raw_float(raw, "customs_value"),
        "valuation_method": _raw_text(raw, "valuation_method") or _product_text(product, "valuation_method") or defaults.valuation_method,
        "valuation_indicator": _raw_text(raw, "valuation_indicator") or _product_text(product, "valuation_indicator"),
        "statistical_value": _raw_float(raw, "statistical_value"),
        "nature_of_transaction": _raw_text(raw, "nature_of_transaction") or _product_text(product, "nature_of_transaction"),
        "preference": _raw_text(raw, "preference") or _product_text(product, "preference_code", "preference"),
        "taric_code": _raw_text(raw, "taric_code") or _product_text(product, "taric_code"),
        "cus_code": _raw_text(raw, "cus_code") or _product_text(product, "cus_code"),
        "national_additional_code": _raw_text(raw, "national_additional_code") or _product_text(product, "national_additional_code"),
        "ni_additional_information_codes": (
            _raw_text(raw, "ni_additional_information_codes")
            or _product_text(product, "ni_additional_information_codes", "ni_additional_info_code")
        ),
        "country_of_preferential_origin": (
            _raw_text(raw, "country_of_preferential_origin")
            or _product_text(product, "country_of_preferential_origin")
        ),
        "invoice_number": _raw_text(raw, "invoice_number"),
        "quota_order_number": _raw_text(raw, "quota_order_number") or _product_text(product, "quota_order_number"),
        "tax_type": _raw_text(raw, "tax_type"),
        "tax_base_unit": _raw_text(raw, "tax_base_unit"),
        "tax_base_quantity": _raw_float(raw, "tax_base_quantity"),
        "payable_tax_amount": _raw_float(raw, "payable_tax_amount"),
        "payable_tax_currency": _raw_text(raw, "payable_tax_currency"),
        "supplementary_units": base_quantity if requires_supp else None,
        "error_message": ("Pending: " + "; ".join(pending_notes)) if pending_notes else None,
    }
    values = _payload_values_for_columns(payload, cols)
    existing = _find_existing_goods_stg(cursor, cons_staging_id, line)
    if existing:
        sets = _coalescing_sets(values)
        if "last_sub_status_change" in cols:
            sets.append("last_sub_status_change = SYSUTCDATETIME()")
        if "updated_at" in cols:
            sets.append("updated_at = SYSUTCDATETIME()")
        if sets:
            cursor.execute(
                f"UPDATE [STG].[BKD_GoodsItems] SET {', '.join(sets)} WHERE stg_item_id = ?",
                [v for _, v in values] + [int(existing[0])],
            )
        summary["staging_id"] = int(existing[0])
        summary["inserted"] = False
        return summary

    insert_cols = [k for k, _ in values]
    placeholders = ["?"] * len(values)
    if "last_sub_status_change" in cols:
        insert_cols.append("last_sub_status_change")
        placeholders.append("SYSUTCDATETIME()")
    cursor.execute(
        f"INSERT INTO [STG].[BKD_GoodsItems] ({', '.join(f'[{c}]' for c in insert_cols)}) "
        f"OUTPUT INSERTED.stg_item_id VALUES ({', '.join(placeholders)})",
        [v for _, v in values],
    )
    summary["staging_id"] = int(cursor.fetchone()[0])
    summary["inserted"] = True
    return summary


def _find_existing_goods_stg(cursor, cons_staging_id: int, line: ParsedGoodsLine):
    cursor.execute(
        """
        SELECT TOP 1 stg_item_id FROM [STG].[BKD_GoodsItems]
        WHERE stg_consignment_id = ?
          AND ISNULL(item_seq, 0) = ?
          AND (sku = ? OR sku IS NULL OR sku = '')
        ORDER BY stg_item_id DESC
        """,
        [cons_staging_id, line.line_no or 0, line.sku],
    )
    return cursor.fetchone()


def _try_learn_doc_product_catalog_unit_price(
    cursor,
    schema: str,
    customer_code: str,
    sku: str,
    unit_price_excl_vat: Any,
) -> bool:
    """Best-effort SKU unit-price learning from Sales Orders.

    The Sales Orders export has SKU in `No.` and per-unit price in
    `Unit Price Excl. VAT`. We only update an existing DocProductCatalog row;
    creating a new catalog row with just a price would make the product look
    known while tariff/weight masterdata may still be missing.
    """

    try:
        return _learn_doc_product_catalog_unit_price(
            cursor,
            schema,
            customer_code,
            sku,
            unit_price_excl_vat,
        )
    except Exception:
        logger.warning("Could not learn unit price for SKU %s", sku, exc_info=True)
        return False


def _learn_doc_product_catalog_unit_price(
    cursor,
    schema: str,
    customer_code: str,
    sku: str,
    unit_price_excl_vat: Any,
) -> bool:
    sku = str(sku or "").strip()
    if not sku or unit_price_excl_vat in (None, ""):
        return False
    try:
        unit_price = float(unit_price_excl_vat)
    except (TypeError, ValueError):
        return False
    if unit_price <= 0:
        return False

    cols = _columns(cursor, schema, "DocProductCatalog")
    if "id" not in cols or "unit_price" not in cols:
        return False

    lookup_cols = [col for col in ("sku", "product_code", "stock_code", "barcode") if col in cols]
    if not lookup_cols:
        return False

    predicates: list[str] = []
    params: list[Any] = []
    lookup_sql = []
    for col in lookup_cols:
        lookup_sql.append(f"UPPER(LTRIM(RTRIM([{col}]))) = UPPER(?)")
        params.append(sku)
    predicates.append("(" + " OR ".join(lookup_sql) + ")")

    customer = str(customer_code or "").strip()
    order_sql = "ORDER BY [id]"
    if "customer_code" in cols:
        if customer:
            predicates.append(
                "(UPPER(LTRIM(RTRIM([customer_code]))) = UPPER(?) "
                "OR UPPER(LTRIM(RTRIM([customer_code]))) = 'ALL' "
                "OR NULLIF(LTRIM(RTRIM([customer_code])), '') IS NULL)"
            )
            params.append(customer)
            order_sql = (
                "ORDER BY CASE "
                "WHEN UPPER(LTRIM(RTRIM([customer_code]))) = UPPER(?) THEN 0 "
                "WHEN UPPER(LTRIM(RTRIM([customer_code]))) = 'ALL' THEN 1 "
                "ELSE 2 END, [id]"
            )
            params.append(customer)
        else:
            predicates.append(
                "(UPPER(LTRIM(RTRIM([customer_code]))) = 'ALL' "
                "OR NULLIF(LTRIM(RTRIM([customer_code])), '') IS NULL)"
            )
    if "active" in cols:
        predicates.append("COALESCE([active], 1) = 1")

    cursor.execute(
        f"""
        SELECT TOP 1 [id]
        FROM [{schema}].DocProductCatalog
        WHERE {' AND '.join(predicates)}
        {order_sql}
        """,
        params,
    )
    row = cursor.fetchone()
    if not row:
        return False

    assignments = ["[unit_price] = ?"]
    update_params: list[Any] = [unit_price]
    if "updated_at" in cols:
        assignments.append("[updated_at] = SYSUTCDATETIME()")
    update_params.append(int(row[0]))
    cursor.execute(
        f"""
        UPDATE [{schema}].DocProductCatalog
        SET {', '.join(assignments)}
        WHERE [id] = ?
          AND [unit_price] IS NULL
        """,
        update_params,
    )
    return True


def _goods_lookup_where(
    cols: set[str],
    cons_staging_id: int,
    line: ParsedGoodsLine,
) -> tuple[str, list[Any]]:
    """Build a schema-aware goods lookup without optional missing columns."""
    if "staging_cons_id" not in cols:
        return "", []

    predicates = ["[staging_cons_id] = ?"]
    params: list[Any] = [cons_staging_id]

    if "item_number" in cols:
        predicates.append("ISNULL([item_number], 0) = ?")
        params.append(line.line_no or 0)
    if "sku" in cols:
        predicates.append("ISNULL([sku], '') = ?")
        params.append(line.sku)

    return " AND ".join(predicates), params


def _compute_weight(product: dict, line: ParsedGoodsLine, kind: str) -> float | None:
    """Per-product weight × line quantity. Returns None if unknown."""
    keys = (
        ("gross_weight_kg", "default_gross_weight_kg", "gross_mass_kg")
        if kind == "gross"
        else ("net_weight_kg", "default_net_weight_kg", "net_mass_kg")
    )
    unit = next((product.get(key) for key in keys if product.get(key) not in (None, "")), None)
    if unit in (None, ""):
        return None
    qty = _line_base_quantity(line)
    try:
        return _normalise_tss_weight(float(unit) * float(qty))
    except (TypeError, ValueError):
        return None


def _consignment_metadata_note(c: ParsedConsignment) -> str:
    parts = []
    if c.sell_to_customer_no:
        parts.append(f"customer={c.sell_to_customer_no}")
    if c.ship_to_phone:
        parts.append(f"phone={c.ship_to_phone}")
    if c.ship_to_county:
        parts.append(f"county={c.ship_to_county}")
    return "; ".join(parts)


def _ship_street(c: ParsedConsignment) -> str:
    return str(c.ship_to_address or "").strip()


def _columns(cursor, schema: str, table: str) -> set[str]:
    cursor.execute(
        """
        SELECT LOWER(COLUMN_NAME) FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [schema, table],
    )
    return {r[0] for r in cursor.fetchall()}
