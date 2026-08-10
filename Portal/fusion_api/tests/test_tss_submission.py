import unittest

from app.tss_submission import build_consignment_submission_plan, missing_required, non_empty_payload


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


    def test_operation_code_route_update_before_submit_is_ready(self):
        route = [
            {"operationCode": "UPDATE_CONSIGNMENT_WITH_ENS", "endpoint": "/consignments", "httpMethod": "POST"},
            {"operationCode": "SUBMIT_CONSIGNMENT", "endpoint": "/consignments", "httpMethod": "POST"},
        ]

        plan = build_consignment_submission_plan(profile=PROFILE, consignment=CONSIGNMENT, goods_items=GOODS, route=route)

        self.assertTrue(plan["routeIsEnsFirst"])
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["steps"][1]["operationCode"], "SUBMIT_CONSIGNMENT")
    def test_payload_decimal_mass_fields_are_tss_safe_two_decimals(self):
        payload = non_empty_payload(
            {
                "gross_mass_kg": "0.42599999999999999",
                "net_mass_kg": "0.42599999999999999",
                "goods_description": "Decimal goods",
            },
            op_type="update",
            ens_value="ENS123",
        )

        self.assertEqual(payload["gross_mass_kg"], "0.43")
        self.assertEqual(payload["net_mass_kg"], "0.43")

    def test_consignee_eori_is_not_missing_when_full_consignee_address_exists(self):
        payload = dict(CONSIGNMENT)
        payload.pop("consignee_eori")
        payload.update({
            "consignee_name": "Stewart Miller Limited",
            "consignee_street_number": "17a Cooke Road",
            "consignee_city": "Newry",
            "consignee_postcode": "BT35 8SA",
            "consignee_country": "GB",
        })

        missing = missing_required(payload, goods_count=1)

        self.assertNotIn("consignee_eori", missing)
        self.assertEqual(missing, [])

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