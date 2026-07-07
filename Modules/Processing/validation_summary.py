from __future__ import annotations

from typing import Any

from .preview_enrichment import decimal_text, normalise_package_type


CONSIGNMENT_VALIDATION_REQUIRED_FIELDS = (
    ("goods_description", "Goods Description"),
    ("transport_document_number", "Transport Document"),
    ("destination_country", "Destination Country"),
)

GOODS_VALIDATION_REQUIRED_FIELDS = (
    ("goods_description", "Goods Description"),
    ("commodity_code", "Commodity Code"),
    ("type_of_packages", "Package Type"),
    ("number_of_packages", "Packages"),
    ("package_marks", "Package Marks"),
    ("gross_mass_kg", "Gross Mass"),
)


def dict_value(row: dict[str, Any] | None, *names: str) -> Any | None:
    if not row:
        return None
    for name in names:
        if name in row:
            return row.get(name)
    folded = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = folded.get(name.lower())
        if value is not None:
            return value
    return None


def text_value(value: Any | None) -> str:
    return str(value or "").strip()


def is_missing(value: Any | None) -> bool:
    return text_value(value) == ""


def build_consignment_validation_summary(
    row: dict[str, Any],
    goods: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a read-only PRS validation/enrichment summary for portal detail views."""
    missing_required: list[dict[str, Any]] = []
    for field, label in CONSIGNMENT_VALIDATION_REQUIRED_FIELDS:
        if is_missing(dict_value(row, field, field.title().replace("_", ""))):
            missing_required.append({"scope": "consignment", "field": field, "label": label, "count": 1})

    goods_missing: dict[str, dict[str, Any]] = {}
    package_buckets: dict[str, dict[str, Any]] = {}
    package_missing_count = 0
    package_normalised_count = 0
    decimal_counts = {"gross_mass_kg": 0, "net_mass_kg": 0, "item_invoice_amount": 0}

    for goods_row in goods:
        for field, label in GOODS_VALIDATION_REQUIRED_FIELDS:
            if is_missing(dict_value(goods_row, field, field.title().replace("_", ""))):
                item = goods_missing.setdefault(field, {"scope": "goods", "field": field, "label": label, "count": 0})
                item["count"] = int(item["count"]) + 1

        raw_package = text_value(dict_value(goods_row, "type_of_packages", "TypeOfPackages"))
        if not raw_package:
            package_missing_count += 1
        else:
            normalised_package = normalise_package_type(raw_package) or raw_package
            if normalised_package != raw_package:
                package_normalised_count += 1
            bucket = package_buckets.setdefault(normalised_package, {"value": normalised_package, "rowCount": 0, "examples": []})
            bucket["rowCount"] = int(bucket["rowCount"]) + 1
            if raw_package != normalised_package and raw_package not in bucket["examples"]:
                bucket["examples"].append(raw_package)

        for field in decimal_counts:
            raw_decimal = text_value(dict_value(goods_row, field, field.title().replace("_", "")))
            normalised_decimal = decimal_text(raw_decimal)
            if raw_decimal and normalised_decimal and normalised_decimal != raw_decimal:
                decimal_counts[field] += 1

    missing_required.extend(goods_missing.values())

    assumptions: list[dict[str, Any]] = []
    if package_missing_count:
        assumptions.append({
            "field": "type_of_packages",
            "rule": "ASSUMPTION:DEFAULT_PACKAGE_TYPE",
            "value": "PK",
            "count": package_missing_count,
            "reason": "No PRS package type is present for these goods rows; Modules/Processing would default to PK before TSS payload creation.",
        })

    enhancements: list[dict[str, Any]] = []
    if package_normalised_count:
        enhancements.append({
            "field": "type_of_packages",
            "source": "Modules.Processing.preview_enrichment.normalise_package_type",
            "count": package_normalised_count,
            "reason": "Raw package/UOM values should be normalised to TSS package choices before submission.",
        })
    for field, count in decimal_counts.items():
        if count:
            enhancements.append({
                "field": field,
                "source": "Modules.Processing.preview_enrichment.decimal_text",
                "count": count,
                "reason": "TSS API payloads require two-decimal numeric strings.",
            })

    package_values = sorted(package_buckets.values(), key=lambda item: (-int(item["rowCount"]), str(item["value"])))
    return {
        "source": "PRS.Consignment + PRS.Goods_Item",
        "engine": "Modules.Processing.validation_summary",
        "databaseWrite": False,
        "tssWrite": False,
        "status": "READY" if not missing_required else "NEEDS_REVIEW",
        "goodsItemCount": len(goods),
        "missingRequired": missing_required,
        "assumptions": assumptions,
        "enhancements": enhancements,
        "packageType": {
            "missingCount": package_missing_count,
            "normalisedCount": package_normalised_count,
            "values": package_values,
        },
        "decimalNormalisation": [
            {"field": field, "count": count}
            for field, count in decimal_counts.items()
            if count
        ],
        "notes": [
            "Read-only portal diagnostic; it does not write to DB or TSS.",
            "Submission still follows PRS -> STG/API/TSS gates from Modules/Submission.",
        ],
    }
