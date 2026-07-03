import unittest

from app import main as portal_main
from app.tss_profiles import fallback_profile


READY_CONSIGNMENT = {
    "declaration_number": "ENS123",
    "consignment_number": "CON123",
    "goods_description": "Mixed goods",
    "transport_document_number": "TDN123",
    "controlled_goods": "false",
    "consignor_eori": "GB111",
    "consignee_eori": "GB222",
    "importer_eori": "GB333",
    "exporter_eori": "GB444",
}
READY_ROUTE = [
    {"operationCode": "UPDATE_CONSIGNMENT_WITH_ENS", "endpoint": "/consignments", "httpMethod": "POST"},
    {"operationCode": "SUBMIT_CONSIGNMENT", "endpoint": "/consignments", "httpMethod": "POST"},
]


class TssReadinessPayloadTests(unittest.TestCase):
    def setUp(self):
        self._original_public_connection_payload = portal_main.public_connection_payload
        self._original_query_one = portal_main.query_one
        self._original_load_consignment_submission_data = portal_main.load_consignment_submission_data
        self._original_load_submission_route = portal_main.load_submission_route

    def tearDown(self):
        portal_main.public_connection_payload = self._original_public_connection_payload
        portal_main.query_one = self._original_query_one
        portal_main.load_consignment_submission_data = self._original_load_consignment_submission_data
        portal_main.load_submission_route = self._original_load_submission_route

    def test_readiness_reports_missing_prs_candidate(self):
        profile = fallback_profile("PLE")
        portal_main.public_connection_payload = lambda _profile: {
            "portalClientCode": "PLE",
            "clientCode": "PLE",
            "route": READY_ROUTE,
        }
        portal_main.query_one = lambda *_args, **_kwargs: None

        payload = portal_main.tss_readiness_payload(profile)

        self.assertFalse(payload["ready"])
        self.assertFalse(payload["dataReady"])
        self.assertIsNone(payload["candidate"])
        self.assertIn("No PRS.Consignment rows", payload["blockers"][0])

    def test_readiness_builds_ready_ens_before_submit_plan(self):
        profile = fallback_profile("PLE")
        portal_main.public_connection_payload = lambda _profile: {
            "portalClientCode": "PLE",
            "clientCode": "PLE",
            "route": READY_ROUTE,
        }
        portal_main.query_one = lambda *_args, **_kwargs: {"ConsignmentRowID": 123, "DeclarationNumber": "ENS123", "GoodsCount": 1}
        portal_main.load_consignment_submission_data = lambda *_args, **_kwargs: (READY_CONSIGNMENT, [{"goods_id": "1"}])
        portal_main.load_submission_route = lambda _profile: READY_ROUTE

        payload = portal_main.tss_readiness_payload(profile)

        self.assertTrue(payload["ready"])
        self.assertTrue(payload["dataReady"])
        self.assertEqual(payload["candidate"]["consignmentRowId"], 123)
        self.assertTrue(payload["candidate"]["hasEnsDeclarationNumber"])
        self.assertEqual(payload["candidate"]["goodsItemCount"], 1)
        self.assertTrue(payload["plan"]["routeIsEnsFirst"])
        self.assertEqual(payload["blockers"], [])


if __name__ == "__main__":
    unittest.main()
