"""Headless SDI/SupDec auto-submit worker.

The production flow keeps this as one operational step:

    SFD ready -> discover SUP in TSS -> stage/enrich goods -> validate -> submit

The worker never creates a SUP reference locally. TSS remains the source of
truth for the SDI header reference and SDI goods IDs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
import json
import logging
import time
from typing import Any

from app import config_store
from app.db import get_standalone_connection
from app.ingestion.defaults import IngestDefaults, resolve_ingest_defaults
from app.sdi_payloads import (
    SDI_HEADER_NESTED_FIELDS,
    SDI_UPDATE_FIELDS,
    build_sdi_goods_update_payload_for_api_attempt,
    build_sdi_update_payload_for_api_attempt,
)
from app.status_utils import consignment_should_discover_sdi
from app.tenant import get_tenant_by_code, normalize_tenant_code
from app.tss_text import tss_safe_text_suggestion
from app.tss_api import build_cfg_client

logger = logging.getLogger(__name__)

SDI_READ_FIELDS = tuple(dict.fromkeys((
    "status",
    "submission_due_date",
    "error_message",
    "sup_dec_number",
    "sfd_number",
    "movement_reference_number",
    *SDI_UPDATE_FIELDS,
    *(
        source_name
        for source_names in SDI_HEADER_NESTED_FIELDS.values()
        for source_name in source_names
    ),
)))

SDI_FALLBACK_FILTERS = ("draft", "trader input required")
SDI_POST_SUBMIT_REVIEW_STATUSES = {
    "DRAFT",
    "TRADER INPUT REQUIRED",
    "AMENDMENT REQUIRED",
    "REJECTED",
    "ERROR",
    "FAILED",
    "FAILURE",
}
SDI_TERMINAL_LOCAL_STATUSES = {
    "SUBMITTED",
    "COMPLETED",
    "CLOSED",
    "CANCELLED",
    "CANCELED",
    "DELETED",
}
SDI_CANCELLED_LOCAL_STATUSES = {
    "CANCELLED",
    "CANCELED",
    "DELETED",
}
_TABLE_COLUMNS_CACHE: dict[tuple[str, str], dict[str, str]] = {}
SDI_DB_TIMEOUT_SECONDS = 180


@dataclass
class SdiAutosubmitResult:
    tenant_code: str
    dry_run: bool = True
    submit_requested: bool = False
    submit_enabled: bool = False
    candidates: int = 0
    discovered: int = 0
    staged_headers: int = 0
    staged_goods: int = 0
    ready: int = 0
    blocked: int = 0
    submitted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def effective_submit(self) -> bool:
        return self.submit_requested and self.submit_enabled and not self.dry_run

    def add_error(self, message: str) -> None:
        if message:
            self.errors.append(str(message))


def run_sdi_autosubmit(
    *,
    tenant_code: str = "BKD",
    dry_run: bool = True,
    submit: bool = False,
    submit_enabled: bool | None = None,
    limit: int | None = None,
    api: Any = None,
    cursor: Any = None,
) -> SdiAutosubmitResult:
    """Discover, enrich, validate and optionally submit SDIs for one tenant.

    `dry_run=True` still stages/updates local STG/TSS mirrors so operators can
    see readiness, but it never calls TSS update or submit endpoints.
    """

    tenant_code = normalize_tenant_code(tenant_code or "BKD")
    if submit_enabled is None:
        submit_enabled = _truthy(
            config_store.get("SDI_AUTO", "SUBMIT_ENABLED", fallback="false", tenant_code=tenant_code)
        )
    if limit is None:
        limit = _as_int(
            config_store.get("SDI_AUTO", "MAX_ITEMS", fallback="25", tenant_code=tenant_code),
            default=25,
        )
    retry_cooldown_minutes = _as_int(
        config_store.get("SDI_AUTO", "RETRY_COOLDOWN_MINUTES", fallback="180", tenant_code=tenant_code),
        default=180,
    )
    db_timeout_seconds = _as_int(
        config_store.get(
            "SDI_AUTO",
            "DB_TIMEOUT_SECONDS",
            fallback=str(SDI_DB_TIMEOUT_SECONDS),
            tenant_code=tenant_code,
        ),
        default=SDI_DB_TIMEOUT_SECONDS,
    )

    result = SdiAutosubmitResult(
        tenant_code=tenant_code,
        dry_run=bool(dry_run),
        submit_requested=bool(submit),
        submit_enabled=bool(submit_enabled),
    )

    own_connection = cursor is None
    conn = None
    cur = cursor
    try:
        if cur is None:
            conn = get_standalone_connection()
            _set_query_timeout(conn, db_timeout_seconds)
            cur = conn.cursor()
        _set_query_timeout(cur, db_timeout_seconds)

        api = api or build_cfg_client()
        defaults = resolve_ingest_defaults(tenant_code)
        master_schema = get_tenant_by_code(tenant_code)["schema"]
        candidates = _fetch_candidates(
            cur,
            tenant_code,
            limit=limit,
            include_existing=bool(result.effective_submit),
            retry_cooldown_minutes=retry_cooldown_minutes,
        )
        result.candidates = len(candidates)

        for candidate in candidates:
            try:
                _process_candidate(cur, api, defaults, candidate, result, master_schema=master_schema)
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                sfd_ref = candidate.get("tss_sfd_number") or candidate.get("sfd_reference") or "unknown SFD"
                message = f"{sfd_ref}: {exc}"
                logger.exception("SDI autosubmit candidate failed: %s", message)
                result.add_error(message)

        if own_connection and conn is not None:
            conn.commit()
    except Exception:
        if own_connection and conn is not None:
            conn.rollback()
        raise
    finally:
        if own_connection and cur is not None:
            cur.close()
        if own_connection and conn is not None:
            conn.close()

    return result


def _set_query_timeout(target: Any, timeout_seconds: int | None) -> None:
    timeout = _as_int(timeout_seconds, default=SDI_DB_TIMEOUT_SECONDS)
    if not timeout or timeout <= 0:
        return
    try:
        target.timeout = int(timeout)
    except Exception:
        return


def _process_candidate(
    cur: Any,
    api: Any,
    defaults: IngestDefaults,
    candidate: dict[str, Any],
    result: SdiAutosubmitResult,
    *,
    master_schema: str,
) -> None:
    sfd_ref = _first_text(candidate.get("tss_sfd_number"), candidate.get("sfd_reference"))
    if not sfd_ref:
        result.blocked += 1
        result.add_error("Candidate has no SFD reference")
        return

    sdi_items = _discover_sdi_items(api, candidate)
    if not sdi_items:
        result.blocked += 1
        result.add_error(f"{sfd_ref}: no SDI returned by TSS yet")
        return

    for item in sdi_items:
        sup_ref = _sdi_reference(item)
        if not sup_ref:
            result.blocked += 1
            result.add_error(f"{sfd_ref}: TSS SDI item missing SUP reference")
            continue

        detail = _read_sdi_detail(api, sup_ref)
        header_record = _build_sdi_header_record(candidate, item, detail, defaults)
        header_trader_defaults = _load_trader_defaults(cur, master_schema, header_record.get("importer_eori"))
        header_record = _enrich_sdi_header_record(header_record, candidate, defaults, header_trader_defaults)
        duplicate = _find_existing_sdi_for_transport_document(
            cur,
            result.tenant_code,
            header_record.get("transport_document_number") or candidate.get("transport_document_number"),
            sup_ref=sup_ref,
            sfd_ref=sfd_ref,
            submission_due_date=header_record.get("tss_submission_due_date"),
        )
        if duplicate:
            existing_sup = _first_text(duplicate.get("sup_ref"), "unknown SUP")
            existing_status = _first_text(duplicate.get("tss_status"), "unknown status")
            transport_doc = _first_text(
                duplicate.get("transport_document_number"),
                header_record.get("transport_document_number"),
                candidate.get("transport_document_number"),
            )
            message = (
                f"{sup_ref}: duplicate transport document {transport_doc}; "
                f"existing SUP {existing_sup} is {existing_status}"
            )
            logger.warning("SDI duplicate transport guard blocked staging: %s", message)
            result.blocked += 1
            result.add_error(message)
            continue

        header_id = _upsert_tss_sdi_header(cur, result.tenant_code, header_record, raw_payload=detail or item)
        stg_sdi_id = _upsert_stg_sdi_header(cur, result.tenant_code, header_record)
        result.discovered += 1
        result.staged_headers += 1

        goods_items = _lookup_sdi_goods(api, sup_ref)
        source_goods = _fetch_source_goods(
            cur,
            result.tenant_code,
            _as_int(candidate.get("stg_consignment_id"), default=0),
        )
        staged_goods_for_submit: list[tuple[int, dict[str, Any]]] = []
        for goods_index, tss_goods in enumerate(goods_items):
            if not _goods_id(tss_goods):
                item_ref = _goods_item_number(tss_goods) or "?"
                logger.warning("%s item %s: TSS SDI goods item has no goods_id; skipping goods update", sup_ref, item_ref)
                continue

            source = _match_source_goods(source_goods, tss_goods, fallback_index=goods_index) or {}
            if not source:
                item_ref = _goods_item_number(tss_goods) or _goods_id(tss_goods) or "?"
                logger.info("%s item %s: no local source goods match; using TSS goods/defaults for API attempt", sup_ref, item_ref)

            product = _load_product_defaults(cur, master_schema, source) if source else {}
            trader = _load_trader_defaults(cur, master_schema, header_record.get("importer_eori"))
            staged = build_staged_sdi_goods_record(
                tss_goods=tss_goods,
                source_goods=source,
                product_defaults=product,
                trader_defaults=trader,
                ingest_defaults=defaults,
                client_code=result.tenant_code,
                stg_sdi_id=stg_sdi_id,
                sup_ref=sup_ref,
                sfd_ref=sfd_ref,
            )
            stg_item_id = _upsert_sdi_goods(cur, result.tenant_code, staged, validation_errors=[])
            _upsert_tss_sdi_goods(cur, result.tenant_code, tss_goods, sup_ref=sup_ref, sfd_ref=sfd_ref)
            result.staged_goods += 1

            staged_goods_for_submit.append((stg_item_id, staged))

        result.ready += 1
        if result.effective_submit:
            latest_detail = _read_sdi_detail(api, sup_ref)
            if latest_detail:
                header_record = _merge_sdi_tss_detail_for_submit(header_record, latest_detail)
                header_id = _upsert_tss_sdi_header(cur, result.tenant_code, header_record, raw_payload=latest_detail)
                stg_sdi_id = _upsert_stg_sdi_header(cur, result.tenant_code, header_record)

            goods_update_errors: list[str] = []
            for stg_item_id, staged in staged_goods_for_submit:
                payload, _payload_warnings = build_sdi_goods_update_payload_for_api_attempt(staged)
                api_result = api.update_sdi_goods(sup_ref, staged["tss_goods_id"], payload)
                if not _api_success(api_result):
                    message = f"{sup_ref} item {stg_item_id}: TSS goods update failed: {_api_message(api_result)}"
                    goods_update_errors.append(message)
                    logger.warning("SDI autosubmit goods update warning: %s", message)

            update_payload, _header_payload_warnings = build_sdi_update_payload_for_api_attempt(header_record)
            update_result = api.update_sdi(sup_ref, update_payload)
            if not _api_success(update_result):
                message = f"{sup_ref}: TSS SDI header update failed: {_api_message(update_result)}"
                result.add_error(message)
                result.blocked += 1
                _mark_sdi_header_state(cur, stg_sdi_id, status="PENDING_REVIEW", errors=[message])
                continue

            submit_result = api.submit_sdi(sup_ref)
            if not _api_success(submit_result):
                message = f"{sup_ref}: TSS SDI submit failed: {_api_message(submit_result)}"
                result.add_error(message)
                result.blocked += 1
                _mark_sdi_header_state(cur, stg_sdi_id, status="PENDING_REVIEW", errors=[message])
                continue

            result.submitted += 1
            final_detail = _read_sdi_detail_after_submit(api, sup_ref)
            if final_detail:
                final_header_record = _merge_sdi_tss_detail_for_submit(header_record, final_detail)
                header_id = _upsert_tss_sdi_header(
                    cur,
                    result.tenant_code,
                    final_header_record,
                    raw_payload=final_detail,
                )
                stg_sdi_id = _upsert_stg_sdi_header(cur, result.tenant_code, final_header_record)
            final_status = _first_text(_item_value(final_detail, "status"))
            final_error = _first_text(_item_value(final_detail, "error_message"))
            final_status_key = _clean_ref(final_status)
            _mark_tss_sdi_status(cur, header_id, final_status or "SUBMITTED")

            if final_error or final_status_key in SDI_POST_SUBMIT_REVIEW_STATUSES:
                message = final_error or f"TSS status after submit: {final_status}"
                review_message = f"{sup_ref}: TSS accepted submit but returned review status: {message}"
                result.add_error(review_message)
                result.blocked += 1
                _mark_sdi_header_state(cur, stg_sdi_id, status="PENDING_REVIEW", errors=[review_message])
            else:
                _mark_sdi_header_state(cur, stg_sdi_id, status="SUBMITTED", errors=[])
        else:
            _mark_sdi_header_state(cur, stg_sdi_id, status="VALIDATED", errors=[])


def build_staged_sdi_goods_record(
    *,
    tss_goods: dict[str, Any],
    source_goods: dict[str, Any],
    product_defaults: dict[str, Any],
    trader_defaults: dict[str, Any],
    ingest_defaults: IngestDefaults,
    client_code: str,
    stg_sdi_id: int,
    sup_ref: str,
    sfd_ref: str,
) -> dict[str, Any]:
    """Return one SDI-stage goods row assembled from TSS + source + defaults."""

    source_goods = source_goods or {}
    product_defaults = product_defaults or {}
    trader_defaults = trader_defaults or {}
    tss_goods = tss_goods or {}

    item_invoice_amount = _first_value(
        source_goods.get("line_amount_excl_vat"),
        source_goods.get("item_invoice_amount"),
        _item_value(tss_goods, "item_invoice_amount", "itemInvoiceAmount", "customs_value", "customsValue"),
    )

    return {
        "ClientCode": client_code,
        "stg_consignment_id": source_goods.get("stg_consignment_id"),
        "sub_status": "VALIDATED",
        "source": "SDI_AUTOSUBMIT",
        "stg_sdi_id": stg_sdi_id,
        "source_stg_item_id": source_goods.get("stg_item_id"),
        "tss_goods_id": _goods_id(tss_goods),
        "tss_sup_dec_number": sup_ref,
        "tss_sfd_number": sfd_ref,
        "tss_consignment_ref": _first_text(source_goods.get("tss_consignment_ref"), sfd_ref),
        "item_seq": _as_int(_first_value(_goods_item_number(tss_goods), source_goods.get("item_seq")), default=None),
        "goods_description": _safe_goods_description(
            source_goods.get("goods_description"),
            _item_value(tss_goods, "goods_description", "goodsDescription"),
            product_defaults.get("description"),
        ),
        "commodity_code": _first_text(
            source_goods.get("commodity_code"),
            _item_value(tss_goods, "commodity_code", "commodityCode"),
            product_defaults.get("commodity_code"),
        ),
        "gross_mass_kg": _first_value(
            source_goods.get("gross_mass_kg"),
            _item_value(tss_goods, "gross_mass_kg", "grossMassKg", "gross_weight_kg", "grossWeightKg"),
            _product_weight_for_quantity(product_defaults, source_goods, "gross"),
        ),
        "net_mass_kg": _first_value(
            source_goods.get("net_mass_kg"),
            _item_value(tss_goods, "net_mass_kg", "netMassKg", "net_weight_kg", "netWeightKg"),
            _product_weight_for_quantity(product_defaults, source_goods, "net"),
        ),
        "number_of_packages": _first_value(
            source_goods.get("number_of_packages"),
            _item_value(tss_goods, "number_of_packages", "numberOfPackages"),
        ),
        "number_of_individual_pieces": _first_value(
            source_goods.get("number_of_individual_pieces"),
            _item_value(tss_goods, "number_of_individual_pieces", "numberOfIndividualPieces"),
        ),
        "type_of_packages": _first_text(
            source_goods.get("type_of_packages"),
            _item_value(tss_goods, "type_of_packages", "typeOfPackages", "type_of_package", "typeOfPackage"),
            product_defaults.get("package_type"),
            ingest_defaults.package_type,
        ),
        "package_marks": _first_text(
            source_goods.get("package_marks"),
            _item_value(tss_goods, "package_marks", "packageMarks"),
            "ADDR",
        ),
        "equipment_number": _first_text(
            source_goods.get("equipment_number"),
            _item_value(tss_goods, "equipment_number", "equipmentNumber"),
        ),
        "un_dangerous_goods_code": _first_text(
            source_goods.get("un_dangerous_goods_code"),
            _item_value(tss_goods, "un_dangerous_goods_code", "unDangerousGoodsCode"),
        ),
        "procedure_code": _first_text(
            source_goods.get("procedure_code"),
            _item_value(tss_goods, "procedure_code", "procedureCode"),
            product_defaults.get("procedure_code"),
            trader_defaults.get("procedure_code"),
            ingest_defaults.procedure_code,
        ),
        "additional_procedure_code": _first_text(
            source_goods.get("additional_procedure_code"),
            _item_value(tss_goods, "additional_procedure_code", "additionalProcedureCode", "additional_procedure_codes"),
            product_defaults.get("additional_procedure_code"),
            trader_defaults.get("additional_procedure_code"),
            ingest_defaults.additional_procedure_code,
        ),
        "controlled_goods": _first_text(
            source_goods.get("controlled_goods"),
            _item_value(tss_goods, "controlled_goods", "controlledGoods"),
            product_defaults.get("controlled_goods"),
            ingest_defaults.controlled_goods,
        ),
        "controlled_goods_type": _first_text(
            source_goods.get("controlled_goods_type"),
            product_defaults.get("controlled_goods_type"),
        ),
        "country_of_origin": _first_text(
            source_goods.get("country_of_origin"),
            _item_value(tss_goods, "country_of_origin", "countryOfOrigin"),
            product_defaults.get("country_of_origin"),
            ingest_defaults.country_of_origin,
        ),
        "item_invoice_amount": item_invoice_amount,
        "customs_value": _first_value(source_goods.get("customs_value"), item_invoice_amount),
        "line_amount_excl_vat": source_goods.get("line_amount_excl_vat"),
        "source_amount": source_goods.get("source_amount"),
        "unit_price_excl_vat": source_goods.get("unit_price_excl_vat"),
        "item_invoice_currency": _first_text(
            source_goods.get("item_invoice_currency"),
            _item_value(tss_goods, "item_invoice_currency", "itemInvoiceCurrency"),
            product_defaults.get("currency"),
            trader_defaults.get("item_invoice_currency"),
            ingest_defaults.invoice_currency,
        ),
        "valuation_method": _first_text(
            source_goods.get("valuation_method"),
            _item_value(tss_goods, "valuation_method", "valuationMethod"),
            product_defaults.get("valuation_method"),
            trader_defaults.get("valuation_method"),
            ingest_defaults.valuation_method,
        ),
        "valuation_indicator": _first_text(
            source_goods.get("valuation_indicator"),
            _item_value(tss_goods, "valuation_indicator", "valuationIndicator"),
            product_defaults.get("valuation_indicator"),
            trader_defaults.get("valuation_indicator"),
        ),
        "statistical_value": _first_value(
            source_goods.get("statistical_value"),
            _item_value(tss_goods, "statistical_value", "statisticalValue"),
        ),
        "nature_of_transaction": _first_text(
            source_goods.get("nature_of_transaction"),
            _item_value(tss_goods, "nature_of_transaction", "natureOfTransaction"),
            product_defaults.get("nature_of_transaction"),
            trader_defaults.get("nature_of_transaction"),
            ingest_defaults.sdi_nature_of_transaction,
        ),
        "preference": _first_text(
            source_goods.get("preference"),
            _item_value(tss_goods, "preference", "preference_code", "preferenceCode"),
            product_defaults.get("preference_code"),
            trader_defaults.get("preference"),
        ),
        "ni_additional_information_codes": _first_text(
            _sdi_choice_value(source_goods.get("ni_additional_information_codes")),
            _sdi_choice_value(_item_value(tss_goods, "ni_additional_information_codes", "niAdditionalInformationCodes")),
            product_defaults.get("ni_additional_information_codes"),
            trader_defaults.get("ni_additional_information_codes"),
            ingest_defaults.sdi_ni_additional_information_codes,
        ),
        "invoice_number": _first_text(
            source_goods.get("invoice_number"),
            _item_value(tss_goods, "invoice_number", "invoiceNumber"),
            source_goods.get("source_invoice_number"),
            source_goods.get("source_transport_document_number"),
            source_goods.get("transport_document_number"),
            source_goods.get("source_trader_reference"),
            source_goods.get("trader_reference"),
        ),
        "country_of_preferential_origin": _first_text(
            source_goods.get("country_of_preferential_origin"),
            _item_value(tss_goods, "country_of_preferential_origin", "countryOfPreferentialOrigin"),
            product_defaults.get("country_of_preferential_origin"),
            trader_defaults.get("country_of_preferential_origin"),
        ),
        "supplementary_units": _first_value(
            source_goods.get("supplementary_units"),
            _item_value(tss_goods, "supplementary_units", "supplementaryUnits"),
        ),
        "quota_order_number": _first_text(
            source_goods.get("quota_order_number"),
            _item_value(tss_goods, "quota_order_number", "quotaOrderNumber"),
            product_defaults.get("quota_order_number"),
        ),
        "taric_code": _first_text(
            source_goods.get("taric_code"),
            _item_value(tss_goods, "taric_code", "taricCode"),
            product_defaults.get("taric_code"),
        ),
        "cus_code": _first_text(
            source_goods.get("cus_code"),
            _item_value(tss_goods, "cus_code", "cusCode"),
            product_defaults.get("cus_code"),
        ),
        "national_additional_code": _first_text(
            source_goods.get("national_additional_code"),
            _item_value(tss_goods, "national_additional_code", "nationalAdditionalCode"),
            product_defaults.get("national_additional_code"),
        ),
        "tax_type": source_goods.get("tax_type"),
        "tax_base_unit": source_goods.get("tax_base_unit"),
        "tax_base_quantity": source_goods.get("tax_base_quantity"),
        "payable_tax_amount": source_goods.get("payable_tax_amount"),
        "payable_tax_currency": source_goods.get("payable_tax_currency"),
        "additional_procedures_json": source_goods.get("additional_procedures_json"),
        "document_references_json": _first_nested_json(
            source_goods.get("document_references_json"),
            product_defaults.get("document_references_json"),
        ),
        "additional_information_json": _first_nested_json(
            source_goods.get("additional_information_json"),
            product_defaults.get("additional_information_json"),
        ),
        "detail_previous_document_json": source_goods.get("detail_previous_document_json"),
        "item_add_ded_json": source_goods.get("item_add_ded_json"),
        "national_additional_codes_json": source_goods.get("national_additional_codes_json"),
        "tax_bases_json": source_goods.get("tax_bases_json"),
        "additional_parties_json": source_goods.get("additional_parties_json"),
    }


def should_call_tss_submit(*, dry_run: bool, submit_requested: bool, submit_enabled: bool) -> bool:
    return bool(submit_requested and submit_enabled and not dry_run)


def _discover_sdi_items(api: Any, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    sfd_ref = _first_text(candidate.get("tss_sfd_number"), candidate.get("sfd_reference"))
    items = []
    try:
        items = list(api.lookup_sdi_items(sfd_ref) or [])
    except Exception as exc:
        logger.warning("SDI lookup by SFD failed for %s: %s", sfd_ref, exc)

    if not items:
        for status_filter in SDI_FALLBACK_FILTERS:
            try:
                for item in api.filter_sdi_items(status_filter) or []:
                    if _sdi_matches_candidate(item, candidate):
                        items.append(item)
            except Exception as exc:
                logger.warning("SDI fallback filter failed for %s/%s: %s", sfd_ref, status_filter, exc)

    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        ref = _sdi_reference(item)
        if ref:
            unique[ref] = item
    return list(unique.values())


def _read_sdi_detail(api: Any, sup_ref: str) -> dict[str, Any]:
    try:
        result = api.read_sdi(sup_ref, fields=list(SDI_READ_FIELDS))
    except Exception as exc:
        logger.warning("Read SDI detail failed for %s: %s", sup_ref, exc)
        return {}
    if isinstance(result, dict):
        response = result.get("response", result.get("data", result))
        if isinstance(response, dict) and isinstance(response.get("result"), dict):
            response = response["result"]
        if isinstance(response, dict):
            return response
    return {}


def _read_sdi_detail_after_submit(
    api: Any,
    sup_ref: str,
    *,
    attempts: int = 5,
    delay_seconds: int = 5,
    initial_delay_seconds: int | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    if initial_delay_seconds is None:
        initial_delay_seconds = delay_seconds
    if initial_delay_seconds and initial_delay_seconds > 0:
        time.sleep(initial_delay_seconds)
    for attempt in range(max(1, int(attempts or 1))):
        if attempt:
            time.sleep(delay_seconds)
        detail = _read_sdi_detail(api, sup_ref)
        status = _clean_ref(_item_value(detail, "status"))
        if status and status != "DRAFT":
            break
    return detail


def _empty_submit_value(value: Any) -> bool:
    if value in (None, ""):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _merge_sdi_tss_detail_for_submit(record: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Fill missing submit fields from the latest TSS SDI GET response."""

    merged = dict(record or {})
    field_map = {
        "status": "tss_status",
        "submission_due_date": "tss_submission_due_date",
        "sfd_number": "sfd_reference",
        "movement_reference_number": "tss_movement_reference_number",
    }

    for field_name in SDI_READ_FIELDS:
        value = _item_value(detail, field_name)
        if _empty_submit_value(value):
            continue
        target_field = field_map.get(field_name, field_name)
        if _empty_submit_value(merged.get(target_field)):
            merged[target_field] = value

    return merged


def _lookup_sdi_goods(api: Any, sup_ref: str) -> list[dict[str, Any]]:
    try:
        return [item for item in (api.lookup_sdi_goods(sup_ref) or []) if isinstance(item, dict)]
    except Exception as exc:
        logger.warning("Lookup SDI goods failed for %s: %s", sup_ref, exc)
        return []


def _build_sdi_header_record(
    candidate: dict[str, Any],
    item: dict[str, Any],
    detail: dict[str, Any],
    defaults: IngestDefaults,
) -> dict[str, Any]:
    merged = {}
    merged.update(candidate or {})
    merged.update(item or {})
    merged.update(detail or {})

    sup_ref = _sdi_reference(merged)
    sfd_ref = _first_text(
        _item_value(merged, "sfd_number", "sfd_reference", "tss_sfd_number", "parent", "u_parent"),
        candidate.get("tss_sfd_number"),
    )

    return {
        "sup_dec_number": sup_ref,
        "tss_sup_dec_number": sup_ref,
        "sfd_reference": sfd_ref,
        "tss_sfd_consignment_ref": sfd_ref,
        "stg_consignment_id": candidate.get("stg_consignment_id"),
        "tss_consignment_ref": candidate.get("tss_consignment_ref"),
        "tss_status": _first_text(
            _item_value(merged, "status", "state", "tss_status"),
            "DRAFT",
        ),
        "arrival_date_time": _first_text(
            _item_value(merged, "arrival_date_time", "arrivalDateTime"),
            candidate.get("arrival_date_time"),
        ),
        "tss_submission_due_date": _first_text(
            _item_value(merged, "submission_due_date", "submissionDueDate")
        ),
        "tss_movement_reference_number": _first_text(
            _item_value(merged, "movement_reference_number", "movementReferenceNumber"),
            candidate.get("tss_movement_reference_number"),
        ),
        "trader_reference": _first_text(
            _item_value(merged, "trader_reference", "u_trader_reference"),
            candidate.get("trader_reference"),
        ),
        "declaration_choice": _first_text(_item_value(merged, "declaration_choice"), "H1"),
        "authorisation_type": _first_text(
            _item_value(merged, "authorisation_type", "authorisationType"),
        ),
        "representation_type": _first_text(
            _item_value(merged, "representation_type", "representationType"),
        ),
        "additional_procedure": _first_text(
            _item_value(merged, "additional_procedure", "additionalProcedure"),
        ),
        "goods_domestic_status": _first_text(
            _item_value(merged, "goods_domestic_status", "goodsDomesticStatus"),
            candidate.get("goods_domestic_status"),
        ),
        "importer_eori": _first_text(candidate.get("importer_eori"), defaults.importer_eori),
        "importer_name": _first_text(
            _item_value(merged, "importer_name", "importerName"),
            candidate.get("importer_name"),
            defaults.importer_name,
        ),
        "importer_street_number": _first_text(
            _item_value(merged, "importer_street_number", "importerStreetNumber"),
            candidate.get("importer_street_number"),
            defaults.importer_street_number,
        ),
        "importer_city": _first_text(
            _item_value(merged, "importer_city", "importerCity"),
            candidate.get("importer_city"),
            defaults.importer_city,
        ),
        "importer_postcode": _first_text(
            _item_value(merged, "importer_postcode", "importerPostcode"),
            candidate.get("importer_postcode"),
            defaults.importer_postcode,
        ),
        "importer_country": _first_text(
            _item_value(merged, "importer_country", "importerCountry"),
            candidate.get("importer_country"),
            defaults.importer_country,
        ),
        "exporter_eori": _first_text(
            _item_value(merged, "exporter_eori", "exporterEori"),
            candidate.get("exporter_eori"),
            defaults.exporter_eori,
        ),
        "exporter_name": _first_text(
            _item_value(merged, "exporter_name", "exporterName"),
            candidate.get("exporter_name"),
            defaults.supplier_name,
        ),
        "exporter_street_number": _first_text(
            _item_value(merged, "exporter_street_number", "exporterStreetNumber"),
            candidate.get("exporter_street_number"),
        ),
        "exporter_city": _first_text(
            _item_value(merged, "exporter_city", "exporterCity"),
            candidate.get("exporter_city"),
        ),
        "exporter_postcode": _first_text(
            _item_value(merged, "exporter_postcode", "exporterPostcode"),
            candidate.get("exporter_postcode"),
        ),
        "exporter_country": _first_text(
            _item_value(merged, "exporter_country", "exporterCountry"),
            candidate.get("exporter_country"),
        ),
        "transport_document_number": _first_text(
            _item_value(merged, "transport_document_number", "transportDocumentNumber"),
            candidate.get("transport_document_number"),
        ),
        "controlled_goods": _first_text(
            _item_value(merged, "controlled_goods", "controlledGoods"),
            candidate.get("controlled_goods"),
            defaults.controlled_goods,
        ),
        "goods_description": _safe_goods_description(
            _item_value(merged, "goods_description", "goodsDescription"),
            candidate.get("goods_description"),
        ),
        "movement_type": _normalise_sdi_movement_type(_first_text(
            _item_value(merged, "movement_type", "movementType"),
            candidate.get("movement_type"),
            defaults.sdi_movement_type,
            defaults.movement_type,
        )),
        "destination_country": _first_text(
            _item_value(merged, "destination_country", "destinationCountry"),
            candidate.get("destination_country"),
        ),
        "nationality_of_transport": _first_text(
            _item_value(merged, "nationality_of_transport", "nationalityOfTransport"),
            candidate.get("nationality_of_transport"),
            defaults.nationality_of_transport,
        ),
        "identity_no_of_transport": _first_text(
            _item_value(merged, "identity_no_of_transport", "identityNoOfTransport", "identity_no_transport"),
            candidate.get("identity_no_of_transport"),
            defaults.identity_no_of_transport,
        ),
        "location_of_goods_border": _first_text(
            _item_value(merged, "location_of_goods_border", "locationOfGoodsBorder"),
            candidate.get("arrival_port"),
        ),
        "location_of_goods_other": _first_text(
            _item_value(merged, "location_of_goods_other", "locationOfGoodsOther"),
        ),
        "un_locode": _first_text(_item_value(merged, "un_locode", "unLocode")),
        "incoterm": _first_text(_item_value(merged, "incoterm")),
        "delivery_location_country": _first_text(
            _item_value(merged, "delivery_location_country", "deliveryLocationCountry")
        ),
        "delivery_location_town": _first_text(
            _item_value(merged, "delivery_location_town", "deliveryLocationTown")
        ),
        "freight_charge": _first_value(_item_value(merged, "freight_charge", "freightCharge")),
        "freight_charge_currency": _first_text(
            _item_value(merged, "freight_charge_currency", "freightChargeCurrency")
        ),
        "insurance": _first_value(_item_value(merged, "insurance")),
        "insurance_currency": _first_text(_item_value(merged, "insurance_currency", "insuranceCurrency")),
        "postponed_vat": _first_text(_item_value(merged, "postponed_vat", "postponedVat")),
        "vat_adjustment": _first_value(_item_value(merged, "vat_adjustment", "vatAdjustment")),
        "vat_adjust_currency": _first_text(_item_value(merged, "vat_adjust_currency", "vatAdjustCurrency")),
        "exchange_rate": _first_value(_item_value(merged, "exchange_rate", "exchangeRate")),
        "vat_number": _first_text(_item_value(merged, "vat_number", "vatNumber")),
        "header_additions_deductions_json": _json_or_none(
            _first_value(_item_value(merged, "header_additions_deductions", "headerAdditionsDeductions"))
        ),
        "header_previous_document_json": _json_or_none(
            _first_value(_item_value(merged, "header_previous_document", "headerPreviousDocument"))
        ),
        "holder_of_authorisation_json": _json_or_none(
            _first_value(_item_value(merged, "holder_of_authorisation", "holderOfAuthorisation"))
        ),
    }


def _enrich_sdi_header_record(
    record: dict[str, Any],
    candidate: dict[str, Any],
    defaults: IngestDefaults,
    trader_defaults: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(record or {})
    candidate = candidate or {}
    trader_defaults = trader_defaults or {}

    text_defaults = {
        "authorisation_type": (trader_defaults.get("authorisation_type"),),
        "representation_type": (trader_defaults.get("representation_type"), defaults.sdi_representation_type),
        "additional_procedure": (trader_defaults.get("additional_procedure"), "no"),
        "goods_domestic_status": (
            trader_defaults.get("goods_domestic_status"),
            defaults.sdi_goods_domestic_status,
            defaults.goods_domestic_status,
        ),
        "movement_type": (
            trader_defaults.get("movement_type"),
            candidate.get("movement_type"),
            defaults.sdi_movement_type,
            defaults.movement_type,
        ),
        "destination_country": (
            trader_defaults.get("destination_country"),
            candidate.get("destination_country"),
            defaults.importer_country,
        ),
        "nationality_of_transport": (
            trader_defaults.get("nationality_of_transport"),
            candidate.get("nationality_of_transport"),
            defaults.nationality_of_transport,
        ),
        "identity_no_of_transport": (
            trader_defaults.get("identity_no_of_transport"),
            candidate.get("identity_no_of_transport"),
            defaults.identity_no_of_transport,
        ),
        "location_of_goods_border": (trader_defaults.get("location_of_goods_border"), candidate.get("arrival_port")),
        "location_of_goods_other": (trader_defaults.get("location_of_goods_other"),),
        "un_locode": (trader_defaults.get("un_locode"),),
        "incoterm": (trader_defaults.get("incoterm"), candidate.get("incoterm"), defaults.sdi_incoterm),
        "delivery_location_country": (
            trader_defaults.get("delivery_location_country"),
            candidate.get("importer_country"),
            defaults.importer_country,
        ),
        "delivery_location_town": (
            trader_defaults.get("delivery_location_town"),
            candidate.get("importer_city"),
            defaults.importer_city,
        ),
        "postponed_vat": (trader_defaults.get("postponed_vat"), defaults.sdi_postponed_vat),
        "vat_number": (trader_defaults.get("vat_number"),),
        "importer_name": (trader_defaults.get("importer_name"), candidate.get("importer_name"), defaults.importer_name),
        "importer_street_number": (
            trader_defaults.get("importer_street_number"),
            candidate.get("importer_street_number"),
            defaults.importer_street_number,
        ),
        "importer_city": (trader_defaults.get("importer_city"), candidate.get("importer_city"), defaults.importer_city),
        "importer_postcode": (
            trader_defaults.get("importer_postcode"),
            candidate.get("importer_postcode"),
            defaults.importer_postcode,
        ),
        "importer_country": (
            trader_defaults.get("importer_country"),
            candidate.get("importer_country"),
            defaults.importer_country,
        ),
        "exporter_name": (trader_defaults.get("exporter_name"), candidate.get("exporter_name"), defaults.supplier_name),
        "exporter_street_number": (
            trader_defaults.get("exporter_street_number"),
            candidate.get("exporter_street_number"),
        ),
        "exporter_city": (trader_defaults.get("exporter_city"), candidate.get("exporter_city")),
        "exporter_postcode": (trader_defaults.get("exporter_postcode"), candidate.get("exporter_postcode")),
        "exporter_country": (trader_defaults.get("exporter_country"), candidate.get("exporter_country")),
        "freight_charge_currency": (trader_defaults.get("freight_charge_currency"),),
        "insurance_currency": (trader_defaults.get("insurance_currency"),),
        "vat_adjust_currency": (trader_defaults.get("vat_adjust_currency"),),
    }

    for field_name, fallback_values in text_defaults.items():
        current = enriched.get(field_name)
        if _first_text(current):
            continue
        value = _first_text(*fallback_values)
        if field_name == "movement_type":
            value = _normalise_sdi_movement_type(value)
        if value:
            enriched[field_name] = value

    numeric_defaults = {
        "freight_charge": (trader_defaults.get("freight_charge"),),
        "insurance": (trader_defaults.get("insurance"),),
        "vat_adjustment": (trader_defaults.get("vat_adjustment"),),
        "exchange_rate": (trader_defaults.get("exchange_rate"),),
    }
    for field_name, fallback_values in numeric_defaults.items():
        if _first_value(enriched.get(field_name)) is not None:
            continue
        value = _first_value(*fallback_values)
        if value is not None:
            enriched[field_name] = value

    for field_name in (
        "header_additions_deductions_json",
        "header_previous_document_json",
        "holder_of_authorisation_json",
    ):
        if _first_text(enriched.get(field_name)):
            continue
        value = _json_or_none(trader_defaults.get(field_name))
        if value:
            enriched[field_name] = value

    return enriched


def _fetch_candidates(
    cur: Any,
    tenant_code: str,
    *,
    limit: int,
    include_existing: bool = False,
    retry_cooldown_minutes: int = 180,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 25), 500))
    sdi_columns = _table_columns(cur, "STG", "BKD_SDI_Headers")
    duplicate_cancelled_placeholders = ", ".join("?" for _ in SDI_CANCELLED_LOCAL_STATUSES)
    existing_retry_sql = ""
    existing_retry_params: list[Any] = []
    if include_existing:
        cooldown_sql = ""
        cooldown_params: list[Any] = []
        if "last_autosubmit_attempt_at" in sdi_columns:
            cooldown_sql = """
                AND (
                    sh_retry.last_autosubmit_attempt_at IS NULL
                    OR sh_retry.last_autosubmit_attempt_at <= DATEADD(MINUTE, -?, SYSUTCDATETIME())
                )
            """
            cooldown_params.append(max(0, int(retry_cooldown_minutes or 0)))
        terminal_placeholders = ", ".join("?" for _ in SDI_TERMINAL_LOCAL_STATUSES)
        existing_retry_sql = f"""
              OR EXISTS (
                  SELECT 1
                  FROM [STG].[BKD_SDI_Headers] sh_retry
                  WHERE sh_retry.ClientCode = s.ClientCode
                    AND sh_retry.tss_sfd_consignment_ref = s.tss_sfd_number
                    AND UPPER(COALESCE(sh_retry.sub_status, '')) NOT IN ({terminal_placeholders})
                    {cooldown_sql}
              )
        """
        existing_retry_params.extend(sorted(SDI_TERMINAL_LOCAL_STATUSES))
        existing_retry_params.extend(cooldown_params)
    cur.execute(
        f"""
        SELECT TOP ({limit})
            s.stg_tracking_id,
            s.ClientCode,
            s.tss_consignment_ref,
            s.tss_sfd_number,
            s.tss_sfd_status,
            s.tss_movement_reference_number,
            c.stg_consignment_id,
            c.trader_reference,
            c.transport_document_number,
            c.importer_eori,
            c.importer_name,
            c.importer_street_number,
            c.importer_city,
            c.importer_postcode,
            c.importer_country,
            c.exporter_eori,
            CAST(NULL AS NVARCHAR(70)) AS exporter_name,
            CAST(NULL AS NVARCHAR(70)) AS exporter_street_number,
            CAST(NULL AS NVARCHAR(35)) AS exporter_city,
            CAST(NULL AS NVARCHAR(9)) AS exporter_postcode,
            CAST(NULL AS NVARCHAR(2)) AS exporter_country,
            c.goods_description,
            c.controlled_goods,
            c.goods_domestic_status,
            c.destination_country,
            c.generate_SD,
            c.no_sfd_reason,
            c.ducr,
            eh.movement_type,
            eh.identity_no_of_transport,
            eh.nationality_of_transport,
            eh.arrival_port,
            eh.arrival_date_time
        FROM [STG].[BKD_SFD_Tracking] s
        LEFT JOIN [STG].[BKD_ENS_Consignments] c
               ON c.ClientCode = s.ClientCode
              AND c.tss_consignment_ref = s.tss_consignment_ref
        LEFT JOIN [STG].[BKD_ENS_Headers] eh
               ON eh.ClientCode = c.ClientCode
              AND eh.stg_header_id = c.stg_header_id
        WHERE s.ClientCode = ?
          AND NULLIF(LTRIM(RTRIM(s.tss_sfd_number)), '') IS NOT NULL
          AND (
              NULLIF(LTRIM(RTRIM(c.transport_document_number)), '') IS NULL
              OR NOT EXISTS (
                  SELECT 1
                  FROM [STG].[BKD_SDI_Headers] sh_doc
                  LEFT JOIN [TSS].[BKD_SDI_Headers] tss_doc
                         ON tss_doc.ClientCode = sh_doc.ClientCode
                        AND tss_doc.SupDecNumber = sh_doc.tss_sup_dec_number
                  LEFT JOIN [STG].[BKD_ENS_Consignments] c_doc
                         ON c_doc.ClientCode = sh_doc.ClientCode
                        AND c_doc.stg_consignment_id = sh_doc.stg_consignment_id
                  WHERE sh_doc.ClientCode = s.ClientCode
                    AND NULLIF(LTRIM(RTRIM(sh_doc.tss_sup_dec_number)), '') IS NOT NULL
                    AND UPPER(LTRIM(RTRIM(COALESCE(NULLIF(sh_doc.transport_document_number, ''), NULLIF(c_doc.transport_document_number, ''))))) =
                        UPPER(LTRIM(RTRIM(c.transport_document_number)))
                    AND (
                        NULLIF(LTRIM(RTRIM(sh_doc.tss_sfd_consignment_ref)), '') IS NULL
                        OR UPPER(LTRIM(RTRIM(sh_doc.tss_sfd_consignment_ref))) <> UPPER(LTRIM(RTRIM(s.tss_sfd_number)))
                    )
                    AND UPPER(COALESCE(NULLIF(tss_doc.TssStatus, ''), NULLIF(sh_doc.tss_status, ''), NULLIF(sh_doc.sub_status, ''), '')) NOT IN ({duplicate_cancelled_placeholders})
              )
          )
          AND (
              NOT EXISTS (
                  SELECT 1
                  FROM [STG].[BKD_SDI_Headers] sh
                  WHERE sh.ClientCode = s.ClientCode
                    AND sh.tss_sfd_consignment_ref = s.tss_sfd_number
              )
              {existing_retry_sql}
          )
        ORDER BY COALESCE(s.stg_polled_at, SYSUTCDATETIME()) DESC, s.stg_tracking_id DESC
        """,
        [tenant_code, *sorted(SDI_CANCELLED_LOCAL_STATUSES), *existing_retry_params],
    )
    return [row for row in _rows_as_dicts(cur) if _candidate_should_discover_sdi(row)]


def _candidate_should_discover_sdi(candidate: dict[str, Any]) -> bool:
    record = dict(candidate or {})
    sfd_ref = _first_text(record.get("tss_sfd_number"), record.get("sfd_reference"))
    if sfd_ref:
        record.setdefault("sfd_reference", sfd_ref)
        record.setdefault("sfd_number", sfd_ref)
        record.setdefault("synced_sfd_reference", sfd_ref)
    return consignment_should_discover_sdi(record)


def _find_existing_sdi_for_transport_document(
    cur: Any,
    tenant_code: str,
    transport_document_number: Any,
    *,
    sup_ref: str = "",
    sfd_ref: str = "",
    submission_due_date: Any = None,
) -> dict[str, Any] | None:
    transport_doc = _clean_ref(transport_document_number)
    if not transport_doc:
        return None

    clean_sup = _clean_ref(sup_ref)
    clean_sfd = _clean_ref(sfd_ref)
    due_date = _date_value(submission_due_date)
    cancelled_placeholders = ", ".join("?" for _ in SDI_CANCELLED_LOCAL_STATUSES)
    cur.execute(
        f"""
        SELECT TOP 1
            h.stg_sdi_id,
            h.tss_sup_dec_number AS sup_ref,
            h.tss_sfd_consignment_ref AS sfd_ref,
            COALESCE(h.tss_submission_due_date, tss_h.SubmissionDueDate) AS submission_due_date,
            COALESCE(NULLIF(tss_h.TssStatus, ''), NULLIF(h.tss_status, ''), NULLIF(h.sub_status, '')) AS tss_status,
            COALESCE(NULLIF(h.transport_document_number, ''), NULLIF(c.transport_document_number, '')) AS transport_document_number
        FROM [STG].[BKD_SDI_Headers] h
        LEFT JOIN [TSS].[BKD_SDI_Headers] tss_h
               ON tss_h.ClientCode = h.ClientCode
              AND tss_h.SupDecNumber = h.tss_sup_dec_number
        LEFT JOIN [STG].[BKD_ENS_Consignments] c
               ON c.ClientCode = h.ClientCode
              AND c.stg_consignment_id = h.stg_consignment_id
        WHERE h.ClientCode = ?
          AND NULLIF(LTRIM(RTRIM(h.tss_sup_dec_number)), '') IS NOT NULL
          AND UPPER(LTRIM(RTRIM(COALESCE(NULLIF(h.transport_document_number, ''), NULLIF(c.transport_document_number, ''))))) = ?
          AND (? = '' OR UPPER(LTRIM(RTRIM(h.tss_sup_dec_number))) <> ?)
          AND (? = '' OR NULLIF(LTRIM(RTRIM(h.tss_sfd_consignment_ref)), '') IS NULL OR UPPER(LTRIM(RTRIM(h.tss_sfd_consignment_ref))) <> ?)
          AND (
              ? IS NULL
              OR COALESCE(TRY_CONVERT(date, h.tss_submission_due_date), TRY_CONVERT(date, tss_h.SubmissionDueDate)) IS NULL
              OR COALESCE(TRY_CONVERT(date, h.tss_submission_due_date), TRY_CONVERT(date, tss_h.SubmissionDueDate)) = ?
          )
          AND UPPER(COALESCE(NULLIF(tss_h.TssStatus, ''), NULLIF(h.tss_status, ''), NULLIF(h.sub_status, ''), '')) NOT IN ({cancelled_placeholders})
        ORDER BY
            CASE
                WHEN UPPER(COALESCE(NULLIF(tss_h.TssStatus, ''), NULLIF(h.tss_status, ''), NULLIF(h.sub_status, ''), '')) IN ('CLOSED', 'COMPLETED', 'ACCEPTED', 'CLEARED') THEN 0
                ELSE 1
            END,
            COALESCE(h.updated_at, h.created_at, SYSUTCDATETIME()) DESC,
            h.stg_sdi_id DESC
        """,
        [
            tenant_code,
            transport_doc,
            clean_sup,
            clean_sup,
            clean_sfd,
            clean_sfd,
            due_date,
            due_date,
            *sorted(SDI_CANCELLED_LOCAL_STATUSES),
        ],
    )
    row = cur.fetchone()
    return _row_as_dict(row, cur.description) if row else None


def _fetch_source_goods(cur: Any, tenant_code: str, stg_consignment_id: int | None) -> list[dict[str, Any]]:
    if not stg_consignment_id:
        return []
    cur.execute(
        """
        SELECT
            g.*,
            c.transport_document_number AS source_transport_document_number,
            c.trader_reference AS source_trader_reference
        FROM [STG].[BKD_GoodsItems] g
        LEFT JOIN [STG].[BKD_ENS_Consignments] c
               ON c.ClientCode = g.ClientCode
              AND c.stg_consignment_id = g.stg_consignment_id
        WHERE g.ClientCode = ?
          AND g.stg_consignment_id = ?
          AND UPPER(COALESCE(g.goods_stage, 'ENS')) <> 'SDI'
        ORDER BY COALESCE(g.item_seq, g.stg_item_id), g.stg_item_id
        """,
        [tenant_code, stg_consignment_id],
    )
    return _rows_as_dicts(cur)


def _match_source_goods(
    source_goods: list[dict[str, Any]],
    tss_goods: dict[str, Any],
    fallback_index: int | None = None,
) -> dict[str, Any] | None:
    if not source_goods:
        return None
    tss_item_number = _as_int(_goods_item_number(tss_goods), default=None)
    if tss_item_number is not None:
        for item in source_goods:
            if _as_int(item.get("item_seq"), default=None) == tss_item_number:
                return item

    if fallback_index is not None and 0 <= fallback_index < len(source_goods):
        return source_goods[fallback_index]

    tss_commodity = _first_text(_item_value(tss_goods, "commodity_code", "commodityCode")).replace(" ", "")
    tss_description = _first_text(_item_value(tss_goods, "goods_description", "goodsDescription")).lower()
    for item in source_goods:
        if tss_commodity and tss_commodity == _first_text(item.get("commodity_code")).replace(" ", ""):
            return item
        if tss_description and tss_description == _first_text(item.get("goods_description")).lower():
            return item
    return source_goods[0]


def _load_product_defaults(cur: Any, master_schema: str, source_goods: dict[str, Any]) -> dict[str, Any]:
    source_goods = source_goods or {}
    try:
        columns = _table_columns(cur, master_schema, "DocProductCatalog")
    except Exception:
        return {}
    if not columns:
        return {}

    lookup_terms = []
    for column_name in ("sku", "product_code", "barcode"):
        actual = columns.get(column_name)
        value = _first_text(source_goods.get(column_name))
        if actual and value:
            lookup_terms.append((actual, value))
    if not lookup_terms:
        return {}

    lookup_where_parts = []
    params: list[Any] = []
    for actual, value in lookup_terms:
        lookup_where_parts.append(f"UPPER(LTRIM(RTRIM([{actual}]))) = UPPER(?)")
        params.append(value)
    where_parts = [f"({' OR '.join(lookup_where_parts)})"]
    if "active" in columns:
        where_parts.append(f"COALESCE([{columns['active']}], 1) = 1")

    order_sql = ""
    customer = _first_text(source_goods.get("ClientCode"))
    if customer and "customer_code" in columns:
        order_sql = (
            f" ORDER BY CASE WHEN UPPER([{columns['customer_code']}]) = UPPER(?) THEN 0 "
            f"WHEN UPPER([{columns['customer_code']}]) = 'ALL' THEN 1 ELSE 2 END, [{columns.get('id', 'id')}]"
        )
        params.append(customer)
    elif "id" in columns:
        order_sql = f" ORDER BY [{columns['id']}]"

    cur.execute(
        f"""
        SELECT TOP 1 *
        FROM {_qualified(master_schema, "DocProductCatalog")}
        WHERE {' AND '.join(where_parts)}
        {order_sql}
        """,
        params,
    )
    product = _row_as_dict(cur.fetchone(), cur.description)
    documents = _load_product_document_defaults(cur, master_schema, product, source_goods)
    if documents and not _first_text(product.get("document_references_json")):
        product["document_references_json"] = _json_dump(documents)
    return product


def _load_product_document_defaults(
    cur: Any,
    master_schema: str,
    product: dict[str, Any],
    source_goods: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return product-level SDI document references learned from masterdata/history."""

    product = product or {}
    source_goods = source_goods or {}
    try:
        columns = _table_columns(cur, master_schema, "DocProductCatalogDocuments")
    except Exception:
        return []
    if not columns or "document_code" not in columns:
        return []

    predicates: list[str] = []
    params: list[Any] = []

    product_id = _first_value(product.get("id"), product.get("product_catalog_id"))
    product_id_column = columns.get("product_catalog_id")
    if product_id and product_id_column:
        predicates.append(f"[{product_id_column}] = ?")
        params.append(product_id)

    customer = _first_text(source_goods.get("ClientCode"), product.get("customer_code"))
    for field_name in ("sku", "product_code"):
        actual = columns.get(field_name)
        value = _first_text(source_goods.get(field_name), product.get(field_name))
        if not actual or not value:
            continue
        condition = f"UPPER(LTRIM(RTRIM([{actual}]))) = UPPER(?)"
        condition_params: list[Any] = [value]
        customer_column = columns.get("customer_code")
        if customer and customer_column:
            condition = (
                f"({condition} AND (NULLIF(LTRIM(RTRIM([{customer_column}])), '') IS NULL "
                f"OR UPPER(LTRIM(RTRIM([{customer_column}]))) IN (UPPER(?), 'ALL')))"
            )
            condition_params.append(customer)
        predicates.append(condition)
        params.extend(condition_params)

    commodity_column = columns.get("commodity_code")
    country_column = columns.get("country_of_origin")
    commodity = _first_text(source_goods.get("commodity_code"), product.get("commodity_code")).replace(" ", "")
    origin = _first_text(source_goods.get("country_of_origin"), product.get("country_of_origin"))
    if commodity and commodity_column:
        condition = f"REPLACE(UPPER(LTRIM(RTRIM([{commodity_column}]))), ' ', '') = UPPER(?)"
        condition_params = [commodity]
        if origin and country_column:
            condition = (
                f"({condition} AND (NULLIF(LTRIM(RTRIM([{country_column}])), '') IS NULL "
                f"OR UPPER(LTRIM(RTRIM([{country_column}]))) = UPPER(?)))"
            )
            condition_params.append(origin)
        predicates.append(condition)
        params.extend(condition_params)

    if not predicates:
        return []

    where_parts = [f"({' OR '.join(predicates)})"]
    if "active" in columns:
        where_parts.append(f"COALESCE([{columns['active']}], 1) = 1")
    if "auto_apply_to_sdi" in columns:
        where_parts.append(f"COALESCE([{columns['auto_apply_to_sdi']}], 1) = 1")
    if "requires_compliance_review" in columns:
        where_parts.append(f"COALESCE([{columns['requires_compliance_review']}], 0) = 0")

    select_columns = [
        column_name
        for column_name in (
            "op_type",
            "document_code",
            "document_status",
            "document_reference",
            "document_part",
            "document_reason",
            "date_of_validity",
            "issuing_authority",
            "amount",
            "currency",
            "measurement_unit",
            "quantity",
        )
        if column_name in columns
    ]
    if "document_code" not in select_columns:
        select_columns.insert(0, "document_code")

    order_parts = []
    if "product_catalog_id" in columns:
        order_parts.append(f"CASE WHEN [{columns['product_catalog_id']}] IS NULL THEN 1 ELSE 0 END")
    if "evidence_count" in columns:
        order_parts.append(f"COALESCE([{columns['evidence_count']}], 1) DESC")
    if "id" in columns:
        order_parts.append(f"[{columns['id']}]")
    order_sql = f"ORDER BY {', '.join(order_parts)}" if order_parts else ""

    cur.execute(
        f"""
        SELECT {', '.join(f'[{columns[name]}]' for name in select_columns)}
        FROM {_qualified(master_schema, "DocProductCatalogDocuments")}
        WHERE {' AND '.join(where_parts)}
          AND UPPER(LTRIM(RTRIM([{columns['document_code']}])) ) <> 'N935'
          AND UPPER(LTRIM(RTRIM([{columns['document_code']}])) ) <> '1UKI'
        {order_sql}
        """,
        params,
    )
    rows = _rows_as_dicts(cur)
    documents: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        document_code = _first_text(row.get(columns.get("document_code", "document_code")), row.get("document_code"))
        if not document_code or document_code.upper() == "N935":
            continue
        document = {"op_type": _first_text(row.get(columns.get("op_type", "op_type")), "create"), "document_code": document_code}
        for source_key, target_key in (
            ("document_status", "document_status"),
            ("document_reference", "document_reference"),
            ("document_part", "document_part"),
            ("document_reason", "document_reason"),
            ("date_of_validity", "date_of_validity"),
            ("issuing_authority", "issuing_authority"),
            ("amount", "amount"),
            ("currency", "currency"),
            ("measurement_unit", "measurement_unit"),
            ("quantity", "quantity"),
        ):
            value = row.get(columns.get(source_key, source_key))
            if value not in (None, ""):
                document[target_key] = value
        key = tuple(str(document.get(name, "")).strip().upper() for name in sorted(document))
        if key in seen:
            continue
        seen.add(key)
        documents.append(document)
    return documents


def _load_trader_defaults(cur: Any, master_schema: str, importer_eori: str | None) -> dict[str, Any]:
    eori = _first_text(importer_eori)
    if not eori:
        return {}
    try:
        cur.execute(
            f"""
            SELECT TOP 1 *
            FROM {_qualified(master_schema, "SupDecTraderDefaults")}
            WHERE UPPER(LTRIM(RTRIM(importer_eori))) = UPPER(?)
            ORDER BY staging_id
            """,
            [eori],
        )
        return _row_as_dict(cur.fetchone(), cur.description)
    except Exception:
        return {}


def _upsert_tss_sdi_header(
    cur: Any,
    client_code: str,
    record: dict[str, Any],
    *,
    raw_payload: dict[str, Any],
) -> int:
    values = {
        "ClientCode": client_code,
        "SupDecNumber": record.get("sup_dec_number"),
        "SfdReference": record.get("sfd_reference"),
        "MovementReferenceNumber": record.get("tss_movement_reference_number"),
        "TssStatus": record.get("tss_status"),
        "ArrivalDateTime": _datetime_value(record.get("arrival_date_time")),
        "SubmissionDueDate": _date_value(record.get("tss_submission_due_date")),
        "RawJson": _json_dump(raw_payload),
        "LastSyncedAt": _sql_now(),
        "UpdatedAt": _sql_now(),
    }
    return _upsert_by_key(
        cur,
        "TSS",
        "BKD_SDI_Headers",
        values,
        key_columns=("ClientCode", "SupDecNumber"),
        identity_column="TssSdiHeaderId",
    )


def _upsert_stg_sdi_header(cur: Any, client_code: str, record: dict[str, Any]) -> int:
    values = {
        "ClientCode": client_code,
        "sub_status": "IMPORTED",
        "stg_consignment_id": record.get("stg_consignment_id"),
        "tss_consignment_ref": record.get("tss_consignment_ref"),
        "tss_sup_dec_number": record.get("sup_dec_number"),
        "tss_sfd_consignment_ref": record.get("sfd_reference"),
        "arrival_date_time": _datetime_value(record.get("arrival_date_time")),
        "tss_submission_due_date": _date_value(record.get("tss_submission_due_date")),
        "tss_status": record.get("tss_status"),
        "tss_movement_reference_number": record.get("tss_movement_reference_number"),
        "trader_reference": record.get("trader_reference"),
        "declaration_choice": record.get("declaration_choice"),
        "authorisation_type": record.get("authorisation_type"),
        "representation_type": record.get("representation_type"),
        "additional_procedure": record.get("additional_procedure"),
        "goods_domestic_status": record.get("goods_domestic_status"),
        "importer_eori": record.get("importer_eori"),
        "importer_name": record.get("importer_name"),
        "importer_street_number": record.get("importer_street_number"),
        "importer_city": record.get("importer_city"),
        "importer_postcode": record.get("importer_postcode"),
        "importer_country": record.get("importer_country"),
        "exporter_eori": record.get("exporter_eori"),
        "exporter_name": record.get("exporter_name"),
        "exporter_street_number": record.get("exporter_street_number"),
        "exporter_city": record.get("exporter_city"),
        "exporter_postcode": record.get("exporter_postcode"),
        "exporter_country": record.get("exporter_country"),
        "transport_document_number": record.get("transport_document_number"),
        "controlled_goods": record.get("controlled_goods"),
        "goods_description": record.get("goods_description"),
        "movement_type": record.get("movement_type"),
        "destination_country": record.get("destination_country"),
        "nationality_of_transport": record.get("nationality_of_transport"),
        "identity_no_of_transport": record.get("identity_no_of_transport"),
        "location_of_goods_border": record.get("location_of_goods_border"),
        "location_of_goods_other": record.get("location_of_goods_other"),
        "un_locode": record.get("un_locode"),
        "supervising_customs_office": record.get("supervising_customs_office"),
        "customs_warehouse_identifier": record.get("customs_warehouse_identifier"),
        "incoterm": record.get("incoterm"),
        "delivery_location_country": record.get("delivery_location_country"),
        "delivery_location_town": record.get("delivery_location_town"),
        "freight_charge": record.get("freight_charge"),
        "freight_charge_currency": record.get("freight_charge_currency"),
        "insurance": record.get("insurance"),
        "insurance_currency": record.get("insurance_currency"),
        "postponed_vat": record.get("postponed_vat"),
        "vat_adjustment": record.get("vat_adjustment"),
        "vat_adjust_currency": record.get("vat_adjust_currency"),
        "exchange_rate": record.get("exchange_rate"),
        "vat_number": record.get("vat_number"),
        "header_additions_deductions_json": record.get("header_additions_deductions_json"),
        "header_previous_document_json": record.get("header_previous_document_json"),
        "holder_of_authorisation_json": record.get("holder_of_authorisation_json"),
        "last_sub_status_change": _sql_now(),
        "updated_at": _sql_now(),
    }
    return _upsert_by_key(
        cur,
        "STG",
        "BKD_SDI_Headers",
        values,
        key_columns=("ClientCode", "tss_sup_dec_number"),
        identity_column="stg_sdi_id",
    )


def _upsert_sdi_goods(
    cur: Any,
    client_code: str,
    record: dict[str, Any],
    *,
    validation_errors: list[str],
) -> int:
    values = dict(record)
    values["ClientCode"] = client_code
    values["sdi_validation_errors_json"] = _json_dump(validation_errors) if validation_errors else None
    values["sdi_ready_at"] = _sql_now() if not validation_errors else None
    values["sdi_auto_submit_enabled"] = 1
    values["sub_status"] = "PENDING_REVIEW" if validation_errors else "VALIDATED"
    values["last_sub_status_change"] = _sql_now()
    values["updated_at"] = _sql_now()
    return _upsert_by_key(
        cur,
        "STG",
        "BKD_SDI_GoodsItems",
        values,
        key_columns=("ClientCode", "tss_goods_id"),
        identity_column="stg_sdi_item_id",
    )


def _upsert_tss_sdi_goods(
    cur: Any,
    client_code: str,
    item: dict[str, Any],
    *,
    sup_ref: str,
    sfd_ref: str,
) -> int | None:
    goods_id = _goods_id(item)
    if not goods_id:
        return None
    values = {
        "ClientCode": client_code,
        "GoodsId": goods_id,
        "SupDecNumber": sup_ref,
        "SfdReference": sfd_ref,
        "ItemNumber": _as_int(_goods_item_number(item), default=None),
        "TssStatus": _first_text(_item_value(item, "status", "state")),
        "RawJson": _json_dump(item),
        "LastSyncedAt": _sql_now(),
        "UpdatedAt": _sql_now(),
    }
    return _upsert_by_key(
        cur,
        "TSS",
        "BKD_SDI_GoodsItems",
        values,
        key_columns=("ClientCode", "GoodsId"),
        identity_column="TssSdiGoodsItemId",
    )


def _mark_sdi_header_state(cur: Any, stg_sdi_id: int, *, status: str, errors: list[str]) -> None:
    columns = _table_columns(cur, "STG", "BKD_SDI_Headers")
    values = {
        "sub_status": status,
        "validation_errors_json": _json_dump(errors) if errors else None,
        "auto_submit_error": "; ".join(errors)[:2000] if errors else None,
        "auto_submit_enabled": 1,
        "sdi_ready_at": _sql_now() if not errors else None,
        "last_autosubmit_attempt_at": _sql_now(),
        "submitted_at": _sql_now() if status == "SUBMITTED" else None,
        "completed_at": _sql_now() if status == "SUBMITTED" else None,
        "last_sub_status_change": _sql_now(),
        "updated_at": _sql_now(),
    }
    update_values = _filter_existing(values, columns)
    if not update_values:
        return
    set_sql, params = _set_clause(update_values)
    params.append(stg_sdi_id)
    cur.execute(f"UPDATE [STG].[BKD_SDI_Headers] SET {set_sql} WHERE stg_sdi_id = ?", params)


def _mark_tss_sdi_submitted(cur: Any, tss_sdi_header_id: int) -> None:
    _mark_tss_sdi_status(cur, tss_sdi_header_id, "SUBMITTED")


def _mark_tss_sdi_status(cur: Any, tss_sdi_header_id: int, status: str) -> None:
    if not tss_sdi_header_id:
        return
    cur.execute(
        """
        UPDATE [TSS].[BKD_SDI_Headers]
        SET TssStatus = ?,
            LastSyncedAt = SYSUTCDATETIME(),
            UpdatedAt = SYSUTCDATETIME()
        WHERE TssSdiHeaderId = ?
        """,
        [status or "UNKNOWN", tss_sdi_header_id],
    )


def _upsert_by_key(
    cur: Any,
    schema: str,
    table: str,
    values: dict[str, Any],
    *,
    key_columns: tuple[str, ...],
    identity_column: str,
) -> int:
    columns = _table_columns(cur, schema, table)
    filtered = _filter_existing(values, columns)
    if not filtered:
        return 0

    actual_key_columns = [columns.get(key.lower(), key) for key in key_columns]
    where_sql = " AND ".join(f"[{column}] = ?" for column in actual_key_columns)
    key_params = [filtered.get(column) for column in actual_key_columns]
    if any(value in (None, "") for value in key_params):
        raise ValueError(f"Cannot upsert {schema}.{table}: missing key {key_columns}")

    cur.execute(
        f"SELECT TOP 1 [{identity_column}] FROM [{schema}].[{table}] WHERE {where_sql}",
        key_params,
    )
    existing = cur.fetchone()
    if existing:
        update_values = {
            column: value
            for column, value in filtered.items()
            if column not in set(actual_key_columns) and column != identity_column
        }
        if update_values:
            set_sql, params = _set_clause(update_values)
            params.extend(key_params)
            cur.execute(f"UPDATE [{schema}].[{table}] SET {set_sql} WHERE {where_sql}", params)
        return int(existing[0])

    insert_values = {
        column: value
        for column, value in filtered.items()
        if column != identity_column
    }
    insert_columns = list(insert_values)
    placeholders = ", ".join("SYSUTCDATETIME()" if value is _SQL_NOW else "?" for value in insert_values.values())
    params = [value for value in insert_values.values() if value is not _SQL_NOW]
    cur.execute(
        f"INSERT INTO [{schema}].[{table}] ({', '.join(f'[{c}]' for c in insert_columns)}) "
        f"VALUES ({placeholders})",
        params,
    )
    cur.execute(
        f"SELECT TOP 1 [{identity_column}] FROM [{schema}].[{table}] WHERE {where_sql}",
        key_params,
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _filter_existing(values: dict[str, Any], columns: dict[str, str]) -> dict[str, Any]:
    filtered: dict[str, Any] = {}
    for key, value in values.items():
        actual = columns.get(str(key).lower())
        if actual:
            filtered[actual] = value
    return filtered


def _set_clause(values: dict[str, Any]) -> tuple[str, list[Any]]:
    parts = []
    params: list[Any] = []
    for column, value in values.items():
        if value is _SQL_NOW:
            parts.append(f"[{column}] = SYSUTCDATETIME()")
        else:
            parts.append(f"[{column}] = ?")
            params.append(value)
    return ", ".join(parts), params


def _table_columns(cur: Any, schema: str, table: str) -> dict[str, str]:
    cache_key = (str(schema or "").upper(), str(table or "").upper())
    cached = _TABLE_COLUMNS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [schema, table],
    )
    columns = {str(row[0]).lower(): str(row[0]) for row in cur.fetchall()}
    _TABLE_COLUMNS_CACHE[cache_key] = columns
    return columns


def _qualified(schema: str, table: str) -> str:
    schema = normalize_tenant_code(schema)
    safe_table = str(table).replace("]", "]]")
    return f"[{schema}].[{safe_table}]"


def _rows_as_dicts(cur: Any) -> list[dict[str, Any]]:
    columns = [column[0] for column in cur.description or []]
    return [_row_as_dict(row, columns) for row in cur.fetchall()]


def _row_as_dict(row: Any, description: Any) -> dict[str, Any]:
    if not row:
        return {}
    columns = [column[0] if isinstance(column, tuple) else column for column in (description or [])]
    return {column: row[idx] for idx, column in enumerate(columns)}


def _sdi_matches_candidate(item: dict[str, Any], candidate: dict[str, Any]) -> bool:
    sfd = _first_text(
        _item_value(item, "sfd_number", "sfd_reference", "tss_sfd_number", "parent", "u_parent")
    )
    candidate_sfd = _first_text(candidate.get("tss_sfd_number"), candidate.get("sfd_reference"))
    if sfd and candidate_sfd:
        return _clean_ref(sfd) == _clean_ref(candidate_sfd)

    trader = _first_text(_item_value(item, "trader_reference", "u_trader_reference"))
    candidate_trader = _first_text(candidate.get("trader_reference"))
    if trader and candidate_trader and _clean_ref(trader) == _clean_ref(candidate_trader):
        return True

    transport = _first_text(_item_value(item, "transport_document_number", "transportDocumentNumber"))
    candidate_transport = _first_text(candidate.get("transport_document_number"))
    return bool(transport and candidate_transport and _clean_ref(transport) == _clean_ref(candidate_transport))


def _sdi_reference(item: dict[str, Any]) -> str:
    return _first_text(
        _item_value(
            item or {},
            "sup_dec_number",
            "reference",
            "supplementary_declaration_number",
            "number",
        )
    )


def _goods_id(item: dict[str, Any]) -> str:
    return _first_text(
        _item_value(item or {}, "goods_id", "goodsId", "id", "sys_id", "u_goods_id", "reference")
    )


def _goods_item_number(item: dict[str, Any]) -> str:
    return _first_text(
        _item_value(
            item or {},
            "item_number",
            "itemNumber",
            "goods_item_number",
            "goodsItemNumber",
            "item_seq",
        )
    )


def _item_value(item: dict[str, Any], *names: str) -> Any:
    if not isinstance(item, dict):
        return None
    for name in names:
        if name in item and item[name] not in (None, ""):
            value = item[name]
            if isinstance(value, dict):
                for nested_key in ("value", "display_value", "displayValue", "label", "name"):
                    nested = value.get(nested_key)
                    if nested not in (None, ""):
                        return nested
                continue
            return value
    return None


def _first_text(*values: Any) -> str:
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _safe_goods_description(*values: Any) -> str:
    text = _first_text(*values)
    return tss_safe_text_suggestion(text) if text else ""


def _sdi_choice_value(value: Any) -> str:
    text = _first_text(value)
    if text.lower() in {"none", "-- none --", "- none -"}:
        return ""
    return text


def _normalise_sdi_movement_type(value: Any) -> str:
    text = _first_text(value)
    if text.lower() in {"1a", "3a"}:
        return "3"
    return text


def _first_value(*values: Any) -> Any:
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _first_nested_json(*values: Any) -> str | None:
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, str):
            text = value.strip()
            if text and text != "[]":
                return text
            continue
        if isinstance(value, (list, tuple)) and not value:
            continue
        return _json_dump(value)
    return None


def _product_weight_for_quantity(product_defaults: dict[str, Any], source_goods: dict[str, Any], kind: str) -> Decimal | None:
    product_defaults = product_defaults or {}
    source_goods = source_goods or {}
    weight_keys = (
        ("gross_weight_kg", "default_gross_weight_kg", "gross_mass_kg")
        if kind == "gross"
        else ("net_weight_kg", "default_net_weight_kg", "net_mass_kg")
    )
    unit_weight = _decimal_value(_first_value(*(product_defaults.get(key) for key in weight_keys)))
    if unit_weight is None or unit_weight <= 0:
        return None
    quantity = _source_goods_quantity(source_goods) or Decimal("1")
    return unit_weight * quantity


def _source_goods_quantity(source_goods: dict[str, Any]) -> Decimal | None:
    value = _first_value(
        source_goods.get("quantity_base"),
        source_goods.get("base_quantity"),
        source_goods.get("quantity"),
        source_goods.get("ordered_quantity"),
        source_goods.get("line_quantity"),
        source_goods.get("number_of_individual_pieces"),
        source_goods.get("number_of_packages"),
    )
    quantity = _decimal_value(value)
    if quantity is None or quantity <= 0:
        return None
    return quantity


def _decimal_value(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except Exception:
        return None


def _json_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return _json_dump(value)


def _clean_ref(value: Any) -> str:
    return str(value or "").strip().upper()


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _as_int(value: Any, default: int | None = 0) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except Exception:
        return default


def _api_success(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("success") is False:
            return False
        statuses = [
            str(result.get("status") or "").strip().lower(),
            str(_api_response_payload(result).get("status") or "").strip().lower(),
        ]
        if any(status in {"error", "failure", "failed"} for status in statuses):
            return False
        http_status = result.get("http_status")
        if http_status and int(http_status) >= 400:
            return False
    return True


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _unwrap_tss_result(value: Any) -> dict[str, Any]:
    payload = _json_dict(value)
    while isinstance(payload.get("result"), dict):
        payload = payload["result"]
    return payload


def _api_response_payload(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    for key in ("response", "data"):
        payload = _unwrap_tss_result(result.get(key))
        if payload:
            return payload
    return _unwrap_tss_result(result.get("raw_response"))


def _api_message(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result or "unknown TSS response")
    response = _api_response_payload(result)
    message_payload = _unwrap_tss_result(result.get("message"))
    for source in (response, message_payload, result):
        for key in ("message", "error_message", "process_message"):
            value = source.get(key) if isinstance(source, dict) else None
            if value:
                return str(value)
    raw_payload = _unwrap_tss_result(result.get("raw_response"))
    for key in ("message", "error_message", "process_message"):
        value = raw_payload.get(key)
        if value:
            return str(value)
    value = result.get("raw_response")
    if value:
        return str(value)
    return "unknown TSS response"


def _json_dump(value: Any) -> str:
    if value in (None, ""):
        return ""
    return json.dumps(value, default=_json_default, ensure_ascii=True)


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _datetime_value(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("T", " ").replace("Z", "")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(normalized[:19], fmt)
        except ValueError:
            continue
    return None


def _date_value(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


class _SqlNow:
    pass


_SQL_NOW = _SqlNow()


def _sql_now() -> _SqlNow:
    return _SQL_NOW


__all__ = [
    "SdiAutosubmitResult",
    "build_staged_sdi_goods_record",
    "run_sdi_autosubmit",
    "should_call_tss_submit",
]
