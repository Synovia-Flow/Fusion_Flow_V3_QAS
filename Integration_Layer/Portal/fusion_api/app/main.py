from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .config import allowed_origins
from .db import DbUnavailable, execute_scalar, query_all, query_one

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
    code = (client_code or "").strip().upper()
    if len(code) != 3 or not code.isalnum():
        raise HTTPException(status_code=422, detail="client_code must be a 3 character client code.")
    return code


def safe_limit(value: int, default: int = 100, maximum: int = 500) -> int:
    if value <= 0:
        return default
    return min(value, maximum)


def db_error(exc: DbUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail={"message": str(exc), "db_available": False})


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


@app.get("/api/session")
def session(client_code: str = Query("PLE", min_length=3, max_length=3)) -> dict[str, object]:
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
def dashboard(client_code: str = Query("PLE", min_length=3, max_length=3)) -> dict[str, object]:
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
    client_code: str = Query("PLE", min_length=3, max_length=3),
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
    client_code: str = Query("PLE", min_length=3, max_length=3),
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
    code = client_code_param(client_code)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)

    return {
        "clientCode": code,
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
