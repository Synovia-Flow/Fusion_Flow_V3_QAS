"""The consignment list must cut its page in SQL, not in Python.

The paging technique is ported from Fusion_Flow_V2_BKD dev02 (3b95601). It is
applied here to the Fusion_Flow_V3 mirror, where the tenant IS the schema and
the only key every tenant shares is consignment_number - the TSS DEC reference.

What is pinned is the property that made the change worth having: the expensive
hydrating query is asked for one page's references, so its per-row cost tracks
the page rather than the tenant. A test that only checked the returned rows
would pass just as happily with read-everything-then-slice, which is what a
100,000-row tenant cannot afford.
"""
import unittest
from datetime import datetime

from app import main as portal_main
from app.tenant_schema import UnknownTenant, tenant_schema
from app.tss_api_dates import tss_api_date_bounds


class FakeMirror:
    """Records every statement issued so the shape of the reads can be asserted."""

    def __init__(self, *, page_refs=None, rows=None, counts=None):
        self.page_refs = page_refs if page_refs is not None else ["DEC03", "DEC02", "DEC01"]
        self.rows = rows if rows is not None else []
        self.counts = counts if counts is not None else [{"Status": "ARRIVED", "Total": 7}]
        self.calls = []

    def query_all(self, sql, params=()):
        # Dispatch on the shape of the statement, not on the tables it names: the
        # hydrating query mentions GoodsItems too, inside its count subquery.
        self.calls.append((sql, list(params)))
        if "OFFSET" in sql:
            return [{"consignment_number": ref} for ref in self.page_refs]
        if "GROUP BY" in sql:
            return list(self.counts)
        return list(self.rows)

    def sql_of(self, marker):
        return [sql for sql, _params in self.calls if marker in sql]

    def params_of(self, marker):
        return [params for sql, params in self.calls if marker in sql]


class ConsignmentListPaginationTests(unittest.TestCase):
    def setUp(self):
        self._real = {
            "mirror_query_all": portal_main.mirror_query_all,
            "status_vocabulary_rows": portal_main.status_vocabulary_rows,
            "consignment_id_expression": portal_main.consignment_id_expression,
            "consignment_loaded_at_expression": portal_main.consignment_loaded_at_expression,
            "tenant_has_sfd": portal_main.tenant_has_sfd,
        }
        portal_main.status_vocabulary_rows = lambda: []
        portal_main.consignment_id_expression = lambda _schema: "c.consignment_id"
        portal_main.consignment_loaded_at_expression = lambda _schema: "c.loaded_at"
        portal_main.tenant_has_sfd = lambda _schema: True

    def tearDown(self):
        for name, value in self._real.items():
            setattr(portal_main, name, value)

    def _run(self, mirror, **kwargs):
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
        portal_main.mirror_query_all = mirror.query_all
        return portal_main.consignments(**call)

    def test_unfiltered_page_picks_its_references_before_hydrating(self):
        mirror = FakeMirror(rows=[{"ConsignmentNumber": "DEC03"}, {"ConsignmentNumber": "DEC02"}])

        payload = self._run(mirror, page=2, page_size=3)

        offset_sql = mirror.sql_of("OFFSET")
        self.assertEqual(len(offset_sql), 1, "the page's references must be picked by their own query")
        self.assertIn("FROM [PLE].[Consignments] c", offset_sql[0])
        self.assertNotIn("JOIN", offset_sql[0])
        self.assertNotIn("GoodsItems", offset_sql[0])
        self.assertTrue(payload["pagination"]["sqlPaged"])
        self.assertEqual(payload["tenantSchema"], "PLE")

    def test_offset_and_page_size_are_bound_not_interpolated(self):
        mirror = FakeMirror()

        self._run(mirror, page=4, page_size=20)

        params = mirror.params_of("OFFSET")[0]
        self.assertEqual(params[-2:], [60, 20], "offset and page size must be bound parameters")
        self.assertNotIn("60", mirror.sql_of("OFFSET")[0])

    def test_page_is_ordered_by_the_unique_reference_so_sql_can_skip_the_sort(self):
        mirror = FakeMirror()

        self._run(mirror, page=1, page_size=20)

        self.assertIn("ORDER BY c.consignment_number DESC", mirror.sql_of("OFFSET")[0])

    def test_hydrating_query_is_restricted_to_the_page_references(self):
        mirror = FakeMirror(page_refs=["DEC000000003361160", "DEC000000003360841"])

        self._run(mirror, page=1, page_size=2)

        hydrate = [sql for sql, _p in mirror.calls if "ENS_Headers" in sql][0]
        self.assertIn("c.consignment_number IN (?, ?)", hydrate)
        hydrate_params = [p for sql, p in mirror.calls if "ENS_Headers" in sql][0]
        self.assertEqual(hydrate_params[-2:], ["DEC000000003361160", "DEC000000003360841"])

    def test_page_past_the_end_asks_for_nothing(self):
        mirror = FakeMirror(page_refs=[])

        self._run(mirror, page=99, page_size=20)

        hydrate = [sql for sql, _p in mirror.calls if "ENS_Headers" in sql][0]
        self.assertIn("AND 1 = 0", hydrate)

    def test_dec_reference_is_what_the_list_returns(self):
        mirror = FakeMirror(rows=[{"ConsignmentNumber": "DEC000000003361160", "DeclarationNumber": "ENS000000000750071"}])

        payload = self._run(mirror, page=1, page_size=20)

        row = payload["consignments"][0]
        self.assertEqual(row["ConsignmentNumber"], "DEC000000003361160")
        self.assertEqual(row["DeclarationNumber"], "ENS000000000750071")
        self.assertEqual(row["ClientCode"], "PLE")

    def test_status_counts_ignore_the_status_filter_but_keep_the_search(self):
        mirror = FakeMirror(counts=[{"Status": "ARRIVED", "Total": 4}, {"Status": "SUBMITTED", "Total": 6}])

        payload = self._run(mirror, status="SUBMITTED", q="DEC0036", page=1, page_size=20)

        count_sql = [sql for sql in mirror.sql_of("COUNT(*)") if "GROUP BY" in sql][0]
        self.assertNotIn("= ?\n", count_sql.replace("LIKE ?", ""))
        self.assertIn("LIKE ?", count_sql)
        self.assertEqual(payload["statusCounts"], {"ARRIVED": 4, "SUBMITTED": 6})
        self.assertEqual(payload["pagination"]["filteredTotal"], 6, "the filtered total follows the active tab")

    def test_date_range_is_bound_into_sql_and_still_pages(self):
        # A capped Python scan ordered by DEC reference reported "0 arrivals this
        # month" for a tenant that had them. ENS_Headers carries a real datetime2
        # alongside the dd/MM/yyyy string, so the window is a bound comparison and
        # the answer is exact.
        mirror = FakeMirror()

        payload = self._run(mirror, api_date_range="this_month", page=1, page_size=20)

        offset_sql = mirror.sql_of("OFFSET")[0]
        self.assertIn("h.arrival_date_time_utc >= ?", offset_sql)
        self.assertIn("h.arrival_date_time_utc < ?", offset_sql)
        self.assertIn("ENS_Headers", offset_sql, "the date predicate names h, so the join must be there")
        params = mirror.params_of("OFFSET")[0]
        self.assertTrue(all(isinstance(value, datetime) for value in params[:2]))
        self.assertTrue(payload["pagination"]["sqlPaged"])

    def test_date_range_bounds_match_the_shared_definition(self):
        mirror = FakeMirror()

        self._run(mirror, api_date_range="this_week", page=1, page_size=20)

        start, end = mirror.params_of("OFFSET")[0][:2]
        self.assertEqual((start, end), tss_api_date_bounds("this_week"))

    def test_counts_carry_the_date_filter_but_skip_the_join_without_it(self):
        mirror = FakeMirror()

        self._run(mirror, api_date_range="all", page=1, page_size=20)
        plain_counts = [sql for sql in mirror.sql_of("GROUP BY")][0]
        self.assertNotIn("ENS_Headers", plain_counts, "no date filter means no reason to join")

        dated = FakeMirror()
        self._run(dated, api_date_range="last_6_months", page=1, page_size=20)
        dated_counts = [sql for sql in dated.sql_of("GROUP BY")][0]
        self.assertIn("h.arrival_date_time_utc >= ?", dated_counts)

    def test_unknown_date_range_is_treated_as_all(self):
        mirror = FakeMirror()

        payload = self._run(mirror, api_date_range="banana", page=1, page_size=20)

        self.assertEqual(payload["apiDateRange"], "all")
        self.assertNotIn("arrival_date_time_utc >=", mirror.sql_of("OFFSET")[0])

    def test_limit_still_sets_the_page_size_for_existing_callers(self):
        mirror = FakeMirror()

        payload = self._run(mirror, limit=25)

        self.assertEqual(payload["pagination"]["pageSize"], 25)
        self.assertEqual(mirror.params_of("OFFSET")[0][-2:], [0, 25])


class TenantSchemaTests(unittest.TestCase):
    def test_portal_codes_resolve_to_their_mirror_schema(self):
        self.assertEqual(tenant_schema("BKD"), "BKD")
        self.assertEqual(tenant_schema("PLE"), "PLE")
        self.assertEqual(tenant_schema("CRS"), "CRS")

    def test_countrywide_portal_code_resolves_to_the_cwf_schema(self):
        self.assertEqual(tenant_schema("CWD"), "CWF")
        self.assertEqual(tenant_schema("cwd"), "CWF")
        self.assertEqual(tenant_schema("CWF"), "CWF")

    def test_unknown_tenant_is_rejected_rather_than_interpolated(self):
        # The schema name goes into the SQL text, so anything outside the
        # allowlist has to be refused rather than passed through.
        for value in ("", None, "XXX", "PLE; DROP TABLE", "dbo", "PL"):
            with self.assertRaises(UnknownTenant):
                tenant_schema(value)


class DateFilterClauseTests(unittest.TestCase):
    def test_all_produces_no_predicate(self):
        self.assertEqual(portal_main.consignment_date_filter("all"), ("", []))
        self.assertEqual(portal_main.consignment_date_filter("banana"), ("", []))

    def test_bounded_ranges_produce_a_half_open_window(self):
        clause, params = portal_main.consignment_date_filter("this_month")
        self.assertEqual(clause, "h.arrival_date_time_utc >= ? AND h.arrival_date_time_utc < ?")
        self.assertEqual(len(params), 2)
        self.assertLess(params[0], params[1])

    def test_over_12_months_is_open_ended_at_the_start(self):
        clause, params = portal_main.consignment_date_filter("over_12_months")
        self.assertEqual(clause, "h.arrival_date_time_utc < ?")
        self.assertEqual(len(params), 1)


if __name__ == "__main__":
    unittest.main()
