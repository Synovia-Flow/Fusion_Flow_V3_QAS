import os
import unittest
import zipfile
from html import escape
from io import BytesIO

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app import file_introspection
from app import main as portal_main
from app.tss_profiles import fallback_profile


CSV_CONTENT = b"consignment_number,goods_description,transport_document_number\nCON-1,Goods,TDN-1\n"


def xlsx_content(rows: list[list[str]]) -> bytes:
    return xlsx_workbook_content([("Sheet1", rows)])


def xlsx_workbook_content(sheets: list[tuple[str, list[list[str]]]]) -> bytes:
    def col_name(index: int) -> str:
        name = ""
        index += 1
        while index:
            index, rem = divmod(index - 1, 26)
            name = chr(65 + rem) + name
        return name

    def sheet_xml(rows: list[list[str]]) -> str:
        sheet_rows = []
        for row_index, row in enumerate(rows, 1):
            cells = []
            for col_index, value in enumerate(row):
                ref = f"{col_name(col_index)}{row_index}"
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
            sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{''.join(sheet_rows)}</sheetData></worksheet>'''

    content_overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    ]
    workbook_sheets = []
    workbook_rels = []
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, (sheet_name, rows) in enumerate(sheets, 1):
            content_overrides.append(f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            workbook_sheets.append(f'<sheet name="{escape(sheet_name)}" sheetId="{index}" r:id="rId{index}"/>')
            workbook_rels.append(f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>')
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml(rows))

        archive.writestr("[Content_Types].xml", f'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
{''.join(content_overrides)}
</Types>''')
        archive.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''')
        archive.writestr("xl/workbook.xml", f'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>{''.join(workbook_sheets)}</sheets>
</workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{''.join(workbook_rels)}
</Relationships>''')
    return buffer.getvalue()

def upload_file(filename: str, content: bytes = CSV_CONTENT) -> UploadFile:
    lower_name = filename.lower()
    if lower_name.endswith(".xlsx"):
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif lower_name.endswith(".pdf"):
        content_type = "application/pdf"
    else:
        content_type = "text/csv"
    return UploadFile(
        BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


class FakeRequest:
    """Enough of a Starlette request for the best-effort login audit to read."""

    def __init__(self, headers=None, host="127.0.0.1"):
        self.headers = headers or {}
        self.client = type("Client", (), {"host": host})()


class PortalAuthTests(unittest.TestCase):
    def test_flow_v1_login_returns_synovia_demo_session_not_primeline_tenant(self):
        original_query_one = portal_main.query_one
        old_user = os.environ.get("FLOW_V1_USER")
        old_password = os.environ.get("FLOW_V1_PASSWORD")
        try:
            portal_main.query_one = lambda *args, **kwargs: None
            os.environ["FLOW_V1_USER"] = "synovia-test"
            os.environ["FLOW_V1_PASSWORD"] = "Password2025!"

            payload = portal_main.auth_login(
                FakeRequest(),
                {"username": "synovia-test", "password": "Password2025!"},
            )
        finally:
            portal_main.query_one = original_query_one
            if old_user is None:
                os.environ.pop("FLOW_V1_USER", None)
            else:
                os.environ["FLOW_V1_USER"] = old_user
            if old_password is None:
                os.environ.pop("FLOW_V1_PASSWORD", None)
            else:
                os.environ["FLOW_V1_PASSWORD"] = old_password

        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["source"], "FLOW_V1_USER")
        self.assertEqual(payload["session"]["tenantCode"], "SYNOVIA")
        self.assertEqual(payload["session"]["tenantName"], "Synovia")
        self.assertEqual(payload["session"]["mode"], "DEMO_ADMIN")
        self.assertEqual(payload["defaultClientCode"], "BKD")
        self.assertEqual(payload["connection"]["portalClientCode"], "BKD")
        self.assertTrue(payload["demoMode"])
        self.assertFalse(payload["databaseWrite"])
        self.assertFalse(payload["tssWrite"])


class UploadPreviewSelectionTests(unittest.TestCase):
    def setUp(self):
        self._original_load_portal_profile = portal_main.load_portal_profile
        portal_main.load_portal_profile = lambda value: fallback_profile(value)

    def tearDown(self):
        portal_main.load_portal_profile = self._original_load_portal_profile

    def test_settings_validation_diagnostics_explains_package_type_resolution_without_writes(self):
        diagnostics = portal_main.admin_validation_diagnostics(client_code="BKD")

        self.assertEqual(diagnostics["writeMode"], "read_only_diagnostics")
        self.assertFalse(diagnostics["databaseWrite"])
        self.assertFalse(diagnostics["tssWrite"])
        self.assertEqual(diagnostics["productMaster"]["source"], "CFG.Product_Master")
        self.assertFalse(diagnostics["productMaster"]["available"])
        self.assertIn(
            "source UOM/package wording normalised through Modules.Processing",
            diagnostics["packageTypeResolutionOrder"],
        )
        self.assertIn("PackageType is not a TSS choice-values download", diagnostics["notes"][1])

    def test_consignment_list_reports_the_dec_reference_and_no_local_validation(self):
        """The list now reads the tenant mirror, so it reports what TSS holds.

        consignment_number there is the TSS DEC reference, which is what the DEC Ref
        column is for; in the Release 1 database the same column holds a value the
        engine derived from the sales order. Fusion's validation summary is computed
        from PRS rows and has no counterpart in the mirror, so the list reports it as
        absent instead of fabricating one from TSS data.
        """
        original_mirror_query_all = portal_main.mirror_query_all
        original_status_vocabulary = portal_main.status_vocabulary_rows
        original_id_expression = portal_main.consignment_id_expression
        original_loaded_expression = portal_main.consignment_loaded_at_expression

        def fake_mirror_query_all(sql, params=None):
            if "OFFSET" in sql:
                return [{"consignment_number": "DEC000000000402020"}]
            if "GROUP BY" in sql:
                return [{"Status": "ARRIVED", "Total": 1}]
            return [
                {
                    "ConsignmentRowID": 6163,
                    "ConsignmentNumber": "DEC000000000402020",
                    "DeclarationNumber": "ENS000000000321362",
                    "Status": "Arrived",
                    "TssStatus": "Arrived",
                    "TransportDocumentNumber": "508182",
                    "GoodsDescription": "Sweets",
                    "GoodsItems": 2,
                    "GrossMassKg": "1.43",
                }
            ]

        portal_main.mirror_query_all = fake_mirror_query_all
        portal_main.status_vocabulary_rows = lambda: []
        portal_main.consignment_id_expression = lambda _schema: "c.consignment_id"
        portal_main.consignment_loaded_at_expression = lambda _schema: "c.loaded_at"
        try:
            payload = portal_main.consignments(
                client_code="BKD", status="ALL", q="", limit=100,
                page=1, page_size=None, api_date_range="all",
            )
        finally:
            portal_main.mirror_query_all = original_mirror_query_all
            portal_main.status_vocabulary_rows = original_status_vocabulary
            portal_main.consignment_id_expression = original_id_expression
            portal_main.consignment_loaded_at_expression = original_loaded_expression

        row = payload["consignments"][0]
        self.assertEqual(row["ConsignmentNumber"], "DEC000000000402020")
        self.assertEqual(row["DeclarationNumber"], "ENS000000000321362")
        self.assertEqual(payload["tenantSchema"], "BKD")
        self.assertIsNone(row["ValidationSummary"])
        self.assertEqual(row["MissingRequiredCount"], 0)
        self.assertEqual(row["AssumptionCount"], 0)

    def test_primeline_preview_maps_first_uploaded_attachment(self):
        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("primeline-first.csv"), upload_file("countrywide-second.csv")],
        )

        self.assertEqual(payload["portalClientCode"], "PLE")
        self.assertEqual(payload["selectedFileOrdinal"], 1)
        self.assertEqual(payload["filename"], "primeline-first.csv")
        self.assertTrue(payload["receivedFiles"][0]["selected"])
        self.assertFalse(payload["receivedFiles"][1]["selected"])

    def test_countrywide_preview_maps_second_uploaded_attachment(self):
        payload = portal_main.upload_consignment_preview(
            client_code="CWD",
            files=[upload_file("primeline-first.csv"), upload_file("countrywide-second.csv")],
        )

        self.assertEqual(payload["portalClientCode"], "CWD")
        self.assertEqual(payload["clientCode"], "CWD")
        self.assertEqual(payload["tssCredentialClientCode"], "CWF")
        self.assertEqual(payload["selectedFileOrdinal"], 2)
        self.assertEqual(payload["filename"], "countrywide-second.csv")
        self.assertFalse(payload["receivedFiles"][0]["selected"])
        self.assertTrue(payload["receivedFiles"][1]["selected"])

    def test_countrywide_preview_requires_second_attachment(self):
        with self.assertRaises(HTTPException) as ctx:
            portal_main.upload_consignment_preview(client_code="CWD", files=[upload_file("only-one.csv")])

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("requires attached file #2", str(ctx.exception.detail))

    def test_countrywide_single_pdf_invoice_maps_first_uploaded_file(self):
        original_inspect_upload = portal_main.inspect_upload
        try:
            portal_main.inspect_upload = lambda filename, content: {
                "fileType": "pdf",
                "columns": [],
                "rows": [],
                "warning": "stubbed pdf preview",
            }
            payload = portal_main.upload_consignment_preview(
                client_code="CWD",
                files=[upload_file("1070939 -814877.pdf", b"%PDF-1.4")],
            )
        finally:
            portal_main.inspect_upload = original_inspect_upload

        self.assertEqual(payload["portalClientCode"], "CWD")
        self.assertEqual(payload["requiredFileOrdinal"], 2)
        self.assertEqual(payload["selectedFileOrdinal"], 1)
        self.assertEqual(payload["filename"], "1070939 -814877.pdf")
        self.assertEqual(payload["selectionRule"], "Map single Countrywide PDF invoice.")
        self.assertTrue(payload["receivedFiles"][0]["selected"])
        self.assertEqual(payload["ignoredFiles"], [])


    def test_demo_mode_supplies_default_ens_without_db_or_tss_write(self):
        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("primeline-demo.csv")],
            demo_mode=True,
        )

        self.assertTrue(payload["demoMode"])
        self.assertFalse(payload["databaseWrite"])
        self.assertFalse(payload["tssWrite"])
        self.assertEqual(payload["writeMode"], "demo_preview_only")
        self.assertEqual(payload["demoEns"]["declarationNumber"], "ENS900000000000001")
        missing = {
            (item["targetTable"], item["targetColumn"])
            for item in payload["mappingSuggestions"]["missingRequiredTargets"]
        }
        self.assertNotIn(("PRS.Consignment", "declaration_number"), missing)
        self.assertEqual(
            payload["validationContext"]["demoSatisfiedTargets"],
            [{"targetTable": "PRS.Consignment", "targetColumn": "declaration_number", "source": "demoEns"}],
        )
        self.assertIn(
            "source UOM/package wording normalised through Modules.Processing",
            payload["validationContext"]["packageTypeResolutionOrder"],
        )
        self.assertIn("processingPreview", payload)
        self.assertFalse(payload["processingPreview"]["summary"]["databaseWrite"])
        self.assertFalse(payload["processingPreview"]["summary"]["tssWrite"])
        consignment = payload["processingPreview"]["consignments"][0]
        declaration_field = next(field for field in consignment["fields"] if field["field"] == "declaration_number")
        self.assertEqual(declaration_field["source"]["source"], "demoEns")
        self.assertTrue(declaration_field["source"]["assumption"])

    def test_demo_mode_labels_generated_preview_values_as_assumptions(self):
        manifest = "\n".join([
            "api_field,source_value",
            "transport_document_number,TDN-GENERATED-1",
            "controlled_goods,no",
            "consignor_eori,XI111111111000",
            "consignee_eori,GB222222222000",
            "importer_eori,XI333333333000",
            "exporter_eori,XI444444444000",
            "PRS.Goods_Item[1].goods_description,Goods-only description",
            "PRS.Goods_Item[1].type_of_packages,PK",
            "PRS.Goods_Item[1].number_of_packages,1",
            "PRS.Goods_Item[1].package_marks,ADDR",
            "PRS.Goods_Item[1].gross_mass_kg,10.00",
        ]).encode("utf-8")

        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("generated-assumptions.csv", manifest)],
            demo_mode=True,
        )

        consignment = payload["processingPreview"]["consignments"][0]
        fields = {field["field"]: field for field in consignment["fields"]}
        self.assertEqual(consignment["values"]["consignment_number"], "PREVIEW-001")
        self.assertEqual(consignment["values"]["goods_description"], "Goods-only description")
        self.assertEqual(fields["declaration_number"]["source"]["source"], "demoEns")
        self.assertTrue(fields["declaration_number"]["source"]["assumption"])
        self.assertEqual(fields["consignment_number"]["source"]["source"], "previewGenerated")
        self.assertTrue(fields["consignment_number"]["source"]["assumption"])
        self.assertEqual(fields["goods_description"]["source"]["source"], "firstGoodsItem")
        self.assertTrue(fields["goods_description"]["source"]["assumption"])
        self.assertEqual(fields["goods_description"]["source"]["originalSource"]["apiField"], "PRS.Goods_Item[1].goods_description")

    def test_demo_mode_maps_api_field_value_manifest_to_consignment_and_goods(self):
        manifest = "\n".join([
            "api_field,source_value",
            "consignment_number,CON-FV-1",
            "transport_document_number,TDN-FV-1",
            "controlled_goods,no",
            "consignor_eori,XI111111111000",
            "consignee_eori,GB222222222000",
            "importer_eori,XI333333333000",
            "exporter_eori,XI444444444000",
            "goods_description,Lisburn manifest goods",
            "type_of_packages,PK",
            "number_of_packages,3",
            "package_marks,ADDR",
            "gross_mass_kg,12.5",
            "net_mass_kg,11.5",
        ]).encode("utf-8")

        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("PLE FILE -LISBURN MANIFEST 01.05.2026 .csv", manifest)],
            demo_mode=True,
        )

        preview = payload["processingPreview"]
        self.assertEqual(preview["rowMode"], "api_field_value")
        self.assertEqual(preview["summary"]["consignmentCount"], 1)
        self.assertEqual(preview["summary"]["goodsItemCount"], 1)
        consignment = preview["consignments"][0]
        self.assertEqual(consignment["values"]["declaration_number"], "ENS900000000000001")
        self.assertEqual(consignment["values"]["consignment_number"], "CON-FV-1")
        self.assertEqual(consignment["goodsItems"][0]["values"]["gross_mass_kg"], "12.50")
        self.assertEqual(consignment["goodsItems"][0]["values"]["goods_description"], "Lisburn manifest goods")

    def test_demo_mode_maps_lisburn_xlsx_field_value_manifest_to_consignment_and_goods(self):
        content = xlsx_content([
            ["api_field", "source_value"],
            ["PRS.Consignment.consignment_number", "CON-LISBURN-001"],
            ["PRS.Consignment.goods_description", "Lisburn consignment"],
            ["PRS.Consignment.transport_document_number", "TDN-LISBURN-001"],
            ["PRS.Consignment.controlled_goods", "no"],
            ["PRS.Consignment.consignor_eori", "XI111111111000"],
            ["PRS.Consignment.consignee_eori", "GB222222222000"],
            ["PRS.Consignment.importer_eori", "XI333333333000"],
            ["PRS.Consignment.exporter_eori", "XI444444444000"],
            ["PRS.Goods_Item[1].goods_description", "Lisburn goods item 1"],
            ["PRS.Goods_Item[1].type_of_packages", "PK"],
            ["PRS.Goods_Item[1].number_of_packages", "2"],
            ["PRS.Goods_Item[1].package_marks", "ADDR"],
            ["PRS.Goods_Item[1].gross_mass_kg", "42.5"],
            ["PRS.Goods_Item[1].net_mass_kg", "40.0"],
            ["PRS.Goods_Item[2].goods_description", "Lisburn goods item 2"],
            ["PRS.Goods_Item[2].type_of_packages", "PK"],
            ["PRS.Goods_Item[2].number_of_packages", "1"],
            ["PRS.Goods_Item[2].package_marks", "ADDR"],
            ["PRS.Goods_Item[2].gross_mass_kg", "12.0"],
        ])

        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("PLE FILE -LISBURN MANIFEST 01.05.2026 .xlsx", content)],
            demo_mode=True,
        )

        self.assertEqual(payload["filename"], "PLE FILE -LISBURN MANIFEST 01.05.2026 .xlsx")
        self.assertEqual(payload["detectedStructure"]["format"], "xlsx")
        self.assertEqual([column["name"] for column in payload["detectedStructure"]["columns"]], ["api_field", "source_value"])
        preview = payload["processingPreview"]
        self.assertEqual(preview["rowMode"], "api_field_value")
        self.assertEqual(preview["summary"]["consignmentCount"], 1)
        self.assertEqual(preview["summary"]["goodsItemCount"], 2)
        self.assertEqual(preview["summary"]["unmatchedFieldCount"], 0)
        self.assertFalse(preview["summary"]["databaseWrite"])
        self.assertFalse(preview["summary"]["tssWrite"])
        consignment = preview["consignments"][0]
        self.assertEqual(consignment["values"]["declaration_number"], "ENS900000000000001")
        self.assertEqual(consignment["values"]["consignment_number"], "CON-LISBURN-001")
        self.assertEqual(consignment["values"]["goods_description"], "Lisburn consignment")
        self.assertEqual(consignment["goodsItems"][0]["values"]["goods_description"], "Lisburn goods item 1")
        self.assertEqual(consignment["goodsItems"][1]["values"]["goods_description"], "Lisburn goods item 2")
        self.assertEqual(consignment["goodsItems"][1]["missingRequired"], [])
        payload_preview = consignment["tssPayloadPreview"]
        self.assertTrue(payload_preview["ready"])
        self.assertFalse(payload_preview["databaseWrite"])
        self.assertFalse(payload_preview["tssWrite"])
        self.assertEqual(payload_preview["operations"][0]["operationCode"], "UPDATE_CONSIGNMENT_WITH_ENS")
        self.assertEqual(payload_preview["operations"][0]["payload"]["consignment_number"], "CON-LISBURN-001")
        self.assertEqual(payload_preview["operations"][1]["operationCode"], "SUBMIT_CONSIGNMENT")
        self.assertEqual(payload_preview["operations"][1]["payload"]["declaration_number"], "ENS900000000000001")
        self.assertEqual(payload_preview["goodsItemCount"], 2)
        self.assertEqual(payload_preview["goodsItems"][0]["gross_mass_kg"], "42.50")

    def test_demo_mode_combines_lisburn_field_value_sheet_with_goods_table_sheet(self):
        content = xlsx_workbook_content([
            ("Sheet1", [
                ["api_field", "source_value"],
                ["movement_type", "RoRo Accompanied ICS2"],
                ["transport_document_number", "ICR2524064"],
                ["arrival_port", "Belfast Port"],
            ]),
            ("Primeline_NI_CAT1_Parts_05_05_2", [
                [
                    "consignment_description",
                    "trader_reference",
                    "transport_document_number",
                    "destination_country",
                    "consignor_eori",
                    "consignor_name",
                    "consignor_country",
                    "consignee_eori",
                    "consignee_name",
                    "consignee_country",
                    "importer_eori",
                    "importer_name",
                    "importer_country",
                    "exporter_eori",
                    "exporter_name",
                    "exporter_country",
                    "commodity_code",
                    "type_of_packages",
                    "number_of_packages",
                    "package_marks",
                    "gross_mass_kg",
                    "goods_description",
                    "country_of_origin",
                ],
                [
                    "Caterpillar Parts",
                    "MANIFEST 01.05.26",
                    "PLEGVT001",
                    "GB",
                    "XI100516042000",
                    "Finning UK",
                    "GB",
                    "XI100516042000",
                    "Finning(NI)",
                    "GB",
                    "XI100516042000",
                    "Finning(NI)",
                    "GB",
                    "XI100516042000",
                    "Finning UK",
                    "GB",
                    "73182200",
                    "BX",
                    "1",
                    "6PC073421A",
                    "0.02",
                    "WASHER -DE",
                    "US",
                ],
                [
                    "Caterpillar Parts",
                    "MANIFEST 01.05.26",
                    "PLEGVT001",
                    "GB",
                    "XI100516042000",
                    "Finning UK",
                    "GB",
                    "XI100516042000",
                    "Finning(NI)",
                    "GB",
                    "XI100516042000",
                    "Finning(NI)",
                    "GB",
                    "XI100516042000",
                    "Finning UK",
                    "GB",
                    "73182200",
                    "BX",
                    "1",
                    "8KS000880A",
                    "0.02",
                    "WASHER -DE",
                    "US",
                ],
            ]),
        ])

        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("PLE FILE -LISBURN MANIFEST 01.05.2026 .xlsx", content)],
            demo_mode=True,
        )

        self.assertEqual(payload["detectedStructure"]["sheetNames"], ["Sheet1", "Primeline_NI_CAT1_Parts_05_05_2"])
        self.assertEqual(len(payload["detectedStructure"]["worksheets"]), 2)
        preview = payload["processingPreview"]
        self.assertEqual(preview["rowMode"], "multi_sheet")
        self.assertEqual([sheet["rowMode"] for sheet in preview["sourceSheets"]], ["api_field_value", "wide_rows"])
        self.assertEqual(preview["summary"]["consignmentCount"], 1)
        self.assertEqual(preview["summary"]["goodsItemCount"], 2)
        self.assertEqual(preview["summary"]["missingRequiredCount"], 0)
        consignment = preview["consignments"][0]
        self.assertEqual(consignment["values"]["declaration_number"], "ENS900000000000001")
        self.assertEqual(consignment["values"]["consignment_number"], "PLEGVT001")
        self.assertEqual(consignment["values"]["goods_description"], "Caterpillar Parts")
        self.assertEqual(consignment["values"]["transport_document_number"], "PLEGVT001")
        self.assertNotIn("controlled_goods", consignment["missingRequired"])
        self.assertEqual(consignment["values"]["controlled_goods"], "no")
        controlled_field = next(field for field in consignment["fields"] if field["field"] == "controlled_goods")
        self.assertEqual(controlled_field["source"]["source"], "assumption")
        self.assertTrue(controlled_field["source"]["assumption"])
        self.assertEqual(consignment["goodsItems"][0]["values"]["package_marks"], "6PC073421A")
        self.assertEqual(consignment["goodsItems"][1]["values"]["package_marks"], "8KS000880A")
        self.assertEqual(consignment["goodsItems"][0]["status"], "READY")
        self.assertTrue(consignment["tssPayloadPreview"]["ready"])

    def test_preview_derives_package_type_from_unit_of_measure_code_with_assumption(self):
        manifest = "\n".join([
            "consignment_number,goods_description,transport_document_number,controlled_goods,consignor_eori,consignee_eori,importer_eori,exporter_eori,Unit of Measure Code,number_of_packages,package_marks,gross_mass_kg",
            "CON-UOM-1,Sales order sweets,TDN-UOM-1,no,XI111111111000,GB222222222000,XI333333333000,XI444444444000,BOX 10,1,ADDR,12.345",
        ]).encode("utf-8")

        payload = portal_main.upload_consignment_preview(
            client_code="BKD",
            files=[upload_file("Sales Orders Synovia.csv", manifest)],
            demo_mode=True,
        )

        goods = payload["processingPreview"]["consignments"][0]["goodsItems"][0]
        package_field = next(field for field in goods["fields"] if field["field"] == "type_of_packages")

        self.assertEqual(goods["values"]["type_of_packages"], "PK")
        self.assertNotIn("type_of_packages", goods["missingRequired"])
        self.assertEqual(package_field["source"]["source"], "ASSUMPTION:PACKAGE_TYPE_FROM_UOM")
        self.assertTrue(package_field["source"]["assumption"])
        self.assertTrue(package_field["source"]["normalised"])
        self.assertEqual(package_field["source"]["originalValue"], "BOX 10")
        self.assertIn("Unit of Measure Code", package_field["source"]["originalSource"]["sourceColumn"])
    def test_demo_mode_maps_text_pdf_invoice_to_consignment_and_goods(self):
        pdf_text = """
Email: sales@frisco.co.uk
Currency: Pound Sterling
Invoiced To: Murdock T/A Newry Building Suppies Ltd Shipment Detail: Invoice No. 0000814877
N.W. (Kg): 39.426 Invoice Date: 05/12/2025
G.W. (Kg): 40 Terms: DAP
Pieces: 2 ctns Customer Ref: PO000187695
Email: Customer EORI: XI203964318000
Code Description Qty Price Origin HS Code SKU Weight (Kg) Line Total
94160 Shelf Bracket 350x300mm WHT 20 GBP1.43 India 8302419000 0.163 GBP28.60
94080 Shelf Bracket 200x150mm WHT 10 GBP0.45 India 8302419000 0.088 GBP4.50
Rampart Road, Greenbank Ind Estate, , , Newry,
Belfast, BT34 2QU, Great Britain
Address:
T: +44 1 992 443 010Unit 14 Pindar Road, Hoddesdon, EN11 0DE, UK
EORI: GB152338719000 / XI152338719000
"""
        original_extract = file_introspection.extract_pdf_text_pages
        original_metadata = file_introspection.extract_pdf_metadata
        try:
            file_introspection.extract_pdf_text_pages = lambda _content: [pdf_text, "Registered in England No: 01551925"]
            file_introspection.extract_pdf_metadata = lambda _content: {}
            payload = portal_main.upload_consignment_preview(
                client_code="CWD",
                files=[upload_file("frisco-invoice.pdf", b"%PDF-1.7")],
                demo_mode=True,
            )
        finally:
            file_introspection.extract_pdf_text_pages = original_extract
            file_introspection.extract_pdf_metadata = original_metadata

        self.assertEqual(payload["detectedStructure"]["format"], "pdf")
        self.assertNotIn(file_introspection.ASSUMPTION_META_KEY, [column["name"] for column in payload["detectedStructure"]["columns"]])
        preview = payload["processingPreview"]
        self.assertEqual(preview["summary"]["consignmentCount"], 1)
        self.assertEqual(preview["summary"]["goodsItemCount"], 2)
        self.assertEqual(preview["summary"]["missingRequiredCount"], 0)
        consignment = preview["consignments"][0]
        self.assertEqual(consignment["values"]["consignment_number"], "PO000187695")
        self.assertEqual(consignment["values"]["transport_document_number"], "PO000187695")
        self.assertEqual(consignment["values"]["consignor_name"], "Frisco UK Sales Ltd")
        self.assertEqual(consignment["values"]["consignor_street_number"], "Unit 14 Pindar Road")
        self.assertEqual(consignment["values"]["consignor_city"], "Hoddesdon")
        self.assertEqual(consignment["values"]["consignor_postcode"], "EN11 0DE")
        self.assertEqual(consignment["values"]["consignee_eori"], "XI203964318000")
        self.assertEqual(consignment["values"]["consignee_street_number"], "Rampart Road, Greenbank Ind Estate, , , Newry")
        self.assertEqual(consignment["values"]["consignee_city"], "Belfast")
        self.assertEqual(consignment["values"]["consignee_postcode"], "BT34 2QU")
        self.assertEqual(consignment["values"]["exporter_eori"], "GB152338719000")
        consignment_fields = {field["field"]: field for field in consignment["fields"]}
        self.assertTrue(consignment_fields["consignor_name"]["source"]["assumption"])
        self.assertTrue(consignment_fields["consignor_postcode"]["source"]["assumption"])
        goods = consignment["goodsItems"][0]
        self.assertEqual(goods["values"]["goods_description"], "Shelf Bracket 350x300mm WHT")
        self.assertEqual(goods["values"]["commodity_code"], "8302419000")
        self.assertEqual(goods["values"]["number_of_packages"], "20")
        self.assertEqual(goods["values"]["gross_mass_kg"], "3.26")
        self.assertEqual(goods["values"]["item_invoice_amount"], "28.60")
        self.assertEqual(goods["values"]["item_invoice_currency"], "GBP")
        goods_fields = {field["field"]: field for field in goods["fields"]}
        self.assertTrue(goods_fields["net_mass_kg"]["source"]["assumption"])
        self.assertEqual(consignment["tssPayloadPreview"]["goodsItems"][0]["gross_mass_kg"], "3.26")
    def test_demo_mode_maps_pdf_continuation_page_goods_to_previous_invoice(self):
        first_page = """
Commercial Invoice
Seller: Goodman Bros
Commercial Invoice Number CI-0002463501223 828718
Shipper's EORI Number GB189435365000
Receiver's EORI Number IE4531617P000
Buyer: Timemark Ltd
No. of Packages: 1
Shipment Weight: 165 Gross kg
Currency Code GBP
Product Code Description Qty Price Origin Comm. Code Weight Line Total
RING001 Stainless Steel Ring 1 Â£4.00 India 7113190000 0.10 Â£4.00
"""
        continuation_page = """
CHAIN001 Initial Alphabet Charm 2 Â£5.00 India 7113190000 0.50 Â£10.00
"""
        original_extract = file_introspection.extract_pdf_text_pages
        original_metadata = file_introspection.extract_pdf_metadata
        try:
            file_introspection.extract_pdf_text_pages = lambda _content: [first_page, continuation_page]
            file_introspection.extract_pdf_metadata = lambda _content: {}
            payload = portal_main.upload_consignment_preview(
                client_code="CWD",
                files=[upload_file("goodman-continuation.pdf", b"%PDF-1.7")],
                demo_mode=True,
            )
        finally:
            file_introspection.extract_pdf_text_pages = original_extract
            file_introspection.extract_pdf_metadata = original_metadata

        preview = payload["processingPreview"]
        self.assertEqual(preview["summary"]["consignmentCount"], 1)
        self.assertEqual(preview["summary"]["goodsItemCount"], 2)
        consignment = preview["consignments"][0]
        self.assertEqual(consignment["values"]["consignment_number"], "CI-0002463501223 828718")
        self.assertEqual(consignment["goodsItems"][1]["values"]["goods_description"], "Initial Alphabet Charm")
        self.assertEqual(consignment["goodsItems"][1]["values"]["gross_mass_kg"], "1.00")
        continuation_fields = {field["field"]: field for field in consignment["goodsItems"][1]["fields"]}
        self.assertTrue(continuation_fields["goods_description"]["source"]["assumption"])
    def test_demo_mode_maps_goodman_reversed_pdf_metrics_and_address(self):
        pdf_text = """
Commercial Invoice
Seller:
Goodman Bros
11-12 Regal Road
Wisbech
United Kingdom
Cambridgeshire
PE13 2RQ
Commercial Invoice Number CI-0002333101223 828718
invoices@goodman-bros.com Shipper's EORI Number GB189435365000www.goodman-bros.com
Receiver's EORI Number IE4531617P000
Buyer:
Timemark Ltd
A6 Calmount Park Delivery City Ballymount
No. of Packages: 1
D12RD88 Shipping Company: Customer Own
Ballymount Total Weight 0.30 Nett kg
Dublin 12 Shipment Weight: 165 Gross kg
Ireland
PriceDiscountPriceWeightQtyCountry of OriginDescriptionComm. CodeProduct DescriptionProduct Code
75.600%6.300.30012.00United Statesear-piercing
contents7113190000Inverness Stainless Steel 3mm
' AB' CZIN-1163C
"""
        original_extract = file_introspection.extract_pdf_text_pages
        original_metadata = file_introspection.extract_pdf_metadata
        try:
            file_introspection.extract_pdf_text_pages = lambda _content: [pdf_text]
            file_introspection.extract_pdf_metadata = lambda _content: {}
            payload = portal_main.upload_consignment_preview(
                client_code="CWD",
                files=[upload_file("goodman-reversed.pdf", b"%PDF-1.7")],
                demo_mode=True,
            )
        finally:
            file_introspection.extract_pdf_text_pages = original_extract
            file_introspection.extract_pdf_metadata = original_metadata

        consignment = payload["processingPreview"]["consignments"][0]
        self.assertEqual(consignment["values"]["consignor_city"], "Wisbech")
        self.assertEqual(consignment["values"]["consignor_postcode"], "PE13 2RQ")
        self.assertEqual(consignment["values"]["exporter_eori"], "GB189435365000")
        goods = consignment["goodsItems"][0]
        self.assertEqual(goods["values"]["number_of_packages"], "12")
        self.assertEqual(goods["values"]["package_marks"], "CZIN-1163C")
        self.assertEqual(goods["values"]["gross_mass_kg"], "0.30")
        self.assertEqual(goods["values"]["country_of_origin"], "US")
        self.assertEqual(goods["values"]["item_invoice_amount"], "75.60")
        tss_goods = consignment["tssPayloadPreview"]["goodsItems"][0]
        self.assertEqual(tss_goods["gross_mass_kg"], "0.30")
        self.assertEqual(tss_goods["item_invoice_amount"], "75.60")

    def test_demo_mode_maps_zok_ocr_postcode_city_and_summary_goods(self):
        pdf_text = """
ZOK International Group Ltd
Airworthy House
Elsted Marsh
Midhurst
West Sussex
GU29 OJT
United Kingdom
Invoice Address:
3Q Industrial Supplies Ltd
Delivery Address:
EP Ballylumford Ltd
Ferris Bay Road
lslandmagee
Ballylumford
Lame
BT40 3RS
Invoice
Currency Code Customer ID Purchase order No. Number: Date: Customer VAT No.
GBP 3QINDUST 135823 220814 05 December 2025 GB
PO No: 135823 Consignee telephone number: 01472 355870
Terms of Trading: Freight & Insurance to EP Ballylumford Ltd
Reason for Export: Sold goods Packed onto 1 pallet Dimensions per pallet: (1) Can Pallet Dims @ 1140 x 1140 x 560mm (WxDxH)
Net weight of consignment: 300kg Gross weight of consignment: 344kg
ZOK EORI Number: GB784419594000 NON FLAMMABLE -Tariff Number: ZOK 34029090
"""
        original_extract = file_introspection.extract_pdf_text_pages
        original_metadata = file_introspection.extract_pdf_metadata
        try:
            file_introspection.extract_pdf_text_pages = lambda _content: [pdf_text]
            file_introspection.extract_pdf_metadata = lambda _content: {}
            payload = portal_main.upload_consignment_preview(
                client_code="CWD",
                files=[upload_file("zok-invoice.pdf", b"%PDF-1.7")],
                demo_mode=True,
            )
        finally:
            file_introspection.extract_pdf_text_pages = original_extract
            file_introspection.extract_pdf_metadata = original_metadata

        consignment = payload["processingPreview"]["consignments"][0]
        self.assertEqual(consignment["values"]["consignor_city"], "Midhurst")
        self.assertEqual(consignment["values"]["consignor_postcode"], "GU29 0JT")
        self.assertEqual(consignment["values"]["consignee_city"], "Lame")
        self.assertEqual(consignment["values"]["consignee_postcode"], "BT40 3RS")
        self.assertEqual(consignment["values"]["exporter_eori"], "GB784419594000")
        goods = consignment["goodsItems"][0]
        self.assertEqual(goods["values"]["commodity_code"], "34029090")
        self.assertEqual(consignment["tssPayloadPreview"]["goodsItems"][0]["gross_mass_kg"], "344.00")
        self.assertEqual(consignment["tssPayloadPreview"]["goodsItems"][0]["net_mass_kg"], "300.00")
    def test_demo_mode_maps_goodman_multiline_pdf_goods_blocks(self):
        pdf_text = """
Commercial Invoice
Seller:
Goodman Bros
11-12 Regal Road
Wisbech
United Kingdom
Cambridgeshire
PE13 2RQ
Commercial Invoice Number CI-0002463301223 828718
invoices@goodman-bros.com Shipper's EORI Number GB189435365000www.goodman-bros.com
Receiver's EORI Number IE4531617P000
Buyer:
Timemark Ltd
A6 Calmount Park Delivery City Ballymount
No. of Packages: 1
D12RD88 Shipping Company: Customer Own
Ballymount Total Weight 0.066 Nett kg
Dublin 12 Shipment Weight: 165 Gross kg
Ireland
PriceDiscountPriceWeightQtyCountry of OriginDescriptionComm. CodeProduct DescriptionProduct Code
82.880%11.840.0077.00United States
Jewellery Charm
and / or jump
ring
7113110000
14/20 Yellow Gold-Filled
3.2mm Heart Link Cable
Chain. (8.27") 21 cm.
Hallmark Programme and
Anchor Protect Certified.
Chain with jump ring ready to
weld.
PJ-14201001-21CM
39.000%6.500.0306.00United States
Jewellery Charm
and / or jump
ring
7113110000
14/20 Yellow Gold-Filled
1.7mm Flat Long and Short
Chain. (8.27") 21 cm.
Hallmark Programme and
Anchor Protect Certified.
Chain with jump ring ready to
weld.
PJ-14201006-21CM
"""
        original_extract = file_introspection.extract_pdf_text_pages
        original_metadata = file_introspection.extract_pdf_metadata
        try:
            file_introspection.extract_pdf_text_pages = lambda _content: [pdf_text]
            file_introspection.extract_pdf_metadata = lambda _content: {}
            payload = portal_main.upload_consignment_preview(
                client_code="CWD",
                files=[upload_file("goodman-multiline.pdf", b"%PDF-1.7")],
                demo_mode=True,
            )
        finally:
            file_introspection.extract_pdf_text_pages = original_extract
            file_introspection.extract_pdf_metadata = original_metadata

        consignment = payload["processingPreview"]["consignments"][0]
        self.assertEqual(consignment["goodsItemCount"], 2)
        first, second = consignment["goodsItems"]
        self.assertEqual(first["values"]["commodity_code"], "7113110000")
        self.assertEqual(first["values"]["package_marks"], "PJ-14201001-21CM")
        self.assertEqual(first["values"]["country_of_origin"], "US")
        self.assertEqual(first["values"]["number_of_packages"], "7")
        self.assertEqual(first["values"]["gross_mass_kg"], "0.01")
        self.assertEqual(first["values"]["item_invoice_amount"], "82.88")
        self.assertEqual(first["tssPayload"]["gross_mass_kg"] if "tssPayload" in first else consignment["tssPayloadPreview"]["goodsItems"][0]["gross_mass_kg"], "0.01")
        self.assertEqual(second["values"]["package_marks"], "PJ-14201006-21CM")
        self.assertEqual(second["values"]["gross_mass_kg"], "0.03")
        self.assertEqual(consignment["tssPayloadPreview"]["goodsItems"][1]["item_invoice_amount"], "39.00")
    def test_demo_mode_pdf_guide_text_does_not_create_false_consignments(self):
        guide_text = """
TSS How-To Guides: TSS API Reference Published: June 2026
Copyright 2026 Trader Support Service. All rights Reserved.
Example goods item record payload for a create or an update might look like the following.
{
  "commodity_code":"0105130000",
  "gross_mass_kg":"400",
  "item_invoice_amount":"100.00",
  "package_marks":"34544421"
}
Input the Invoice Number and Number of Packages in the portal screen.
"""
        original_extract = file_introspection.extract_pdf_text_pages
        original_metadata = file_introspection.extract_pdf_metadata
        try:
            file_introspection.extract_pdf_text_pages = lambda _content: [guide_text]
            file_introspection.extract_pdf_metadata = lambda _content: {}
            payload = portal_main.upload_consignment_preview(
                client_code="PLE",
                files=[upload_file("TSS-Declaration-API-Reference-v2.9.6.pdf", b"%PDF-1.7")],
                demo_mode=True,
            )
        finally:
            file_introspection.extract_pdf_text_pages = original_extract
            file_introspection.extract_pdf_metadata = original_metadata

        self.assertEqual(payload["detectedStructure"]["format"], "pdf")
        self.assertIn("no invoice/consignment fields", payload["detectedStructure"]["warning"])
        self.assertEqual(payload["processingPreview"]["summary"]["consignmentCount"], 0)
        self.assertEqual(payload["processingPreview"]["summary"]["goodsItemCount"], 0)
    def test_demo_mode_scanned_pdf_maps_when_optional_ocr_returns_invoice_text(self):
        ocr_text = """
Commercial Invoice
Seller: Frisco UK Sales Ltd
Shipper's EORI Number GB152338719000
Invoiced To: Murdock T/A Newry Building Supplies Ltd Shipment Detail: Invoice No. 0000814877
N.W. (Kg): 39.426
G.W. (Kg): 40
Pieces: 2 ctns Customer Ref: PO000187695
Customer EORI: XI203964318000
Currency Code GBP
Code Description Qty Price Origin HS Code SKU Weight (Kg) Line Total
94160 Shelf Bracket 350x300mm WHT 20 1.43 India 8302419000 0.163 28.60
"""
        original_extract = file_introspection.extract_pdf_text_pages
        original_metadata = file_introspection.extract_pdf_metadata
        original_ocr_configured = file_introspection.pdf_ocr_configured
        original_ocr = file_introspection.extract_pdf_ocr_text_pages
        try:
            file_introspection.extract_pdf_text_pages = lambda _content: [""]
            file_introspection.extract_pdf_metadata = lambda _content: {"Title": "Scanned Frisco invoice"}
            file_introspection.pdf_ocr_configured = lambda: True
            file_introspection.extract_pdf_ocr_text_pages = lambda _content: [ocr_text]
            payload = portal_main.upload_consignment_preview(
                client_code="CWD",
                files=[upload_file("scanned-frisco-invoice.pdf", b"%PDF-1.7")],
                demo_mode=True,
            )
        finally:
            file_introspection.extract_pdf_text_pages = original_extract
            file_introspection.extract_pdf_metadata = original_metadata
            file_introspection.pdf_ocr_configured = original_ocr_configured
            file_introspection.extract_pdf_ocr_text_pages = original_ocr

        structure = payload["detectedStructure"]
        self.assertEqual(structure["format"], "pdf")
        self.assertFalse(structure["pdfRequiresOcr"])
        self.assertTrue(structure["pdfOcrConfigured"])
        self.assertTrue(structure["pdfOcrAttempted"])
        self.assertTrue(structure["pdfOcrUsed"])
        preview = payload["processingPreview"]
        self.assertEqual(preview["summary"]["consignmentCount"], 1)
        self.assertEqual(preview["summary"]["goodsItemCount"], 1)
        self.assertEqual(preview["summary"]["missingRequiredCount"], 0)
        consignment = preview["consignments"][0]
        self.assertEqual(consignment["values"]["consignment_number"], "PO000187695")
        self.assertEqual(consignment["values"]["consignee_eori"], "XI203964318000")
        self.assertEqual(consignment["values"]["exporter_eori"], "GB152338719000")
        goods = consignment["goodsItems"][0]
        self.assertEqual(goods["values"]["commodity_code"], "8302419000")
        self.assertEqual(goods["values"]["gross_mass_kg"], "3.26")
        self.assertEqual(consignment["tssPayloadPreview"]["goodsItems"][0]["gross_mass_kg"], "3.26")

    def test_demo_mode_pdf_without_extractable_text_returns_ocr_warning_and_detected_refs(self):
        original_extract = file_introspection.extract_pdf_text_pages
        original_metadata = file_introspection.extract_pdf_metadata
        original_ocr_configured = file_introspection.pdf_ocr_configured
        original_ocr = file_introspection.extract_pdf_ocr_text_pages
        try:
            file_introspection.extract_pdf_text_pages = lambda _content: [""]
            file_introspection.extract_pdf_metadata = lambda _content: {"Title": "BIRKDALE ENS Movement Pack - ENS000000002615752", "Author": "IT Synovia", "Producer": "Microsoft: Print To PDF"}
            file_introspection.pdf_ocr_configured = lambda: False
            file_introspection.extract_pdf_ocr_text_pages = lambda _content: []
            payload = portal_main.upload_consignment_preview(
                client_code="PLE",
                files=[upload_file("BIRKDALE ENS Movement Pack - ENS000000002615752.pdf", b"%PDF-1.7")],
                demo_mode=True,
            )
        finally:
            file_introspection.extract_pdf_text_pages = original_extract
            file_introspection.extract_pdf_metadata = original_metadata
            file_introspection.pdf_ocr_configured = original_ocr_configured
            file_introspection.extract_pdf_ocr_text_pages = original_ocr

        self.assertEqual(payload["detectedStructure"]["format"], "pdf")
        self.assertIn("OCR", payload["detectedStructure"]["warning"])
        self.assertTrue(payload["detectedStructure"]["pdfRequiresOcr"])
        self.assertFalse(payload["detectedStructure"]["pdfOcrConfigured"])
        self.assertFalse(payload["detectedStructure"]["pdfOcrAttempted"])
        self.assertFalse(payload["detectedStructure"]["pdfOcrUsed"])
        self.assertEqual(payload["detectedStructure"]["pdfTitle"], "BIRKDALE ENS Movement Pack - ENS000000002615752")
        self.assertEqual(payload["detectedStructure"]["pdfMetadata"]["Author"], "IT Synovia")
        self.assertEqual(payload["detectedStructure"]["pdfMetadata"]["Producer"], "Microsoft: Print To PDF")
        self.assertIn(
            {"type": "ENS", "value": "ENS000000002615752", "source": "filename"},
            payload["detectedStructure"]["pdfDetectedReferences"],
        )
        self.assertEqual(payload["processingPreview"]["summary"]["consignmentCount"], 0)
        self.assertEqual(payload["processingPreview"]["summary"]["goodsItemCount"], 0)

    def test_demo_mode_maps_tss_style_api_paths_to_consignment_and_goods(self):
        content = xlsx_content([
            ["api_field", "source_value"],
            ["request.consignment.consignmentNumber", "CON-TSS-PATH-001"],
            ["request.consignment.goodsDescription", "TSS path consignment"],
            ["request.consignment.transportDocumentNumber", "TDN-TSS-PATH-001"],
            ["request.consignment.controlledGoods", "no"],
            ["request.consignment.consignorEori", "XI111111111000"],
            ["request.consignment.consigneeEori", "GB222222222000"],
            ["request.consignment.importerEori", "XI333333333000"],
            ["request.consignment.exporterEori", "XI444444444000"],
            ["request.goodsItems[1].goodsDescription", "TSS path goods item"],
            ["request.goodsItems[1].typeOfPackages", "PK"],
            ["request.goodsItems[1].numberOfPackages", "4"],
            ["request.goodsItems[1].packageMarks", "ADDR"],
            ["request.goodsItems[1].grossMassKg", "99.5"],
            ["request.goodsItems[1].netMassKg", "95.0"],
            ["request.goodsItems[1].controlledGoods", "yes"],
        ])

        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("PLE FILE -LISBURN MANIFEST 01.05.2026 .xlsx", content)],
            demo_mode=True,
        )

        preview = payload["processingPreview"]
        consignment = preview["consignments"][0]
        goods = consignment["goodsItems"][0]
        self.assertEqual(preview["rowMode"], "api_field_value")
        self.assertEqual(preview["summary"]["mappedFieldCount"], 15)
        self.assertEqual(preview["summary"]["unmatchedFieldCount"], 0)
        self.assertEqual(consignment["status"], "READY")
        self.assertEqual(consignment["values"]["consignment_number"], "CON-TSS-PATH-001")
        self.assertEqual(goods["values"]["goods_description"], "TSS path goods item")
        self.assertEqual(goods["values"]["gross_mass_kg"], "99.50")
        self.assertEqual(goods["values"]["controlled_goods"], "yes")
        self.assertNotEqual(consignment["values"].get("controlled_goods"), "yes")

    def test_demo_mode_marks_consignment_needs_review_when_goods_weight_missing(self):
        content = xlsx_content([
            ["api_field", "source_value"],
            ["PRS.Consignment.consignment_number", "CON-LISBURN-MISSING-WEIGHT"],
            ["PRS.Consignment.goods_description", "Lisburn consignment"],
            ["PRS.Consignment.transport_document_number", "TDN-LISBURN-002"],
            ["PRS.Consignment.controlled_goods", "no"],
            ["PRS.Consignment.consignor_eori", "XI111111111000"],
            ["PRS.Consignment.consignee_eori", "GB222222222000"],
            ["PRS.Consignment.importer_eori", "XI333333333000"],
            ["PRS.Consignment.exporter_eori", "XI444444444000"],
            ["PRS.Goods_Item[1].goods_description", "Lisburn goods missing gross"],
            ["PRS.Goods_Item[1].type_of_packages", "PK"],
            ["PRS.Goods_Item[1].number_of_packages", "2"],
            ["PRS.Goods_Item[1].package_marks", "ADDR"],
            ["PRS.Goods_Item[1].net_mass_kg", "40.0"],
        ])

        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("PLE FILE -LISBURN MANIFEST 01.05.2026 .xlsx", content)],
            demo_mode=True,
        )

        preview = payload["processingPreview"]
        consignment = preview["consignments"][0]
        goods = consignment["goodsItems"][0]
        self.assertEqual(consignment["status"], "NEEDS_REVIEW")
        self.assertEqual(goods["status"], "NEEDS_REVIEW")
        self.assertIn("gross_mass_kg", goods["missingRequired"])
        self.assertEqual(preview["summary"]["missingRequiredCount"], 1)
        self.assertFalse(consignment["tssPayloadPreview"]["ready"])
        self.assertNotIn("gross_mass_kg", consignment["tssPayloadPreview"]["goodsItems"][0])
        self.assertTrue(any("Gross mass kg is required" in issue["message"] for issue in goods["issues"]))

    def test_demo_mode_splits_more_than_99_goods_into_multiple_consignments(self):
        header = (
            "consignment_number,goods_description,transport_document_number,controlled_goods,"
            "consignor_eori,consignee_eori,importer_eori,exporter_eori,"
            "type_of_packages,number_of_packages,package_marks,gross_mass_kg,net_mass_kg"
        )
        rows = [header]
        for index in range(1, 106):
            rows.append(
                f"CON-99,Goods item {index},TDN-99,no,XI111111111000,GB222222222000,"
                f"XI333333333000,XI444444444000,PK,1,ADDR,{index}.0,{index}.0"
            )
        content = "\n".join(rows).encode("utf-8")

        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("PLE-split.csv", content)],
            demo_mode=True,
        )

        preview = payload["processingPreview"]
        self.assertEqual(preview["maxGoodsPerConsignment"], 99)
        self.assertEqual(preview["summary"]["goodsItemCount"], 105)
        self.assertEqual(preview["summary"]["consignmentCount"], 2)
        self.assertEqual(preview["consignments"][0]["goodsItemCount"], 99)
        self.assertEqual(preview["consignments"][1]["goodsItemCount"], 6)
        self.assertEqual(preview["consignments"][0]["values"]["consignment_number"], "CON-99-01")
        self.assertEqual(preview["consignments"][1]["values"]["consignment_number"], "CON-99-02")
        self.assertTrue(preview["consignments"][1]["split"]["isSplit"])

    def test_demo_mode_maps_countrywide_wos_xlsx_second_attachment_and_splits_goods(self):
        headers = [
            "consignment_description",
            "trader_reference",
            "transport_document_number",
            "destination_country",
            "consignor_eori",
            "consignor_name",
            "consignor_country",
            "consignee_eori",
            "consignee_name",
            "consignee_country",
            "importer_eori",
            "importer_name",
            "importer_country",
            "exporter_eori",
            "exporter_name",
            "exporter_country",
            "commodity_code",
            "type_of_packages",
            "number_of_packages",
            "gross_mass_kg",
            "goods_description",
            "controlled_goods_type",
            "goods_domestic_status",
            "country_of_origin",
            "package_marks",
            "item_invoice_amount",
            "item_invoice_currency",
            "net_mass_kg",
            "procedure_code",
            "additional_procedure_code",
        ]
        data_rows = []
        for index in range(1, 161):
            data_rows.append([
                "CONFECTIONERY PRODUCTS",
                "ORD567945",
                "GVT1606Test3",
                "GB",
                "XI113773873000",
                "World Of Sweets",
                "GB",
                "",
                "Stewart Miller Limited",
                "GB",
                "GB113773873000",
                "World Of Sweets",
                "GB",
                "GB113773873000",
                "World Of Sweets",
                "GB",
                "1806321000",
                "Boxes",
                "1",
                "0.623",
                "WORLD OF SWEETS VARIOUS CONFECTIONERY PRODUCTS AND THE LIKE",
                "WEAPONS",
                "NIDOM",
                "US",
                f"ADDR-{index:03d}",
                "15.77",
                "GBP",
                "0.623",
                "4000",
                "000",
            ])
        content = xlsx_workbook_content([
            ("ControlledGoodsExample", [headers, *data_rows]),
            ("Next Priority Fields", [
                ["Level", "Column 2", "Column 3"],
                ["Goods Item", "document_references > document_code", "Repeatable field"],
                ["Goods Item", "tax_base_unit", ""],
            ]),
        ])

        payload = portal_main.upload_consignment_preview(
            client_code="CWD",
            files=[upload_file("first-not-selected.csv"), upload_file("CW FILE -WOS 16.04.2026.xlsx", content)],
            demo_mode=True,
        )

        self.assertEqual(payload["portalClientCode"], "CWD")
        self.assertEqual(payload["tssCredentialClientCode"], "CWF")
        self.assertEqual(payload["selectedFileOrdinal"], 2)
        self.assertEqual(payload["filename"], "CW FILE -WOS 16.04.2026.xlsx")
        self.assertEqual([item["filename"] for item in payload["ignoredFiles"]], ["first-not-selected.csv"])
        self.assertEqual(payload["detectedStructure"]["sheetNames"], ["ControlledGoodsExample", "Next Priority Fields"])
        preview = payload["processingPreview"]
        self.assertEqual(preview["rowMode"], "wide_rows")
        self.assertEqual([sheet["sheetName"] for sheet in preview["sourceSheets"]], ["ControlledGoodsExample"])
        self.assertEqual(preview["summary"]["sourceRows"], 160)
        self.assertEqual(preview["summary"]["unmatchedFieldCount"], 0)
        self.assertEqual(preview["summary"]["consignmentCount"], 2)
        self.assertEqual(preview["summary"]["goodsItemCount"], 160)
        self.assertEqual(preview["summary"]["splitConsignmentCount"], 2)
        self.assertEqual(preview["summary"]["missingRequiredCount"], 8)
        first, second = preview["consignments"]
        self.assertEqual(first["values"]["consignment_number"], "GVT1606Test3-01")
        self.assertEqual(second["values"]["consignment_number"], "GVT1606Test3-02")
        split_field = next(field for field in first["fields"] if field["field"] == "consignment_number")
        self.assertEqual(split_field["source"]["source"], "splitRule")
        self.assertTrue(split_field["source"]["assumption"])
        self.assertEqual(first["split"]["originalConsignmentNumber"], "GVT1606Test3")
        self.assertEqual(first["goodsItemCount"], 99)
        self.assertEqual(second["goodsItemCount"], 61)
        self.assertEqual(first["goodsItems"][0]["status"], "READY")
        self.assertNotIn("controlled_goods", first["missingRequired"])
        self.assertEqual(first["values"]["controlled_goods"], "no")
        controlled_field = next(field for field in first["fields"] if field["field"] == "controlled_goods")
        self.assertEqual(controlled_field["source"]["source"], "assumption")
        self.assertTrue(controlled_field["source"]["assumption"])
        self.assertIn("consignee_eori", first["missingRequired"])
        self.assertIn("consignee_city", first["missingRequired"])
        self.assertIn("consignee_postcode", first["missingRequired"])
        self.assertFalse(first["tssPayloadPreview"]["ready"])
        self.assertEqual(first["tssPayloadPreview"]["goodsItemCount"], 99)

    def test_demo_mode_formats_mass_fields_to_two_decimals_in_tss_payload_preview(self):
        manifest = "\n".join([
            "api_field,source_value",
            "consignment_number,CON-DECIMAL-1",
            "transport_document_number,TDN-DECIMAL-1",
            "controlled_goods,no",
            "consignor_eori,XI111111111000",
            "consignee_eori,GB222222222000",
            "importer_eori,XI333333333000",
            "exporter_eori,XI444444444000",
            "goods_description,Decimal goods",
            "type_of_packages,PK",
            "number_of_packages,1",
            "package_marks,ADDR",
            "gross_mass_kg,0.42599999999999999",
            "net_mass_kg,0.42599999999999999",
            "item_invoice_amount,1.236",
        ]).encode("utf-8")

        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("decimal-preview.csv", manifest)],
            demo_mode=True,
        )

        consignment = payload["processingPreview"]["consignments"][0]
        goods_payload = consignment["tssPayloadPreview"]["goodsItems"][0]
        self.assertEqual(goods_payload["gross_mass_kg"], "0.43")
        self.assertEqual(goods_payload["net_mass_kg"], "0.43")
        self.assertEqual(goods_payload["item_invoice_amount"], "1.24")

    def test_consignee_eori_is_not_required_when_full_consignee_address_is_present(self):
        manifest = "\n".join([
            "api_field,source_value",
            "consignment_number,CON-ADDRESS-1",
            "transport_document_number,TDN-ADDRESS-1",
            "controlled_goods,no",
            "consignor_eori,XI111111111000",
            "consignee_name,Stewart Miller Limited",
            "consignee_street_number,17a Cooke Road",
            "consignee_city,Newry",
            "consignee_postcode,BT35 8SA",
            "consignee_country,GB",
            "importer_eori,XI333333333000",
            "exporter_eori,XI444444444000",
            "goods_description,Address covered goods",
            "type_of_packages,PK",
            "number_of_packages,1",
            "package_marks,ADDR",
            "gross_mass_kg,1.00",
        ]).encode("utf-8")

        payload = portal_main.upload_consignment_preview(
            client_code="PLE",
            files=[upload_file("address-preview.csv", manifest)],
            demo_mode=True,
        )

        consignment = payload["processingPreview"]["consignments"][0]
        self.assertNotIn("consignee_eori", consignment["missingRequired"])
        self.assertEqual(consignment["status"], "READY")
        field_lookup = {field["field"]: field for field in consignment["fields"]}
        self.assertFalse(field_lookup["consignee_eori"]["required"])
        self.assertTrue(field_lookup["consignee_eori"]["blank"])
        self.assertFalse(field_lookup["consignee_eori"]["missing"])
        self.assertTrue(any(
            issue["severity"] == "info" and "full consignee address" in issue["message"]
            for issue in field_lookup["consignee_eori"]["issues"]
        ))
        self.assertEqual(consignment["tssPayloadPreview"]["operations"][0]["payload"]["consignee_postcode"], "BT35 8SA")

    def test_processing_preview_enriches_package_type_from_product_master(self):
        structure = {
            "columns": [{"name": "api_field"}, {"name": "source_value"}],
            "dataRows": [
                {"api_field": "PRS.Goods_Item[1].goods_id", "source_value": "B806921B"},
                {"api_field": "PRS.Goods_Item[1].goods_description", "source_value": "Source description"},
                {"api_field": "PRS.Goods_Item[1].number_of_packages", "source_value": "1"},
                {"api_field": "PRS.Goods_Item[1].package_marks", "source_value": "B806921B"},
                {"api_field": "PRS.Goods_Item[1].gross_mass_kg", "source_value": "2.5"},
            ],
        }

        preview = portal_main.build_processing_preview(
            profile=fallback_profile("BKD"),
            structure=structure,
            demo_ens=None,
            product_master={
                "B806921B": {
                    "SKU": "B806921B",
                    "ProductCode": "B806921B",
                    "CommodityCode": "1806905090",
                    "CountryOfOrigin": "GB",
                    "PackageType": "Boxes",
                }
            },
        )

        goods = preview["consignments"][0]["goodsItems"][0]
        fields = {field["field"]: field for field in goods["fields"]}
        self.assertEqual(goods["values"]["type_of_packages"], "PK")
        self.assertEqual(goods["values"]["commodity_code"], "1806905090")
        self.assertEqual(fields["type_of_packages"]["source"]["source"], "CFG.Product_Master")
        self.assertFalse(fields["type_of_packages"]["source"]["assumption"])
        self.assertTrue(fields["type_of_packages"]["source"]["normalised"])
        self.assertEqual(fields["type_of_packages"]["source"]["originalValue"], "Boxes")
        self.assertIn("Package type normalised from Boxes to PK", fields["type_of_packages"]["source"]["reason"])
        self.assertIn({"field": "type_of_packages", "source": "processingNormalisation"}, goods["enhancements"])
        self.assertNotIn("type_of_packages", goods["missingRequired"])
        self.assertGreaterEqual(preview["summary"]["enrichmentCount"], 2)
        lineage = preview["summary"]["lineageSummary"]
        self.assertGreaterEqual(lineage["counts"]["masterdata"], 1)
        self.assertGreaterEqual(lineage["counts"]["normalised"], 1)
        self.assertTrue(any(example["field"] == "type_of_packages" for example in lineage["examples"]))

    def test_processing_preview_defaults_package_type_as_visible_assumption(self):
        structure = {
            "columns": [{"name": "api_field"}, {"name": "source_value"}],
            "dataRows": [
                {"api_field": "PRS.Goods_Item[1].goods_id", "source_value": "UNKNOWN-SKU"},
                {"api_field": "PRS.Goods_Item[1].goods_description", "source_value": "Source description"},
                {"api_field": "PRS.Goods_Item[1].number_of_packages", "source_value": "1"},
                {"api_field": "PRS.Goods_Item[1].package_marks", "source_value": "UNKNOWN-SKU"},
                {"api_field": "PRS.Goods_Item[1].gross_mass_kg", "source_value": "2.5"},
            ],
        }

        preview = portal_main.build_processing_preview(
            profile=fallback_profile("BKD"),
            structure=structure,
            demo_ens=None,
            product_master={},
        )

        goods = preview["consignments"][0]["goodsItems"][0]
        fields = {field["field"]: field for field in goods["fields"]}
        self.assertEqual(goods["values"]["type_of_packages"], "PK")
        self.assertEqual(fields["type_of_packages"]["source"]["source"], "ASSUMPTION:DEFAULT_PACKAGE_TYPE")
        self.assertTrue(fields["type_of_packages"]["source"]["assumption"])
        self.assertIn("defaulted to PK", fields["type_of_packages"]["source"]["reason"])
        self.assertIn("assumed for preview", fields["type_of_packages"]["issues"][0]["message"])
        self.assertNotIn("type_of_packages", goods["missingRequired"])
        self.assertGreaterEqual(preview["summary"]["enrichmentCount"], 1)
        lineage = preview["summary"]["lineageSummary"]
        self.assertGreaterEqual(lineage["counts"]["assumption"], 1)
        self.assertTrue(any("assumption" in example["kinds"] for example in lineage["examples"]))

    def test_processing_preview_revalidate_keeps_visible_assumption_count(self):
        structure = {
            "columns": [{"name": "api_field"}, {"name": "source_value"}],
            "dataRows": [
                {"api_field": "PRS.Goods_Item[1].goods_id", "source_value": "UNKNOWN-SKU"},
                {"api_field": "PRS.Goods_Item[1].goods_description", "source_value": "Source description"},
                {"api_field": "PRS.Goods_Item[1].number_of_packages", "source_value": "1"},
                {"api_field": "PRS.Goods_Item[1].package_marks", "source_value": "UNKNOWN-SKU"},
                {"api_field": "PRS.Goods_Item[1].gross_mass_kg", "source_value": "2.5"},
                {"api_field": "PRS.Goods_Item[1].item_invoice_amount", "source_value": "1.236"},
            ],
        }
        preview = portal_main.build_processing_preview(
            profile=fallback_profile("BKD"),
            structure=structure,
            demo_ens=None,
            product_master={},
        )

        refreshed = portal_main.revalidate_processing_preview(
            preview,
            profile=fallback_profile("BKD"),
            product_master={},
            apply_enrichment=True,
        )

        self.assertGreaterEqual(refreshed["summary"]["enrichmentCount"], 4)
        refreshed_goods = refreshed["consignments"][0]["goodsItems"][0]
        fields = {field["field"]: field for field in refreshed_goods["fields"]}
        self.assertEqual(fields["type_of_packages"]["source"]["source"], "ASSUMPTION:DEFAULT_PACKAGE_TYPE")
        self.assertTrue(fields["type_of_packages"]["source"]["assumption"])
        self.assertGreaterEqual(refreshed["summary"]["lineageSummary"]["counts"]["assumption"], 1)
    def test_processing_preview_validate_replaces_assumed_package_type_from_masterdata(self):
        structure = {
            "columns": [{"name": "api_field"}, {"name": "source_value"}],
            "dataRows": [
                {"api_field": "PRS.Goods_Item[1].goods_id", "source_value": "UNKNOWN-SKU"},
                {"api_field": "PRS.Goods_Item[1].goods_description", "source_value": "Source description"},
                {"api_field": "PRS.Goods_Item[1].number_of_packages", "source_value": "1"},
                {"api_field": "PRS.Goods_Item[1].package_marks", "source_value": "UNKNOWN-SKU"},
                {"api_field": "PRS.Goods_Item[1].gross_mass_kg", "source_value": "2.5"},
                {"api_field": "PRS.Goods_Item[1].item_invoice_amount", "source_value": "1.236"},
            ],
        }
        preview = portal_main.build_processing_preview(
            profile=fallback_profile("BKD"),
            structure=structure,
            demo_ens=None,
            product_master={},
        )
        goods = preview["consignments"][0]["goodsItems"][0]
        for field in goods["fields"]:
            if field["field"] == "goods_id":
                field["value"] = "B806921B"
                field["source"] = {"source": "manualEdit", "label": "EDITED", "reason": "Edited in preview."}
        goods["values"]["goods_id"] = "B806921B"

        refreshed = portal_main.revalidate_processing_preview(
            preview,
            profile=fallback_profile("BKD"),
            product_master={
                "B806921B": {
                    "SKU": "B806921B",
                    "ProductCode": "B806921B",
                    "CommodityCode": "1806905090",
                    "CountryOfOrigin": "GB",
                    "PackageType": "Boxes",
                }
            },
            apply_enrichment=True,
        )

        refreshed_goods = refreshed["consignments"][0]["goodsItems"][0]
        fields = {field["field"]: field for field in refreshed_goods["fields"]}
        self.assertEqual(refreshed_goods["values"]["type_of_packages"], "PK")
        self.assertEqual(refreshed_goods["values"]["commodity_code"], "1806905090")
        self.assertEqual(refreshed_goods["values"]["item_invoice_amount"], "1.24")
        self.assertTrue(fields["item_invoice_amount"]["source"]["normalised"])
        self.assertEqual(fields["type_of_packages"]["source"]["source"], "CFG.Product_Master")
        self.assertFalse(fields["type_of_packages"]["source"]["assumption"])
        self.assertTrue(fields["type_of_packages"]["source"]["normalised"])
        self.assertEqual(refreshed["summary"]["lastEnrichmentMode"], "portal_edit_preview")
        self.assertGreaterEqual(refreshed["summary"]["enrichmentCount"], 2)

    def test_preview_validate_endpoint_rechecks_edited_goods_without_writes(self):
        structure = {
            "columns": [{"name": "api_field"}, {"name": "source_value"}],
            "dataRows": [
                {"api_field": "PRS.Goods_Item[1].goods_id", "source_value": "UNKNOWN-SKU"},
                {"api_field": "PRS.Goods_Item[1].goods_description", "source_value": "Source description"},
                {"api_field": "PRS.Goods_Item[1].type_of_packages", "source_value": "PK"},
                {"api_field": "PRS.Goods_Item[1].number_of_packages", "source_value": "1"},
                {"api_field": "PRS.Goods_Item[1].package_marks", "source_value": "UNKNOWN-SKU"},
                {"api_field": "PRS.Goods_Item[1].gross_mass_kg", "source_value": "2.5"},
                {"api_field": "PRS.Goods_Item[1].item_invoice_amount", "source_value": "1.236"},
            ],
        }
        preview = portal_main.build_processing_preview(
            profile=fallback_profile("BKD"),
            structure=structure,
            demo_ens=None,
            product_master={},
        )
        goods = preview["consignments"][0]["goodsItems"][0]
        for field in goods["fields"]:
            if field["field"] == "gross_mass_kg":
                field["value"] = ""
                field["source"] = {"source": "manualEdit", "label": "EDITED", "reason": "Edited in preview."}
        goods["values"]["gross_mass_kg"] = ""

        response = portal_main.validate_upload_processing_preview({
            "clientCode": "BKD",
            "demoMode": True,
            "processingPreview": preview,
        })

        refreshed = response["processingPreview"]
        refreshed_goods = refreshed["consignments"][0]["goodsItems"][0]
        self.assertFalse(response["databaseWrite"])
        self.assertFalse(response["tssWrite"])
        self.assertEqual(response["writeMode"], "preview_validation_only")
        self.assertEqual(response["validationContext"]["mode"], "portal_edit_preview")
        self.assertEqual(response["validationContext"]["productMaster"]["source"], "CFG.Product_Master")
        self.assertFalse(response["validationContext"]["productMaster"]["available"])
        self.assertIn("Demo mode", response["validationContext"]["productMaster"]["reason"])
        self.assertIn(
            "source UOM/package wording normalised through Modules.Processing",
            response["validationContext"]["packageTypeResolutionOrder"],
        )
        self.assertEqual(refreshed["summary"]["lastValidationMode"], "portal_edit_preview")
        self.assertIn("gross_mass_kg", refreshed_goods["missingRequired"])
        self.assertEqual(refreshed_goods["status"], "NEEDS_REVIEW")

    def test_demo_mode_keeps_client_file_selection_validation(self):
        with self.assertRaises(HTTPException) as ctx:
            portal_main.upload_consignment_preview(client_code="CWD", files=[upload_file("only-one.csv")], demo_mode=True)

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("requires attached file #2", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
