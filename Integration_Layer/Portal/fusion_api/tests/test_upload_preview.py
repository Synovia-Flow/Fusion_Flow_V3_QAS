import unittest
from io import BytesIO

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app import main as portal_main
from app.tss_profiles import fallback_profile


CSV_CONTENT = b"consignment_number,goods_description,transport_document_number\nCON-1,Goods,TDN-1\n"


def upload_file(filename: str, content: bytes = CSV_CONTENT) -> UploadFile:
    return UploadFile(
        BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": "text/csv"}),
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
