import unittest

from app import db


class DbConnectionFallbackTests(unittest.TestCase):
    def test_parse_odbc_connection_string_handles_braced_driver(self):
        parsed = db._parse_connection_string(
            "Driver={ODBC Driver 17 for SQL Server};Server=tcp:example.database.windows.net,1433;"
            "Database=Fusion;Uid=user;Pwd=secret;Encrypt=yes;"
        )

        self.assertEqual(parsed["driver"], "ODBC Driver 17 for SQL Server")
        self.assertEqual(parsed["server"], "tcp:example.database.windows.net,1433")
        self.assertEqual(parsed["database"], "Fusion")
        self.assertEqual(parsed["uid"], "user")

    def test_server_and_port_normalises_azure_tcp_server(self):
        self.assertEqual(
            db._server_and_port("tcp:example.database.windows.net,1433"),
            ("example.database.windows.net", 1433),
        )

    def test_qmark_to_pyformat_leaves_literal_question_marks_alone(self):
        sql = "SELECT * FROM CFG.Clients WHERE ClientCode = ? AND Notes <> '?'"

        self.assertEqual(
            db._qmark_to_pyformat(sql),
            "SELECT * FROM CFG.Clients WHERE ClientCode = %s AND Notes <> '?'",
        )


if __name__ == "__main__":
    unittest.main()