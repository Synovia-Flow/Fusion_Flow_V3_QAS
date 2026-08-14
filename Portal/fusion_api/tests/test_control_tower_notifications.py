import unittest

from app import main as portal_main


class ControlTowerNotificationTests(unittest.TestCase):
    def setUp(self):
        self._real = {
            "object_exists": portal_main.object_exists,
            "safe_count": portal_main.safe_count,
            "safe_rows": portal_main.safe_rows,
        }

    def tearDown(self):
        for name, value in self._real.items():
            setattr(portal_main, name, value)

    def test_notification_settings_use_existing_application_parameters(self):
        expected_keys = {
            "ENS_RECEIVED_ENABLED",
            "CONSIGNMENTS_RECEIVED_ENABLED",
            "STAGING_FAILURES_ENABLED",
            "MOVEMENT_AUTHORISED_ENABLED",
            "STAGING_FAILURES_TO",
            "MOVEMENT_AUTHORISED_TO",
            "ENS_PACK_AUTO_TO",
        }

        mapped_keys = {key for section, key in portal_main.APP_PARAMETER_WRITE_MAP if section == "NOTIFY"}

        self.assertEqual(mapped_keys, expected_keys)
        self.assertTrue(all(("NOTIFY", key) not in portal_main.READ_ONLY_SETTING_KEYS for key in expected_keys))

    def test_control_tower_does_not_require_notification_table(self):
        portal_main.object_exists = lambda name: name != "LOG.Notification"
        portal_main.safe_count = lambda *_args, **_kwargs: 0
        portal_main.safe_rows = lambda *_args, **_kwargs: []

        payload = portal_main.control_tower(client_code="BKD", limit=20)

        self.assertFalse(payload["availability"]["LOG.Notification"])
        self.assertEqual(payload["counts"]["notifications"], 0)
        self.assertEqual(payload["notifications"], [])


if __name__ == "__main__":
    unittest.main()