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
    def col_name(index: int) -> str:
        name = ""
        index += 1
        while index:
            index, rem = divmod(index - 1, 26)
            name = chr(65 + rem) + name
        return name

    sheet_rows = []
    for row_index, row in enumerate(rows, 1):
        cells = []
        for col_index, value in enumerate(row):
            ref = f"{col_name(col_index)}{row_index}"
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""")
        archive.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""")
        archive.writestr("xl/workbook.xml", """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>""")
        archive.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""")
        archive.writestr("xl/worksheets/sheet1.xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{''.join(sheet_rows)}</sheetData></worksheet>""")
    return buffer.getvalue()


def upload_file(filename: str, content: bytes = CSV_CONTENT) -> UploadFile:
    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if filename.lower().endswith(".xlsx") else "text/csv"
    return UploadFile(
        BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


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
        self.assertFalse(preview["summary"]["databaseWrite"])
        self.assertFalse(preview["summary"]["tssWrite"])
        consignment = preview["consignments"][0]
        self.assertEqual(consignment["values"]["declaration_number"], "ENS900000000000001")
        self.assertEqual(consignment["values"]["consignment_number"], "CON-LISBURN-001")
        self.assertEqual(consignment["values"]["goods_description"], "Lisburn consignment")
        self.assertEqual(consignment["goodsItems"][0]["values"]["goods_description"], "Lisburn goods item 1")
        self.assertEqual(consignment["goodsItems"][1]["values"]["goods_description"], "Lisburn goods item 2")
        self.assertEqual(consignment["goodsItems"][1]["missingRequired"], [])

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

    def test_demo_mode_keeps_client_file_selection_validation(self):
        with self.assertRaises(HTTPException) as ctx:
            portal_main.upload_consignment_preview(client_code="CWD", files=[upload_file("only-one.csv")], demo_mode=True)

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("requires attached file #2", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
