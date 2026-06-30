import unittest

from app.tss_profiles import fallback_profile, normalize_portal_code


class PortalClientBridgeTests(unittest.TestCase):
    def test_primeline_uses_first_file_and_primeline_credentials(self):
        profile = fallback_profile("PLE")

        self.assertEqual(profile["portalClientCode"], "PLE")
        self.assertEqual(profile["clientCode"], "PLE")
        self.assertEqual(profile["tssCredentialClientCode"], "PLE")
        self.assertEqual(profile["preferredEnvCode"], "PRD")
        self.assertEqual(profile["fileSelection"]["requiredFileOrdinal"], 1)
        self.assertTrue(profile["requiresEnsBeforeSubmit"])

    def test_countrywide_uses_second_file_and_countrywide_credentials(self):
        profile = fallback_profile("CW")

        self.assertEqual(profile["portalClientCode"], "CW")
        self.assertEqual(profile["clientCode"], "CWD")
        self.assertEqual(profile["tssCredentialClientCode"], "CWF")
        self.assertEqual(profile["preferredEnvCode"], "TST")
        self.assertEqual(profile["fileSelection"]["requiredFileOrdinal"], 2)
        self.assertTrue(profile["requiresEnsBeforeSubmit"])

    def test_countrywide_aliases_normalise_to_cw(self):
        for value in ("Countrywide", "Country Wide", "CWD", "CWF", "CWH"):
            with self.subTest(value=value):
                self.assertEqual(normalize_portal_code(value), "CW")


if __name__ == "__main__":
    unittest.main()