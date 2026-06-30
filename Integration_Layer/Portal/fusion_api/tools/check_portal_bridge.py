from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import query_all
from app.tss_profiles import fallback_profile, required_file_ordinal

EXPECTED = {
    "PLE": {"clientCode": "PLE", "credentialClientCode": "PLE", "envCode": "PRD", "fileOrdinal": 1},
    "CW": {"clientCode": "CWD", "credentialClientCode": "CWF", "envCode": "TST", "fileOrdinal": 2},
}
NEW_CFG_TABLES = ("Portal_Client_Profile", "File_Profile", "File_Profile_Column_Map", "TSS_Submission_Route")


def fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


def assert_equal(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        fail(f"{label}: expected {expected!r}, got {actual!r}")


def table_rows() -> list[dict[str, object]]:
    quoted = ",".join(f"'{name}'" for name in NEW_CFG_TABLES)
    return query_all(
        f"""
        SELECT s.name + '.' + t.name AS TableName
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = 'CFG' AND t.name IN ({quoted})
        ORDER BY t.name
        """
    )


def client_rows() -> dict[str, dict[str, object]]:
    rows = query_all(
        """
        SELECT ClientCode, ClientName, CAST(IsActive AS int) AS IsActive
        FROM CFG.Clients
        WHERE ClientCode IN ('PLE', 'CWD')
        """
    )
    return {str(row["ClientCode"]): row for row in rows}


def credential_rows() -> dict[tuple[str, str], dict[str, object]]:
    rows = query_all(
        """
        SELECT ClientCode, EnvCode,
               CAST(IsActive AS int) AS IsActive,
               CASE WHEN TssPassword IS NULL OR TssPassword = '' THEN 0 ELSE 1 END AS HasPassword,
               LastStatus,
               HttpStatus
        FROM CFG.TSS_Credential
        WHERE (ClientCode = 'PLE' AND EnvCode = 'PRD')
           OR (ClientCode = 'CWF' AND EnvCode = 'TST')
        """
    )
    return {(str(row["ClientCode"]), str(row["EnvCode"])): row for row in rows}


def main() -> int:
    unexpected_tables = table_rows()
    if unexpected_tables:
        fail("unexpected portal CFG tables exist: " + ", ".join(str(row["TableName"]) for row in unexpected_tables))

    clients = client_rows()
    credentials = credential_rows()
    for portal_code, expected in EXPECTED.items():
        profile = fallback_profile(portal_code)
        if not profile:
            fail(f"missing portal bridge profile for {portal_code}")

        assert_equal(f"{portal_code} data client", profile["clientCode"], expected["clientCode"])
        assert_equal(f"{portal_code} TSS credential client", profile["tssCredentialClientCode"], expected["credentialClientCode"])
        assert_equal(f"{portal_code} env", profile["preferredEnvCode"], expected["envCode"])
        assert_equal(f"{portal_code} file ordinal", required_file_ordinal(profile), expected["fileOrdinal"])

        if expected["clientCode"] not in clients:
            fail(f"CFG.Clients missing {expected['clientCode']}")

        credential_key = (expected["credentialClientCode"], expected["envCode"])
        credential = credentials.get(credential_key)
        if not credential:
            fail(f"CFG.TSS_Credential missing {credential_key[0]}/{credential_key[1]}")
        if not credential["IsActive"]:
            fail(f"CFG.TSS_Credential {credential_key[0]}/{credential_key[1]} is not active")
        if not credential["HasPassword"]:
            fail(f"CFG.TSS_Credential {credential_key[0]}/{credential_key[1]} has no password configured")

        print(
            f"OK {portal_code}: client={expected['clientCode']} "
            f"credential={credential_key[0]}/{credential_key[1]} file=#{expected['fileOrdinal']} "
            f"lastStatus={credential.get('LastStatus') or 'UNKNOWN'} http={credential.get('HttpStatus') or 'UNKNOWN'}"
        )

    print("OK no portal CFG profile tables exist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())