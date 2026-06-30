from __future__ import annotations

import base64
import hashlib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .config import allowed_origins
from .db import DbUnavailable, execute, execute_scalar, query_all, query_one
from .tss_profiles import fallback_profile, fallback_profiles, normalize_portal_code

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
    try:
        if table_exists("CFG.Portal_Client_Profile"):
            profile = query_one(
                """
                SELECT PortalClientCode, ClientCode, ClientName, TssCredentialClientCode,
                       PreferredEnvCode, UploadProfileCode, RequiresEnsBeforeSubmit, IsActive, Notes
                FROM CFG.Portal_Client_Profile
                WHERE PortalClientCode = ? OR ClientCode = ? OR TssCredentialClientCode = ?
                """,
                [portal_code, portal_code, portal_code],
            )
            if profile:
                file_profile = None
                if table_exists("CFG.File_Profile"):
                    file_profile = query_one(
                        """
                        SELECT ProfileCode, FileRole, RequiredFileOrdinal, FileDisplayName,
                               AcceptedExtensions, TargetLandingTable, TargetCanonicalRoot,
                               MappingStatus, IsRequired, IsActive, Notes
                        FROM CFG.File_Profile
                        WHERE ProfileCode = ? AND IsActive = 1
                        """,
                        [profile["UploadProfileCode"]],
                    )
                return {
                    "portalClientCode": profile["PortalClientCode"],
                    "clientCode": profile["ClientCode"],
                    "clientName": profile["ClientName"],
                    "tssCredentialClientCode": profile["TssCredentialClientCode"],
                    "preferredEnvCode": profile["PreferredEnvCode"],
                    "uploadProfileCode": profile["UploadProfileCode"],
                    "requiresEnsBeforeSubmit": bool(profile["RequiresEnsBeforeSubmit"]),
                    "isActive": bool(profile["IsActive"]),
                    "notes": profile.get("Notes"),
                    "fileProfile": {
                        "profileCode": (file_profile or {}).get("ProfileCode", profile["UploadProfileCode"]),
                        "fileRole": (file_profile or {}).get("FileRole", "CONSIGNMENT_UPLOAD"),
                        "requiredFileOrdinal": (file_profile or {}).get("RequiredFileOrdinal"),
                        "fileDisplayName": (file_profile or {}).get("FileDisplayName"),
                        "acceptedExtensions": (file_profile or {}).get("AcceptedExtensions"),
                        "targetLandingTable": (file_profile or {}).get("TargetLandingTable"),
                        "targetCanonicalRoot": (file_profile or {}).get("TargetCanonicalRoot"),
                        "mappingStatus": (file_profile or {}).get("MappingStatus", "UNKNOWN"),
                        "notes": (file_profile or {}).get("Notes"),
                    },
                }
    except DbUnavailable:
        raise
    except Exception:
        # Keep API usable before the optional 013 migration is deployed.
        pass

    profile = fallback_profile(portal_code)
    if not profile:
        raise HTTPException(status_code=404, detail=f"No portal profile configured for {value}.")
    return profile


def load_submission_route(profile: dict[str, object]) -> list[dict[str, object]]:
    try:
        if table_exists("CFG.TSS_Submission_Route"):
            rows = query_all(
                """
                SELECT StepNo, OperationCode, ResourceName, Endpoint, HttpMethod, OpType, RequiresPrevious, Notes
                FROM CFG.TSS_Submission_Route
                WHERE PortalClientCode = ? AND IsActive = 1
                ORDER BY RouteCode, StepNo
                """,
                [profile["portalClientCode"]],
            )
            if rows:
                return [
                    {
                        "stepNo": row["StepNo"],
                        "operationCode": row["OperationCode"],
                        "resourceName": row["ResourceName"],
                        "endpoint": row["Endpoint"],
                        "httpMethod": row["HttpMethod"],
                        "opType": row["OpType"],
                        "requiresPrevious": row.get("RequiresPrevious"),
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
        if table_exists("CFG.Portal_Client_Profile"):
            rows = query_all(
                """
                SELECT PortalClientCode, ClientCode, ClientName, TssCredentialClientCode,
                       PreferredEnvCode, UploadProfileCode, RequiresEnsBeforeSubmit, IsActive, Notes
                FROM CFG.Portal_Client_Profile
                WHERE IsActive = 1
                ORDER BY PortalClientCode
                """
            )
            if rows:
                return {"profiles": [load_portal_profile(row["PortalClientCode"]) for row in rows], "source": "CFG.Portal_Client_Profile"}
    except DbUnavailable as exc:
        raise db_error(exc) from exc
    except Exception:
        pass
    return {"profiles": fallback_profiles(), "source": "fallback"}


@app.get("/api/file-profiles")
def file_profiles(client_code: str | None = Query(None)) -> dict[str, object]:
    profiles = [load_portal_profile(client_code)] if client_code else (portal_profiles()["profiles"])
    return {
        "profiles": [
            {
                "portalClientCode": profile["portalClientCode"],
                "clientCode": profile["clientCode"],
                "clientName": profile["clientName"],
                "uploadProfileCode": profile["uploadProfileCode"],
                "fileProfile": profile["fileProfile"],
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


@app.post("/api/uploads/consignments/preview")
def upload_consignment_preview(
    client_code: Annotated[str, Form()] = "PLE",
    file: Annotated[UploadFile, File()] = None,
) -> dict[str, object]:
    if file is None:
        raise HTTPException(status_code=422, detail="file is required.")
    profile = load_portal_profile(client_code)
    code = str(profile["clientCode"])
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)

    return {
        "portalClientCode": profile["portalClientCode"],
        "clientCode": code,
        "tssCredentialClientCode": profile["tssCredentialClientCode"],
        "uploadProfile": profile["fileProfile"],
        "filename": file.filename,
        "contentType": file.content_type,
        "sizeBytes": size,
        "sha256": digest.hexdigest(),
        "writeMode": "preview_only",
        "wouldLand": {
            "fileTable": "ING.Inbound_File",
            "rowTable": "ING.Raw_Record",
            "sourceChannel": "MANUAL",
            "status": "INGESTED",
        },
        "nextStep": "Wire this preview route to the Module 1 landing/parser before enabling DB writes.",
    }
