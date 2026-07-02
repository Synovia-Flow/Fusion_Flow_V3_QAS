import os
import unittest
import zipfile
from html import escape
from io import BytesIO

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

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
    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if filename.lower().endswith(".xlsx") else "text/csv"
    return UploadFile(
        BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


class PortalAuthTests(unittest.TestCase):
    def test_flow_v1_login_returns_synovia_demo_session_not_primeline_tenant(self):
        original_query_one = portal_main.query_one
        old_user = os.environ.get("FLOW_V1_USER")
        old_password = os.environ.get("FLOW_V1_PASSWORD")
        try:
            portal_main.query_one = lambda *args, **kwargs: None
            os.environ["FLOW_V1_USER"] = "synovia-test"
            os.environ["FLOW_V1_PASSWORD"] = "Password2025!"

            payload = portal_main.auth_login({"username": "synovia-test", "password": "Password2025!"})
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
        self.assertEqual(payload["defaultClientCode"], "PLE")
        self.assertEqual(payload["connection"]["portalClientCode"], "PLE")
        self.assertTrue(payload["demoMode"])
        self.assertFalse(payload["databaseWrite"])
        self.assertFalse(payload["tssWrite"])


class UploadPreviewSelectionTests(unittest.TestCase):
    def setUp(self):
        self._original_load_portal_profile = portal_main.load_portal_profile
        portal_main.load_portal_profile = lambda value: fallback_profile(value)

    def tearDown(self):
        portal_main.load_portal_profile = self._original_load_portal_profile

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
        self.assertEqual(consignment["goodsItems"][0]["values"]["gross_mass_kg"], "12.5")
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
        self.assertEqual(goods["values"]["gross_mass_kg"], "99.5")
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
        self.assertEqual(consignment["tssPayloadPreview"]["operations"][0]["payload"]["consignee_postcode"], "BT35 8SA")

    def test_demo_mode_keeps_client_file_selection_validation(self):
        with self.assertRaises(HTTPException) as ctx:
            portal_main.upload_consignment_preview(client_code="CWD", files=[upload_file("only-one.csv")], demo_mode=True)

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("requires attached file #2", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
