import unittest

from app.tss_profiles import fallback_profile, normalize_portal_code, portal_code_for_tss_client, required_file_index, required_file_ordinal, select_required_file


class PortalClientBridgeTests(unittest.TestCase):
    def test_primeline_uses_first_file_and_primeline_credentials(self):
        profile = fallback_profile("PLE")

        self.assertEqual(profile["portalClientCode"], "PLE")
        self.assertEqual(profile["clientCode"], "PLE")
        self.assertEqual(profile["tssCredentialClientCode"], "PLE")
        self.assertEqual(profile["preferredEnvCode"], "PRD")
        self.assertEqual(required_file_ordinal(profile), 1)
        self.assertEqual(required_file_index(profile), 0)
        self.assertTrue(profile["requiresEnsBeforeSubmit"])

    def test_countrywide_uses_second_file_and_countrywide_credentials(self):
        profile = fallback_profile("CW")

        self.assertEqual(profile["portalClientCode"], "CW")
        self.assertEqual(profile["clientCode"], "CWD")
        self.assertEqual(profile["tssCredentialClientCode"], "CWF")
        self.assertEqual(profile["preferredEnvCode"], "TST")
        self.assertEqual(required_file_ordinal(profile), 2)
        self.assertEqual(required_file_index(profile), 1)
        self.assertTrue(profile["requiresEnsBeforeSubmit"])

    def test_invalid_file_ordinal_falls_back_to_first_attachment(self):
        bad_profile = {"fileSelection": {"requiredFileOrdinal": "bad"}}
        zero_profile = {"fileSelection": {"requiredFileOrdinal": 0}}

        self.assertEqual(required_file_ordinal(bad_profile), 1)
        self.assertEqual(required_file_index(bad_profile), 0)
        self.assertEqual(required_file_ordinal(zero_profile), 1)
        self.assertEqual(required_file_index(zero_profile), 0)

    def test_primeline_selects_first_file_from_uploaded_attachments(self):
        profile = fallback_profile("PLE")

        selected = select_required_file(["primeline.xlsx", "countrywide.xlsx"], profile)

        self.assertEqual(selected, "primeline.xlsx")

    def test_countrywide_selects_second_file_from_uploaded_attachments(self):
        profile = fallback_profile("CW")

        selected = select_required_file(["primeline.xlsx", "countrywide.xlsx"], profile)

        self.assertEqual(selected, "countrywide.xlsx")

    def test_countrywide_requires_second_uploaded_attachment(self):
        profile = fallback_profile("CW")

        with self.assertRaisesRegex(ValueError, "requires attached file #2"):
            select_required_file(["only-one.xlsx"], profile)


    def test_tss_credential_clients_map_to_portal_codes(self):
        self.assertEqual(portal_code_for_tss_client("PLE"), "PLE")
        self.assertEqual(portal_code_for_tss_client("CWF"), "CW")
        self.assertEqual(portal_code_for_tss_client("CWD"), "CW")
    def test_countrywide_aliases_normalise_to_cw(self):
        for value in ("Countrywide", "Country Wide", "CWD", "CWF", "CWH"):
            with self.subTest(value=value):
                self.assertEqual(normalize_portal_code(value), "CW")


if __name__ == "__main__":
    unittest.main()