from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


PACKAGE_TYPE_ALIASES = {
    "palle": "pallets",
    "pallet": "pallets",
    "pallets": "pallets",
    "plt": "pallets",
    "plts": "pallets",
    "box": "PK",
    "boxes": "PK",
    "bx": "PK",
    "carton": "PK",
    "cartons": "PK",
    "ctn": "PK",
    "package": "PK",
    "packages": "PK",
    "pack": "PK",
    "pk": "PK",
    "bag": "BG",
    "bags": "BG",
    "bg": "BG",
    "sack": "BG",
    "sacks": "BG",
    "each": "PK",
    "ea": "PK",
    "unit": "PK",
    "units": "PK",
    "set": "PK",
    "sets": "PK",
    "piece": "PK",
    "pieces": "PK",
    "pcs": "PK",
    "pair": "PK",
    "pairs": "PK",
    "pr": "PK",
    "m": "PK",
    "mtr": "PK",
    "mtrs": "PK",
    "metre": "PK",
    "metres": "PK",
    "meter": "PK",
    "meters": "PK",
    "tub": "TB",
    "tubs": "TB",
    "tb": "TB",
    "tube": "TU",
    "tubes": "TU",
    "tu": "TU",
}


TSS_TWO_DECIMAL_FIELDS = {"gross_mass_kg", "net_mass_kg", "item_invoice_amount"}


PACKAGE_TYPE_RESOLUTION_ORDER = [
    "source PRS.Goods_Item.type_of_packages",
    "CFG.Product_Master.PackageType",
    "source UOM/package wording normalised through Modules.Processing",
    "ASSUMPTION:DEFAULT_PACKAGE_TYPE",
]

PRODUCT_FIELD_MAP = (
    ("GoodsDescription", "goods_description"),
    ("ProductName", "goods_description"),
    ("CommodityCode", "commodity_code"),
    ("CountryOfOrigin", "country_of_origin"),
    ("PackageType", "type_of_packages"),
    ("PackageMarks", "package_marks"),
    ("ProcedureCode", "procedure_code"),
    ("AdditionalProcedureCode", "additional_procedure_code"),
    ("ValuationMethod", "valuation_method"),
    ("PreferenceCode", "preference"),
    ("NiAdditionalInfoCode", "ni_additional_information_codes"),
    ("NatureOfTransaction", "nature_of_transaction"),
    ("GrossWeightKg", "gross_mass_kg"),
    ("NetWeightKg", "net_mass_kg"),
    ("UnitValue", "item_invoice_amount"),
    ("Currency", "item_invoice_currency"),
    ("ControlledGoods", "controlled_goods"),
)


def compact(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalise_sku(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).upper()


def normalise_package_type(value: Any, default: str | None = None) -> str | None:
    text = compact(value)
    if text is None:
        return default
    key = text.lower().replace("_", " ").strip()
    if " - " in key:
        key = key.split(" - ", 1)[0].strip()
    key = re.sub(r"[^\w\s]+$", "", key).strip()
    key_prefix = key.split(None, 1)[0] if key else key
    key_prefix = re.sub(r"[^\w]+$", "", key_prefix)
    if key.startswith(("\u00a3", "$", "\u20ac", "eur", "gbp")):
        return default or "PK"
    return PACKAGE_TYPE_ALIASES.get(key) or PACKAGE_TYPE_ALIASES.get(key_prefix) or text


def decimal_text(value: Any, places: str = "0.01") -> str | None:
    text = compact(value)
    if text is None:
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return text
    return format(number.quantize(Decimal(places)), "f")


def normalise_tss_decimal_fields(
    values: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    *,
    fields: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Format TSS decimal fields to two decimals and keep lineage visible."""
    enhancements: list[dict[str, Any]] = []
    for field in sorted(fields or TSS_TWO_DECIMAL_FIELDS):
        original_value = compact(values.get(field))
        if original_value is None:
            continue
        normalised_value = decimal_text(original_value)
        if normalised_value is None or normalised_value == original_value:
            continue
        values[field] = normalised_value
        existing = dict(sources.get(field) or {})
        sources[field] = {
            **existing,
            "source": existing.get("source") or "processingNormalisation",
            "label": existing.get("label") or "NORMALISED",
            "normalised": True,
            "originalValue": original_value,
            "normalisedValue": normalised_value,
            "reason": f"{field} normalised from {original_value} to {normalised_value} for TSS two-decimal API format.",
        }
        enhancements.append({"field": field, "source": "processingNormalisation"})
    return enhancements


def product_lookup_key(values: dict[str, Any]) -> str:
    for field in ("_source_sku", "sku", "product_code", "stock_code", "item_code", "label", "goods_id", "package_marks"):
        key = normalise_sku(values.get(field))
        if key:
            return key
    return ""


def product_master_lookup(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in rows:
        customer_code = normalise_sku(row.get("CustomerCode")) or "ALL"
        is_generic = customer_code == "ALL"
        for field in ("SKU", "ProductCode", "ProductName"):
            key = normalise_sku(row.get(field))
            if not key:
                continue
            customer_lookup = f"{customer_code}::{key}"
            if customer_lookup not in lookup:
                lookup[customer_lookup] = row
            if is_generic and key not in lookup:
                lookup[key] = row
    return lookup

def customer_lookup_key(values: dict[str, Any] | None) -> str:
    values = values or {}
    for field in (
        "_source_customer_code",
        "customer_code",
        "CustomerCode",
        "sell_to_customer_no",
        "sell_to_customer_number",
        "sell_to_customer",
        "customer_no",
        "customer_number",
    ):
        key = normalise_sku(values.get(field))
        if key:
            return key
    return ""


def product_master_get(
    lookup: dict[str, dict[str, Any]],
    goods_values: dict[str, Any],
    consignment_values: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    product_key = product_lookup_key(goods_values)
    if not product_key:
        return None
    customer_key = customer_lookup_key(goods_values) or customer_lookup_key(consignment_values)
    for lookup_key in (
        f"{customer_key}::{product_key}" if customer_key else "",
        f"ALL::{product_key}",
        product_key,
    ):
        if lookup_key and lookup_key in lookup:
            return lookup[lookup_key]
    return None


def product_master_lookup_key_count(rows: list[dict[str, Any]]) -> int:
    keys: set[str] = set()
    for row in rows:
        for field in ("SKU", "ProductCode", "ProductName"):
            key = normalise_sku(row.get(field))
            if key:
                keys.add(key)
    return len(keys)


def product_master_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    row_count = len(rows)
    lookup_key_count = product_master_lookup_key_count(rows)
    package_counts: dict[str, dict[str, Any]] = {}
    package_type_row_count = 0

    for row in rows:
        raw_package_type = compact(row.get("PackageType"))
        if raw_package_type is None:
            continue
        package_type_row_count += 1
        normalised = normalise_package_type(raw_package_type) or raw_package_type
        bucket = package_counts.setdefault(
            normalised,
            {
                "value": normalised,
                "rowCount": 0,
                "examples": [],
            },
        )
        bucket["rowCount"] += 1
        if raw_package_type != normalised and raw_package_type not in bucket["examples"]:
            bucket["examples"].append(raw_package_type)

    package_values = sorted(
        package_counts.values(),
        key=lambda item: (-int(item["rowCount"]), str(item["value"])),
    )
    summary: dict[str, Any] = {
        "rowCount": row_count,
        "lookupKeyCount": lookup_key_count,
        "packageTypeRowCount": package_type_row_count,
        "packageTypeMissingCount": max(row_count - package_type_row_count, 0),
        "packageTypeValues": package_values[:12],
    }
    if row_count and package_type_row_count == 0:
        summary["packageTypeWarning"] = (
            "CFG.Product_Master rows are loaded but PackageType is blank for all matched rows; "
            "package type will be normalised from source/UOM or defaulted as an assumption."
        )
    elif row_count and package_type_row_count < row_count:
        summary["packageTypeWarning"] = (
            "Some CFG.Product_Master rows do not have PackageType; affected goods will use "
            "source/UOM mapping or the visible default-package assumption."
        )
    return summary


def _source(source: str, reason: str, *, assumption: bool = False, **extra: Any) -> dict[str, Any]:
    payload = {
        "source": source,
        "label": "ASSUMPTION" if assumption else source,
        "assumption": assumption,
        "reason": reason,
        **extra,
    }
    return payload


def _is_assumption_source(source: dict[str, Any] | None) -> bool:
    if not isinstance(source, dict):
        return False
    return bool(source.get("assumption")) or str(source.get("source") or "").upper().startswith("ASSUMPTION:")


def _set_if_blank(
    values: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    field: str,
    value: Any,
    source: dict[str, Any],
    *,
    replace_assumption: bool = False,
) -> bool:
    if compact(value) is None:
        return False
    existing = compact(values.get(field))
    if existing is not None and not (replace_assumption and _is_assumption_source(sources.get(field))):
        return False
    values[field] = str(value).strip()
    sources[field] = source
    return True


def enrich_goods_preview(
    values: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    product: dict[str, Any] | None,
    *,
    default_package_type: str = "PK",
    replace_assumptions: bool = False,
) -> list[dict[str, Any]]:
    enhancements: list[dict[str, Any]] = []
    sku = product_lookup_key(values)

    if product:
        for product_field, target_field in PRODUCT_FIELD_MAP:
            product_value = product.get(product_field)
            source_extra: dict[str, Any] = {}
            if target_field == "goods_description" and compact(values.get(target_field)) is not None:
                continue
            if product_field == "ProductName" and compact(product.get("GoodsDescription")) is not None:
                continue
            if target_field == "type_of_packages":
                original_package_type = compact(product_value)
                product_value = normalise_package_type(product_value)
                if original_package_type and product_value and str(product_value) != str(original_package_type):
                    source_extra.update({
                        "normalised": True,
                        "originalValue": original_package_type,
                        "normalisedValue": product_value,
                    })
            elif target_field in TSS_TWO_DECIMAL_FIELDS:
                original_decimal_value = compact(product_value)
                product_value = decimal_text(product_value)
                if original_decimal_value and product_value and str(product_value) != str(original_decimal_value):
                    source_extra.update({
                        "normalised": True,
                        "originalValue": original_decimal_value,
                        "normalisedValue": product_value,
                    })
            reason = f"{target_field} enriched from CFG.Product_Master using SKU {sku or product.get('SKU') or product.get('ProductCode')}."
            if source_extra.get("normalised") and target_field == "type_of_packages":
                reason = f"Package type normalised from {source_extra['originalValue']} to {product_value} using BKD/TSS package mapping after CFG.Product_Master enrichment."
            elif source_extra.get("normalised") and target_field in TSS_TWO_DECIMAL_FIELDS:
                reason = f"{target_field} normalised from {source_extra['originalValue']} to {product_value} for TSS two-decimal API format after CFG.Product_Master enrichment."
            if _set_if_blank(
                values,
                sources,
                target_field,
                product_value,
                _source(
                    "CFG.Product_Master",
                    reason,
                    productField=product_field,
                    productSku=product.get("SKU"),
                    productCode=product.get("ProductCode"),
                    **source_extra,
                ),
                replace_assumption=replace_assumptions,
            ):
                enhancements.append({"field": target_field, "source": "CFG.Product_Master", "productField": product_field})
                if source_extra.get("normalised"):
                    enhancements.append({"field": target_field, "source": "processingNormalisation"})

    enhancements.extend(normalise_tss_decimal_fields(values, sources))

    package_before = compact(values.get("type_of_packages"))
    normalised_package = normalise_package_type(package_before)
    if package_before and normalised_package and normalised_package != package_before:
        values["type_of_packages"] = normalised_package
        existing = dict(sources.get("type_of_packages") or {})
        sources["type_of_packages"] = {
            **existing,
            "source": existing.get("source") or "processingNormalisation",
            "label": existing.get("label") or "NORMALISED",
            "normalised": True,
            "originalValue": package_before,
            "reason": f"Package type normalised from {package_before} to {normalised_package} using BKD/TSS package mapping.",
        }
        enhancements.append({"field": "type_of_packages", "source": "processingNormalisation"})

    if compact(values.get("type_of_packages")) is None and default_package_type:
        values["type_of_packages"] = default_package_type
        sources["type_of_packages"] = _source(
            "ASSUMPTION:DEFAULT_PACKAGE_TYPE",
            "No source package type or CFG.Product_Master.PackageType was available; assumed for preview and defaulted to PK.",
            assumption=True,
        )
        enhancements.append({"field": "type_of_packages", "source": "ASSUMPTION:DEFAULT_PACKAGE_TYPE"})

    return enhancements
