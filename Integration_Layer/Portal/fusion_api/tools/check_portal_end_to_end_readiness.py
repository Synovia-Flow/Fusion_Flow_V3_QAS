from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import query_one
from app.main import auth_login, load_consignment_submission_data, load_portal_profile, load_submission_route
from app.tss_profiles import required_file_ordinal
from app.tss_submission import build_consignment_submission_plan

EXPECTED = {
    "PLE": {
        "credentialClientCode": "PLE",
        "envCode": "PRD",
        "portalClientCode": "PLE",
        "fileOrdinal": 1,
    },
    "CW": {
        "credentialClientCode": "CWF",
        "envCode": "TST",
        "portalClientCode": "CW",
        "fileOrdinal": 2,
    },
}


def get_credential(client_code: str, env_code: str) -> dict[str, object] | None:
    return query_one(
        """
        SELECT ClientCode, EnvCode, TssUsername, TssPassword, CAST(IsActive AS int) AS IsActive,
               CASE WHEN TssPassword IS NULL OR TssPassword = '' THEN 0 ELSE 1 END AS HasPassword
        FROM CFG.TSS_Credential
        WHERE ClientCode = ? AND EnvCode = ?
        """,
        [client_code, env_code],
    )


def candidate_consignment(client_code: str) -> dict[str, object] | None:
    return query_one(
        """
        SELECT TOP 1 c.ConsignmentRowID,
               COALESCE(c.declaration_number, h.declaration_number) AS DeclarationNumber,
               g.GoodsCount
        FROM PRS.Consignment c
        LEFT JOIN PRS.ENS_Header h ON h.EnsHeaderRowID = c.EnsHeaderRowID
        OUTER APPLY (
            SELECT COUNT(*) AS GoodsCount
            FROM PRS.Goods_Item gi
            WHERE gi.ConsignmentRowID = c.ConsignmentRowID
        ) g
        WHERE c.ClientCode = ?
        ORDER BY
            CASE WHEN COALESCE(c.declaration_number, h.declaration_number) IS NOT NULL THEN 0 ELSE 1 END,
            CASE WHEN g.GoodsCount > 0 THEN 0 ELSE 1 END,
            c.ConsignmentRowID
        """,
        [client_code],
    )


def check_portal(portal_code: str, strict: bool) -> bool:
    expected = EXPECTED[portal_code]
    credential = get_credential(expected["credentialClientCode"], expected["envCode"])
    if not credential:
        print(f"FAIL {portal_code}: missing credential {expected['credentialClientCode']}/{expected['envCode']}")
        return False
    if not credential["IsActive"] or not credential["HasPassword"]:
        print(f"FAIL {portal_code}: credential inactive or password missing")
        return False

    login_payload = auth_login({"username": credential["TssUsername"], "password": credential["TssPassword"]})
    session = login_payload["session"]
    connection = login_payload["connection"]
    profile = load_portal_profile(portal_code)

    login_ok = (
        session["tenantCode"] == expected["portalClientCode"]
        and connection["tssCredentialClientCode"] == expected["credentialClientCode"]
        and connection["preferredEnvCode"] == expected["envCode"]
        and required_file_ordinal(connection) == expected["fileOrdinal"]
    )
    if not login_ok:
        print(
            f"FAIL {portal_code}: login mapped tenant={session['tenantCode']} "
            f"credential={connection['tssCredentialClientCode']}/{connection['preferredEnvCode']} "
            f"file=#{required_file_ordinal(connection)}"
        )
        return False

    print(
        f"OK {portal_code}: login tenant={session['tenantCode']} "
        f"file=#{required_file_ordinal(connection)} "
        f"credential={connection['tssCredentialClientCode']}/{connection['preferredEnvCode']}"
    )

    candidate = candidate_consignment(str(profile["clientCode"]))
    if not candidate:
        print(f"MISSING {portal_code}: no PRS.Consignment rows for data client {profile['clientCode']}")
        return not strict

    consignment, goods = load_consignment_submission_data(int(candidate["ConsignmentRowID"]), profile)
    route = load_submission_route(profile)
    plan = build_consignment_submission_plan(profile=profile, consignment=consignment, goods_items=goods, route=route)
    status = "OK" if plan["ready"] else "MISSING"
    missing = ",".join(plan["missing"]) or "none"
    print(
        f"{status} {portal_code}: dry-run candidate ConsignmentRowID={candidate['ConsignmentRowID']} "
        f"routeEnsFirst={plan['routeIsEnsFirst']} goods={plan['goodsItemCount']} "
        f"ens={'yes' if plan['ensDeclarationNumber'] else 'no'} missing={missing}"
    )
    return bool(plan["ready"]) or not strict


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit PLE/CW portal readiness without printing secrets.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when PRS dry-run data is missing or not ready.")
    args = parser.parse_args()

    ok = True
    for portal_code in ("PLE", "CW"):
        ok = check_portal(portal_code, strict=args.strict) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
