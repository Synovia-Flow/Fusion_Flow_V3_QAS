import unittest

from app.tss_submission import build_consignment_submission_plan


PROFILE = {"requiresEnsBeforeSubmit": True}
CONSIGNMENT = {
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
GOODS = [{"goods_id": "1"}]


class TssSubmissionPlanTests(unittest.TestCase):
    def test_cfg_route_update_before_submit_is_ready(self):
        route = [
            {"ResourceName": "Consignment", "Endpoint": "/consignments", "HttpMethod": "POST", "OpType": "update"},
            {"ResourceName": "Consignment", "Endpoint": "/consignments", "HttpMethod": "POST", "OpType": "submit"},
        ]

        plan = build_consignment_submission_plan(profile=PROFILE, consignment=CONSIGNMENT, goods_items=GOODS, route=route)

        self.assertTrue(plan["routeIsEnsFirst"])
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["steps"][0]["operationCode"], "UPDATE_CONSIGNMENT_WITH_ENS")
        self.assertEqual(plan["steps"][0]["payload"]["declaration_number"], "ENS123")

    def test_submit_before_update_is_blocked(self):
        route = [
            {"ResourceName": "Consignment", "Endpoint": "/consignments", "HttpMethod": "POST", "OpType": "submit"},
            {"ResourceName": "Consignment", "Endpoint": "/consignments", "HttpMethod": "POST", "OpType": "update"},
        ]

        plan = build_consignment_submission_plan(profile=PROFILE, consignment=CONSIGNMENT, goods_items=GOODS, route=route)

        self.assertFalse(plan["routeIsEnsFirst"])
        self.assertFalse(plan["ready"])
        self.assertIn("UPDATE_CONSIGNMENT_WITH_ENS", plan["routeBlockers"][0])


if __name__ == "__main__":
    unittest.main()