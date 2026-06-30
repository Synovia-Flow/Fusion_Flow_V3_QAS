import unittest
from io import BytesIO

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app import main as portal_main
from app.tss_profiles import fallback_profile


CSV_CONTENT = b"consignment_number,goods_description,transport_document_number\nCON-1,Goods,TDN-1\n"


def upload_file(filename: str) -> UploadFile:
    return UploadFile(
        BytesIO(CSV_CONTENT),
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


if __name__ == "__main__":
    unittest.main()
