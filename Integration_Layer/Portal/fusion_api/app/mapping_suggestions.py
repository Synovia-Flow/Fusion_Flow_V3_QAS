from __future__ import annotations

import re
from typing import Any

from .file_introspection import clean_cell
from .tss_submission import CONSIGNMENT_REQUIRED_FIELDS

TARGET_FIELDS: dict[str, set[str]] = {
    "PRS.Consignment": {
        "declaration_number",
        "consignment_number",
        "goods_description",
        "trader_reference",
        "transport_document_number",
        "controlled_goods",
        "goods_domestic_status",
        "destination_country",
        "consignor_eori",
        "consignor_name",
        "consignor_street_number",
        "consignor_city",
        "consignor_postcode",
        "consignor_country",
        "consignee_eori",
        "consignee_name",
        "consignee_street_number",
        "consignee_city",
        "consignee_postcode",
        "consignee_country",
        "importer_eori",
        "importer_name",
        "importer_street_number",
        "importer_city",
        "importer_postcode",
        "importer_country",
        "exporter_eori",
        "exporter_name",
        "exporter_street_number",
        "exporter_city",
        "exporter_postcode",
        "exporter_country",
        "container_indicator",
    },
    "PRS.Goods_Item": {
        "consignment_number",
        "goods_id",
        "type_of_packages",
        "number_of_packages",
        "number_of_individual_pieces",
        "package_marks",
        "gross_mass_kg",
        "net_mass_kg",
        "goods_description",
        "controlled_goods",
        "controlled_goods_type",
        "commodity_code",
        "preference",
        "country_of_origin",
        "item_invoice_amount",
        "item_invoice_currency",
        "procedure_code",
        "additional_procedure_code",
        "invoice_number",
        "nature_of_transaction",
    },
}

ALIASES: dict[str, tuple[str, str]] = {
    "ens": ("PRS.Consignment", "declaration_number"),
    "ensnumber": ("PRS.Consignment", "declaration_number"),
    "ensreference": ("PRS.Consignment", "declaration_number"),
    "declarationnumber": ("PRS.Consignment", "declaration_number"),
    "declarationref": ("PRS.Consignment", "declaration_number"),
    "consignment": ("PRS.Consignment", "consignment_number"),
    "consignmentnumber": ("PRS.Consignment", "consignment_number"),
    "consignmentreference": ("PRS.Consignment", "consignment_number"),
    "consignmentref": ("PRS.Consignment", "consignment_number"),
    "consignmentdescription": ("PRS.Consignment", "goods_description"),
    "dec": ("PRS.Consignment", "consignment_number"),
    "decnumber": ("PRS.Consignment", "consignment_number"),
    "traderreference": ("PRS.Consignment", "trader_reference"),
    "traderref": ("PRS.Consignment", "trader_reference"),
    "transportdocumentnumber": ("PRS.Consignment", "transport_document_number"),
    "transportdocument": ("PRS.Consignment", "transport_document_number"),
    "tdn": ("PRS.Consignment", "transport_document_number"),
    "goodsdescription": ("PRS.Goods_Item", "goods_description"),
    "descriptionofgoods": ("PRS.Goods_Item", "goods_description"),
    "description": ("PRS.Goods_Item", "goods_description"),
    "controlledgoods": ("PRS.Consignment", "controlled_goods"),
    "controlled": ("PRS.Consignment", "controlled_goods"),
    "consignoreori": ("PRS.Consignment", "consignor_eori"),
    "consigneeeori": ("PRS.Consignment", "consignee_eori"),
    "importereori": ("PRS.Consignment", "importer_eori"),
    "exportereori": ("PRS.Consignment", "exporter_eori"),
    "consignorname": ("PRS.Consignment", "consignor_name"),
    "consignorstreetnumber": ("PRS.Consignment", "consignor_street_number"),
    "consignorstreetandnumber": ("PRS.Consignment", "consignor_street_number"),
    "consignoraddress": ("PRS.Consignment", "consignor_street_number"),
    "consignorcity": ("PRS.Consignment", "consignor_city"),
    "consignorpostcode": ("PRS.Consignment", "consignor_postcode"),
    "consigneename": ("PRS.Consignment", "consignee_name"),
    "consigneestreetnumber": ("PRS.Consignment", "consignee_street_number"),
    "consigneestreetandnumber": ("PRS.Consignment", "consignee_street_number"),
    "consigneeaddress": ("PRS.Consignment", "consignee_street_number"),
    "consigneecity": ("PRS.Consignment", "consignee_city"),
    "consigneepostcode": ("PRS.Consignment", "consignee_postcode"),
    "importername": ("PRS.Consignment", "importer_name"),
    "importerstreetnumber": ("PRS.Consignment", "importer_street_number"),
    "importerstreetandnumber": ("PRS.Consignment", "importer_street_number"),
    "importeraddress": ("PRS.Consignment", "importer_street_number"),
    "importercity": ("PRS.Consignment", "importer_city"),
    "importerpostcode": ("PRS.Consignment", "importer_postcode"),
    "exportername": ("PRS.Consignment", "exporter_name"),
    "exporterstreetnumber": ("PRS.Consignment", "exporter_street_number"),
    "exporterstreetandnumber": ("PRS.Consignment", "exporter_street_number"),
    "exporteraddress": ("PRS.Consignment", "exporter_street_number"),
    "exportercity": ("PRS.Consignment", "exporter_city"),
    "exporterpostcode": ("PRS.Consignment", "exporter_postcode"),
    "destinationcountry": ("PRS.Consignment", "destination_country"),
    "countryofdestination": ("PRS.Consignment", "destination_country"),
    "packagetype": ("PRS.Goods_Item", "type_of_packages"),
    "typeofpackages": ("PRS.Goods_Item", "type_of_packages"),
    "packages": ("PRS.Goods_Item", "number_of_packages"),
    "numberofpackages": ("PRS.Goods_Item", "number_of_packages"),
    "packagemarks": ("PRS.Goods_Item", "package_marks"),
    "marks": ("PRS.Goods_Item", "package_marks"),
    "grossmass": ("PRS.Goods_Item", "gross_mass_kg"),
    "grossweight": ("PRS.Goods_Item", "gross_mass_kg"),
    "grossmasskg": ("PRS.Goods_Item", "gross_mass_kg"),
    "netmass": ("PRS.Goods_Item", "net_mass_kg"),
    "netweight": ("PRS.Goods_Item", "net_mass_kg"),
    "netmasskg": ("PRS.Goods_Item", "net_mass_kg"),
    "commoditycode": ("PRS.Goods_Item", "commodity_code"),
    "commodity": ("PRS.Goods_Item", "commodity_code"),
    "hscode": ("PRS.Goods_Item", "commodity_code"),
    "tariffcode": ("PRS.Goods_Item", "commodity_code"),
    "origin": ("PRS.Goods_Item", "country_of_origin"),
    "countryoforigin": ("PRS.Goods_Item", "country_of_origin"),
    "invoiceamount": ("PRS.Goods_Item", "item_invoice_amount"),
    "invoicevalue": ("PRS.Goods_Item", "item_invoice_amount"),
    "value": ("PRS.Goods_Item", "item_invoice_amount"),
    "currency": ("PRS.Goods_Item", "item_invoice_currency"),
    "invoicecurrency": ("PRS.Goods_Item", "item_invoice_currency"),
    "invoicenumber": ("PRS.Goods_Item", "invoice_number"),
}

REQUIRED_TARGETS = {("PRS.Consignment", field) for field in CONSIGNMENT_REQUIRED_FIELDS}
REQUIRED_TARGETS.update({
    ("PRS.Goods_Item", "type_of_packages"),
    ("PRS.Goods_Item", "number_of_packages"),
    ("PRS.Goods_Item", "package_marks"),
    ("PRS.Goods_Item", "gross_mass_kg"),
    ("PRS.Goods_Item", "goods_description"),
})


def normalise(value: Any) -> str:
    text = clean_cell(value).lower()
    text = text.replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", text)


def target_lookup() -> dict[str, tuple[str, str]]:
    lookup = dict(ALIASES)
    for table_name, columns in TARGET_FIELDS.items():
        for column in columns:
            lookup.setdefault(normalise(column), (table_name, column))
    return lookup


def suggest_column_mappings(columns: list[dict[str, Any]]) -> dict[str, Any]:
    lookup = target_lookup()
    suggestions: list[dict[str, Any]] = []
    covered_targets: set[tuple[str, str]] = set()
    for column in columns:
        source = clean_cell(column.get("name"))
        target = lookup.get(normalise(source))
        if target:
            table_name, target_column = target
            covered_targets.add(target)
            suggestions.append({
                "sourceColumn": source,
                "sourceOrdinal": column.get("ordinal"),
                "targetTable": table_name,
                "targetColumn": target_column,
                "confidence": "high",
                "reason": "Exact or known alias match.",
                "isRequired": target in REQUIRED_TARGETS,
            })
        else:
            suggestions.append({
                "sourceColumn": source,
                "sourceOrdinal": column.get("ordinal"),
                "targetTable": None,
                "targetColumn": None,
                "confidence": "none",
                "reason": "No safe automatic target match.",
                "isRequired": False,
            })
    missing_required = [
        {"targetTable": table_name, "targetColumn": target_column}
        for table_name, target_column in sorted(REQUIRED_TARGETS)
        if (table_name, target_column) not in covered_targets
    ]
    return {
        "suggestions": suggestions,
        "suggestedCount": sum(1 for item in suggestions if item["targetColumn"]),
        "unmatchedCount": sum(1 for item in suggestions if not item["targetColumn"]),
        "missingRequiredTargets": missing_required,
    }
