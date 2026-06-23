"""Write traceability events to ING.BKD_ProcessLog.

Each call records one ING-source → Fusion Flow staging transfer.
Failures are silenced so the main ingestion path is never blocked.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_TABLE = "ING.BKD_ProcessLog"
_CHECK_SQL = """
    SELECT 1
    FROM sys.tables t
    JOIN sys.schemas s ON s.schema_id = t.schema_id
    WHERE s.name = 'ING' AND t.name = 'BKD_ProcessLog'
"""


def log_event(
    cursor: Any,
    *,
    env_code: str,
    event_type: str,
    source_table: str,
    source_record_id: int | None = None,
    source_document_no: str | None = None,
    source_row_num: int | None = None,
    file_id: int | None = None,
    target_table: str | None = None,
    target_record_id: int | None = None,
    target_ref: str | None = None,
    transform_status: str = "SUCCESS",
    transform_error: str | None = None,
    processed_by: str | None = None,
) -> None:
    """Insert one row into ING.BKD_ProcessLog.

    Silently skips if the table does not exist (pre-migration 041).
    Never raises — caller must not depend on this succeeding.
    """
    try:
        cursor.execute(_CHECK_SQL)
        if not cursor.fetchone():
            return
        source_document_no = str(source_document_no or "").strip()[:50] or None

        cursor.execute(
            f"""
            INSERT INTO {_TABLE} (
                EnvCode, FileId,
                SourceTable, SourceRecordId, SourceDocumentNo, SourceRowNum,
                EventType,
                TargetTable, TargetRecordId, TargetRef,
                TransformStatus, TransformError, TransformRetryCount,
                TransformedAt, ProcessedBy
            ) VALUES (
                ?, ?,
                ?, ?, ?, ?,
                ?,
                ?, ?, ?,
                ?, ?, 0,
                SYSUTCDATETIME(), ?
            )
            """,
            [
                env_code, file_id,
                source_table, source_record_id, source_document_no, source_row_num,
                event_type,
                target_table, target_record_id, target_ref,
                transform_status, transform_error,
                processed_by,
            ],
        )
    except Exception as exc:
        log.warning("ING.BKD_ProcessLog write failed (non-fatal): %s", exc)


def log_consignment(
    cursor: Any,
    *,
    env_code: str,
    staging_cons_id: int,
    target_ref: str | None = None,
    source_table: str = "app/ingestion/stage.py",
    source_record_id: int | None = None,
    source_document_no: str | None = None,
    file_id: int | None = None,
    processed_by: str | None = "ingestion",
) -> None:
    log_event(
        cursor,
        env_code=env_code,
        event_type="INGEST_CONSIGNMENT",
        source_table=source_table,
        source_record_id=source_record_id,
        source_document_no=source_document_no,
        file_id=file_id,
        target_table="STG.BKD_ENS_Consignments",
        target_record_id=staging_cons_id,
        target_ref=target_ref,
        transform_status="SUCCESS",
        processed_by=processed_by,
    )


def log_goods(
    cursor: Any,
    *,
    env_code: str,
    staging_goods_id: int,
    staging_cons_id: int | None = None,
    source_table: str = "app/ingestion/stage.py",
    source_record_id: int | None = None,
    source_document_no: str | None = None,
    file_id: int | None = None,
    processed_by: str | None = "ingestion",
) -> None:
    log_event(
        cursor,
        env_code=env_code,
        event_type="INGEST_GOODS",
        source_table=source_table,
        source_record_id=source_record_id,
        source_document_no=source_document_no,
        file_id=file_id,
        target_table="STG.BKD_GoodsItems",
        target_record_id=staging_goods_id,
        target_ref=str(staging_cons_id) if staging_cons_id else None,
        transform_status="SUCCESS",
        processed_by=processed_by,
    )


def log_failure(
    cursor: Any,
    *,
    env_code: str,
    event_type: str,
    source_table: str,
    source_record_id: int | None = None,
    source_document_no: str | None = None,
    file_id: int | None = None,
    error: str,
    processed_by: str | None = "ingestion",
) -> None:
    log_event(
        cursor,
        env_code=env_code,
        event_type=event_type,
        source_table=source_table,
        source_record_id=source_record_id,
        source_document_no=source_document_no,
        file_id=file_id,
        transform_status="FAILED",
        transform_error=error[:2000] if error else None,
        processed_by=processed_by,
    )
