"""The read screens must agree with each other, because they read one source.

The portal used to answer "how many consignments" from the Release 1 database
(PRS/STG) while the operator was looking at TSS references. Those are different
worlds and they disagreed: 16 consignments in PRS against 6,167 in the mirror for
the same tenant. These tests pin that the declaration, consignment and dashboard
reads all resolve to the same tenant schema, and that the schema drift between
tenants is asked about rather than assumed.
"""
import unittest

from app import main as portal_main
from app.tenant_schema import UnknownTenant, first_present_column, tenant_has_sfd, tenant_schema


class RecordingMirror:
    def __init__(self, rows=None, refs=None, total=7, one=None):
        self.rows = rows if rows is not None else []
        self.refs = refs if refs is not None else ["ENS0002"]
        self.total = total
        self.one = one or {}
        self.calls = []

    def query_all(self, sql, params=()):
        self.calls.append((sql, list(params)))
        if "OFFSET" in sql:
            return [{"declaration_number": ref, "consignment_number": ref} for ref in self.refs]
        return list(self.rows)

    def query_one(self, sql, params=()):
        self.calls.append((sql, list(params)))
        return dict(self.one)

    def execute_scalar(self, sql, params=()):
        self.calls.append((sql, list(params)))
        return self.total

    def sql_of(self, marker):
        return [sql for sql, _params in self.calls if marker in sql]


class DeclarationsMirrorTests(unittest.TestCase):
    def setUp(self):
        self._real = {name: getattr(portal_main, name) for name in (
            "mirror_query_all", "mirror_execute_scalar", "status_vocabulary_rows",
            "ens_header_id_expression", "ens_header_loaded_at_expression",
        )}
        portal_main.status_vocabulary_rows = lambda: []
        portal_main.ens_header_id_expression = lambda _schema: "h.header_id"
        portal_main.ens_header_loaded_at_expression = lambda _schema: "h.loaded_at"

    def tearDown(self):
        for name, value in self._real.items():
            setattr(portal_main, name, value)

    def _run(self, mirror, **kwargs):
        call = {"client_code": "PLE", "limit": 100, "page": 1, "page_size": None,
                "q": "", "api_date_range": "all"}
        call.update(kwargs)
        portal_main.mirror_query_all = mirror.query_all
        portal_main.mirror_execute_scalar = mirror.execute_scalar
        return portal_main.declarations(**call)

    def test_reads_the_tenant_schema_not_the_release_1_model(self):
        mirror = RecordingMirror(rows=[{"DeclarationNumber": "ENS000000002717228"}])

        payload = self._run(mirror, client_code="CWD")

        self.assertEqual(payload["tenantSchema"], "CWF")
        hydrate = [sql for sql in mirror.sql_of("ENS_Headers") if "OFFSET" not in sql][0]
        self.assertIn("FROM [CWF].[ENS_Headers] h", hydrate)
        self.assertNotIn("PRS.", hydrate)
        self.assertNotIn("STG.", hydrate)
        self.assertEqual(payload["declarations"][0]["SourceTable"], "CWF.ENS_Headers")

    def test_page_is_cut_on_the_primary_key_with_bound_offset(self):
        mirror = RecordingMirror()

        self._run(mirror, page=3, page_size=10)

        offset_sql = mirror.sql_of("OFFSET")[0]
        self.assertIn("ORDER BY h.declaration_number DESC", offset_sql)
        params = [p for sql, p in mirror.calls if "OFFSET" in sql][0]
        self.assertEqual(params[-2:], [20, 10])

    def test_counts_come_from_the_same_tenant_schema(self):
        mirror = RecordingMirror(rows=[{"DeclarationNumber": "ENS1"}])

        self._run(mirror, client_code="BKD")

        hydrate = [sql for sql in mirror.sql_of("ENS_Headers") if "OFFSET" not in sql][0]
        self.assertIn("FROM [BKD].[Consignments] c", hydrate)
        self.assertIn("FROM [BKD].[GoodsItems] g", hydrate)

    def test_unknown_tenant_is_rejected(self):
        mirror = RecordingMirror()
        with self.assertRaises(Exception):
            self._run(mirror, client_code="ZZZ")


class DashboardMirrorTests(unittest.TestCase):
    def setUp(self):
        self._real = {name: getattr(portal_main, name) for name in (
            "mirror_query_one", "query_one", "query_all",
        )}

    def tearDown(self):
        for name, value in self._real.items():
            setattr(portal_main, name, value)

    def test_totals_come_from_the_mirror_and_ingestion_stays_local(self):
        mirror = RecordingMirror(one={"EnsHeaders": 1320, "Consignments": 6167, "GoodsItems": 11115})
        portal_main.mirror_query_one = mirror.query_one
        portal_main.query_one = lambda sql, params=(): {"InboundFiles": 22, "RawRecords": 0, "SourceEmails": 0}
        portal_main.query_all = lambda sql, params=(): []

        payload = portal_main.dashboard(client_code="BKD")

        self.assertEqual(payload["counts"]["Consignments"], 6167)
        self.assertEqual(payload["counts"]["InboundFiles"], 22)
        self.assertEqual(payload["sources"]["declarations"], "Fusion_Flow_V3 [BKD]")
        self.assertEqual(payload["sources"]["ingestion"], "Fusion_Flow_V3_QAS ING.*")
        self.assertTrue(payload["mirrorAvailable"])
        self.assertIn("FROM [BKD].[SFD_Declarations]", mirror.sql_of("SFD_Declarations")[0])

    def test_a_tenant_without_sfd_tables_is_not_queried_for_them(self):
        mirror = RecordingMirror(one={"EnsHeaders": 131, "Consignments": 746})
        portal_main.mirror_query_one = mirror.query_one
        portal_main.query_one = lambda sql, params=(): {}
        portal_main.query_all = lambda sql, params=(): []

        portal_main.dashboard(client_code="CRS")

        self.assertEqual(mirror.sql_of("FROM [CRS].[SFD_Declarations]"), [],
                         "CRS carries no SFD/SDI tables; querying them would error")


class SchemaDriftTests(unittest.TestCase):
    def test_sfd_tables_exist_only_where_they_do(self):
        self.assertTrue(tenant_has_sfd("BKD"))
        self.assertTrue(tenant_has_sfd("PLE"))
        self.assertTrue(tenant_has_sfd("CWF"))
        self.assertFalse(tenant_has_sfd("CRS"))

    def test_missing_columns_become_a_typed_null_not_a_broken_query(self):
        real = portal_main.mirror_has_column
        try:
            import app.tenant_schema as tenant_module
            tenant_module.mirror_has_column = lambda schema, table, column: column == "created_at"
            self.assertEqual(
                first_present_column("CWF", "Consignments", "c", ("consignment_id",), "int"),
                "CAST(NULL AS int)",
            )
            self.assertEqual(
                first_present_column("CWF", "Consignments", "c", ("loaded_at", "created_at"), "datetime2"),
                "c.created_at",
            )
        finally:
            import app.tenant_schema as tenant_module
            tenant_module.mirror_has_column = real

    def test_tenant_codes_resolve(self):
        self.assertEqual(tenant_schema("CWD"), "CWF")
        with self.assertRaises(UnknownTenant):
            tenant_schema("nope")


if __name__ == "__main__":
    unittest.main()
