"""Date range helpers for TSS payload-only filters.

Ported from Fusion_Flow_V2_BKD dev02 (961f2e9, tests/test_tss_api_dates.py).
"""
from datetime import date, datetime
import unittest

from app.tss_api_dates import (
    normalise_tss_api_date_range,
    parse_tss_api_datetime,
    tss_api_date_in_range,
)


class TssApiDateParsingTests(unittest.TestCase):
    def test_parses_common_tss_date_formats(self):
        self.assertEqual(parse_tss_api_datetime('08/05/2026 06:45:00'), datetime(2026, 5, 8, 6, 45))
        self.assertEqual(parse_tss_api_datetime('2026-05-08T06:45:00Z'), datetime(2026, 5, 8, 6, 45))
        self.assertEqual(parse_tss_api_datetime('2026-05-08'), datetime(2026, 5, 8, 0, 0))

    def test_parses_datetime_values_from_the_driver(self):
        self.assertEqual(parse_tss_api_datetime(datetime(2026, 5, 8, 6, 45)), datetime(2026, 5, 8, 6, 45))
        self.assertEqual(parse_tss_api_datetime(date(2026, 5, 8)), datetime(2026, 5, 8, 0, 0))

    def test_invalid_or_blank_dates_are_none(self):
        self.assertIsNone(parse_tss_api_datetime(''))
        self.assertIsNone(parse_tss_api_datetime(None))
        self.assertIsNone(parse_tss_api_datetime('not a date'))


class TssApiDateRangeTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 8, 7)

    def test_normalises_unknown_range_to_all(self):
        self.assertEqual(normalise_tss_api_date_range('banana'), 'all')
        self.assertEqual(normalise_tss_api_date_range(None), 'all')
        self.assertEqual(normalise_tss_api_date_range('THIS_WEEK'), 'this_week')

    def test_this_week_uses_current_week_window(self):
        self.assertTrue(tss_api_date_in_range('2026-08-03T00:00:00', 'this_week', today=self.today))
        self.assertTrue(tss_api_date_in_range('2026-08-07T23:59:59', 'this_week', today=self.today))
        self.assertTrue(tss_api_date_in_range('2026-08-09T23:59:59', 'this_week', today=self.today))
        self.assertFalse(tss_api_date_in_range('2026-08-02T23:59:59', 'this_week', today=self.today))
        self.assertFalse(tss_api_date_in_range('2026-08-10T00:00:00', 'this_week', today=self.today))

    def test_this_month_uses_calendar_month(self):
        self.assertTrue(tss_api_date_in_range('2026-08-31T23:59:59', 'this_month', today=self.today))
        self.assertFalse(tss_api_date_in_range('2026-09-01T00:00:00', 'this_month', today=self.today))

    def test_last_12_and_over_12_are_split(self):
        self.assertTrue(tss_api_date_in_range('2025-08-08', 'last_12_months', today=self.today))
        self.assertFalse(tss_api_date_in_range('2025-08-06', 'last_12_months', today=self.today))
        self.assertTrue(tss_api_date_in_range('2025-08-06', 'over_12_months', today=self.today))

    def test_specific_ranges_exclude_missing_dates(self):
        self.assertFalse(tss_api_date_in_range('', 'this_month', today=self.today))
        self.assertTrue(tss_api_date_in_range('', 'all', today=self.today))


if __name__ == '__main__':
    unittest.main()
