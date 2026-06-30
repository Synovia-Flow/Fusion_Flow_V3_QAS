from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .config import allowed_origins
from .db import DbUnavailable, execute, execute_scalar, query_all, query_one
from .file_introspection import inspect_upload, summarise_mapping
from .mapping_suggestions import suggest_column_mappings
from .tss_profiles import fallback_profile, fallback_profiles, normalize_portal_code, required_file_index, required_file_ordinal
from .tss_submission import build_consignment_submission_plan, post_tss_json

app = FastAPI(
    title="Fusion Flow Portal API",
    version="0.1.0",
    description="Read API for Fusion Flow V3 QAS CFG/ING/PRS portal routes.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def client_code_param(client_code: str) -> str:
    portal_code = normalize_portal_code(client_code)
    if portal_code == "CW":
        return "CWD"
    if len(portal_code) != 3 or not portal_code.isalnum():
        raise HTTPException(status_code=422, detail="client_code must be a known portal/client code.")
    profile = fallback_profile(portal_code)
    if profile:
        return str(profile["clientCode"])
    return portal_code


def safe_limit(value: int, default: int = 100, maximum: int = 500) -> int:
    if value <= 0:
        return default
    return min(value, maximum)


def db_error(exc: DbUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail={"message": str(exc), "db_available": False})


def table_exists(table_name: str) -> bool:
    return bool(execute_scalar(f"SELECT OBJECT_ID('{table_name}', 'U')"))


def fallback_route(profile: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "stepNo": 1,
            "operationCode": "UPDATE_CONSIGNMENT_WITH_ENS",
            "resourceName": "Consignment",
            "endpoint": "/consignments",
            "httpMethod": "POST",
            "opType": "update",
            "requiresPrevious": None,
            "notes": "ENS/declaration_number must be present before submit.",
        },
        {
            "stepNo": 2,
            "operationCode": "SUBMIT_CONSIGNMENT",
            "resourceName": "Consignment",
            "endpoint": "/consignments",
            "httpMethod": "POST",
            "opType": "submit",
            "requiresPrevious": "UPDATE_CONSIGNMENT_WITH_ENS",
            "notes": "Submit only after the ENS update step.",
        },
    ]


def load_portal_profile(value: str) -> dict[str, object]:
    portal_code = normalize_portal_code(value)
    profile = fallback_profile(portal_code)
    if not profile:
        raise HTTPException(status_code=404, detail=f"No portal profile configured for {value}.")

    try:
        client = query_one(
            """
            SELECT ClientCode, ClientName, SchemaName, DefaultRoute, IsAgent, IsActive, Notes
            FROM CFG.Clients
            WHERE ClientCode = ?
            """,
            [profile["clientCode"]],
        )
        if client:
            profile["clientCode"] = client["ClientCode"]
            profile["clientName"] = client.get("ClientName") or profile["clientName"]
            profile["schemaName"] = client.get("SchemaName")
            profile["defaultRoute"] = client.get("DefaultRoute")
            profile["isAgent"] = bool(client.get("IsAgent"))
            profile["isActive"] = bool(client.get("IsActive"))
            profile["notes"] = client.get("Notes") or profile.get("notes")

        credential = query_one(
            """
            SELECT TOP 1 ClientCode, EnvCode
            FROM CFG.TSS_Credential
            WHERE ClientCode = ?
            ORDER BY IsActive DESC, CASE WHEN EnvCode IN ('PRD', 'TST') THEN 0 ELSE 1 END, EnvCode
            """,
            [profile["tssCredentialClientCode"]],
        )
        if credential:
            profile["tssCredentialClientCode"] = credential["ClientCode"]
            profile["preferredEnvCode"] = credential["EnvCode"]
    except DbUnavailable:
        raise
    except Exception:
        pass

    return profile


def load_submission_route(profile: dict[str, object]) -> list[dict[str, object]]:
    try:
        rows = query_all(
            """
            SELECT StepNo, ResourceName, Endpoint, HttpMethod, OpType, WaitSeconds, Notes
            FROM CFG.API_Process_Map
            WHERE ClientCode = ? AND RouteCode = COALESCE(?, RouteCode) AND IsActive = 1
            ORDER BY StepNo
            """,
            [profile["clientCode"], profile.get("defaultRoute")],
        )
        if rows:
            return [
                {
                    "stepNo": row["StepNo"],
                    "operationCode": f"{str(row.get('ResourceName') or 'TSS').upper().replace(' ', '_')}_{str(row.get('OpType') or '').upper()}",
                    "resourceName": row["ResourceName"],
                    "endpoint": row["Endpoint"],
                    "httpMethod": row["HttpMethod"],
                    "opType": row.get("OpType"),
                    "requiresPrevious": None,
                    "waitSeconds": row.get("WaitSeconds"),
                    "notes": row.get("Notes"),
                }
                for row in rows
            ]
    except DbUnavailable:
        raise
    except Exception:
        pass
    return fallback_route(profile)


def credential_status(profile: dict[str, object], env_code: str | None = None, include_secret: bool = False) -> dict[str, object] | None:
    env = (env_code or str(profile["preferredEnvCode"])).upper()
    row = query_one(
        """
        SELECT c.ClientCode AS CredentialClientCode, c.EnvCode, c.TssUsername,
               CASE WHEN c.TssPassword IS NULL OR c.TssPassword = '' THEN 0 ELSE 1 END AS HasPassword,
               c.TssPassword, c.IsActive, c.LastVerified, c.LastStatus, c.HttpStatus,
               e.BaseUrl, e.EnvName
        FROM CFG.TSS_Credential c
        LEFT JOIN CFG.TSS_Environment e ON e.EnvCode = c.EnvCode
        WHERE c.ClientCode = ? AND c.EnvCode = ?
        """,
        [profile["tssCredentialClientCode"], env],
    )
    if not row:
        return None
    payload: dict[str, object] = {
        "credentialClientCode": row["CredentialClientCode"],
        "envCode": row["EnvCode"],
        "envName": row.get("EnvName"),
        "baseUrl": row.get("BaseUrl"),
        "tssUsername": row["TssUsername"],
        "hasPassword": bool(row["HasPassword"]),
        "isActive": bool(row["IsActive"]),
        "lastVerified": row.get("LastVerified"),
        "lastStatus": row.get("LastStatus"),
        "httpStatus": row.get("HttpStatus"),
    }
    if include_secret:
        payload["password"] = row.get("TssPassword")
    return payload


def classify_tss_status(status: int | None) -> str:
    if status is None:
        return "ERROR"
    if status == 200:
        return "PASS"
    if status in (401, 403):
        return "FAIL"
    return "REACHABLE"


def load_consignment_submission_data(consignment_row_id: int, profile: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    row = query_one(
        """
        SELECT c.*, h.declaration_number AS HeaderDeclarationNumber,
               h.arrival_date_time AS HeaderArrivalDateTime, h.MovementKey AS HeaderMovementKey
        FROM PRS.Consignment c
        LEFT JOIN PRS.ENS_Header h ON h.EnsHeaderRowID = c.EnsHeaderRowID
        WHERE c.ConsignmentRowID = ? AND c.ClientCode = ?
        """,
        [consignment_row_id, profile["clientCode"]],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Consignment was not found for this portal/client profile.")
    goods = query_all(
        """
        SELECT TOP 100 *
        FROM PRS.Goods_Item
        WHERE ConsignmentRowID = ?
        ORDER BY GoodsItemOrdinal, GoodsItemRowID
        """,
        [consignment_row_id],
    )
    return row, goods


def public_credential_payload(credential: dict[str, object] | None) -> dict[str, object] | None:
    if not credential:
        return None
    return {key: value for key, value in credential.items() if key != "password"}


def log_api_trace(client_code: str, step: dict[str, object], request_payload: dict[str, object], result: dict[str, object]) -> None:
    execute(
        """
        INSERT INTO LOG.API_Trace
            (ClientCode, ResourceName, Endpoint, HttpMethod, RequestJson, ResponseJson, StatusCode, DurationMs)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            client_code,
            str(step.get("operationCode") or "TSS"),
            str(step.get("endpoint") or ""),
            str(step.get("httpMethod") or "POST"),
            json.dumps(request_payload, ensure_ascii=False, default=str),
            str(result.get("responseText") or "")[:8000],
            result.get("statusCode"),
            result.get("durationMs"),
        ],
    )


@app.get("/api/health")
def health(check_db: bool = Query(False)) -> dict[str, object]:
    payload: dict[str, object] = {"status": "ok", "service": "fusion_portal_api"}
    if not check_db:
        payload["db_checked"] = False
        return payload
    try:
        execute_scalar("SELECT 1")
    except DbUnavailable as exc:
        payload.update({"status": "degraded", "db_checked": True, "db_available": False, "detail": str(exc)})
        return payload
    payload.update({"db_checked": True, "db_available": True})
    return payload


@app.get("/api/portal/profiles")
def portal_profiles() -> dict[str, object]:
    try:
        return {
            "profiles": [load_portal_profile(str(profile["portalClientCode"])) for profile in fallback_profiles()],
            "source": "CFG.Clients + CFG.TSS_Credential",
        }
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/file-profiles")
def file_profiles(client_code: str | None = Query(None)) -> dict[str, object]:
    profiles = [load_portal_profile(client_code)] if client_code else (portal_profiles()["profiles"])
    return {
        "profiles": [
            {
                "portalClientCode": profile["portalClientCode"],
                "clientCode": profile["clientCode"],
                "clientName": profile["clientName"],
                "fileSelection": profile["fileSelection"],
                "source": "portal_bridge",
            }
            for profile in profiles
        ]
    }


@app.get("/api/tss/connections")
def tss_connections(client_code: str | None = Query(None), env_code: str | None = Query(None)) -> dict[str, object]:
    try:
        profiles = [load_portal_profile(client_code)] if client_code else portal_profiles()["profiles"]
        connections = []
        for profile in profiles:
            credential = credential_status(profile, env_code=env_code)
            connections.append({
                "portalClientCode": profile["portalClientCode"],
                "clientCode": profile["clientCode"],
                "clientName": profile["clientName"],
                "tssCredentialClientCode": profile["tssCredentialClientCode"],
                "preferredEnvCode": profile["preferredEnvCode"],
                "requiresEnsBeforeSubmit": profile["requiresEnsBeforeSubmit"],
                "credential": credential,
                "route": load_submission_route(profile),
            })
    except DbUnavailable as exc:
        raise db_error(exc) from exc
    return {"connections": connections}


@app.get("/api/tss/route-plan")
def tss_route_plan(client_code: str = Query("PLE")) -> dict[str, object]:
    try:
        profile = load_portal_profile(client_code)
        credential = credential_status(profile)
        return {
            "profile": profile,
            "credential": credential,
            "route": load_submission_route(profile),
            "invariant": "UPDATE_CONSIGNMENT_WITH_ENS must complete before SUBMIT_CONSIGNMENT.",
        }
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.post("/api/tss/connections/test")
def test_tss_connection(client_code: str = Query("PLE"), env_code: str | None = Query(None)) -> dict[str, object]:
    try:
        profile = load_portal_profile(client_code)
        credential = credential_status(profile, env_code=env_code, include_secret=True)
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    if not credential:
        raise HTTPException(status_code=404, detail="No TSS credential row found for this portal profile/environment.")
    if not credential.get("hasPassword") or not credential.get("password"):
        raise HTTPException(status_code=409, detail="TSS credential has no password configured in CFG.TSS_Credential.")
    if not credential.get("baseUrl"):
        raise HTTPException(status_code=409, detail="TSS environment has no BaseUrl configured.")

    url = str(credential["baseUrl"]).rstrip("/") + "/choice_values/country"
    token = base64.b64encode(f"{credential['tssUsername']}:{credential['password']}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
    http_status: int | None = None
    detail = ""
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - URL comes from trusted CFG.TSS_Environment
            http_status = int(response.status)
            detail = response.read(160).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        http_status = int(error.code)
        detail = error.read(160).decode("utf-8", errors="replace")
    except urllib.error.URLError as error:
        detail = str(error.reason)[:160]

    result = classify_tss_status(http_status)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        execute(
            """
            UPDATE CFG.TSS_Credential
            SET LastVerified = SYSUTCDATETIME(), LastStatus = ?, HttpStatus = ?, UpdatedAt = SYSUTCDATETIME()
            WHERE ClientCode = ? AND EnvCode = ?
            """,
            [result, http_status, credential["credentialClientCode"], credential["envCode"]],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    return {
        "portalClientCode": profile["portalClientCode"],
        "clientCode": profile["clientCode"],
        "credentialClientCode": credential["credentialClientCode"],
        "envCode": credential["envCode"],
        "httpStatus": http_status,
        "result": result,
        "detail": detail,
        "checkedAt": checked_at,
    }


@app.post("/api/tss/consignments/{consignment_row_id}/update-ens-plan")
def update_ens_plan(consignment_row_id: int, client_code: str = Query("PLE")) -> dict[str, object]:
    try:
        profile = load_portal_profile(client_code)
        row = query_one(
            """
            SELECT c.ConsignmentRowID, c.EnsHeaderRowID, c.ClientCode, c.consignment_number,
                   c.declaration_number AS ConsignmentDeclarationNumber,
                   h.declaration_number AS HeaderDeclarationNumber,
                   h.MovementKey, h.arrival_date_time
            FROM PRS.Consignment c
            LEFT JOIN PRS.ENS_Header h ON h.EnsHeaderRowID = c.EnsHeaderRowID
            WHERE c.ConsignmentRowID = ? AND c.ClientCode = ?
            """,
            [consignment_row_id, profile["clientCode"]],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    if not row:
        raise HTTPException(status_code=404, detail="Consignment was not found for this portal/client profile.")

    ens_value = row.get("ConsignmentDeclarationNumber") or row.get("HeaderDeclarationNumber")
    return {
        "profile": profile,
        "consignment": row,
        "hasEnsDeclarationNumber": bool(ens_value),
        "ensDeclarationNumber": ens_value,
        "route": load_submission_route(profile),
        "submitAllowed": bool(ens_value),
        "invariant": "Submit is blocked until UPDATE_CONSIGNMENT_WITH_ENS has an ENS/declaration_number value.",
    }

@app.post("/api/tss/consignments/{consignment_row_id}/submit")
def submit_consignment_to_tss(
    consignment_row_id: int,
    client_code: str = Query("PLE"),
    dry_run: bool = Query(True),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        profile = load_portal_profile(client_code)
        credential = credential_status(profile, include_secret=not dry_run)
        route = load_submission_route(profile)
        consignment, goods = load_consignment_submission_data(consignment_row_id, profile)
        plan = build_consignment_submission_plan(profile=profile, consignment=consignment, goods_items=goods, route=route)
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    response: dict[str, object] = {
        "dryRun": dry_run,
        "profile": profile,
        "credential": public_credential_payload(credential),
        "consignmentRowId": consignment_row_id,
        "plan": plan,
    }
    if dry_run:
        response["execution"] = "preview_only"
        return response

    if not confirm_live:
        raise HTTPException(status_code=409, detail="Live TSS submit requires confirm_live=true. Re-run dry_run first and review payloads.")
    if not plan["ready"]:
        raise HTTPException(status_code=409, detail={"message": "Consignment is not ready for TSS submit.", "missing": plan["missing"]})
    if not credential:
        raise HTTPException(status_code=404, detail="No TSS credential row found for this portal profile/environment.")
    if not credential.get("isActive"):
        raise HTTPException(status_code=409, detail="TSS credential is not active for this portal profile/environment.")
    if not credential.get("hasPassword") or not credential.get("password"):
        raise HTTPException(status_code=409, detail="TSS credential has no password configured in CFG.TSS_Credential.")
    if not credential.get("baseUrl"):
        raise HTTPException(status_code=409, detail="TSS environment has no BaseUrl configured.")

    results = []
    for step in plan["steps"]:
        result = post_tss_json(
            base_url=str(credential["baseUrl"]),
            username=str(credential["tssUsername"]),
            password=str(credential["password"]),
            endpoint=str(step["endpoint"]),
            payload=step["payload"],
        )
        try:
            log_api_trace(str(profile["clientCode"]), step, step["payload"], result)
        except DbUnavailable as exc:
            raise db_error(exc) from exc
        results.append({
            "operationCode": step["operationCode"],
            "endpoint": step["endpoint"],
            "statusCode": result["statusCode"],
            "ok": result["ok"],
            "durationMs": result["durationMs"],
            "responseText": result["responseText"],
        })
        if not result["ok"]:
            break

    response["execution"] = "sent_to_tss"
    response["results"] = results
    response["allOk"] = all(item["ok"] for item in results) and len(results) == len(plan["steps"])
    return response

@app.get("/api/session")
def session(client_code: str = Query("PLE")) -> dict[str, object]:
    code = client_code_param(client_code)
    try:
        client = query_one(
            """
            SELECT ClientCode, ClientName, SchemaName, DefaultRoute, IsAgent, IsActive
            FROM CFG.Clients
            WHERE ClientCode = ?
            """,
            [code],
        )
        paths = query_all(
            """
            SELECT PathType, PathValue, IsActive
            FROM CFG.Folder_Paths
            WHERE ClientCode = ? AND IsActive = 1
            ORDER BY PathType
            """,
            [code],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    if not client:
        raise HTTPException(status_code=404, detail=f"Client {code} was not found in CFG.Clients.")

    return {
        "username": "synovia",
        "role": "CentralAdmin",
        "tenantCode": client["ClientCode"],
        "tenantName": client.get("ClientName") or client["ClientCode"],
        "schemaName": client.get("SchemaName"),
        "defaultRoute": client.get("DefaultRoute"),
        "isActive": bool(client.get("IsActive")),
        "folderPaths": paths,
    }


@app.get("/api/dashboard")
def dashboard(client_code: str = Query("PLE")) -> dict[str, object]:
    code = client_code_param(client_code)
    try:
        counts = query_one(
            """
            SELECT
                (SELECT COUNT(*) FROM ING.Inbound_File WHERE ClientCode = ?) AS InboundFiles,
                (SELECT COUNT(*) FROM ING.Raw_Record WHERE ClientCode = ?) AS RawRecords,
                (SELECT COUNT(*) FROM ING.Source_Email WHERE ClientCode = ?) AS SourceEmails,
                (SELECT COUNT(*) FROM PRS.ENS_Header WHERE ClientCode = ?) AS EnsHeaders,
                (SELECT COUNT(*) FROM PRS.Consignment WHERE ClientCode = ?) AS Consignments,
                (SELECT COUNT(*) FROM PRS.Goods_Item WHERE ClientCode = ?) AS GoodsItems,
                (SELECT COUNT(*) FROM PRS.Consignment WHERE ClientCode = ? AND Status IN ('READY', 'VALIDATED')) AS ReadyConsignments
            """,
            [code, code, code, code, code, code, code],
        )
        latest = query_all(
            """
            SELECT TOP 10 FileID, SourceChannel, SourceName, RowsLanded, Status, CreatedAt
            FROM ING.Inbound_File
            WHERE ClientCode = ?
            ORDER BY CreatedAt DESC, FileID DESC
            """,
            [code],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    return {"clientCode": code, "counts": counts or {}, "latestInboundFiles": latest}


@app.get("/api/ingestion/files")
def ingestion_files(
    client_code: str = Query("PLE"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, object]:
    code = client_code_param(client_code)
    top = safe_limit(limit, default=50)
    try:
        rows = query_all(
            f"""
            SELECT TOP {top}
                FileID, ExecutionID, TransactionID, ClientCode, SourceChannel, SourceName, SourcePath,
                Mailbox, Sender, ReceivedUtc, SizeBytes, ContentType, RowsLanded, Status, FailReason, CreatedAt
            FROM ING.Inbound_File
            WHERE ClientCode = ?
            ORDER BY CreatedAt DESC, FileID DESC
            """,
            [code],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc
    return {"clientCode": code, "files": rows}


@app.get("/api/consignments")
def consignments(
    client_code: str = Query("PLE"),
    status: str = Query("ALL"),
    q: str = Query(""),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, object]:
    code = client_code_param(client_code)
    top = safe_limit(limit)
    where = ["c.ClientCode = ?"]
    params: list[object] = [code]
    clean_status = status.strip().upper()
    if clean_status and clean_status != "ALL":
        where.append("c.Status = ?")
        params.append(clean_status)
    clean_q = q.strip()
    if clean_q:
        like = f"%{clean_q}%"
        where.append(
            """(
                c.consignment_number LIKE ? OR c.trader_reference LIKE ? OR
                c.transport_document_number LIKE ? OR c.goods_description LIKE ? OR
                c.consignee_name LIKE ? OR c.MovementKey LIKE ?
            )"""
        )
        params.extend([like, like, like, like, like, like])

    try:
        rows = query_all(
            f"""
            SELECT TOP {top}
                c.ConsignmentRowID,
                c.EnsHeaderRowID,
                c.ClientCode,
                COALESCE(c.Status, h.Status, 'DRAFT') AS Status,
                c.RejectReason,
                c.MovementKey,
                COALESCE(c.declaration_number, h.declaration_number) AS DeclarationNumber,
                c.consignment_number AS ConsignmentNumber,
                c.trader_reference AS TraderReference,
                c.transport_document_number AS TransportDocumentNumber,
                c.goods_description AS GoodsDescription,
                c.consignee_name AS ConsigneeName,
                c.destination_country AS DestinationCountry,
                COUNT(g.GoodsItemRowID) AS GoodsItems,
                COALESCE(SUM(g.gross_mass_kg), 0) AS GrossMassKg,
                c.UpdatedAt
            FROM PRS.Consignment c
            LEFT JOIN PRS.ENS_Header h ON h.EnsHeaderRowID = c.EnsHeaderRowID
            LEFT JOIN PRS.Goods_Item g ON g.ConsignmentRowID = c.ConsignmentRowID
            WHERE {' AND '.join(where)}
            GROUP BY
                c.ConsignmentRowID, c.EnsHeaderRowID, c.ClientCode, c.Status, h.Status,
                c.RejectReason, c.MovementKey, c.declaration_number, h.declaration_number,
                c.consignment_number, c.trader_reference, c.transport_document_number,
                c.goods_description, c.consignee_name, c.destination_country, c.UpdatedAt
            ORDER BY c.UpdatedAt DESC, c.ConsignmentRowID DESC
            """,
            params,
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    return {"clientCode": code, "consignments": rows}


@app.get("/api/consignments/{consignment_row_id}")
def consignment_detail(consignment_row_id: int) -> dict[str, object]:
    try:
        row = query_one(
            """
            SELECT
                c.*, h.declaration_number AS HeaderDeclarationNumber, h.arrival_date_time AS HeaderArrivalDateTime
            FROM PRS.Consignment c
            LEFT JOIN PRS.ENS_Header h ON h.EnsHeaderRowID = c.EnsHeaderRowID
            WHERE c.ConsignmentRowID = ?
            """,
            [consignment_row_id],
        )
        goods = query_all(
            """
            SELECT TOP 100
                GoodsItemRowID, ConsignmentRowID, GoodsItemOrdinal, Status, RejectReason,
                goods_id, commodity_code, goods_description, gross_mass_kg, net_mass_kg,
                item_invoice_amount, item_invoice_currency, SourceSalesOrderLoadID, UpdatedAt
            FROM PRS.Goods_Item
            WHERE ConsignmentRowID = ?
            ORDER BY GoodsItemOrdinal, GoodsItemRowID
            """,
            [consignment_row_id],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    if not row:
        raise HTTPException(status_code=404, detail=f"ConsignmentRowID {consignment_row_id} was not found.")
    return {"consignment": row, "goodsItems": goods}


def selected_file_ordinal(profile: dict[str, object]) -> int:
    return required_file_ordinal(profile)


def load_file_profile_column_map(profile: dict[str, object]) -> list[dict[str, object]]:
    return []


@app.post("/api/uploads/consignments/preview")
def upload_consignment_preview(
    client_code: Annotated[str, Form()] = "PLE",
    files: Annotated[list[UploadFile] | None, File()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> dict[str, object]:
    uploaded_files = list(files or [])
    if file is not None:
        uploaded_files.append(file)
    if not uploaded_files:
        raise HTTPException(status_code=422, detail="at least one file is required.")

    profile = load_portal_profile(client_code)
    code = str(profile["clientCode"])
    required_ordinal = selected_file_ordinal(profile)
    if len(uploaded_files) < required_ordinal:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{profile['portalClientCode']} requires attached file #{required_ordinal}; "
                f"only {len(uploaded_files)} file(s) were provided."
            ),
        )

    selected_file = uploaded_files[required_file_index(profile)]
    selected_content = selected_file.file.read()
    digest = hashlib.sha256(selected_content)
    size = len(selected_content)
    structure = inspect_upload(selected_file.filename, selected_content)
    column_mappings = load_file_profile_column_map(profile)
    mapping_summary = summarise_mapping(structure.get("columns", []), column_mappings)
    mapping_suggestions = suggest_column_mappings(structure.get("columns", []))

    received_files = [
        {
            "ordinal": index + 1,
            "filename": item.filename,
            "contentType": item.content_type,
            "selected": index + 1 == required_ordinal,
        }
        for index, item in enumerate(uploaded_files)
    ]

    return {
        "portalClientCode": profile["portalClientCode"],
        "clientCode": code,
        "tssCredentialClientCode": profile["tssCredentialClientCode"],
        "fileSelection": profile["fileSelection"],
        "requiredFileOrdinal": required_ordinal,
        "selectedFileOrdinal": required_ordinal,
        "selectionRule": f"Map attached file #{required_ordinal} for {profile['portalClientCode']}.",
        "receivedFiles": received_files,
        "ignoredFiles": [item for item in received_files if not item["selected"]],
        "filename": selected_file.filename,
        "contentType": selected_file.content_type,
        "sizeBytes": size,
        "sha256": digest.hexdigest(),
        "detectedStructure": structure,
        "columnMappings": column_mappings,
        "mappingSummary": mapping_summary,
        "mappingSuggestions": mapping_suggestions,
        "writeMode": "preview_only",
        "wouldLand": {
            "fileTable": "ING.Inbound_File",
            "rowTable": "ING.Raw_Record",
            "sourceChannel": "MANUAL",
            "status": "INGESTED",
        },
        "nextStep": "Use Module 1 ingestion/processing logic to land and transform rows before enabling DB writes from the portal.",
    }
