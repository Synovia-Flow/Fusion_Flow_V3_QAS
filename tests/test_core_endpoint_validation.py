from __future__ import annotations

import argparse
import importlib.util
import sys
from io import BytesIO
from pathlib import Path
import unittest
from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parents[1]
GRAPH_PATH = ROOT / "Integration_Layer" / "Graph" / "graph_mail_customer_downloader.py"
TSS_PATH = WORKSPACE_ROOT / "Synovia_Flow_Quality" / "Configuration_Layer" / "Scripts" / "tss_api_endpoints.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


graph = load_module("graph_mail_customer_downloader_under_test", GRAPH_PATH)
tss = load_module("tss_api_endpoints_under_test", TSS_PATH)


def runtime_args(**overrides):
    values = {
        "base_url": None,
        "auth_mode": None,
        "token_url": None,
        "prod": False,
        "transition_test": False,
        "username": "",
        "password": "",
        "client_id": "",
        "client_secret": "",
        "timeout": None,
        "dry_run": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def workbook_bytes() -> bytes:
    workbook = Workbook()
    report = workbook.active
    report.title = "Report"
    report.append(["AutoTable", "Formula", "Lookup"])
    report.append(["technical", "metadata", "only"])

    data = workbook.create_sheet("Sales Lines")
    data.append(["Document No.", "Line Amount Excl. VAT", "Gross Weight", "Net Weight"])
    data.append(["TDN-1", "12.345", "10.000", "9.999"])

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def bkd_config() -> dict[str, object]:
    return {
        "tenant_code": "BKD",
        "tenant_name": "Birkdale",
        "map_transport_document_number": ["Document No."],
        "map_item_invoice_amount": ["Line Amount Excl. VAT"],
        "map_gross_mass_kg": ["Gross Weight"],
        "map_net_mass_kg": ["Net Weight"],
        "source_goods_description": "product_master_data",
        "source_controlled_goods": "product_master_data_or_tenant_default",
        "source_consignor_eori": "tenant_master_data",
        "source_consignee_eori": "customer_master_data",
        "source_importer_eori": "customer_master_data",
        "source_exporter_eori": "tenant_master_data",
        "source_type_of_packages": "product_master_data_or_package_rules",
        "source_number_of_packages": "package_rules",
        "source_package_marks": "order_reference_or_package_rules",
    }


class CoreEndpointValidationTests(unittest.TestCase):
    def test_transition_build_url_strips_legacy_tss_api_prefix(self):
        transition = tss.build_runtime_config(runtime_args(transition_test=True))
        legacy = tss.build_runtime_config(runtime_args())

        self.assertEqual(
            tss.build_url(transition, tss.HEADERS_ENDPOINT),
            "https://tt.nc-tss.uk/api/v1/trader_api/transition/headers",
        )
        self.assertEqual(
            tss.build_url(transition, f"{tss.CHOICE_VALUES_ENDPOINT}/country"),
            "https://tt.nc-tss.uk/api/v1/trader_api/transition/choice_values/country",
        )
        self.assertEqual(
            tss.build_url(legacy, tss.HEADERS_ENDPOINT),
            "https://api.tsstestenv.co.uk/api/x_fhmrc_tss_api/v1/tss_api/headers",
        )

    def test_tss_preflight_runs_before_dry_run_send(self):
        config = tss.build_runtime_config(runtime_args())

        with self.assertRaisesRegex(tss.TSSAPIError, "addition_deduction_currency"):
            tss.api_post(
                config,
                tss.SD_ENDPOINT,
                {"header_additions_deductions": [{"addition_deduction_code": "AE"}]},
            )

        with self.assertRaisesRegex(tss.TSSAPIError, "taric_code"):
            tss.api_post(config, tss.GOODS_ENDPOINT, {"taric_code": "8708 2990"})

        result = tss.api_post(config, tss.GOODS_ENDPOINT, {"taric_code": "87082990"})
        self.assertIs(result["dry_run"], True)

    def test_xlsx_reader_chooses_business_sheet_with_tenant_aliases(self):
        headers, rows = graph.read_xlsx_rows(workbook_bytes(), bkd_config())
        mapping = graph.mapped_columns(headers, bkd_config())

        self.assertIn("Document No.", headers)
        self.assertNotIn("AutoTable", headers)
        self.assertEqual(rows[0]["Document No."], "TDN-1")
        self.assertEqual(mapping["Document No."], {"kind": "api", "field_name": "transport_document_number"})
        self.assertEqual(mapping["Line Amount Excl. VAT"], {"kind": "api", "field_name": "item_invoice_amount"})

    def test_decimal_normalisation_rounds_for_api_ready_values(self):
        self.assertEqual(graph.normalise_field_value_for_api("item_invoice_amount", "12.345")[0], "12.35")
        self.assertEqual(graph.normalise_field_value_for_api("item_invoice_amount", "12.344")[0], "12.34")
        self.assertEqual(graph.normalise_field_value_for_api("item_invoice_amount", "12.3"), ("12.3", []))

        comma_issues = graph.validate_field_value("item_invoice_amount", "12,34")
        self.assertTrue(any(rule == "Decimal format" for _, rule, _ in comma_issues))

        negative_gross = graph.validate_field_value("gross_mass_kg", "-1")
        self.assertTrue(any(rule == "Decimal range" for _, rule, _ in negative_gross))

    def test_validation_report_rows_include_normalized_value_without_failing_run(self):
        content = workbook_bytes()
        stats = graph.new_stats()
        rows: list[dict[str, str]] = []
        message = {
            "receivedDateTime": "2026-06-24T10:00:00Z",
            "subject": "Sales Lines",
            "from": {"emailAddress": {"address": "sales@birkdalesales.com"}},
        }

        graph.validate_xlsx_attachment(message, bkd_config(), "sales.xlsx", content, "sales.xlsx", rows, stats)

        self.assertGreaterEqual(stats["validation_warnings"], 2)
        self.assertEqual(stats["failed"], 0)
        self.assertTrue(all("normalizedValue" in row for row in rows))
        self.assertTrue(any(row["rule"] == "Decimal rounded" and row["normalizedValue"] == "12.35" for row in rows))

        headers, data_rows = graph.read_xlsx_rows(content, bkd_config())
        api_values = graph.api_ready_values_for_row(data_rows[0], graph.mapped_columns(headers, bkd_config()))
        self.assertEqual(api_values["api_item_invoice_amount"], "12.35")
        self.assertEqual(api_values["api_gross_mass_kg"], "10")


if __name__ == "__main__":
    unittest.main()