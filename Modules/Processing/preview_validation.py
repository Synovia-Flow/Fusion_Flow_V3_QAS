from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


CONSIGNMENT_REQUIRED_FIELDS = (
    "declaration_number",
    "consignment_number",
    "goods_description",
    "transport_document_number",
    "controlled_goods",
)

PARTY_PREFIXES = ("consignor", "consignee", "importer", "exporter")
PARTY_ADDRESS_SUFFIXES = ("name", "street_number", "city", "postcode", "country")
TSS_TWO_DECIMAL_FIELDS = {"gross_mass_kg", "net_mass_kg", "item_invoice_amount"}

GOODS_REQUIRED_FIELDS = (
    "type_of_packages",
    "number_of_packages",
    "package_marks",
    "gross_mass_kg",
    "goods_description",
)
GOODS_ATTENTION_FIELDS = GOODS_REQUIRED_FIELDS + ("net_mass_kg", "commodity_code")

FIELD_LABELS = {
    "declaration_number": "Declaration number",
    "consignment_number": "Consignment number",
    "goods_description": "Description",
    "trader_reference": "Trader reference",
    "transport_document_number": "Transport document",
    "controlled_goods": "Controlled goods",
    "goods_domestic_status": "Goods domestic status",
    "destination_country": "Destination country",
    "consignor_eori": "Consignor EORI",
    "consignor_name": "Consignor name",
    "consignor_street_number": "Consignor street and number",
    "consignor_city": "Consignor city",
    "consignor_postcode": "Consignor postcode",
    "consignor_country": "Consignor country",
    "consignee_eori": "Consignee EORI",
    "consignee_name": "Consignee name",
    "consignee_street_number": "Consignee street and number",
    "consignee_city": "Consignee city",
    "consignee_postcode": "Consignee postcode",
    "consignee_country": "Consignee country",
    "importer_eori": "Importer EORI",
    "importer_name": "Importer name",
    "importer_street_number": "Importer street and number",
    "importer_city": "Importer city",
    "importer_postcode": "Importer postcode",
    "importer_country": "Importer country",
    "exporter_eori": "Exporter EORI",
    "exporter_name": "Exporter name",
    "exporter_street_number": "Exporter street and number",
    "exporter_city": "Exporter city",
    "exporter_postcode": "Exporter postcode",
    "exporter_country": "Exporter country",
    "container_indicator": "Container indicator",
    "goods_id": "Goods ID",
    "commodity_code": "Commodity code",
    "type_of_packages": "Package type",
    "number_of_packages": "Packages",
    "package_marks": "Package marks",
    "gross_mass_kg": "Gross mass kg",
    "net_mass_kg": "Net mass kg",
    "country_of_origin": "Origin country",
    "item_invoice_amount": "Invoice amount",
    "item_invoice_currency": "Currency",
    "invoice_number": "Invoice number",
    "controlled_goods_type": "Controlled goods type",
    "procedure_code": "Procedure code",
    "additional_procedure_code": "Additional procedure",
    "preference": "Preference",
    "nature_of_transaction": "Nature of transaction",
}


def compact(value: Any) -> Any:
    if isinstance(value, str):
        clean = value.strip()
        return clean or None
    return value


def tss_decimal_2(value: Any) -> str | None:
    clean = compact(value)
    if clean is None:
        return None
    text = str(clean).strip().replace(",", "")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return str(clean)
    return format(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def tss_payload_value(field: str, value: Any) -> Any:
    clean = compact(value)
    if clean is None:
        return None
    if field in TSS_TWO_DECIMAL_FIELDS:
        return tss_decimal_2(clean)
    return clean


def party_address_fields(prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}_{suffix}" for suffix in PARTY_ADDRESS_SUFFIXES)


def has_full_party_address(payload: dict[str, Any], prefix: str) -> bool:
    return all(compact(payload.get(field)) is not None for field in party_address_fields(prefix))


def required_consignment_fields_for_payload(payload: dict[str, Any]) -> set[str]:
    required = set(CONSIGNMENT_REQUIRED_FIELDS)
    for prefix in PARTY_PREFIXES:
        eori_field = f"{prefix}_eori"
        if compact(payload.get(eori_field)) is not None:
            continue
        if has_full_party_address(payload, prefix):
            required.update(party_address_fields(prefix))
            continue
        required.add(eori_field)
        required.update(field for field in party_address_fields(prefix) if compact(payload.get(field)) is None)
    return required


def numeric(value: Any) -> tuple[bool, float | None]:
    text = str(compact(value) or "").replace(",", "")
    if not text:
        return False, None
    try:
        return True, float(text)
    except ValueError:
        return False, None


def field_issue(scope: str, field: str, value: Any, required: bool) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if compact(value) is None:
        if required:
            issues.append({"severity": "error", "message": f"{FIELD_LABELS.get(field, field)} is required before TSS processing."})
        elif field == "net_mass_kg":
            issues.append({"severity": "warning", "message": "Net mass is blank; confirm fallback before live processing."})
        return issues
    if field in TSS_TWO_DECIMAL_FIELDS:
        ok, number = numeric(value)
        if not ok:
            issues.append({"severity": "error", "message": f"{FIELD_LABELS.get(field, field)} must be numeric."})
        elif field == "gross_mass_kg" and (number or 0) <= 0:
            issues.append({"severity": "error", "message": "Gross mass must be greater than zero."})
        elif field == "net_mass_kg" and (number or 0) < 0:
            issues.append({"severity": "error", "message": "Net mass cannot be negative."})
    if scope == "goods" and field == "number_of_packages":
        ok, number = numeric(value)
        if not ok:
            issues.append({"severity": "error", "message": "Number of packages must be numeric."})
        elif (number or 0) <= 0:
            issues.append({"severity": "error", "message": "Number of packages must be greater than zero."})
    return issues


def fields_payload(
    *,
    scope: str,
    values: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    display_fields: tuple[str, ...],
    required_fields: tuple[str, ...] | set[str],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    fields: list[dict[str, Any]] = []
    missing_required: list[str] = []
    issues: list[dict[str, Any]] = []
    required_set = set(required_fields)
    for field in display_fields:
        value = values.get(field)
        required = field in required_set
        field_issues = field_issue(scope, field, value, required)
        if required and compact(value) is None:
            missing_required.append(field)
        if scope == "consignment" and field.endswith("_eori") and compact(value) is None:
            party_prefix = field[:-5]
            if has_full_party_address(values, party_prefix):
                field_issues.append({
                    "severity": "info",
                    "message": f"{FIELD_LABELS.get(field, field)} is blank; TSS can use the full {party_prefix} address supplied in this preview.",
                })
        source = sources.get(field)
        if (source or {}).get("assumption") or (source or {}).get("source") == "assumption":
            field_issues.append({
                "severity": "warning",
                "message": (source or {}).get("reason") or f"{FIELD_LABELS.get(field, field)} was assumed for preview.",
            })
        for issue in field_issues:
            issues.append({"field": field, "label": FIELD_LABELS.get(field, field), **issue})
        is_blank = compact(value) is None
        fields.append({
            "field": field,
            "label": FIELD_LABELS.get(field, field),
            "value": compact(value),
            "required": required,
            "missing": required and is_blank,
            "blank": is_blank,
            "source": source,
            "issues": field_issues,
        })
    return fields, missing_required, issues


def is_enriched_source(source: dict[str, Any] | None) -> bool:
    if not isinstance(source, dict):
        return False
    raw_source = str(source.get("source") or source.get("label") or "").upper()
    return (
        bool(source.get("assumption"))
        or bool(source.get("normalised"))
        or raw_source.startswith("ASSUMPTION:")
        or "CFG.PRODUCT_MASTER" in raw_source
        or "MASTERDATA" in raw_source
    )


def visible_enrichment_count(consignment_fields: list[dict[str, Any]], goods_items: list[dict[str, Any]]) -> int:
    count = sum(1 for field in consignment_fields if is_enriched_source(field.get("source")))
    for goods in goods_items:
        count += sum(1 for field in goods.get("fields") or [] if is_enriched_source(field.get("source")))
    return count


def empty_lineage_summary() -> dict[str, Any]:
    return {
        "counts": {"masterdata": 0, "assumption": 0, "normalised": 0, "edited": 0},
        "examples": [],
        "total": 0,
    }


def lineage_summary(consignment_fields: list[dict[str, Any]], goods_items: list[dict[str, Any]]) -> dict[str, Any]:
    summary = empty_lineage_summary()

    def register(field: dict[str, Any], scope: str) -> None:
        source = field.get("source") if isinstance(field, dict) else None
        if not isinstance(source, dict):
            return
        raw_source = str(source.get("source") or source.get("label") or "")
        raw_upper = raw_source.upper()
        kinds: list[str] = []
        if "CFG.PRODUCT_MASTER" in raw_upper or "MASTERDATA" in raw_upper:
            kinds.append("masterdata")
        if source.get("assumption") or raw_upper.startswith("ASSUMPTION:") or raw_source == "assumption":
            kinds.append("assumption")
        if source.get("normalised"):
            kinds.append("normalised")
        if raw_source == "manualEdit":
            kinds.append("edited")
        if not kinds:
            return
        for kind in dict.fromkeys(kinds):
            summary["counts"][kind] += 1
        summary["total"] += 1
        if len(summary["examples"]) < 8:
            summary["examples"].append({
                "scope": scope,
                "field": field.get("field"),
                "label": field.get("label") or field.get("field"),
                "source": raw_source,
                "kinds": list(dict.fromkeys(kinds)),
                "reason": source.get("reason"),
                "originalValue": source.get("originalValue"),
            })

    for field in consignment_fields:
        register(field, "consignment")
    for goods in goods_items:
        for field in goods.get("fields") or []:
            register(field, "goods")
    return summary


def merge_lineage_summary(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in (incoming.get("counts") or {}).items():
        if key in target["counts"]:
            target["counts"][key] += int(value or 0)
    target["total"] += int(incoming.get("total") or 0)
    remaining = max(8 - len(target["examples"]), 0)
    if remaining:
        target["examples"].extend((incoming.get("examples") or [])[:remaining])


def edited_values_from_fields(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    values = dict(record.get("values") or {})
    sources: dict[str, dict[str, Any]] = {}
    for field in record.get("fields") or []:
        if not isinstance(field, dict):
            continue
        field_name = field.get("field")
        if not field_name:
            continue
        values[str(field_name)] = compact(field.get("value"))
        source = field.get("source")
        if isinstance(source, dict):
            sources[str(field_name)] = dict(source)
    return values, sources