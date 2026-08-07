"""The consignment list must cut its page in SQL, not in Python.

Ported from Fusion_Flow_V2_BKD dev02 (3b95601, tests/test_consignment_list_pagination.py)
and adapted to the V3 PRS model and the FastAPI handler.

What is pinned here is the property that made the V2 change worth having: the
expensive hydrating query is asked for one page's ids, so its per-row cost
tracks the page rather than the tenant. A test that only checked the returned
rows would pass just as happily with the old read-everything-then-slice.
"""
import unittest

from app import main as portal_main


class FakeDb:
    """Records every statement issued so the shape of the reads can be asserted."""

    def __init__(self, *, page_ids=None, rows=None, counts=None, goods=None):
        self.page_ids = page_ids if page_ids is not None else [3, 2, 1]
        self.rows = rows if rows is not None else []
        self.counts = counts if counts is not None else [{"Status": "DRAFT", "Total": 7}]
        self.goods = goods if goods is not None else []
        self.calls = []

    def query_all(self, sql, params=()):
        self.calls.append((sql, list(params)))
        if "OFFSET" in sql:
            return [{"ConsignmentRowID": row_id} for row_id in self.page_ids]
        if "COUNT(*)" in sql:
            return list(self.counts)
        if "FROM PRS.Goods_Item" in sql:
            return list(self.goods)
        return list(self.rows)

    def sql_of(self, marker):
        return [sql for sql, _params in self.calls if marker in sql]

    def params_of(self, marker):
        return [params for sql, params in self.calls if marker in sql]


class ConsignmentListPaginationTests(unittest.TestCase):
    def setUp(self):
        self._real = {
            "query_all": portal_main.query_all,
            "object_exists": portal_main.object_exists,
            "status_vocabulary_rows": portal_main.status_vocabulary_rows,
            "summary": portal_main.build_consignment_validation_summary,
        }
        portal_main.object_exists = lambda _name: False
        portal_main.status_vocabulary_rows = lambda: []
        portal_main.build_consignment_validation_summary = lambda _row, _goods: {"status": "OK"}

    def tearDown(self):
        portal_main.query_all = self._real["query_all"]
        portal_main.object_exists = self._real["object_exists"]
        portal_main.status_vocabulary_rows = self._real["status_vocabulary_rows"]
        portal_main.build_consignment_validation_summary = self._real["summary"]

    def _run(self, db, **kwargs):
        # Called as a plain function, so FastAPI's Query() defaults never resolve;
        # every parameter has to be passed the way the router would pass it.
        call = {
            "client_code": "PLE",
            "status": "ALL",
            "q": "",
            "limit": 100,
            "page": 1,
            "page_size": None,
            "api_date_range": "all",
        }
        call.update(kwargs)
        portal_main.query_all = db.query_all
        return portal_main.consignments(**call)

    def test_unfiltered_page_picks_its_ids_before_hydrating(self):
        db = FakeDb(rows=[{"ConsignmentRowID": 3}, {"ConsignmentRowID": 2}, {"ConsignmentRowID": 1}])

        payload = self._run(db, client_code="PLE", page=2, page_size=3)

        offset_sql = db.sql_of("OFFSET")
        self.assertEqual(len(offset_sql), 1, "the page's ids must be picked by their own query")
        self.assertIn("FROM PRS.Consignment c", offset_sql[0])
        self.assertNotIn("OUTER APPLY", offset_sql[0])
        self.assertNotIn("PRS.Goods_Item", offset_sql[0])
        self.assertTrue(payload["pagination"]["sqlPaged"])

    def test_offset_and_page_size_are_bound_not_interpolated(self):
        db = FakeDb()

        self._run(db, client_code="PLE", page=4, page_size=20)

        params = db.params_of("OFFSET")[0]
        self.assertEqual(params[-2:], [60, 20], "offset and page size must be bound parameters")
        self.assertNotIn("60", db.sql_of("OFFSET")[0])

    def test_hydrating_query_is_restricted_to_the_page_ids(self):
        db = FakeDb(page_ids=[9, 8])

        self._run(db, client_code="PLE", page=1, page_size=2)

        hydrate = [sql for sql, _p in db.calls if "PRS.Goods_Item g" in sql][0]
        self.assertIn("c.ConsignmentRowID IN (?, ?)", hydrate)
        hydrate_params = [p for sql, p in db.calls if "PRS.Goods_Item g" in sql][0]
        self.assertEqual(hydrate_params[-2:], [9, 8])

    def test_page_past_the_end_asks_for_nothing(self):
        db = FakeDb(page_ids=[])

        self._run(db, client_code="PLE", page=99, page_size=20)

        hydrate = [sql for sql, _p in db.calls if "PRS.Goods_Item g" in sql][0]
        self.assertIn("AND 1 = 0", hydrate)

    def test_status_counts_ignore_the_status_filter_but_keep_the_search(self):
        db = FakeDb(counts=[{"Status": "DRAFT", "Total": 4}, {"Status": "SUBMITTED", "Total": 6}])

        payload = self._run(db, client_code="PLE", status="SUBMITTED", q="ABC", page=1, page_size=20)

        count_sql = db.sql_of("COUNT(*)")[0]
        self.assertNotIn("c.Status = ?", count_sql)
        self.assertIn("LIKE ?", count_sql)
        self.assertEqual(payload["statusCounts"], {"DRAFT": 4, "SUBMITTED": 6})
        self.assertEqual(payload["pagination"]["filteredTotal"], 6, "the filtered total follows the active tab")

    def test_date_range_filter_leaves_the_sql_paged_path(self):
        rows = [
            {"ConsignmentRowID": 1, "Status": "DRAFT", "ArrivalDateTime": "2026-08-05T09:00:00"},
            {"ConsignmentRowID": 2, "Status": "DRAFT", "ArrivalDateTime": "2020-01-01T09:00:00"},
            {"ConsignmentRowID": 3, "Status": "DRAFT", "ArrivalDateTime": None},
        ]
        db = FakeDb(rows=rows)

        payload = self._run(db, client_code="PLE", api_date_range="over_12_months", page=1, page_size=20)

        self.assertEqual(db.sql_of("OFFSET"), [], "a Python-side filter cannot page in SQL")
        self.assertFalse(payload["pagination"]["sqlPaged"])
        self.assertEqual([row["ConsignmentRowID"] for row in payload["consignments"]], [2])
        self.assertEqual(payload["pagination"]["scanCap"], portal_main.CONSIGNMENT_DATE_FILTER_SCAN_CAP)
        self.assertFalse(payload["pagination"]["scanTruncated"])

    def test_scan_truncation_is_reported_rather_than_hidden(self):
        rows = [
            {"ConsignmentRowID": index, "Status": "DRAFT", "ArrivalDateTime": "2026-08-05T09:00:00"}
            for index in range(portal_main.CONSIGNMENT_DATE_FILTER_SCAN_CAP)
        ]
        db = FakeDb(rows=rows)

        payload = self._run(db, client_code="PLE", api_date_range="this_month", page=1, page_size=20)

        self.assertTrue(payload["pagination"]["scanTruncated"])

    def test_unknown_date_range_is_treated_as_all(self):
        db = FakeDb()

        payload = self._run(db, client_code="PLE", api_date_range="banana", page=1, page_size=20)

        self.assertEqual(payload["apiDateRange"], "all")
        self.assertTrue(payload["pagination"]["sqlPaged"])

    def test_limit_still_sets_the_page_size_for_existing_callers(self):
        db = FakeDb()

        payload = self._run(db, client_code="PLE", limit=25)

        self.assertEqual(payload["pagination"]["pageSize"], 25)
        self.assertEqual(db.params_of("OFFSET")[0][-2:], [0, 25])


class SqlPageablePredicateTests(unittest.TestCase):
    def test_only_the_date_range_blocks_sql_paging(self):
        self.assertTrue(portal_main.consignment_list_is_sql_pageable("all"))
        self.assertTrue(portal_main.consignment_list_is_sql_pageable("banana"))
        self.assertFalse(portal_main.consignment_list_is_sql_pageable("this_week"))
        self.assertFalse(portal_main.consignment_list_is_sql_pageable("over_12_months"))


if __name__ == "__main__":
    unittest.main()
