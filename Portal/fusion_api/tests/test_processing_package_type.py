from Modules.Processing import process_data
from Modules.Processing import preview_enrichment
from app import main as portal_main


class FakeProcessingDb:
    def __init__(self):
        self.events = []

    def log_enhancement(self, layer, table, column, entity_ref, old_value, new_value, rule):
        self.events.append({
            "layer": layer,
            "table": table,
            "column": column,
            "entity_ref": entity_ref,
            "old_value": old_value,
            "new_value": new_value,
            "rule": rule,
        })
        return old_value != new_value


def test_product_master_replaces_package_type_assumption_from_uom():
    db = FakeProcessingDb()
    goods = {}
    process_data._set_field(
        db,
        goods,
        "Goods_Item",
        "type_of_packages",
        "G1",
        "pallets",
        "ASSUMPTION:PACKAGE_TYPE_FROM_UOM",
    )

    process_data._apply_product_master_enrichment(
        db,
        goods,
        {"PackageType": "BOX 10"},
        "G1",
    )

    assert goods["type_of_packages"] == "PK"
    assert goods["_field_rules"]["type_of_packages"] == "MASTERDATA:BKD_PRODUCT_MASTER_PACKAGE_TYPE"
    package_events = [event for event in db.events if event["column"] == "type_of_packages"]
    assert package_events[-1]["old_value"] == "pallets"
    assert package_events[-1]["new_value"] == "PK"


def test_product_master_does_not_replace_explicit_package_type():
    db = FakeProcessingDb()
    goods = {}
    process_data._set_field(
        db,
        goods,
        "Goods_Item",
        "type_of_packages",
        "G1",
        "pallets",
        "DP-FR-01:MAP_SO_GOODS",
    )

    process_data._apply_product_master_enrichment(
        db,
        goods,
        {"PackageType": "BOX 10"},
        "G1",
    )

    assert goods["type_of_packages"] == "pallets"
    assert goods["_field_rules"]["type_of_packages"] == "DP-FR-01:MAP_SO_GOODS"


def test_preview_enrichment_normalises_tss_decimal_fields():
    values = {
        "gross_mass_kg": "0.42599999999999999",
        "net_mass_kg": "0.42599999999999999",
        "item_invoice_amount": "1.236",
        "type_of_packages": "PK",
    }
    sources = {
        "gross_mass_kg": {"sourceColumn": "gross_mass_kg"},
        "net_mass_kg": {"sourceColumn": "net_mass_kg"},
        "item_invoice_amount": {"sourceColumn": "item_invoice_amount"},
    }

    enhancements = preview_enrichment.enrich_goods_preview(values, sources, None)

    assert values["gross_mass_kg"] == "0.43"
    assert values["net_mass_kg"] == "0.43"
    assert values["item_invoice_amount"] == "1.24"
    assert sources["gross_mass_kg"]["normalised"] is True
    assert sources["gross_mass_kg"]["source"] == "processingNormalisation"
    assert {event["field"] for event in enhancements if event["source"] == "processingNormalisation"} >= {
        "gross_mass_kg",
        "net_mass_kg",
        "item_invoice_amount",
    }

def test_product_master_summary_reports_package_type_coverage():
    summary = preview_enrichment.product_master_summary([
        {"SKU": "A1", "ProductCode": "A1", "PackageType": ""},
        {"SKU": "A2", "ProductCode": "A2", "PackageType": "BOX 10"},
        {"SKU": "A3", "ProductCode": "A3", "PackageType": None},
    ])

    assert summary["rowCount"] == 3
    assert summary["lookupKeyCount"] == 3
    assert summary["packageTypeRowCount"] == 1
    assert summary["packageTypeMissingCount"] == 2
    assert summary["packageTypeValues"][0]["value"] == "PK"
    assert summary["packageTypeValues"][0]["examples"] == ["BOX 10"]
    assert "Some CFG.Product_Master rows" in summary["packageTypeWarning"]


def test_product_master_summary_warns_when_all_package_types_blank():
    summary = preview_enrichment.product_master_summary([
        {"SKU": "A1", "ProductCode": "A1", "PackageType": ""},
        {"SKU": "A2", "ProductCode": "A2", "PackageType": None},
    ])

    assert summary["packageTypeRowCount"] == 0
    assert summary["packageTypeMissingCount"] == 2
    assert "PackageType is blank for all" in summary["packageTypeWarning"]



def test_consignment_validation_summary_reports_package_enrichment_without_writes():
    summary = portal_main.build_consignment_validation_summary(
        {
            "goods_description": "Sweets",
            "transport_document_number": "TD-1",
            "destination_country": "GB",
        },
        [
            {
                "goods_description": "Tub sweets",
                "commodity_code": "1704906500",
                "type_of_packages": "TUB 1000",
                "number_of_packages": 1,
                "package_marks": "TD-1",
                "gross_mass_kg": "0.42599999999999999",
                "net_mass_kg": "0.42599999999999999",
            },
            {
                "goods_description": "Loose sweets",
                "commodity_code": "1704906500",
                "number_of_packages": 1,
                "package_marks": "TD-1",
                "gross_mass_kg": "1.00",
            },
        ],
    )

    assert summary["databaseWrite"] is False
    assert summary["tssWrite"] is False
    assert summary["packageType"]["normalisedCount"] == 1
    assert summary["packageType"]["missingCount"] == 1
    assert summary["packageType"]["values"][0]["value"] == "TB"
    assert summary["assumptions"][0]["rule"] == "ASSUMPTION:DEFAULT_PACKAGE_TYPE"
    assert {item["field"] for item in summary["decimalNormalisation"]} >= {"gross_mass_kg", "net_mass_kg"}
    assert summary["status"] == "NEEDS_REVIEW"

def test_product_master_lookup_prefers_matching_customer_code():
    lookup = preview_enrichment.product_master_lookup([
        {"ClientCode": "BKD", "CustomerCode": "CUST1", "SKU": "A1", "PackageType": "PALLET 70"},
        {"ClientCode": "BKD", "CustomerCode": "ALL", "SKU": "A1", "PackageType": "BOX 10"},
    ])

    assert preview_enrichment.product_master_get(
        lookup,
        {"_source_sku": "A1"},
        {"_source_customer_code": "CUST1"},
    )["PackageType"] == "PALLET 70"
    assert preview_enrichment.product_master_get(
        lookup,
        {"_source_sku": "A1"},
        {"_source_customer_code": "CUST2"},
    )["PackageType"] == "BOX 10"
