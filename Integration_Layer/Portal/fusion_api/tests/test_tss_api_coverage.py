import unittest

from app import main as portal_main


ROUTE_A_STEPS = [
    {"Endpoint": "/permission_grant", "OpType": "read", "ResourceName": "Permission Grant"},
    {"Endpoint": "/headers", "OpType": "create", "ResourceName": "Declaration Header"},
    {"Endpoint": "/consignments", "OpType": "create", "ResourceName": "Consignment"},
    {"Endpoint": "/goods", "OpType": "create", "ResourceName": "Goods Item"},
    {"Endpoint": "/consignments", "OpType": "submit", "ResourceName": "Consignment"},
    {"Endpoint": "/simplified_frontier_declarations", "OpType": "lookup", "ResourceName": "SFD Consignment"},
    {"Endpoint": "/gvms_gmr", "OpType": "create", "ResourceName": "GVMS GMR"},
    {"Endpoint": "/gvms_gmr", "OpType": "submit", "ResourceName": "GVMS GMR"},
    {"Endpoint": "/gvms_gmr", "OpType": "read", "ResourceName": "GVMS GMR"},
    {"Endpoint": "/supplementary_declarations", "OpType": "lookup", "ResourceName": "Supplementary Dec."},
    {"Endpoint": "/goods", "OpType": "lookup", "ResourceName": "Goods Item"},
    {"Endpoint": "/goods", "OpType": "update", "ResourceName": "Goods Item"},
    {"Endpoint": "/supplementary_declarations", "OpType": "submit", "ResourceName": "Supplementary Dec."},
]


class TssApiCoverageTests(unittest.TestCase):
    def setUp(self):
        self._original_load_tss_client_profile = portal_main.load_tss_client_profile
        self._original_resolve_tss_step = portal_main.resolve_tss_step
        self._original_credential_status = portal_main.credential_status
        self._original_tss_api_base_path = portal_main.tss_api_base_path

    def tearDown(self):
        portal_main.load_tss_client_profile = self._original_load_tss_client_profile
        portal_main.resolve_tss_step = self._original_resolve_tss_step
        portal_main.credential_status = self._original_credential_status
        portal_main.tss_api_base_path = self._original_tss_api_base_path

    def test_route_a_steps_have_public_endpoint_coverage(self):
        covered = {portal_main.tss_public_endpoint(step) for step in ROUTE_A_STEPS}

        self.assertEqual(
            covered,
            {
                "/api/tss/permission-grant",
                "/api/tss/headers",
                "/api/tss/consignments",
                "/api/tss/consignments/submit",
                "/api/tss/goods",
                "/api/tss/goods/update",
                "/api/tss/simplified-frontier-declarations",
                "/api/tss/gvms-gmr",
                "/api/tss/gvms-gmr/submit",
                "/api/tss/supplementary-declarations",
                "/api/tss/supplementary-declarations/submit",
            },
        )

    def test_tss_operation_payload_is_preview_only_by_default(self):
        portal_main.load_tss_client_profile = lambda _code, env_code=None: {
            "portalClientCode": "BKD",
            "clientCode": "BKD",
            "clientName": "Birkdale",
            "defaultRoute": "A",
        }
        portal_main.resolve_tss_step = lambda *_args, **_kwargs: {
            "StepNo": 1,
            "ResourceName": "Declaration Header",
            "Endpoint": "/headers",
            "HttpMethod": "POST",
            "OpType": "create",
        }
        portal_main.credential_status = lambda *_args, **_kwargs: {
            "credentialClientCode": "BKD",
            "envCode": "TST",
            "baseUrl": "https://api.tsstestenv.co.uk/api",
            "tssUsername": "masked-user",
            "hasPassword": True,
            "isActive": True,
        }
        portal_main.tss_api_base_path = lambda: "/x_fhmrc_tss_api/v1/tss_api"

        payload = portal_main.tss_operation_payload(
            client_code="BKD",
            endpoint="/headers",
            method="POST",
            op_type="create",
            route_code="A",
            env_code=None,
            payload={"arrival_port": "GBAUBELBELBEL"},
        )

        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["execution"], "preview_only")
        self.assertEqual(payload["credential"]["envCode"], "TST")
        self.assertEqual(
            payload["target"]["url"],
            "https://api.tsstestenv.co.uk/api/x_fhmrc_tss_api/v1/tss_api/headers",
        )
        self.assertEqual(payload["request"]["payload"]["op_type"], "create")


if __name__ == "__main__":
    unittest.main()