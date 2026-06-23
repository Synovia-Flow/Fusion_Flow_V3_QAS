"""
Ingest Blueprint — Fusion Flow V2 BKD Portal
Local document receive endpoint + queue dashboard.

Workflow:
  1. Files arrive via POST /ingest/receive (webhook from local server watchdog
     or any HTTP client that drops documents into the pipe).
  2. The router checks if the filename/content matches a known customer profile
     in BKD.DocCustomerProfile.
  3. KNOWN (TEMPLATE / DIGITAL channel) → forward to INGEST_SERVICE_URL for
     structured extraction using the configured field mapping.  The ingest
     service already knows the customer profile and can translate the document
     directly into staging data without AI assistance.
  4. UNKNOWN (UNMAPPED channel) → forward to INGEST_SERVICE_URL with an
     UNMAPPED flag so the Synovia team can configure a new profile.
  5. Every receive event is logged to BKD.DocIngestDocument (status ROUTING)
     so the queue dashboard can track it.

The /ingest/queue page shows all recent inbound documents with routing outcomes.
"""
import io
import json
import os
import logging
import contextlib
import base64
import re
import copy
from pathlib import Path
import zipfile
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import urlparse

import requests as _req
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, current_app, jsonify, session, has_app_context
)
from app.db import query_all, query_one, execute
from app.status_utils import status_filter_tabs
from app.ingestion.defaults import (
    resolve_graph_mail_settings,
    resolve_imap_settings,
    resolve_ingest_defaults,
)
from app.ingestion.email_batch import (
    build_email_metadata,
    extract_email_body_text,
    extract_supported_attachments,
    is_sales_order_workbook_attachment,
    parse_email_bytes,
)
from app.ingestion.parser import parse_birkdale_invoice_file
from app.ingestion.stage import (
    build_invoice_batch_review,
    build_goods_attach_review,
    build_goods_attach_review_from_review,
    CONSIGNMENT_REVIEW_FIELDS,
    CONSIGNMENT_REQUIRED_FIELDS,
    GOODS_REVIEW_FIELDS,
    GOODS_REQUIRED_FIELDS,
    HEADER_REVIEW_FIELDS,
    HEADER_REQUIRED_FIELDS,
    REVIEW_MODE_ATTACH_GOODS,
    refresh_review_status,
    stage_invoice_batch,
    stage_invoice_review,
    stage_goods_attach_review,
)
from app.status_utils import tss_allows_data_changes, tss_data_lock_reason
from app.tenant import TENANT_REGISTRY, get_tenant, get_tenant_by_code

log = logging.getLogger(__name__)

ingest_bp = Blueprint('ingest', __name__,
    template_folder='../../templates/ingest',
    url_prefix='/ingest')

_ING_ENV_CODE = 'PRD'


def _ing_env_code() -> str:
    """Operational ING/STG trace environment for this production branch."""
    return _ING_ENV_CODE


S = 'BKD'

AUTO_ENS_SUBMIT_FIELDS = [
    'movement_type', 'type_of_passive_transport',
    'identity_no_of_transport', 'nationality_of_transport',
    'conveyance_ref', 'arrival_date_time', 'arrival_port',
    'place_of_loading', 'place_of_unloading',
    'place_of_acceptance_same_as_loading', 'place_of_acceptance',
    'place_of_delivery_same_as_unloading', 'place_of_delivery',
    'seal_number', 'transport_charges',
    'carrier_eori', 'carrier_name', 'carrier_street_number',
    'carrier_city', 'carrier_postcode', 'carrier_country',
    'haulier_eori',
]

ALLOWED_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.xml', '.csv', '.zip', '.xlsx'}
INGEST_CREATION_REVIEW = 'review_required'
INGEST_CREATION_AUTO = 'auto_create_if_clean'
INGEST_CREATION_ALIASES = {
    'preview': INGEST_CREATION_REVIEW,
    'review': INGEST_CREATION_REVIEW,
    'review_required': INGEST_CREATION_REVIEW,
    'auto': INGEST_CREATION_AUTO,
    'auto_create': INGEST_CREATION_AUTO,
    'auto_create_if_clean': INGEST_CREATION_AUTO,
}


def _ingest_disabled_message():
    return (
        'Record creation is disabled for this tenant. Review/parsing still works, but Confirm will not '
        'create ENS, consignments or goods until Ingestion creation enabled is set to Enabled in '
        'Master Data > Company > Operational Defaults.'
    )


def _ingest_disabled_json():
    return jsonify({
        'ok': False,
        'error': _ingest_disabled_message(),
        'config_key': 'INGEST_AUTO.ENABLED',
    }), 503


def _flash_ingest_disabled():
    flash({
        'text': _ingest_disabled_message(),
        'technical_url': url_for(
            'master_data.company_edit',
            advanced='1',
            highlight='INGEST_AUTO.ENABLED',
            _anchor='ingestCreationEnabled',
        ),
        'technical_label': 'Open Operational Defaults',
    }, 'warning')


# ── Helpers ────────────────────────────────────────────────────────────────────

def _technical_ingest_url():
    return url_for('technical.index', tab='ingest')


def _flash_ingest_technical(message, category='warning'):
    flash({
        'text': message,
        'technical_url': _technical_ingest_url(),
        'technical_label': 'Technical',
    }, category)


def _is_mailbox_retryable_doc(row):
    if not row:
        return False
    channel = str(row.get('channel') or '').upper()
    filename = str(row.get('original_filename') or '').lower()
    notes = str(row.get('routing_notes') or '').lower()
    return (
        channel == 'EMAIL'
        or filename.endswith(('.xlsx', '.eml'))
        or 'graph' in notes
        or 'imap' in notes
        or 'sales orders excel' in notes
        or 'email' in notes
    )


def _default_mailbox_provider(tenant_code):
    try:
        graph_settings = resolve_graph_mail_settings(tenant_code=tenant_code)
        if graph_settings and graph_settings.enabled:
            return 'graph'
    except Exception:
        pass
    try:
        imap_settings = resolve_imap_settings(tenant_code=tenant_code)
        if imap_settings and imap_settings.enabled:
            return 'imap'
    except Exception:
        pass
    return None


def _run_mailbox_worker(provider, limit=5, tenant_code=None):
    output_buffer = io.StringIO()
    try:
        from scripts.pull_inbound_email import process_messages

        with contextlib.redirect_stdout(output_buffer):
            exit_code = process_messages(limit=limit, provider=provider, tenant_code=tenant_code)
        return exit_code, output_buffer.getvalue().strip()
    except Exception as exc:
        return 1, str(exc)


def _mailbox_worker_output_has_failures(output):
    return any(
        line.strip().startswith('FAILED ')
        for line in str(output or '').splitlines()
    )


def _mailbox_worker_output_has_skips(output):
    return any(
        line.strip().startswith('SKIPPED ')
        for line in str(output or '').splitlines()
    )


def _mailbox_worker_output_has_missing_ens(output):
    return any(
        line.strip().startswith('OK ')
        and any(token in line for token in ('ENS=None', 'ENS=---', 'ENS=0'))
        for line in str(output or '').splitlines()
    )


def _mailbox_fetch_redirect(status, tenant_code=None):
    kwargs = {'status': status}
    if tenant_code:
        kwargs['tenant_code'] = tenant_code
    return redirect(url_for('ingest.queue', **kwargs))


def _log_mailbox_fetch_event(provider, status, routing_notes, error=None):
    provider_label = (provider or 'auto').upper()
    try:
        tenant_code = (_active_tenant() or {}).get('code') or 'BKD'
    except Exception:
        tenant_code = 'BKD'
    _log_receive(
        f'Mailbox fetch {provider_label}',
        'EMAIL',
        tenant_code,
        routing_notes,
        'MAILBOX_FETCH',
        status,
        error,
    )


def _append_ingest_retry_note(doc_id, note, *, status=None, error_message=None, increment_retry=False):
    assignments = []
    if increment_retry:
        assignments.append("retry_count = ISNULL(retry_count, 0) + 1")
    assignments.append(
        """
        routing_notes = CONCAT(
            ISNULL(routing_notes, ''),
            CASE WHEN routing_notes IS NULL OR routing_notes = '' THEN '' ELSE CHAR(10) END,
            ?
        )
        """,
    )
    params = [note[:1000]]
    if status:
        assignments.append("status = ?")
        params.append(status)
    if error_message is not None:
        assignments.append("error_message = ?")
        params.append(error_message[:4000])
    assignments.append("processing_started_at = SYSUTCDATETIME()")
    params.append(doc_id)
    execute(
        f"UPDATE {S}.DocIngestDocument SET {', '.join(assignments)} WHERE id = ?",
        params,
    )


def _log_ingest_failure(filename, message, *, call_type='INGEST_REVIEW'):
    route = _route_document_channel(filename)
    routing_notes = route.get('routing_notes') or ''
    if call_type:
        routing_notes = f"{call_type}: {routing_notes}" if routing_notes else call_type
    _log_receive(
        filename,
        route.get('channel'),
        route.get('customer_code'),
        routing_notes,
        request.path,
        'FAILED',
        message,
    )


def _flash_ingest_exception(message, exc, *, call_type='INGEST_REVIEW', payload=None):
    payload = payload or {}
    filenames = payload.get('filenames') or []
    filename = payload.get('filename')
    if filename:
        filenames.append(filename)
    for item in filenames:
        _log_ingest_failure(item, str(exc), call_type=call_type)

    flash({
        'text': message,
        'technical_url': _technical_ingest_url(),
        'technical_label': 'Technical',
    }, 'danger')


def _allowed(filename):
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _get_profiles():
    """Load customer profiles from DB for routing."""
    try:
        return query_all(f"""
            SELECT id, customer_code, profile_name, filename_regex,
                   logo_fingerprint_hash, conversion_tool_sig, ocr_required,
                   priority
            FROM {S}.DocCustomerProfile
            WHERE active = 1
            ORDER BY priority ASC
        """) or []
    except Exception:
        return []


def _log_receive(filename, channel, customer_code, routing_notes, forwarded_to, status, error=None):
    """Write a row to DocIngestDocument to track this receive event."""
    try:
        execute(f"""
            INSERT INTO {S}.DocIngestDocument
                (original_filename, customer_code, status, channel,
                 routing_notes, error_message, escalated_to_synovia, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, GETUTCDATE())
        """, [
            filename[:500],
            customer_code or 'UNKNOWN',
            status,
            channel or 'UNKNOWN',
            (routing_notes or '')[:2000],
            (error or '')[:4000] if error else None,
            1 if channel == 'UNMAPPED' else 0,
        ])
    except Exception as e:
        log.warning('_log_receive failed: %s', e)


def _ing_email_message_id_from_cursor(cursor, client_code, graph_message_id):
    graph_message_id = str(graph_message_id or '').strip()
    if not graph_message_id:
        return None
    try:
        cursor.execute(
            """
            SELECT TOP 1 EmailMessageId
            FROM ING.BKD_EmailMessage
            WHERE ClientCode = ? AND GraphMessageId = ?
            ORDER BY EmailMessageId DESC
            """,
            [client_code, graph_message_id],
        )
        row = cursor.fetchone()
        return int(row[0]) if row else None
    except Exception as exc:
        log.warning('EmailMessage lookup failed for Graph id %s: %s', graph_message_id, exc)
        return None


def _ensure_ing_email_message_from_request(
    cursor,
    *,
    client_code,
    env_code,
    graph_message_id,
    subject='',
    sender_raw='',
    received_at=None,
    mailbox='',
):
    """Ensure Graph webhook posts have an ING email row for sender traceability."""
    graph_message_id = str(graph_message_id or '').strip()
    if not graph_message_id:
        return None
    try:
        cursor.execute("SELECT OBJECT_ID('ING.BKD_EmailMessage', 'U')")
        row = cursor.fetchone()
        if not row or not row[0]:
            return None

        sender_name, sender_email = parseaddr(str(sender_raw or ''))
        sender_email = (sender_email or str(sender_raw or '')).strip()[:320] or None
        sender_name = (sender_name or '').strip()[:200] or None
        subject = str(subject or '')[:500]
        mailbox = str(mailbox or '')[:320]

        cursor.execute(
            """
            SELECT TOP 1 EmailMessageId
            FROM ING.BKD_EmailMessage
            WHERE ClientCode = ? AND GraphMessageId = ?
            ORDER BY CASE WHEN EnvCode = ? THEN 0 ELSE 1 END, EmailMessageId DESC
            """,
            [client_code, graph_message_id, env_code],
        )
        existing = cursor.fetchone()
        if existing:
            email_message_id = int(existing[0])
            cursor.execute(
                """
                UPDATE ING.BKD_EmailMessage
                   SET EnvCode = ?,
                       ClientCode = ?,
                       Mailbox = COALESCE(NULLIF(?, ''), Mailbox),
                       SenderEmail = COALESCE(?, SenderEmail),
                       SenderName = COALESCE(?, SenderName),
                       Subject = COALESCE(NULLIF(?, ''), Subject),
                       ReceivedAt = COALESCE(?, ReceivedAt),
                       LoadedAt = COALESCE(LoadedAt, SYSUTCDATETIME())
                 WHERE EmailMessageId = ?
                """,
                [
                    env_code,
                    client_code,
                    mailbox,
                    sender_email,
                    sender_name,
                    subject,
                    received_at,
                    email_message_id,
                ],
            )
            return email_message_id

        cursor.execute(
            """
            INSERT INTO ING.BKD_EmailMessage (
                EnvCode, ClientCode, Mailbox, GraphMessageId,
                SenderEmail, SenderName, Subject, ReceivedAt,
                HasAttachments, AttachmentCount, AllowedSender, Skipped, LoadedAt
            )
            OUTPUT INSERTED.EmailMessageId
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, 0, SYSUTCDATETIME())
            """,
            [
                env_code,
                client_code,
                mailbox or None,
                graph_message_id[:200],
                sender_email,
                sender_name,
                subject,
                received_at,
            ],
        )
        inserted = cursor.fetchone()
        return int(inserted[0]) if inserted else None
    except Exception as exc:
        log.warning('EmailMessage ensure failed for Graph id %s: %s', graph_message_id, exc)
        return None


def _log_ing_email_message_process(
    cursor,
    *,
    client_code,
    graph_message_id,
    env_code,
    event_type,
    target_table=None,
    target_record_id=None,
    target_ref=None,
    transform_status='SUCCESS',
    transform_error=None,
):
    """Attach parser/staging trace to a body-only Graph email."""
    email_message_id = _ing_email_message_id_from_cursor(cursor, client_code, graph_message_id)
    if not email_message_id:
        if str(graph_message_id or '').strip():
            log.warning(
                'EmailMessage process trace skipped: no ING.BKD_EmailMessage row for Graph id %s',
                graph_message_id,
            )
        return
    try:
        from app.ingestion.ing_process_log import log_event
        log_event(
            cursor,
            env_code=env_code,
            event_type=event_type,
            source_table='ING.BKD_EmailMessage',
            source_record_id=email_message_id,
            source_document_no=graph_message_id,
            target_table=target_table,
            target_record_id=target_record_id,
            target_ref=target_ref,
            transform_status=transform_status,
            transform_error=transform_error,
            processed_by='graph_email',
        )
    except Exception as exc:
        log.warning('EmailMessage process trace failed: %s', exc)


def _auto_ens_payload_from_stg_header(header):
    payload = {}
    for field in AUTO_ENS_SUBMIT_FIELDS:
        value = (header or {}).get(field)
        if value not in (None, ''):
            payload[field] = value
    if payload.get('arrival_date_time') and isinstance(payload['arrival_date_time'], datetime):
        payload['arrival_date_time'] = payload['arrival_date_time'].strftime('%d/%m/%Y %H:%M:%S')
    return payload


def _json_error_payload(message, errors=None, result=None):
    payload = {'message': message}
    if errors:
        payload['errors'] = errors
    if result:
        payload['tss_result'] = {
            'http_status': result.get('http_status'),
            'status': result.get('status'),
            'message': result.get('message'),
            'reference': result.get('reference'),
        }
    return json.dumps(payload, default=str, ensure_ascii=True)


def _mark_stg_ens_auto_state(cursor, stg_header_id, client_code, sub_status, *, errors=None, message=''):
    cursor.execute(
        """
        UPDATE STG.BKD_ENS_Headers
           SET sub_status = ?,
               validation_errors_json = ?,
               last_sub_status_change = SYSUTCDATETIME(),
               updated_at = SYSUTCDATETIME()
         WHERE ClientCode = ? AND stg_header_id = ?
        """,
        [
            sub_status,
            _json_error_payload(message or 'ENS automation failed.', errors) if errors or message else None,
            client_code,
            stg_header_id,
        ],
    )


def _write_stg_ens_header_autofixes(cursor, stg_header_id, client_code, payload):
    assignments = []
    params = []
    for field in AUTO_ENS_SUBMIT_FIELDS:
        if field not in payload:
            continue
        assignments.append(f'[{field}] = ?')
        params.append(payload.get(field))
    if not assignments:
        return
    assignments.extend([
        'last_sub_status_change = SYSUTCDATETIME()',
        'updated_at = SYSUTCDATETIME()',
    ])
    cursor.execute(
        f"""
        UPDATE STG.BKD_ENS_Headers
           SET {', '.join(assignments)}
         WHERE ClientCode = ? AND stg_header_id = ?
        """,
        params + [client_code, stg_header_id],
    )


def _upsert_tss_ens_header_mirror(cursor, *, client_code, reference, status, payload, result):
    raw_json = json.dumps(result.get('response') or {}, default=str)
    cursor.execute(
        """
        MERGE TSS.BKD_ENS_Headers AS target
        USING (
            SELECT
                ? AS ClientCode,
                ? AS DeclarationNumber,
                ? AS TssStatus,
                ? AS MovementType,
                ? AS ArrivalPort,
                ? AS ArrivalDateTime,
                ? AS CarrierName,
                ? AS CarrierEori,
                ? AS RawJson
        ) AS src
        ON target.ClientCode = src.ClientCode
           AND target.DeclarationNumber = src.DeclarationNumber
        WHEN MATCHED THEN
            UPDATE SET
                TssStatus = src.TssStatus,
                MovementType = src.MovementType,
                ArrivalPort = src.ArrivalPort,
                ArrivalDateTime = src.ArrivalDateTime,
                CarrierName = src.CarrierName,
                CarrierEori = src.CarrierEori,
                RawJson = src.RawJson,
                LastSyncedAt = SYSUTCDATETIME(),
                UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (
                ClientCode, DeclarationNumber, TssStatus, MovementType,
                ArrivalPort, ArrivalDateTime, CarrierName, CarrierEori,
                RawJson, LastSyncedAt, UpdatedAt
            )
            VALUES (
                src.ClientCode, src.DeclarationNumber, src.TssStatus,
                src.MovementType, src.ArrivalPort, src.ArrivalDateTime,
                src.CarrierName, src.CarrierEori, src.RawJson,
                SYSUTCDATETIME(), SYSUTCDATETIME()
            );
        """,
        [
            client_code,
            reference,
            result.get('status') or status or '',
            payload.get('movement_type'),
            payload.get('arrival_port'),
            payload.get('arrival_date_time'),
            payload.get('carrier_name'),
            payload.get('carrier_eori'),
            raw_json,
        ],
    )


def _auto_validate_and_submit_stg_ens_header(stg_header_id, *, tenant_code):
    """Validate the new STG ENS header and submit it to TSS without operator clicks."""
    from app.db import db_cursor
    from app.data_model import insert_tss_api_exchange
    from app.ens_validation import load_choice_values, validate_ens_payload
    from app.blueprints.declarations.routes import _apply_ens_auto_fixes

    client_code = str(tenant_code or 'BKD').strip().upper()

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM STG.BKD_ENS_Headers
            WHERE ClientCode = ? AND stg_header_id = ?
            """,
            [client_code, stg_header_id],
        )
        row = cur.fetchone()
        if not row:
            return {'ok': False, 'stage': 'load', 'message': f'ENS header #{stg_header_id} not found.'}
        columns = [d[0] for d in cur.description]
        header = dict(zip(columns, row))
        if str(header.get('tss_ens_header_ref') or '').strip():
            if str(header.get('sub_status') or '').upper() != 'SUBMITTED':
                cur.execute(
                    """
                    UPDATE STG.BKD_ENS_Headers
                       SET sub_status = 'SUBMITTED',
                           validation_errors_json = NULL,
                           last_sub_status_change = SYSUTCDATETIME(),
                           updated_at = SYSUTCDATETIME()
                     WHERE ClientCode = ?
                       AND stg_header_id = ?
                       AND NULLIF(LTRIM(RTRIM(COALESCE(tss_ens_header_ref, ''))), '') IS NOT NULL
                    """,
                    [client_code, stg_header_id],
                )
            return {
                'ok': True,
                'stage': 'already_submitted',
                'reference': header.get('tss_ens_header_ref'),
                'message': f"ENS header already has TSS ref {header.get('tss_ens_header_ref')}.",
            }

        payload = _auto_ens_payload_from_stg_header(header)
        cv = load_choice_values(cur)
        errors = validate_ens_payload(payload, cv)
        auto_fixes = _apply_ens_auto_fixes(payload, errors, cv)
        if auto_fixes:
            _write_stg_ens_header_autofixes(cur, stg_header_id, client_code, payload)
            errors = validate_ens_payload(payload, cv)
        if errors:
            _mark_stg_ens_auto_state(
                cur,
                stg_header_id,
                client_code,
                'FAILED',
                errors=errors,
                message='ENS header failed automatic validation.',
            )
            return {
                'ok': False,
                'stage': 'validation',
                'payload': payload,
                'errors': errors,
                'auto_fixes': auto_fixes,
                'message': 'ENS header failed automatic validation.',
            }

        _mark_stg_ens_auto_state(cur, stg_header_id, client_code, 'VALIDATED')

    from app.tss_api import build_cfg_client
    api = build_cfg_client()
    result = api.create_header(payload)
    reference = result.get('reference') or ''

    with db_cursor() as cur:
        insert_tss_api_exchange(
            cur,
            schema_name=client_code,
            legacy_api_call_log_id=None,
            call_type='CREATE_ENS_HEADER',
            staging_id=stg_header_id,
            http_method=result.get('method') or 'POST',
            url=result.get('url') or '',
            request_payload=payload,
            http_status=result.get('http_status'),
            response_status=result.get('status'),
            response_message=result.get('message'),
            response_json=result.get('raw_response') or result.get('response'),
            duration_ms=result.get('duration_ms'),
            error_detail=None if result.get('success') else result.get('message'),
        )

        if result.get('success') and reference:
            cur.execute(
                """
                UPDATE STG.BKD_ENS_Headers
                   SET sub_status = 'SUBMITTED',
                       tss_ens_header_ref = ?,
                       tss_api_http_status = ?,
                       validation_errors_json = NULL,
                       submitted_at = COALESCE(submitted_at, SYSUTCDATETIME()),
                       stg_submitted_at = COALESCE(stg_submitted_at, SYSUTCDATETIME()),
                       last_sub_status_change = SYSUTCDATETIME(),
                       updated_at = SYSUTCDATETIME()
                 WHERE ClientCode = ? AND stg_header_id = ?
                """,
                [reference, result.get('http_status'), client_code, stg_header_id],
            )
            _upsert_tss_ens_header_mirror(
                cur,
                client_code=client_code,
                reference=reference,
                status=result.get('status') or '',
                payload=payload,
                result=result,
            )
            return {
                'ok': True,
                'stage': 'submit',
                'reference': reference,
                'payload': payload,
                'auto_fixes': auto_fixes,
                'message': f'ENS header submitted to TSS as {reference}.',
            }

        message = result.get('message') or 'TSS did not return an ENS reference.'
        cur.execute(
            """
            UPDATE STG.BKD_ENS_Headers
               SET sub_status = 'FAILED',
                   tss_api_http_status = ?,
                   validation_errors_json = ?,
                   last_sub_status_change = SYSUTCDATETIME(),
                   updated_at = SYSUTCDATETIME()
             WHERE ClientCode = ? AND stg_header_id = ?
            """,
            [
                result.get('http_status'),
                _json_error_payload('ENS header TSS submit failed.', result=result, errors=[message]),
                client_code,
                stg_header_id,
            ],
        )
        return {
            'ok': False,
            'stage': 'submit',
            'payload': payload,
            'result': result,
            'auto_fixes': auto_fixes,
            'message': message,
        }


def _complete_ens_header_auto_submit(
    stg_header_id,
    *,
    tenant_code,
    env_code,
    graph_message_id='',
    subject='',
    summary=None,
):
    """Run ENS validation/submission after the webhook has returned to Graph."""
    from app.db import db_cursor

    summary = dict(summary or {})
    auto_submit = {}
    try:
        auto_submit = _auto_validate_and_submit_stg_ens_header(
            stg_header_id,
            tenant_code=tenant_code,
        )
    except Exception as exc:
        friendly_error = _friendly_ingest_error(exc)
        log.exception('ENS DETAILS automatic submit failed: %s', friendly_error)
        auto_submit = {
            'ok': False,
            'stage': 'submit_exception',
            'message': friendly_error,
        }
        try:
            with db_cursor() as cur:
                _mark_stg_ens_auto_state(
                    cur,
                    stg_header_id,
                    tenant_code,
                    'FAILED',
                    errors=[friendly_error],
                    message='ENS automatic submission failed.',
                )
        except Exception as _state_exc:
            log.warning('Could not mark ENS auto submit exception state: %s', _state_exc)

    try:
        with db_cursor() as cur:
            _log_ing_email_message_process(
                cur,
                client_code=tenant_code,
                graph_message_id=graph_message_id,
                env_code=env_code,
                event_type='AUTO_SUBMIT_ENS_HEADER',
                target_table='STG.BKD_ENS_Headers',
                target_record_id=stg_header_id,
                target_ref=auto_submit.get('reference') or summary.get('conveyance_ref') or str(stg_header_id),
                transform_status='SUCCESS' if auto_submit.get('ok') else 'FAILED',
                transform_error=None if auto_submit.get('ok') else auto_submit.get('message'),
            )
    except Exception as _trace_exc:
        log.warning('DETAILS auto submit process trace failed: %s', _trace_exc)

    if not auto_submit.get('ok'):
        try:
            from app.ingestion.automation_notify import notify_pipeline_error
            notify_pipeline_error(
                f"ens_{auto_submit.get('stage') or 'automation'}_error",
                auto_submit.get('message') or 'ENS automatic validation/submission failed.',
                tenant_code=tenant_code,
                stg_header_id=stg_header_id,
                filename=subject,
            )
        except Exception as _notify_exc:
            log.warning('notify_pipeline_error ENS auto submit error: %s', _notify_exc)

    if auto_submit.get('ok') and summary.get('send_ingest_success_notification', True):
        try:
            from app.ingestion.automation_notify import notify_ingest_success
            notify_ingest_success(
                'ens_email_received',
                tenant_code=tenant_code,
                stg_header_id=stg_header_id,
                subject_hint=subject,
                summary={
                    **summary,
                    'ens_local_draft_id': stg_header_id,
                    'tss_ens_header_ref': auto_submit.get('reference'),
                    'auto_submit_stage': auto_submit.get('stage'),
                    'auto_submit_message': auto_submit.get('message'),
                },
            )
        except Exception as _notify_exc:
            log.warning('notify_ingest_success ENS details error: %s', _notify_exc)

    if auto_submit.get('ok'):
        try:
            from app.ingestion.ens_status_watcher import start_ens_status_watcher
            app_obj = current_app._get_current_object() if has_app_context() else None
            start_ens_status_watcher(
                stg_header_id,
                tenant_code=tenant_code,
                app_obj=app_obj,
            )
        except Exception as _watcher_exc:
            log.warning('ENS status watcher start failed: %s', _watcher_exc)

    return auto_submit


def _start_ens_header_auto_submit_worker(
    stg_header_id,
    *,
    tenant_code,
    env_code,
    graph_message_id='',
    subject='',
    summary=None,
):
    """Queue ENS submit work so Graph mailbox fetch never waits on TSS."""
    import threading

    app_obj = current_app._get_current_object() if has_app_context() else None

    def _run():
        if app_obj is not None:
            with app_obj.app_context():
                _complete_ens_header_auto_submit(
                    stg_header_id,
                    tenant_code=tenant_code,
                    env_code=env_code,
                    graph_message_id=graph_message_id,
                    subject=subject,
                    summary=summary,
                )
            return
        _complete_ens_header_auto_submit(
            stg_header_id,
            tenant_code=tenant_code,
            env_code=env_code,
            graph_message_id=graph_message_id,
            subject=subject,
            summary=summary,
        )

    threading.Thread(
        target=_run,
        name=f'ens-auto-submit-{tenant_code}-{stg_header_id}',
        daemon=True,
    ).start()
    return {
        'ok': True,
        'queued': True,
        'stage': 'queued',
        'message': 'ENS automatic validation/submission queued.',
    }


def _cargo_auto_submit_wait_settings():
    def _int_env(name, default):
        try:
            return max(0, int(os.environ.get(name, default)))
        except (TypeError, ValueError):
            return default

    return (
        _int_env('AUTO_CARGO_WAIT_ATTEMPTS', 24),
        _int_env('AUTO_CARGO_WAIT_INTERVAL_SECONDS', 5),
    )


def _read_stg_header_tss_ref(stg_header_id, tenant_code):
    row = query_one(
        """
        SELECT tss_ens_header_ref
        FROM STG.BKD_ENS_Headers
        WHERE ClientCode = ? AND stg_header_id = ?
        """,
        [tenant_code, stg_header_id],
    )
    return str((row or {}).get('tss_ens_header_ref') or '').strip()


def _prd_cargo_has_pending_work(stg_header_id, tenant_code):
    row = query_one(
        """
        SELECT
            SUM(CASE
                    WHEN NULLIF(LTRIM(RTRIM(COALESCE(c.tss_consignment_ref, ''))), '') IS NULL
                    THEN 1 ELSE 0
                END) AS missing_consignment_refs,
            SUM(CASE
                    WHEN NULLIF(LTRIM(RTRIM(COALESCE(c.tss_consignment_ref, ''))), '') IS NOT NULL
                     AND UPPER(COALESCE(c.sub_status, '')) NOT IN ('SUBMITTED', 'COMPLETED', 'CANCELLED', 'DELETED')
                    THEN 1 ELSE 0
                END) AS consignments_to_submit,
            (
                SELECT COUNT(*)
                FROM STG.BKD_GoodsItems g
                INNER JOIN STG.BKD_ENS_Consignments cg
                  ON cg.ClientCode = g.ClientCode
                 AND cg.stg_consignment_id = g.stg_consignment_id
                WHERE cg.ClientCode = c.ClientCode
                  AND cg.stg_header_id = c.stg_header_id
                  AND UPPER(COALESCE(cg.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
                  AND UPPER(COALESCE(g.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
                  AND NULLIF(LTRIM(RTRIM(COALESCE(g.tss_hex_id, ''))), '') IS NULL
            ) AS missing_goods_refs
        FROM STG.BKD_ENS_Consignments c
        WHERE c.ClientCode = ?
          AND c.stg_header_id = ?
          AND UPPER(COALESCE(c.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
        GROUP BY c.ClientCode, c.stg_header_id
        """,
        [tenant_code, stg_header_id],
    )
    if not row:
        return False
    return any(
        int((row or {}).get(key) or 0) > 0
        for key in ('missing_consignment_refs', 'consignments_to_submit', 'missing_goods_refs')
    )


def _log_auto_cargo_process(
    *,
    tenant_code,
    env_code,
    graph_message_id,
    event_type,
    stg_header_id,
    target_ref='',
    status='SUCCESS',
    error=None,
):
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            _log_ing_email_message_process(
                cur,
                client_code=tenant_code,
                graph_message_id=graph_message_id,
                env_code=env_code,
                event_type=event_type,
                target_table='STG.BKD_ENS_Headers',
                target_record_id=stg_header_id,
                target_ref=target_ref or str(stg_header_id or ''),
                transform_status=status,
                transform_error=error,
            )
    except Exception as exc:
        log.warning('AUTO_SUBMIT_CARGO process trace failed: %s', exc)


def _notify_auto_cargo_error(error_type, detail, *, tenant_code, stg_header_id, filename=''):
    try:
        from app.ingestion.automation_notify import notify_pipeline_error
        notify_pipeline_error(
            error_type,
            detail,
            tenant_code=tenant_code,
            stg_header_id=stg_header_id,
            filename=filename,
        )
    except Exception as exc:
        log.warning('notify_pipeline_error AUTO_SUBMIT_CARGO error: %s', exc)


def _load_auto_cargo_notification_context(stg_header_id, tenant_code):
    """Load compact ENS/DEC context for the cargo-submitted email."""
    from app.db import db_cursor
    client_code = str(tenant_code or 'BKD').strip().upper()
    with db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT
                h.stg_header_id,
                h.tss_ens_header_ref,
                h.conveyance_ref,
                h.arrival_date_time,
                h.arrival_port,
                h.carrier_name,
                th.TssStatus AS tss_status
            FROM STG.BKD_ENS_Headers h
            LEFT JOIN TSS.BKD_ENS_Headers th
              ON th.ClientCode = h.ClientCode
             AND th.DeclarationNumber = h.tss_ens_header_ref
            WHERE h.ClientCode = ? AND h.stg_header_id = ?
            """,
            [client_code, stg_header_id],
        )
        row = cur.fetchone()
        if not row:
            return {}, []
        header = dict(zip([d[0] for d in cur.description], row))

        cur.execute(
            """
            SELECT
                c.stg_consignment_id,
                COALESCE(c.trader_reference, c.transport_document_number, c.tss_consignment_ref) AS document_no,
                c.trader_reference,
                c.transport_document_number,
                c.tss_consignment_ref,
                c.sub_status,
                tc.TssStatus AS tss_status,
                COUNT(g.stg_item_id) AS goods_count
            FROM STG.BKD_ENS_Consignments c
            LEFT JOIN STG.BKD_GoodsItems g
              ON g.ClientCode = c.ClientCode
             AND g.stg_consignment_id = c.stg_consignment_id
             AND UPPER(COALESCE(g.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
            LEFT JOIN TSS.BKD_ENS_Consignments tc
              ON tc.ClientCode = c.ClientCode
             AND tc.ConsignmentReference = c.tss_consignment_ref
            WHERE c.ClientCode = ?
              AND c.stg_header_id = ?
              AND UPPER(COALESCE(c.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
            GROUP BY
                c.stg_consignment_id, c.trader_reference, c.transport_document_number,
                c.tss_consignment_ref, c.sub_status, tc.TssStatus
            ORDER BY c.stg_consignment_id
            """,
            [client_code, stg_header_id],
        )
        cols = [d[0] for d in cur.description]
        consignments = [dict(zip(cols, r)) for r in cur.fetchall()]
        return header, consignments


def _notify_auto_cargo_submitted(stg_header_id, *, tenant_code, summary):
    """Best-effort cargo submitted notification. Never raises."""
    try:
        from app.ingestion.automation_notify import notify_cargo_submitted
        header, consignments = _load_auto_cargo_notification_context(stg_header_id, tenant_code)
        if not header:
            return False, 'ENS header not found for cargo submitted notification'
        return notify_cargo_submitted(
            header,
            consignments,
            tenant_code=tenant_code,
            summary=summary or {},
        )
    except Exception as exc:
        log.warning('notify_cargo_submitted AUTO_SUBMIT_CARGO error: %s', exc)
        return False, str(exc)


def _start_status_watcher_best_effort(stg_header_id, *, tenant_code):
    try:
        from app.ingestion.ens_status_watcher import start_ens_status_watcher
        app_obj = current_app._get_current_object() if has_app_context() else None
        start_ens_status_watcher(
            stg_header_id,
            tenant_code=tenant_code,
            app_obj=app_obj,
        )
    except Exception as exc:
        log.warning('ENS status watcher start failed: %s', exc)


def _complete_prd_cargo_auto_submit(
    stg_header_id,
    *,
    tenant_code,
    env_code,
    graph_message_id='',
    subject='',
    filename='',
    wait_attempts=None,
    wait_interval_seconds=None,
):
    """Run automatic PRD cargo creation/submission after Sales Orders XLSX staging."""
    import time

    tenant_code = str(tenant_code or 'BKD').strip().upper()
    default_attempts, default_interval = _cargo_auto_submit_wait_settings()
    wait_attempts = default_attempts if wait_attempts is None else max(0, int(wait_attempts))
    wait_interval_seconds = (
        default_interval if wait_interval_seconds is None else max(0, float(wait_interval_seconds))
    )

    _log_auto_cargo_process(
        tenant_code=tenant_code,
        env_code=env_code,
        graph_message_id=graph_message_id,
        event_type='AUTO_SUBMIT_CARGO',
        stg_header_id=stg_header_id,
        status='STARTED',
    )

    ens_ref = ''
    for attempt in range(0, wait_attempts + 1):
        ens_ref = _read_stg_header_tss_ref(stg_header_id, tenant_code)
        if ens_ref:
            break
        if attempt < wait_attempts:
            _log_auto_cargo_process(
                tenant_code=tenant_code,
                env_code=env_code,
                graph_message_id=graph_message_id,
                event_type='AUTO_SUBMIT_CARGO_WAITING_ENS_REF',
                stg_header_id=stg_header_id,
                status='PENDING',
            )
            time.sleep(wait_interval_seconds)

    if not ens_ref:
        message = 'ENS header TSS reference was not available before automatic cargo submit timeout.'
        _log_auto_cargo_process(
            tenant_code=tenant_code,
            env_code=env_code,
            graph_message_id=graph_message_id,
            event_type='AUTO_SUBMIT_CARGO_FAILED',
            stg_header_id=stg_header_id,
            status='FAILED',
            error=message,
        )
        _notify_auto_cargo_error(
            'auto_cargo_wait_timeout',
            message,
            tenant_code=tenant_code,
            stg_header_id=stg_header_id,
            filename=filename or subject,
        )
        return {'ok': False, 'stage': 'wait_ens_ref', 'message': message}

    if not _prd_cargo_has_pending_work(stg_header_id, tenant_code):
        _notify_auto_cargo_submitted(
            stg_header_id,
            tenant_code=tenant_code,
            summary={'ok': True, 'stage': 'no_pending_work'},
        )
        _log_auto_cargo_process(
            tenant_code=tenant_code,
            env_code=env_code,
            graph_message_id=graph_message_id,
            event_type='AUTO_SUBMIT_CARGO_SUCCESS',
            stg_header_id=stg_header_id,
            target_ref=ens_ref,
            status='SUCCESS',
        )
        _start_status_watcher_best_effort(stg_header_id, tenant_code=tenant_code)
        return {'ok': True, 'stage': 'no_pending_work', 'reference': ens_ref}

    try:
        from app.blueprints.declarations.routes import _submit_prd_cargo_for_header
        summary = _submit_prd_cargo_for_header(stg_header_id, client_code=tenant_code)
    except Exception as exc:
        message = _friendly_ingest_error(exc)
        _log_auto_cargo_process(
            tenant_code=tenant_code,
            env_code=env_code,
            graph_message_id=graph_message_id,
            event_type='AUTO_SUBMIT_CARGO_FAILED',
            stg_header_id=stg_header_id,
            target_ref=ens_ref,
            status='FAILED',
            error=message,
        )
        _notify_auto_cargo_error(
            'auto_cargo_submit_exception',
            message,
            tenant_code=tenant_code,
            stg_header_id=stg_header_id,
            filename=filename or subject,
        )
        return {'ok': False, 'stage': 'submit_exception', 'reference': ens_ref, 'message': message}

    failed_count = int(summary.get('cons_failed') or 0) + int(summary.get('goods_failed') or 0)
    messages = [str(item) for item in (summary.get('messages') or []) if str(item or '').strip()]
    ok = bool(summary.get('ok')) and failed_count == 0
    if ok:
        _notify_auto_cargo_submitted(
            stg_header_id,
            tenant_code=tenant_code,
            summary=summary,
        )
        _log_auto_cargo_process(
            tenant_code=tenant_code,
            env_code=env_code,
            graph_message_id=graph_message_id,
            event_type='AUTO_SUBMIT_CARGO_SUCCESS',
            stg_header_id=stg_header_id,
            target_ref=ens_ref,
            status='SUCCESS',
        )
    else:
        detail = '; '.join(messages[:6]) or summary.get('message') or 'Automatic cargo submit did not complete cleanly.'
        _log_auto_cargo_process(
            tenant_code=tenant_code,
            env_code=env_code,
            graph_message_id=graph_message_id,
            event_type='AUTO_SUBMIT_CARGO_FAILED',
            stg_header_id=stg_header_id,
            target_ref=ens_ref,
            status='FAILED',
            error=detail,
        )
        _notify_auto_cargo_error(
            'auto_cargo_submit_failed',
            detail,
            tenant_code=tenant_code,
            stg_header_id=stg_header_id,
            filename=filename or subject,
        )

    _start_status_watcher_best_effort(stg_header_id, tenant_code=tenant_code)
    return {
        **dict(summary or {}),
        'ok': ok,
        'stage': 'submit',
        'reference': ens_ref,
    }


def _start_prd_cargo_auto_submit_worker(
    stg_header_id,
    *,
    tenant_code,
    env_code,
    graph_message_id='',
    subject='',
    filename='',
):
    """Queue cargo submit work so Graph mailbox fetch never waits on TSS."""
    import threading

    app_obj = current_app._get_current_object() if has_app_context() else None

    def _run():
        if app_obj is not None:
            with app_obj.app_context():
                _complete_prd_cargo_auto_submit(
                    stg_header_id,
                    tenant_code=tenant_code,
                    env_code=env_code,
                    graph_message_id=graph_message_id,
                    subject=subject,
                    filename=filename,
                )
            return
        _complete_prd_cargo_auto_submit(
            stg_header_id,
            tenant_code=tenant_code,
            env_code=env_code,
            graph_message_id=graph_message_id,
            subject=subject,
            filename=filename,
        )

    threading.Thread(
        target=_run,
        name=f'cargo-auto-submit-{tenant_code}-{stg_header_id}',
        daemon=True,
    ).start()
    return {
        'ok': True,
        'queued': True,
        'stage': 'queued',
        'message': 'Cargo automatic validation/submission queued.',
    }


def _forward_to_ingest(file_bytes, filename, channel, ingest_url, api_key=None):
    """
    POST the file to the ingest service upload endpoint.
    Returns (ok: bool, response_json: dict|None, error: str|None).
    """
    upload_url = ingest_url.rstrip('/') + '/documents/upload'
    headers = {}
    if api_key:
        headers['X-API-Key'] = api_key

    try:
        resp = _req.post(
            upload_url,
            files={'files': (filename, io.BytesIO(file_bytes), 'application/octet-stream')},
            data={'channel': channel, 'source': 'local_server'},
            headers=headers,
            timeout=60,
        )
        resp.raise_for_status()
        try:
            return True, resp.json(), None
        except Exception:
            return True, {'status': 'ok'}, None
    except _req.exceptions.ConnectionError:
        return False, None, f'Cannot connect to ingest service at {upload_url}'
    except _req.exceptions.Timeout:
        return False, None, 'Ingest service timed out (>60s)'
    except _req.exceptions.HTTPError as e:
        return False, None, f'Ingest service returned {e.response.status_code}: {e}'
    except Exception as e:
        return False, None, str(e)


def _check_webhook_auth():
    webhook_key = os.environ.get('INGEST_WEBHOOK_KEY', '')
    if not webhook_key:
        return None
    provided = request.headers.get('X-API-Key', '')
    if provided == webhook_key:
        return None
    return jsonify({'ok': False, 'error': 'Unauthorized'}), 401


def _active_tenant():
    explicit = (
        request.headers.get('X-Tenant-Code')
        or request.form.get('tenant_code')
        or request.args.get('tenant_code')
        or ''
    ).strip().upper()
    if explicit and explicit in TENANT_REGISTRY:
        return get_tenant_by_code(explicit)
    return get_tenant()


def _load_attach_consignment(consignment_id):
    try:
        sid = int(consignment_id or 0)
    except (TypeError, ValueError):
        return None
    if sid <= 0:
        return None

    client_code = (_active_tenant().get('code') or 'BKD').upper()
    row = query_one("""
        SELECT
            c.stg_consignment_id AS staging_id,
            c.stg_header_id AS staging_ens_id,
            c.sub_status AS status,
            tc.TssStatus AS tss_status,
            c.tss_consignment_ref AS dec_reference,
            CAST(NULL AS NVARCHAR(80)) AS sfd_reference,
            COALESCE(c.trader_reference, c.transport_document_number, c.tss_consignment_ref) AS label,
            c.transport_document_number,
            c.goods_description,
            c.controlled_goods,
            c.container_indicator,
            h.tss_ens_header_ref AS ens_reference,
            (
                SELECT COUNT(*)
                FROM STG.BKD_GoodsItems g
                WHERE g.stg_consignment_id = c.stg_consignment_id
            ) AS goods_count
        FROM STG.BKD_ENS_Consignments c
        LEFT JOIN STG.BKD_ENS_Headers h
          ON h.ClientCode = c.ClientCode
         AND h.stg_header_id = c.stg_header_id
        LEFT JOIN TSS.BKD_ENS_Consignments tc
          ON tc.ClientCode = c.ClientCode
         AND tc.ConsignmentReference = c.tss_consignment_ref
        WHERE c.ClientCode = ? AND c.stg_consignment_id = ?
    """, [client_code, sid])
    if not row:
        return None

    row['reference_label'] = (
        row.get('dec_reference')
        or row.get('sfd_reference')
        or row.get('label')
        or f"Consignment #{row.get('staging_id')}"
    )
    row['can_attach_goods'] = tss_allows_data_changes(row.get('tss_status'), row.get('status'))
    row['lock_reason'] = tss_data_lock_reason(
        row.get('tss_status'),
        row.get('status'),
        entity_label='consignment',
    )
    return row


def _route_document_channel(filename):
    suffix = Path(filename).suffix.lower()
    if suffix == '.xlsx':
        prefix = _simple_prefix(filename)
        profiles = _get_profiles()
        matched = next((p for p in profiles if p.get('customer_code', '').upper() == prefix), None)
        notes = [f'Excel workbook upload; prefix detected: {prefix or "none"}']
        if matched:
            notes.append(f"profile matched: {matched['customer_code']}")
        else:
            notes.append('handled by local Excel ingestion; document router not required')
        return {
            'channel': 'TEMPLATE',
            'customer_code': matched['customer_code'] if matched else None,
            'routing_notes': '; '.join(notes),
            'is_unmapped': False,
        }

    try:
        from ingest_service.blueprints.pdf_ingestion.router import route_document
        profiles = _get_profiles()
        result = route_document(
            filename=filename,
            text_content='',
            pdf_metadata={},
            page_count=0,
            logo_hash=None,
            db_profiles=profiles,
        )
        return {
            'channel': result.channel,
            'customer_code': result.customer_code,
            'routing_notes': '; '.join(result.routing_notes),
            'is_unmapped': result.is_unmapped,
        }
    except ImportError:
        prefix = _simple_prefix(filename)
        profiles = _get_profiles()
        matched = next((p for p in profiles if p.get('customer_code', '').upper() == prefix), None)
        return {
            'channel': 'TEMPLATE' if matched else 'UNMAPPED',
            'customer_code': matched['customer_code'] if matched else None,
            'routing_notes': f'Simple prefix match: {prefix or "none"}',
            'is_unmapped': not matched,
        }
    except Exception as exc:
        log.error('Routing error for %s: %s', filename, exc)
        return {
            'channel': 'UNMAPPED',
            'customer_code': None,
            'routing_notes': f'Routing error: {exc}',
            'is_unmapped': True,
        }


def _friendly_ingest_error(exc):
    text = str(exc)
    lower = text.lower()
    if (
        'sql server' in lower
        and (
            'client unable to establish connection' in lower
            or 'login timeout expired' in lower
            or 'ssl provider' in lower
            or 'encryption not supported' in lower
        )
    ):
        return (
            "Database connection failed while processing ingestion. Check the PRD SQL connection "
            "settings/ODBC driver encryption settings, then retry the mailbox fetch. "
            f"Original error: {text}"
        )
    if 'string or binary data would be truncated' in lower:
        table_match = re.search(r"table '([^']+)'", text, re.IGNORECASE)
        column_match = re.search(r"column '([^']+)'", text, re.IGNORECASE)
        table = table_match.group(1) if table_match else 'the staging table'
        column = column_match.group(1) if column_match else 'a text column'
        if 'staginggoodsitems' in table.lower() and column.lower() == 'type_of_packages':
            return (
                f"Database schema is too narrow for TSS package type values: {table}.{column} "
                "must be NVARCHAR(40). Apply migrations/056_align_columns_with_tss_v295.sql "
                "or the latest 067 package-type alignment migration, then retry this ingestion."
            )
        return (
            f"Database schema is too narrow for {table}.{column}. Apply the latest TSS v2.9.5 "
            "column-length migrations, then retry this ingestion."
        )
    return text


def _local_autostage_enabled():
    try:
        return bool(resolve_ingest_defaults(tenant_code=_active_tenant()["code"]).enabled)
    except Exception:
        return False


def _safe_ingest_next_url(value, fallback=None):
    fallback = fallback or url_for('ingest.queue', panel='upload')
    candidate = (value or '').strip()
    if not candidate:
        return fallback
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc or not candidate.startswith('/'):
        return fallback
    return candidate


def _normalize_creation_mode(value):
    return INGEST_CREATION_ALIASES.get((value or '').strip().lower())


def _current_ingest_defaults():
    try:
        return resolve_ingest_defaults(tenant_code=_active_tenant()["code"])
    except Exception:
        return None


def _default_creation_mode(defaults=None):
    defaults = defaults or _current_ingest_defaults()
    mode = _normalize_creation_mode(getattr(defaults, 'mode', ''))
    return mode or INGEST_CREATION_REVIEW


def _selected_creation_mode(defaults=None):
    explicit = (
        request.form.get('creation_mode')
        or request.form.get('ingestion_mode')
        or request.form.get('ingest_mode')
    )
    if explicit:
        return _normalize_creation_mode(explicit) or _default_creation_mode(defaults)
    if 'auto_create' in request.form:
        auto_text = (request.form.get('auto_create') or '').strip().lower()
        return INGEST_CREATION_AUTO if auto_text in {'1', 'true', 'yes', 'y', 'on'} else INGEST_CREATION_REVIEW
    return _default_creation_mode(defaults)


def _auto_create_if_clean_requested(defaults=None):
    return _selected_creation_mode(defaults) == INGEST_CREATION_AUTO


def _email_sales_orders_auto_create_requested(defaults=None):
    explicit = (
        request.form.get('creation_mode')
        or request.form.get('ingestion_mode')
        or request.form.get('ingest_mode')
    )
    if explicit or 'auto_create' in request.form:
        return _auto_create_if_clean_requested(defaults)
    return True


def _expand_supported_files(files):
    expanded = []
    for item in files:
        filename = item['filename']
        suffix = Path(filename).suffix.lower()
        if suffix != '.zip':
            expanded.append(item)
            continue
        with zipfile.ZipFile(io.BytesIO(item['bytes'])) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                entry_name = entry.filename
                entry_suffix = Path(entry_name).suffix.lower()
                if entry_suffix not in {'.pdf', '.csv'}:
                    continue
                expanded.append({
                    'filename': Path(entry_name).name,
                    'bytes': archive.read(entry),
                })
    return expanded


def _parse_uploaded_invoices(files):
    tenant = _active_tenant()
    parsed_invoices = []
    route_info = []
    for item in _expand_supported_files(files):
        parsed_invoices.append(parse_birkdale_invoice_file(item['bytes'], item['filename']))
        route_info.append(_route_document_channel(item['filename']))
    if not parsed_invoices:
        raise ValueError('No supported Birkdale PDF or mapped CSV files were found in the batch.')
    return tenant, parsed_invoices, route_info


def _stage_uploaded_invoices(files, source, channel, email_meta=None, no_sfd_reason=''):
    tenant, parsed_invoices, route_info = _parse_uploaded_invoices(files)
    result = stage_invoice_batch(
        parsed_invoices,
        source=source,
        channel=channel,
        email_meta=email_meta,
        tenant_code=tenant["code"],
        no_sfd_reason=(no_sfd_reason or '').strip(),
    )
    return parsed_invoices, route_info, result


def _review_uploaded_invoices(files, source, channel, email_meta=None):
    tenant, parsed_invoices, route_info = _parse_uploaded_invoices(files)
    review = build_invoice_batch_review(
        parsed_invoices,
        source=source,
        channel=channel,
        email_meta=email_meta,
        tenant_code=tenant["code"],
    )
    review['route_info'] = route_info
    return parsed_invoices, route_info, review


def _review_uploaded_invoices_for_attach(files, consignment, source, channel, email_meta=None):
    tenant, parsed_invoices, route_info = _parse_uploaded_invoices(files)
    review = build_goods_attach_review(
        parsed_invoices,
        consignment,
        source=source,
        channel=channel,
        email_meta=email_meta,
        tenant_code=tenant["code"],
    )
    review['route_info'] = route_info
    return parsed_invoices, route_info, review


def _apply_review_form(review, form):
    review = review or {}
    for key, value in form.items():
        if key == 'review_json' or not key:
            continue
        parts = key.split('__')
        if len(parts) == 2 and parts[0] == 'header':
            review.setdefault('header', {}).setdefault('payload', {})[parts[1]] = (value or '').strip()
        elif len(parts) == 3 and parts[0] == 'cons':
            try:
                idx = int(parts[1])
            except (TypeError, ValueError):
                continue
            invoices = review.setdefault('invoices', [])
            if 0 <= idx < len(invoices):
                invoices[idx].setdefault('consignment', {}).setdefault('payload', {})[parts[2]] = (value or '').strip()
        elif len(parts) == 4 and parts[0] == 'goods':
            try:
                invoice_idx = int(parts[1])
                goods_idx = int(parts[2])
            except (TypeError, ValueError):
                continue
            invoices = review.setdefault('invoices', [])
            if 0 <= invoice_idx < len(invoices):
                goods = invoices[invoice_idx].setdefault('goods', [])
                if 0 <= goods_idx < len(goods):
                    goods[goods_idx].setdefault('payload', {})[parts[3]] = (value or '').strip()
    for idx, invoice in enumerate(review.get('invoices') or []):
        payload = invoice.setdefault('consignment', {}).setdefault('payload', {})
        if (payload.get('no_sfd_reason') or '').strip():
            payload['generate_SD'] = 'no'
        elif f'cons__{idx}__no_sfd_reason' in form and payload.get('generate_SD') == 'no':
            payload['generate_SD'] = ''
    return refresh_review_status(review)


def _load_ens_review_choices():
    empty_choices = {
        'movement_types': [],
        'ports': [],
        'countries': [],
        'transport_charges': [],
        'passive_transport_types': [],
        'no_sfd_reason': [],
    }
    try:
        from app.blueprints.declarations.routes import get_choices, get_partners_by_type, load_form_choices

        choices = load_form_choices()
        choices['no_sfd_reason'] = get_choices('CV_no_sfd_reason')
        return choices, get_partners_by_type('Carrier')
    except Exception:
        log.exception('Could not load ENS review choices')
        return empty_choices, []


def _load_no_sfd_reason_choices():
    try:
        from app.blueprints.declarations.routes import get_choices

        return get_choices('CV_no_sfd_reason')
    except Exception:
        log.exception('Could not load no_sfd_reason choices')
        return []


def _is_bkd_tenant():
    tenant = _active_tenant()
    return bool(tenant and tenant.get('code') == 'BKD' and tenant.get('schema') == 'BKD')


def _detect_sales_orders_workbook_format(xlsx_bytes):
    from app.ingestion.excel_sales_orders import detect_excel_format

    return detect_excel_format(xlsx_bytes)


def _sales_orders_workbook_allowed_for_tenant(xlsx_bytes):
    workbook_format = _detect_sales_orders_workbook_format(xlsx_bytes)
    return workbook_format, workbook_format == 'synoviaflow' or _is_bkd_tenant()


def _flash_sales_orders_tenant_block():
    flash(
        'Legacy BKD Sales Orders workbooks are only available for the BKD tenant. '
        'For CLR or other tenants, use the SynoviaFlow consignment template.',
        'warning',
    )


def _load_bkd_sales_orders_ens_options():
    tenant = _active_tenant()
    if not tenant:
        return []
    client_code = (tenant.get('code') or tenant.get('schema') or 'BKD').strip().upper()
    try:
        rows = query_all("""
            SELECT TOP 100
                   h.stg_header_id AS staging_id,
                   h.label,
                   h.source,
                   h.tss_ens_header_ref AS ens_reference,
                   h.conveyance_ref,
                   h.identity_no_of_transport,
                   h.arrival_date_time,
                   h.arrival_port,
                   h.sub_status AS status,
                   t.TssStatus AS tss_status,
                   h.stg_created_at AS created_at
            FROM STG.BKD_ENS_Headers h
            LEFT JOIN TSS.BKD_ENS_Headers t
              ON t.ClientCode = h.ClientCode
             AND NULLIF(t.DeclarationNumber, '') = NULLIF(h.tss_ens_header_ref, '')
            WHERE h.ClientCode = ?
              AND UPPER(COALESCE(h.sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
            ORDER BY COALESCE(h.updated_at, h.stg_created_at) DESC, h.stg_header_id DESC
        """, [client_code]) or []
        return [
            row for row in rows
            if tss_allows_data_changes(row.get('tss_status'), row.get('status'))
        ][:30]
    except Exception:
        log.exception('Could not load sales-order ENS options')
        return []


def _load_bkd_sales_orders_ens_header(schema, ens_staging_id):
    header = query_one(
        """
        SELECT TOP 1
               h.*,
               h.stg_header_id AS staging_id,
               h.tss_ens_header_ref AS ens_reference,
               h.sub_status AS status,
               t.TssStatus AS tss_status,
               h.stg_created_at AS created_at
        FROM STG.BKD_ENS_Headers h
        LEFT JOIN TSS.BKD_ENS_Headers t
          ON t.ClientCode = h.ClientCode
         AND NULLIF(t.DeclarationNumber, '') = NULLIF(h.tss_ens_header_ref, '')
        WHERE h.ClientCode = ?
          AND h.stg_header_id = ?
        """,
        [(schema or 'BKD').strip().upper(), ens_staging_id],
    )
    if not header:
        return None
    header['place_of_acceptance_same_as_loading'] = (
        header.get('place_of_acceptance_same_as_loading')
        or header.get('place_of_acceptance_same')
        or ''
    )
    header['place_of_delivery_same_as_unloading'] = (
        header.get('place_of_delivery_same_as_unloading')
        or header.get('place_of_delivery_same')
        or ''
    )
    if not tss_allows_data_changes(header.get('tss_status'), header.get('status')):
        return None
    return header


def _bkd_sales_orders_existing_ens_warnings(header):
    """Return warnings that keep selected-ENS imports in review mode."""
    header = header or {}
    movement_type = (header.get('movement_type') or '').strip()
    missing = []
    for field_name in (
        'movement_type',
        'conveyance_ref',
        'arrival_date_time',
        'identity_no_of_transport',
        'arrival_port',
        'place_of_loading',
        'place_of_unloading',
        'transport_charges',
    ):
        if not header.get(field_name):
            missing.append(field_name)
    if movement_type == '3a':
        for field_name in (
            'type_of_passive_transport',
            'place_of_acceptance_same_as_loading',
            'place_of_delivery_same_as_unloading',
        ):
            if not header.get(field_name):
                missing.append(field_name)
    if not missing:
        return []
    return [
        'Selected ENS header is missing: ' + ', '.join(missing)
        + ' - import staged for review before submission'
    ]


def _selected_no_sfd_reason():
    return (request.form.get('no_sfd_reason') or '').strip()


def _apply_no_sfd_reason_to_parsed(parsed, no_sfd_reason):
    no_sfd_reason = (no_sfd_reason or '').strip()
    if not no_sfd_reason:
        return
    for consignment in parsed.consignments or []:
        for line in consignment.goods or []:
            line.raw = dict(line.raw or {})
            line.raw['no_sfd_reason'] = no_sfd_reason
            line.raw['generate_SD'] = 'no'


def _apply_no_sfd_reason_to_review(review, no_sfd_reason):
    no_sfd_reason = (no_sfd_reason or '').strip()
    if not no_sfd_reason:
        return review
    for invoice in (review or {}).get('invoices') or []:
        payload = invoice.setdefault('consignment', {}).setdefault('payload', {})
        payload['no_sfd_reason'] = no_sfd_reason
        payload['generate_SD'] = 'no'
    return refresh_review_status(review)


def _to_datetime_local(value):
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%dT%H:%M')
    text = str(value or '').strip()
    if not text:
        return ''
    if 'T' in text:
        return text[:16]
    try:
        return datetime.strptime(text, '%d/%m/%Y %H:%M:%S').strftime('%Y-%m-%dT%H:%M')
    except ValueError:
        return text


def _render_review(review, error=None):
    review = refresh_review_status(review)
    choices, carriers = _load_ens_review_choices()
    header_payload = review.get('header', {}).get('payload') or {}
    goods_table_fields = [
        field for field in GOODS_REVIEW_FIELDS
        if field[0] in {
            'item_number', 'goods_description', 'commodity_code',
            'type_of_packages', 'number_of_packages', 'gross_mass_kg',
            'net_mass_kg', 'country_of_origin', 'procedure_code',
            'item_invoice_amount', 'item_invoice_currency',
        }
    ]
    goods_detail_fields = [
        field for field in GOODS_REVIEW_FIELDS
        if field[0] in {
            'label', 'package_marks', 'controlled_goods',
            'additional_procedure_code', 'valuation_method',
        }
    ]
    template_name = 'ingest/review_attach_goods.html' if review.get('review_mode') == REVIEW_MODE_ATTACH_GOODS else 'ingest/review.html'
    return render_template(
        template_name,
        review=review,
        review_json=json.dumps(review, ensure_ascii=True, default=str),
        error=error,
        header_fields=HEADER_REVIEW_FIELDS,
        consignment_fields=CONSIGNMENT_REVIEW_FIELDS,
        goods_table_fields=goods_table_fields,
        goods_detail_fields=goods_detail_fields,
        header_required_fields=review.get('header', {}).get('required') or HEADER_REQUIRED_FIELDS,
        consignment_required_fields=CONSIGNMENT_REQUIRED_FIELDS,
        goods_required_fields=GOODS_REQUIRED_FIELDS,
        choices=choices,
        carriers=carriers,
        header_datetime_local=_to_datetime_local(header_payload.get('arrival_date_time')),
    )


def _render_or_auto_create_review(review, creation_mode):
    review = refresh_review_status(review)
    if creation_mode != INGEST_CREATION_AUTO:
        return _render_review(review)

    missing_total = review.get('summary', {}).get('missing_total') or 0
    warnings = review.get('warnings') or []
    if missing_total or warnings:
        flash(
            'Auto-create was requested, but this batch still needs review before records are created.',
            'warning',
        )
        return _render_review(review)

    result = stage_invoice_review(
        review,
        tenant_code=_active_tenant()["code"],
        approved_by=session.get('username') or 'portal_auto_create',
    )
    flash(
        f"Auto-created ENS #{result.get('declaration_id')} with "
        f"{len(result.get('consignment_ids') or [])} consignments and "
        f"{len(result.get('goods_ids') or [])} goods items.",
        'success',
    )
    return redirect(url_for('declarations.detail', dec_id=result.get('declaration_id')))


def _stage_email_message(message_bytes, source='smtp_inject', no_sfd_reason=''):
    message = parse_email_bytes(message_bytes)
    attachments = [
        item for item in extract_supported_attachments(message)
        if not is_sales_order_workbook_attachment(item)
    ]
    if not attachments:
        raise ValueError('No supported PDF/CSV/ZIP invoice attachments found in the email.')
    email_meta = build_email_metadata(message)
    parsed_invoices, route_info, result = _stage_uploaded_invoices(
        attachments,
        source=source,
        channel='EMAIL',
        email_meta=email_meta,
        no_sfd_reason=no_sfd_reason,
    )
    return message, attachments, email_meta, parsed_invoices, route_info, result


def _received_at_from_email_meta(email_meta):
    raw_date = (email_meta or {}).get('date') or ''
    if raw_date:
        try:
            return parsedate_to_datetime(raw_date)
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
        try:
            from datetime import timezone
            parsed = datetime.fromisoformat(str(raw_date).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (TypeError, ValueError):
            pass
    from datetime import timezone
    return datetime.now(timezone.utc)


def _source_tenant_code_for_sales_orders(active_tenant=None):
    active_tenant = active_tenant or _active_tenant() or {}
    active_code = str(active_tenant.get('code') or '').strip().upper()
    raw = (
        request.headers.get('X-Source-Tenant-Code')
        or request.form.get('source_tenant_code')
        or request.args.get('source_tenant_code')
        or ''
    ).strip().upper()
    if active_code == 'SYD' and raw in TENANT_REGISTRY:
        return raw
    return active_code


def _sales_orders_master_schema(active_tenant=None):
    active_tenant = active_tenant or _active_tenant() or {}
    source_code = _source_tenant_code_for_sales_orders(active_tenant)
    if source_code and source_code in TENANT_REGISTRY:
        return TENANT_REGISTRY[source_code].get('schema') or source_code
    return active_tenant.get('schema') or active_tenant.get('code') or 'BKD'


def _latest_sales_orders_details_ens_id(client_code):
    client_code = (client_code or 'BKD').strip().upper()
    try:
        rows = query_all(f"""
            SELECT TOP 20 stg_header_id, sub_status, source, stg_created_at
            FROM [STG].[BKD_ENS_Headers]
            WHERE ClientCode = ?
              AND COALESCE(source, '') = 'EXCEL_SALES_ORDERS_DETAILS'
              AND UPPER(COALESCE(sub_status, '')) NOT IN ('CANCELLED', 'DELETED', 'COMPLETED')
              AND stg_created_at >= DATEADD(hour, -36, SYSUTCDATETIME())
            ORDER BY stg_created_at DESC, stg_header_id DESC
        """, [client_code]) or []
    except Exception:
        log.exception('Could not locate latest Sales Orders DETAILS STG ENS draft')
        return None
    for row in rows:
        if str(row.get('sub_status') or '').upper() not in {'CANCELLED', 'DELETED', 'COMPLETED'}:
            return int(row['stg_header_id'])
    return None


SALES_ORDERS_MISSING_DETAILS_ENS_ERROR = (
    'No prior Sales Orders DETAILS ENS draft found; process the ENS/header email first.'
)


def _resolve_sales_orders_email_target(schema, email_body, received_at):
    """Resolve whether a Sales Orders email creates an ENS or appends to one."""
    from app.ingestion.excel_sales_orders import parse_email_carrier_block

    body_meta = parse_email_carrier_block(email_body or '', received_at=received_at) if email_body else None
    target_ens_id = _latest_sales_orders_details_ens_id(schema)
    if target_ens_id:
        return {
            'body_meta': body_meta,
            'existing_ens_staging_id': target_ens_id,
            'create_header_from_email_body': False,
            'resolution_mode': 'xlsx_links_latest_details_ens',
            'error': None,
        }

    if body_meta and body_meta.raw_block:
        return {
            'body_meta': body_meta,
            'existing_ens_staging_id': None,
            'create_header_from_email_body': True,
            'resolution_mode': 'details_body_creates_ens',
            'error': None,
        }

    return {
        'body_meta': body_meta,
        'existing_ens_staging_id': None,
        'create_header_from_email_body': False,
        'resolution_mode': 'missing_details_ens',
        'error': SALES_ORDERS_MISSING_DETAILS_ENS_ERROR,
    }


def _parse_sales_orders_workbook(xlsx_bytes, email_body, received_at, *, require_email_body=True):
    from app.ingestion.excel_sales_orders import (
        EmailMetadata,
        detect_excel_format,
        parse_email_carrier_block,
        parse_sales_orders_excel,
        parse_synoviaflow_template_excel,
    )

    fmt = detect_excel_format(xlsx_bytes)
    if fmt == 'synoviaflow':
        template_meta, parsed = parse_synoviaflow_template_excel(xlsx_bytes)
        body_meta = parse_email_carrier_block(email_body or '', received_at=received_at) if email_body else None
        email_meta = body_meta if (body_meta and body_meta.conveyance_ref) else template_meta
        return email_meta, parsed

    email_meta = (
        parse_email_carrier_block(email_body or '', received_at=received_at)
        if require_email_body or email_body else EmailMetadata()
    )
    return email_meta, parse_sales_orders_excel(xlsx_bytes)


def _stage_sales_orders_excel(
    filename,
    xlsx_bytes,
    email_body,
    received_at,
    *,
    auto_create=False,
    no_sfd_reason='',
    create_header_from_email_body=False,
    existing_ens_staging_id=None,
    master_schema_override=None,
):
    from app.db import db_cursor
    from app.ingestion.sales_orders_stage import (
        record_sales_orders_ing_lines,
        record_sales_orders_source_file,
        stage_sales_orders_batch_stg,
    )

    tenant = _active_tenant()
    if tenant is None:
        raise ValueError('Unknown tenant')

    tenant_code = tenant['code']
    schema = tenant['schema']
    master_schema = master_schema_override or _sales_orders_master_schema(tenant)
    defaults = resolve_ingest_defaults(tenant_code=tenant_code)
    env_code = _ing_env_code()
    auto_create_if_clean = bool(auto_create)

    email_meta, parsed = _parse_sales_orders_workbook(xlsx_bytes, email_body, received_at)
    parsed.source_filename = filename
    _apply_no_sfd_reason_to_parsed(parsed, no_sfd_reason)

    with db_cursor() as cur:
        source_file_id = record_sales_orders_source_file(
            cur,
            env_code=env_code,
            filename=filename,
            xlsx_bytes=xlsx_bytes,
            parsed=parsed,
        )
        ing_line_ids = record_sales_orders_ing_lines(
            cur,
            env_code=env_code,
            file_id=source_file_id,
            parsed=parsed,
        )
        result = stage_sales_orders_batch_stg(
            cur,
            tenant_code=tenant_code,
            schema=schema,
            master_schema=master_schema,
            env_code=env_code,
            email_meta=email_meta,
            parsed=parsed,
            defaults=defaults,
            received_at=received_at,
            auto_create_if_clean=auto_create_if_clean,
            existing_ens_staging_id=existing_ens_staging_id,
            create_header_from_email_body=create_header_from_email_body,
            ing_line_ids=ing_line_ids,
        )

    return tenant_code, env_code, parsed, result


def _sales_orders_log_status(result) -> str:
    if result.blockers:
        return 'REVIEW' if result.ens_staging_id else 'FAILED'
    if result.needs_review:
        return 'REVIEW'
    return 'STAGED'


def _log_sales_orders_receive(filename, tenant_code, parsed, result, *, source, error=None):
    cons_count = len(parsed.consignments)
    goods_count = sum(len(c.goods) for c in parsed.consignments)
    notes = [
        f'{source}: Sales Orders Excel staged',
        f'ENS={result.ens_staging_id or "---"}',
        f'{cons_count} consignments',
        f'{goods_count} goods',
    ]
    if result.diff_flags:
        notes.append('diffs=' + '; '.join(result.diff_flags[:4]))
    if result.warnings:
        notes.append('warnings=' + '; '.join(result.warnings[:4]))
    if result.blockers:
        notes.append('blockers=' + '; '.join(result.blockers[:4]))
    _log_receive(
        filename,
        'EMAIL',
        tenant_code,
        ' | '.join(notes),
        'SALES_ORDER_STAGE',
        _sales_orders_log_status(result),
        error or (' | '.join(result.blockers[:4]) if result.blockers else None),
    )


def _sales_orders_json_payload(tenant_code, env_code, parsed, result):
    return {
        'ok': True,
        'mode': 'sales_orders_stage',
        'tenant_code': tenant_code,
        'env_code': env_code,
        'ens_staging_id': result.ens_staging_id,
        'ens_inserted': result.ens_inserted,
        'consignments': result.consignments,
        'diff_flags': result.diff_flags,
        'blockers': result.blockers,
        'warnings': result.warnings,
        'needs_review': result.needs_review,
        'parsed_summary': {
            'consignment_count': len(parsed.consignments),
            'goods_count': sum(len(c.goods) for c in parsed.consignments),
        },
    }


def _is_nonblocking_sales_orders_warning(message: str) -> bool:
    text = str(message or '').strip().lower()
    return text in {
        'row skipped - missing document no.',
        'row skipped — missing document no.',
        'row skipped -- missing document no.',
    }


def _blocking_sales_orders_warnings(warnings) -> list[str]:
    return [
        str(w)
        for w in (warnings or [])
        if not _is_nonblocking_sales_orders_warning(str(w))
    ]


def _sales_orders_result_for_notification(result, warnings: list[str]):
    filtered = copy.copy(result)
    filtered.warnings = warnings
    return filtered


def _stage_sales_order_email_attachments(message, attachments, *, source, no_sfd_reason='', auto_create=False):
    email_meta = build_email_metadata(message)
    email_body = extract_email_body_text(message)
    received_at = _received_at_from_email_meta(email_meta)
    tenant = _active_tenant()
    client_code = (tenant or {}).get('code') or (tenant or {}).get('schema') or 'BKD'
    master_schema = _sales_orders_master_schema(tenant)
    resolution = _resolve_sales_orders_email_target(client_code, email_body, received_at)
    if resolution['error']:
        tenant_code = (tenant or {}).get('code') or 'BKD'
        for item in attachments:
            _log_receive(
                item.get('filename') or 'Sales Orders.xlsx',
                'EMAIL',
                tenant_code,
                f'{source}: Sales Orders Excel ingestion blocked',
                'SALES_ORDER_STAGE',
                'FAILED',
                resolution['error'],
            )
        raise ValueError(resolution['error'])

    staged = []

    for item in attachments:
        try:
            tenant_code, env_code, parsed, result = _stage_sales_orders_excel(
                item['filename'],
                item['bytes'],
                email_body,
                received_at,
                auto_create=auto_create,
                no_sfd_reason=no_sfd_reason,
                create_header_from_email_body=resolution['create_header_from_email_body'],
                existing_ens_staging_id=resolution['existing_ens_staging_id'],
                master_schema_override=master_schema,
            )
        except Exception as exc:
            tenant = _active_tenant()
            _log_receive(
                item.get('filename') or 'Sales Orders.xlsx',
                'EMAIL',
                (tenant or {}).get('code') or 'BKD',
                f'{source}: Sales Orders Excel ingestion failed',
                'SALES_ORDER_STAGE',
                'FAILED',
                str(exc),
            )
            raise
        _log_sales_orders_receive(item['filename'], tenant_code, parsed, result, source=source)
        payload = _sales_orders_json_payload(tenant_code, env_code, parsed, result)
        payload['email_resolution_mode'] = resolution['resolution_mode']
        payload['target_ens_staging_id'] = resolution['existing_ens_staging_id']
        staged.append(payload)

    return email_meta, staged


def _email_intake_status_clause(status_filter: str) -> tuple[str, list]:
    """Return the SQL clause for the visible ING email intake status tabs."""
    status = (status_filter or 'ALL').strip().upper()
    if status in {'RECEIVED', 'PROCESSED'}:
        return (
            """
            AND m.Skipped = 0 AND m.AllowedSender = 1
            AND NOT EXISTS (
                SELECT 1
                FROM ING.BKD_ProcessLog p
                WHERE (
                    (p.SourceTable = 'ING.BKD_EmailMessage' AND p.SourceRecordId = m.EmailMessageId)
                    OR EXISTS (
                        SELECT 1 FROM ING.BKD_EmailAttachment a
                        WHERE a.EmailMessageId = m.EmailMessageId
                          AND p.SourceTable = 'ING.BKD_EmailAttachment'
                          AND p.SourceRecordId = a.EmailAttachmentId
                    )
                )
                  AND p.TransformStatus IN ('SUCCESS', 'FAILED')
            )
            """,
            [],
        )
    if status == 'SKIPPED':
        return " AND m.Skipped = 1", []
    if status == 'BLOCKED':
        return " AND m.AllowedSender = 0", []
    if status == 'FAILED':
        return (
            """
            AND (
                EXISTS (
                    SELECT 1
                    FROM ING.BKD_EmailAttachment a
                    WHERE a.EmailMessageId = m.EmailMessageId
                      AND a.Status = 'Error'
                )
                OR EXISTS (
                    SELECT 1
                    FROM ING.BKD_ProcessLog p
                    WHERE (
                        (p.SourceTable = 'ING.BKD_EmailMessage' AND p.SourceRecordId = m.EmailMessageId)
                        OR EXISTS (
                            SELECT 1 FROM ING.BKD_EmailAttachment a
                            WHERE a.EmailMessageId = m.EmailMessageId
                              AND p.SourceTable = 'ING.BKD_EmailAttachment'
                              AND p.SourceRecordId = a.EmailAttachmentId
                        )
                    )
                      AND p.TransformStatus = 'FAILED'
                )
            )
            """,
            [],
        )
    if status == 'STAGED':
        return (
            """
            AND EXISTS (
                SELECT 1
                FROM ING.BKD_ProcessLog p
                WHERE (
                    (p.SourceTable = 'ING.BKD_EmailMessage' AND p.SourceRecordId = m.EmailMessageId)
                    OR EXISTS (
                        SELECT 1 FROM ING.BKD_EmailAttachment a
                        WHERE a.EmailMessageId = m.EmailMessageId
                          AND p.SourceTable = 'ING.BKD_EmailAttachment'
                          AND p.SourceRecordId = a.EmailAttachmentId
                    )
                )
                  AND p.TransformStatus = 'SUCCESS'
            )
            """,
            [],
        )
    return "", []


def _load_email_intake_counts(client_code: str) -> dict:
    """Counts for the email automation monitor on /ingest."""
    try:
        row = query_one(
            """
            SELECT
                COUNT(*) AS all_count,
                SUM(CASE WHEN m.Skipped = 0 AND m.AllowedSender = 1 THEN 1 ELSE 0 END) AS processed_count,
                SUM(CASE WHEN m.Skipped = 0 AND m.AllowedSender = 1
                          AND flags.has_failed = 0
                          AND flags.has_staged = 0 THEN 1 ELSE 0 END) AS received_count,
                SUM(CASE WHEN m.Skipped = 1 THEN 1 ELSE 0 END) AS skipped_count,
                SUM(CASE WHEN m.AllowedSender = 0 THEN 1 ELSE 0 END) AS blocked_count,
                SUM(CASE WHEN flags.has_failed = 1 THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN flags.has_staged = 1 THEN 1 ELSE 0 END) AS staged_count
            FROM ING.BKD_EmailMessage m
            OUTER APPLY (
                SELECT
                    CASE WHEN EXISTS (
                            SELECT 1
                            FROM ING.BKD_EmailAttachment a
                            WHERE a.EmailMessageId = m.EmailMessageId
                              AND a.Status = 'Error'
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM ING.BKD_ProcessLog p
                            WHERE (
                                (p.SourceTable = 'ING.BKD_EmailMessage' AND p.SourceRecordId = m.EmailMessageId)
                                OR EXISTS (
                                    SELECT 1 FROM ING.BKD_EmailAttachment a
                                    WHERE a.EmailMessageId = m.EmailMessageId
                                      AND p.SourceTable = 'ING.BKD_EmailAttachment'
                                      AND p.SourceRecordId = a.EmailAttachmentId
                                )
                            )
                              AND p.TransformStatus = 'FAILED'
                        )
                        THEN 1 ELSE 0 END AS has_failed,
                    CASE WHEN EXISTS (
                            SELECT 1
                            FROM ING.BKD_ProcessLog p
                            WHERE (
                                (p.SourceTable = 'ING.BKD_EmailMessage' AND p.SourceRecordId = m.EmailMessageId)
                                OR EXISTS (
                                    SELECT 1 FROM ING.BKD_EmailAttachment a
                                    WHERE a.EmailMessageId = m.EmailMessageId
                                      AND p.SourceTable = 'ING.BKD_EmailAttachment'
                                      AND p.SourceRecordId = a.EmailAttachmentId
                                )
                            )
                              AND p.TransformStatus = 'SUCCESS'
                        )
                        THEN 1 ELSE 0 END AS has_staged
            ) flags
            WHERE m.ClientCode = ?
            """,
            [client_code],
        ) or {}
    except Exception:
        row = {}
    return {
        'ALL': row.get('all_count') or 0,
        'RECEIVED': row.get('received_count') or 0,
        'PROCESSED': row.get('processed_count') or 0,
        'STAGED': row.get('staged_count') or 0,
        'FAILED': row.get('failed_count') or 0,
        'SKIPPED': row.get('skipped_count') or 0,
        'BLOCKED': row.get('blocked_count') or 0,
    }


def _load_email_intake_activity(client_code: str, status_filter: str, page: int, per_page: int) -> tuple[list, int, dict]:
    """Recent Graph/email intake rows with attachment and staging context."""
    clause, extra_params = _email_intake_status_clause(status_filter)
    params = [client_code] + extra_params
    offset = max(0, (page - 1) * per_page)
    counts = _load_email_intake_counts(client_code)

    try:
        total = (query_one(
            f"""
            SELECT COUNT(*) AS c
            FROM ING.BKD_EmailMessage m
            WHERE m.ClientCode = ?
            {clause}
            """,
            params,
        ) or {}).get('c', 0) or 0
    except Exception:
        total = counts.get((status_filter or 'ALL').strip().upper(), 0) or 0

    try:
        rows = query_all(
            f"""
            SELECT
                m.EmailMessageId,
                m.GraphMessageId,
                m.Mailbox,
                m.SenderEmail,
                m.SenderName,
                m.Subject,
                m.ReceivedAt,
                m.LoadedAt,
                m.AttachmentCount,
                m.AllowedSender,
                m.Skipped,
                m.SkipReason,
                m.MarkedReadAt,
                COALESCE(att.saved_count, 0) AS SavedAttachments,
                COALESCE(att.error_count, 0) AS ErrorAttachments,
                COALESCE(att.skipped_count, 0) AS SkippedAttachments,
                latest_process.ProcessLogId,
                latest_process.EventType,
                latest_process.TargetTable,
                latest_process.TargetRecordId,
                latest_process.TargetRef,
                latest_process.TransformStatus,
                latest_process.TransformError,
                latest_process.TransformedAt,
                CASE
                    WHEN m.Skipped = 1 THEN 'SKIPPED'
                    WHEN m.AllowedSender = 0 THEN 'BLOCKED'
                    WHEN COALESCE(att.error_count, 0) > 0
                      OR latest_process.TransformStatus = 'FAILED' THEN 'FAILED'
                    WHEN latest_process.TransformStatus = 'SUCCESS' THEN 'STAGED'
                    WHEN m.AllowedSender = 1 THEN 'RECEIVED'
                    ELSE 'RECEIVED'
                END AS AutomationStatus
            FROM ING.BKD_EmailMessage m
            OUTER APPLY (
                SELECT
                    SUM(CASE WHEN a.Status = 'Saved' THEN 1 ELSE 0 END) AS saved_count,
                    SUM(CASE WHEN a.Status = 'Error' THEN 1 ELSE 0 END) AS error_count,
                    SUM(CASE WHEN a.Status = 'Skipped' THEN 1 ELSE 0 END) AS skipped_count
                FROM ING.BKD_EmailAttachment a
                WHERE a.EmailMessageId = m.EmailMessageId
            ) att
            OUTER APPLY (
                SELECT TOP 1
                    p.ProcessLogId,
                    p.EventType,
                    p.TargetTable,
                    p.TargetRecordId,
                    p.TargetRef,
                    p.TransformStatus,
                    p.TransformError,
                    p.TransformedAt
                FROM ING.BKD_ProcessLog p
                WHERE (
                    (p.SourceTable = 'ING.BKD_EmailMessage' AND p.SourceRecordId = m.EmailMessageId)
                    OR EXISTS (
                        SELECT 1 FROM ING.BKD_EmailAttachment a
                        WHERE a.EmailMessageId = m.EmailMessageId
                          AND p.SourceTable = 'ING.BKD_EmailAttachment'
                          AND p.SourceRecordId = a.EmailAttachmentId
                    )
                )
                ORDER BY COALESCE(p.TransformedAt, p.LoadedAt) DESC, p.ProcessLogId DESC
            ) latest_process
            WHERE m.ClientCode = ?
            {clause}
            ORDER BY COALESCE(m.ReceivedAt, m.LoadedAt) DESC, m.EmailMessageId DESC
            OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
            """,
            params + [offset, per_page],
        ) or []
    except Exception as exc:
        log.warning('Email intake activity query failed: %s', exc)
        rows = []

    return rows, total, counts


def _email_intake_has_success_trace(email_message_id: int) -> bool:
    row = query_one(
        """
        SELECT TOP 1 p.ProcessLogId
        FROM ING.BKD_ProcessLog p
        WHERE p.TransformStatus = 'SUCCESS'
          AND (
              (p.SourceTable = 'ING.BKD_EmailMessage' AND p.SourceRecordId = ?)
              OR EXISTS (
                  SELECT 1 FROM ING.BKD_EmailAttachment a
                  WHERE a.EmailMessageId = ?
                    AND p.SourceTable = 'ING.BKD_EmailAttachment'
                    AND p.SourceRecordId = a.EmailAttachmentId
              )
          )
        """,
        [email_message_id, email_message_id],
    )
    return bool(row)


def _email_message_from_graph_payload(message: dict) -> EmailMessage:
    email = EmailMessage()
    email['Subject'] = message.get('subject', '') or ''
    email['From'] = message.get('from', '') or ''
    if message.get('received'):
        email['Date'] = message.get('received') or ''
    if message.get('message_id'):
        email['Message-ID'] = message.get('message_id') or ''
    email.set_content(message.get('body') or '')
    return email


# ── Routes ─────────────────────────────────────────────────────────────────────

@ingest_bp.route('/')
def queue():
    """Operational monitor for Graph/email intake and automation outcomes."""
    page = int(request.args.get('page', 1))
    per_page = 30
    status_filter = (request.args.get('status', 'ALL') or 'ALL').strip().upper()
    tenant = _active_tenant()
    client_code = (tenant or {}).get('code') or 'BKD'

    email_rows, total, status_counts = _load_email_intake_activity(
        client_code, status_filter, page, per_page,
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    status_tabs = status_filter_tabs(
        status_counts,
        ['ALL', 'RECEIVED', 'STAGED', 'FAILED', 'SKIPPED', 'BLOCKED'],
        status_filter,
    )

    ingest_defaults = None
    imap_settings = None
    graph_settings = None
    try:
        ingest_defaults = resolve_ingest_defaults(tenant_code=tenant["code"])
    except Exception:
        pass
    try:
        imap_settings = resolve_imap_settings(tenant_code=tenant["code"])
    except Exception:
        pass
    try:
        graph_settings = resolve_graph_mail_settings(tenant_code=tenant["code"])
    except Exception:
        pass
    no_sfd_reason_choices = _load_no_sfd_reason_choices()
    creation_mode = _default_creation_mode(ingest_defaults)
    mailbox_fetch_tenants = []
    mailbox_fetch_default_tenant = client_code
    return render_template('ingest/queue.html',
                           docs=[], page=page, total_pages=total_pages,
                           total=total, status_filter=status_filter,
                           status_counts=status_counts,
                           status_tabs=status_tabs,
                           ingest_defaults=ingest_defaults,
                           imap_settings=imap_settings,
                           graph_settings=graph_settings,
                           panel='queue', queue_preview=[],
                           tenant=tenant,
                           no_sfd_reason_choices=no_sfd_reason_choices,
                           creation_mode=creation_mode,
                           attach_consignment=None,
                           bkd_sales_orders_ens_options=[],
                           email_activity_rows=email_rows,
                           mailbox_fetch_tenants=mailbox_fetch_tenants,
                           mailbox_fetch_default_tenant=mailbox_fetch_default_tenant)


@ingest_bp.route('/email-intake/<int:email_message_id>/retry', methods=['POST'])
def retry_email_intake(email_message_id):
    """Retry staging from one captured Graph email without creating duplicates."""
    tenant = _active_tenant()
    client_code = (tenant or {}).get('code') or 'BKD'
    row = query_one(
        """
        SELECT TOP 1 EmailMessageId, GraphMessageId, Subject, Mailbox
        FROM ING.BKD_EmailMessage
        WHERE EmailMessageId = ? AND ClientCode = ?
        """,
        [email_message_id, client_code],
    )
    if not row:
        flash('Email intake row not found for this tenant.', 'warning')
        return redirect(url_for('ingest.queue'))
    if _email_intake_has_success_trace(email_message_id):
        flash('This email already has a successful staging trace; retry skipped to avoid duplicates.', 'info')
        return redirect(url_for('ingest.queue', status='STAGED'))

    graph_message_id = row.get('GraphMessageId') or ''
    if not graph_message_id:
        flash('This email row has no GraphMessageId, so it cannot be retried from Graph.', 'warning')
        return redirect(url_for('ingest.queue'))

    try:
        from app.db import db_cursor
        from app.ingestion.excel_sales_orders import parse_email_carrier_block
        from app.ingestion.graph_mail import GraphMailClient
        from app.ingestion.sales_orders_stage import stage_sales_orders_details_header_stg

        settings = resolve_graph_mail_settings(tenant_code=client_code)
        client = GraphMailClient(settings)
        if not client.is_configured():
            raise RuntimeError('Graph mailbox settings are not configured')
        message = client.get_message_by_id(graph_message_id)
        attachments = message.get('attachments') or []
        sales_order_attachments = [
            item for item in attachments
            if is_sales_order_workbook_attachment(item)
        ]

        if sales_order_attachments:
            email_message = _email_message_from_graph_payload(message)
            _email_meta, staged = _stage_sales_order_email_attachments(
                email_message,
                sales_order_attachments,
                source='graph_retry',
                auto_create=True,
            )
            first = staged[0] if staged else {}
            ens_id = first.get('ens_staging_id')
            with db_cursor() as cur:
                _log_ing_email_message_process(
                    cur,
                    client_code=client_code,
                    graph_message_id=graph_message_id,
                    env_code=_ing_env_code(),
                    event_type='INGEST_CONSIGNMENTS_GOODS',
                    target_table='STG.BKD_ENS_Headers',
                    target_record_id=ens_id,
                    target_ref=str(ens_id or ''),
                    transform_status='SUCCESS',
                )
            try:
                from app.ingestion.automation_notify import notify_ingest_success
                notify_ingest_success(
                    'consignments_email_received',
                    tenant_code=client_code,
                    stg_header_id=ens_id,
                    subject_hint=message.get('subject') or row.get('Subject') or '',
                    summary={
                        'ens_staging_id': ens_id,
                        'attachment_count': len(sales_order_attachments),
                        'consignment_count': sum(len(s.get('consignments') or []) for s in staged),
                    },
                )
            except Exception as notify_exc:
                log.warning('retry notify_ingest_success consignments error: %s', notify_exc)
            flash(f'Retry staged Sales Orders email into ENS #{ens_id or "?"}.', 'success')
            return redirect(url_for('ingest.queue', status='STAGED'))

        received_at = _received_at_from_email_meta({'date': message.get('received') or ''})
        email_meta = parse_email_carrier_block(message.get('body') or '', received_at=received_at)
        if not email_meta.raw_block:
            raise ValueError("No 'DETAILS FOR' carrier block found in Graph email body")

        defaults = resolve_ingest_defaults(tenant_code=client_code)
        env_code = _ing_env_code()
        with db_cursor() as cur:
            ens_id, inserted = stage_sales_orders_details_header_stg(
                cur,
                tenant_code=client_code,
                env_code=env_code,
                email_meta=email_meta,
                defaults=defaults,
                received_at=received_at,
                source='EXCEL_SALES_ORDERS_DETAILS',
                overwrite=True,
            )
            _log_ing_email_message_process(
                cur,
                client_code=client_code,
                graph_message_id=graph_message_id,
                env_code=env_code,
                event_type='INGEST_ENS_HEADER',
                target_table='STG.BKD_ENS_Headers',
                target_record_id=ens_id,
                target_ref=email_meta.conveyance_ref or email_meta.identity_no_of_transport or str(ens_id),
                transform_status='SUCCESS',
            )
        try:
            from app.ingestion.automation_notify import notify_ingest_success
            notify_ingest_success(
                'ens_email_received',
                tenant_code=client_code,
                stg_header_id=ens_id,
                subject_hint=message.get('subject') or row.get('Subject') or '',
                summary={
                    'ens_local_draft_id': ens_id,
                    'ens_inserted': inserted,
                    'conveyance_ref': email_meta.conveyance_ref,
                    'arrival_date_time': email_meta.arrival_date_time,
                    'transport_identity': email_meta.identity_no_of_transport,
                },
            )
        except Exception as notify_exc:
            log.warning('retry notify_ingest_success ENS error: %s', notify_exc)
        flash(f'Retry staged ENS header #{ens_id}.', 'success')
        return redirect(url_for('ingest.queue', status='STAGED'))
    except Exception as exc:
        friendly_error = _friendly_ingest_error(exc)
        try:
            from app.db import db_cursor
            with db_cursor() as cur:
                _log_ing_email_message_process(
                    cur,
                    client_code=client_code,
                    graph_message_id=graph_message_id,
                    env_code=_ing_env_code(),
                    event_type='INGEST_RETRY',
                    transform_status='FAILED',
                    transform_error=friendly_error,
                )
        except Exception as trace_exc:
            log.warning('retry failure process trace failed: %s', trace_exc)
        try:
            from app.ingestion.automation_notify import notify_pipeline_error
            notify_pipeline_error(
                'staging_exception',
                friendly_error,
                tenant_code=client_code,
                filename=row.get('Subject') or f'EmailMessageId {email_message_id}',
            )
        except Exception as notify_exc:
            log.warning('retry notify_pipeline_error failed: %s', notify_exc)
        flash(f'Email retry failed: {friendly_error}', 'danger')
        return redirect(url_for('ingest.queue', status='FAILED'))


@ingest_bp.route('/bkd-sales-orders/import', methods=['POST'])
def bkd_sales_orders_import():
    """Import Sales Orders rows under a selected editable ENS header."""
    return_url = _safe_ingest_next_url(request.form.get('next_url'))
    manual_ens_detail = str(request.form.get('manual_ens_detail') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    if not _local_autostage_enabled():
        _flash_ingest_disabled()
        return redirect(return_url)

    tenant = _active_tenant()
    schema = tenant["schema"]
    try:
        ens_staging_id = int(request.form.get('ens_staging_id') or 0)
    except (TypeError, ValueError):
        ens_staging_id = 0
    if ens_staging_id <= 0:
        flash('Choose the ENS header that should receive the Sales Orders rows.', 'warning')
        return redirect(return_url)

    selected_header = _load_bkd_sales_orders_ens_header(schema, ens_staging_id)
    if not selected_header:
        flash('The selected ENS header could not be found or is locked by its current TSS/local status.', 'danger')
        return redirect(return_url)

    upload = request.files.get('sales_orders_file')
    if upload is None or not upload.filename:
        flash('Choose a Sales Orders .xlsx workbook first.', 'warning')
        return redirect(return_url)
    if not upload.filename.lower().endswith('.xlsx'):
        flash('Only .xlsx Sales Orders workbooks are accepted for this import.', 'danger')
        return redirect(return_url)

    xlsx_bytes = upload.read()
    workbook_format, allowed_for_tenant = _sales_orders_workbook_allowed_for_tenant(xlsx_bytes)
    if not allowed_for_tenant:
        _flash_sales_orders_tenant_block()
        return redirect(return_url)

    try:
        preview = _build_sales_orders_preview(
            {'filename': upload.filename, 'bytes': xlsx_bytes},
            '',
            _selected_no_sfd_reason(),
            creation_mode=_selected_creation_mode(),
            require_email_body=False,
            existing_ens_staging_id=ens_staging_id,
            selected_header=selected_header,
            return_url=return_url,
            suppress_auto_pipeline=manual_ens_detail,
        )
        return _render_sales_orders_preview(preview)
    except Exception as exc:
        log.exception('sales-order selected-ENS import failed')
        _flash_ingest_exception(
            f'Sales Orders preview failed: {exc}',
            exc,
            call_type='SALES_ORDERS_IMPORT_PREVIEW',
            payload={'filename': upload.filename, 'workbook_format': workbook_format},
        )
    return redirect(return_url)


@ingest_bp.route('/receive', methods=['POST'])
def receive():
    """
    Webhook endpoint — accepts a multipart file upload, routes it,
    and forwards to the ingest service.

    Accepts:
        POST /ingest/receive
        Content-Type: multipart/form-data
        Field: file (the document)
        Field: source (optional label, e.g. 'local_watchdog')
        Header: X-API-Key (optional — matched against INGEST_WEBHOOK_KEY env var)

    Returns JSON.
    """
    auth_error = _check_webhook_auth()
    if auth_error:
        return auth_error

    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'No file field in request'}), 400

    f = request.files['file']
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'Empty file'}), 400

    filename = f.filename
    if not _allowed(filename):
        return jsonify({'ok': False, 'error': f'File type not allowed: {Path(filename).suffix}'}), 400

    file_bytes = f.read()
    source = request.form.get('source', 'http_receive')

    if _local_autostage_enabled() and Path(filename).suffix.lower() in {'.pdf', '.csv', '.zip'}:
        try:
            parsed_invoices, route_info, stage_result = _stage_uploaded_invoices(
                [{'filename': filename, 'bytes': file_bytes}],
                source=source,
                channel='EMAIL' if source == 'imap_email' else 'UPLOAD',
                no_sfd_reason=_selected_no_sfd_reason(),
            )
            route = route_info[0] if route_info else {}
            _log_receive(
                filename,
                route.get('channel'),
                route.get('customer_code'),
                route.get('routing_notes'),
                'LOCAL_STAGE',
                'STAGED',
            )
            return jsonify({
                'ok': True,
                'mode': 'local_autostage',
                'filename': filename,
                'parsed_invoice_count': len(parsed_invoices),
                'declaration_id': stage_result.get('declaration_id'),
                'staging_ens_id': stage_result.get('staging_ens_id'),
                'consignment_ids': stage_result.get('consignment_ids'),
                'goods_ids': stage_result.get('goods_ids'),
                'warnings': stage_result.get('warnings') or [],
                'auto_pipeline': [],
            }), 200
        except Exception as exc:
            log.warning('Local autostage failed for %s, falling back to forward path: %s', filename, exc)

    # Route the document using the prefix/profile logic
    try:
        from ingest_service.blueprints.pdf_ingestion.router import route_document, extract_prefix
        profiles = _get_profiles()
        result = route_document(
            filename=filename,
            text_content='',        # text extraction happens in ingest service
            pdf_metadata={},
            page_count=0,
            logo_hash=None,
            db_profiles=profiles,
        )
        channel       = result.channel
        customer_code = result.customer_code
        routing_notes = '; '.join(result.routing_notes)
        is_unmapped   = result.is_unmapped
    except ImportError:
        # Ingest service not available in this context — use simple prefix match
        prefix = _simple_prefix(filename)
        profiles = _get_profiles()
        matched = next((p for p in profiles if p.get('customer_code', '').upper() == prefix), None)
        channel = 'TEMPLATE' if matched else 'UNMAPPED'
        customer_code = matched['customer_code'] if matched else None
        routing_notes = f'Simple prefix match: {prefix or "none"}'
        is_unmapped = not matched
    except Exception as e:
        log.error('Routing error for %s: %s', filename, e)
        channel = 'UNMAPPED'
        customer_code = None
        routing_notes = f'Routing error: {e}'
        is_unmapped = True

    # Forward to ingest service
    ingest_url = os.environ.get('INGEST_SERVICE_URL', '').rstrip('/')
    if not ingest_url:
        _log_receive(filename, channel, customer_code, routing_notes, None, 'FAILED',
                     'INGEST_SERVICE_URL not configured')
        return jsonify({'ok': False, 'error': 'INGEST_SERVICE_URL not set — cannot forward'}), 503

    ok, resp_json, err = _forward_to_ingest(file_bytes, filename, channel, ingest_url)

    status = 'ROUTING' if ok else 'FAILED'
    _log_receive(filename, channel, customer_code, routing_notes, ingest_url, status, err)

    log.info('Received: %s → channel=%s, customer=%s, forwarded=%s',
             filename, channel, customer_code, ok)

    return jsonify({
        'ok': ok,
        'filename': filename,
        'channel': channel,
        'customer_code': customer_code,
        'is_unmapped': is_unmapped,
        'routing_notes': routing_notes,
        'ingest_response': resp_json,
        'error': err,
    }), 200 if ok else 502


@ingest_bp.route('/bulk-delete-logs', methods=['POST'])
def bulk_delete_logs():
    """Delete the selected DocIngestDocument rows (queue log history only).

    Posts `selected_ids` as a multi-value form field, matching the same
    Select mode pattern used by /declarations, /consignments, /supdec.

    Staged ENS / consignments / goods are not touched; only the inbound
    queue log rows are removed.
    """
    raw_ids = request.form.getlist('selected_ids')
    status_filter = (request.form.get('status', 'ALL') or 'ALL').strip().upper()
    ids: list[int] = []
    for raw in raw_ids:
        try:
            ids.append(int(str(raw).strip()))
        except (TypeError, ValueError):
            continue
    if not ids:
        flash('No rows selected.', 'warning')
        return redirect(url_for('ingest.queue', status=status_filter))

    try:
        placeholders = ','.join(['?'] * len(ids))
        execute(f"DELETE FROM {S}.DocIngestDocument WHERE id IN ({placeholders})", ids)
        flash(f'Deleted {len(ids)} ingest log row(s). Staged data preserved.', 'success')
    except Exception as exc:
        log.exception('bulk_delete_logs failed')
        flash(f'Bulk delete failed: {exc}', 'danger')
    return redirect(url_for('ingest.queue', status=status_filter))


@ingest_bp.route('/<int:doc_id>/retry', methods=['POST'])
def retry_ingest_document(doc_id):
    """Retry a failed mailbox-backed ingestion row by running the mailbox worker now."""
    row = query_one(f"""
        SELECT id, original_filename, status, channel, routing_notes, error_message, retry_count
        FROM {S}.DocIngestDocument
        WHERE id = ?
    """, [doc_id])
    if not row:
        flash('Ingestion row was not found.', 'warning')
        return redirect(url_for('ingest.queue', status='FAILED'))

    if str(row.get('status') or '').upper() != 'FAILED':
        flash('Only failed ingestion rows can be retried from here.', 'warning')
        return redirect(url_for('ingest.queue', status='ALL'))

    if not _is_mailbox_retryable_doc(row):
        flash(
            'This failed row does not have a recoverable mailbox source. Upload the source file/email again.',
            'warning',
        )
        return redirect(url_for('ingest.queue', status='FAILED'))

    tenant = _active_tenant()
    provider = (request.form.get('provider') or '').strip().lower()
    if provider not in {'graph', 'imap'}:
        provider = _default_mailbox_provider((tenant or {}).get('code'))
    if not provider:
        flash('No mailbox provider is enabled. Enable GRAPH or IMAP settings before retrying.', 'warning')
        return redirect(url_for('ingest.queue', status='FAILED'))

    note = (
        f"Retry requested via {provider.upper()} for failed row #{doc_id} "
        f"at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    _append_ingest_retry_note(
        doc_id,
        note,
        status='FAILED',
        error_message=row.get('error_message') or '',
        increment_retry=True,
    )

    exit_code, output = _run_mailbox_worker(provider, limit=5)

    redirect_status = 'FAILED'
    if _mailbox_worker_output_has_failures(output):
        _append_ingest_retry_note(
            doc_id,
            f"Retry worker found mailbox message(s) but ingestion failed: {output[:700] or 'No output'}",
            status='FAILED',
            error_message=output or 'Mailbox retry failed.',
        )
        flash(f'Mailbox retry found message(s), but ingestion failed: {(output or "unknown error")[:220]}', 'danger')
    elif exit_code == 0 and 'OK ' in output:
        _append_ingest_retry_note(
            doc_id,
            f"Retry worker processed mailbox message(s): {output[:700]}",
            status='ROUTING',
            error_message='',
        )
        flash(
            'Mailbox retry ran and processed message(s). Check the latest Ingestion rows for the new staged result.',
            'success',
        )
        redirect_status = 'ALL'
    elif _mailbox_worker_output_has_skips(output):
        _append_ingest_retry_note(
            doc_id,
            f"Retry worker found mailbox message(s) but skipped them: {output[:700] or 'No output'}",
            status='FAILED',
            error_message=output or 'Mailbox retry skipped message(s).',
        )
        flash(f'Mailbox retry found message(s), but skipped them: {(output or "unknown reason")[:220]}', 'warning')
    elif exit_code == 0:
        _append_ingest_retry_note(
            doc_id,
            f"Retry worker completed but did not process a matching unread message. Output: {output[:700] or 'No output'}",
            status='FAILED',
            error_message='Retry completed but no unread mailbox message was processed. The email may already be marked read or moved.',
        )
        flash(
            'Retry ran, but no unread mailbox message was processed. The email may already be read or moved.',
            'warning',
        )
    else:
        _append_ingest_retry_note(
            doc_id,
            f"Retry worker failed: {output[:700] or 'No output'}",
            status='FAILED',
            error_message=output or 'Mailbox retry failed.',
        )
        flash(f'Mailbox retry failed: {(output or "unknown error")[:220]}', 'danger')

    return redirect(url_for('ingest.queue', status=redirect_status))


@ingest_bp.route('/fetch-emails', methods=['POST'])
def fetch_mailbox():
    """Run the configured mailbox worker once from the Ingest screen."""
    tenant = _active_tenant()
    tenant_code = (tenant or {}).get('code')
    provider = (request.form.get('provider') or 'auto').strip().lower()
    if provider not in {'auto', 'graph', 'imap'}:
        provider = 'auto'
    if provider == 'auto':
        provider = _default_mailbox_provider(tenant_code)

    if not provider:
        _log_mailbox_fetch_event(
            'auto',
            'FAILED',
            'Manual mailbox fetch blocked: no GRAPH or IMAP provider is enabled.',
            'No mailbox provider is enabled.',
        )
        _flash_ingest_technical(
            'No mailbox provider is enabled. Enable GRAPH or IMAP settings before fetching emails.',
            'warning',
        )
        return _mailbox_fetch_redirect('ALL', tenant_code)

    try:
        limit = int(request.form.get('limit') or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 25))

    exit_code, output = _run_mailbox_worker(provider, limit=limit, tenant_code=tenant_code)
    output_preview = (output or 'No output')[:700]
    if _mailbox_worker_output_has_failures(output):
        _log_mailbox_fetch_event(
            provider,
            'FAILED',
            f'Manual mailbox fetch found message(s), but ingestion failed via {provider.upper()}.',
            output_preview,
        )
        _flash_ingest_technical(
            f'Mailbox fetch found message(s), but ingestion failed via {provider.upper()}: {output_preview}',
            'danger',
        )
        return _mailbox_fetch_redirect('FAILED', tenant_code)

    if _mailbox_worker_output_has_missing_ens(output):
        _log_mailbox_fetch_event(
            provider,
            'REVIEW',
            f'Manual mailbox fetch processed message(s), but no ENS id was returned via {provider.upper()}.',
            output_preview,
        )
        _flash_ingest_technical(
            f'Mailbox fetch processed message(s), but no ENS id was returned via {provider.upper()}: {output_preview}',
            'warning',
        )
        return _mailbox_fetch_redirect('REVIEW', tenant_code)

    if exit_code == 0 and 'OK ' in output:
        processed_count = output.count('OK ')
        _log_mailbox_fetch_event(
            provider,
            'STAGED',
            f'Manual mailbox fetch processed {processed_count} item(s) via {provider.upper()} for {tenant_code or "active tenant"}.',
            output_preview,
        )
        flash(
            f'Fetched {processed_count} mailbox item(s) via {provider.upper()} for {tenant_code or "active tenant"}. '
            'Check the latest Ingestion rows.',
            'success',
        )
        return _mailbox_fetch_redirect('ALL', tenant_code)

    if _mailbox_worker_output_has_skips(output):
        _log_mailbox_fetch_event(
            provider,
            'REVIEW',
            f'Manual mailbox fetch found message(s), but skipped them via {provider.upper()}.',
            output_preview,
        )
        _flash_ingest_technical(
            f'Mailbox fetch found message(s), but skipped them via {provider.upper()}: {output_preview}',
            'warning',
        )
        return _mailbox_fetch_redirect('ALL', tenant_code)

    if exit_code == 0:
        _log_mailbox_fetch_event(
            provider,
            'REVIEW',
            (
                f'Manual mailbox fetch ran via {provider.upper()}, but no unread messages were processed. '
                'The email may already be read, moved, or outside the configured folder.'
            ),
            output_preview,
        )
        _flash_ingest_technical(
            'Mailbox fetch ran, but no unread messages were processed. '
            'The email may already be read, moved, or outside the configured folder.',
            'warning',
        )
        return _mailbox_fetch_redirect('ALL', tenant_code)

    _log_mailbox_fetch_event(
        provider,
        'FAILED',
        f'Manual mailbox fetch failed via {provider.upper()}.',
        output_preview,
    )
    _flash_ingest_technical(f'Mailbox fetch failed via {provider.upper()}: {output_preview}', 'danger')
    return _mailbox_fetch_redirect('FAILED', tenant_code)


@ingest_bp.route('/receive-sales-orders-details', methods=['POST'])
def receive_sales_orders_details():
    """Accept a carrier DETAILS email body and create/update the Sales Orders ENS draft."""
    auth_error = _check_webhook_auth()
    if auth_error:
        return auth_error
    if not _local_autostage_enabled():
        return _ingest_disabled_json()

    from app.db import db_cursor
    from app.ingestion.excel_sales_orders import parse_email_carrier_block
    from app.ingestion.sales_orders_stage import stage_sales_orders_details_header_stg

    tenant = _active_tenant()
    if tenant is None:
        return jsonify({'ok': False, 'error': 'Unknown tenant'}), 400

    email_body = request.form.get('email_body', '') or ''
    if not email_body.strip():
        return jsonify({'ok': False, 'error': 'No email_body supplied'}), 400

    received_at = _received_at_from_email_meta({
        'date': request.form.get('received_at') or request.form.get('date') or '',
    })
    email_meta = parse_email_carrier_block(email_body, received_at=received_at)
    if not email_meta.raw_block:
        try:
            from app.ingestion.automation_notify import notify_pipeline_error
            notify_pipeline_error(
                'parse_failure',
                '; '.join(email_meta.parse_warnings or []) or "No 'DETAILS FOR' carrier block found in email body",
                tenant_code=tenant.get('code') or 'BKD',
                filename=request.form.get('subject') or 'Sales Orders DETAILS email',
            )
        except Exception as _notify_exc:
            log.warning('notify_pipeline_error DETAILS parse error: %s', _notify_exc)
        return jsonify({
            'ok': False,
            'error': "No 'DETAILS FOR' carrier block found in email body",
            'warnings': email_meta.parse_warnings,
        }), 422

    defaults = resolve_ingest_defaults(tenant_code=tenant["code"])
    env_code = _ing_env_code()

    subject = (request.form.get('subject') or 'Sales Orders DETAILS email').strip()
    graph_message_id = (
        request.form.get('graph_message_id')
        or request.form.get('graph_id')
        or ''
    ).strip()
    sender_raw = request.form.get('from') or request.form.get('sender') or ''
    mailbox = request.form.get('mailbox') or request.form.get('mailbox_address') or ''
    try:
        with db_cursor() as cur:
            _ensure_ing_email_message_from_request(
                cur,
                client_code=tenant["code"],
                env_code=env_code,
                graph_message_id=graph_message_id,
                subject=subject,
                sender_raw=sender_raw,
                received_at=received_at,
                mailbox=mailbox,
            )
            ens_id, inserted = stage_sales_orders_details_header_stg(
                cur,
                tenant_code=tenant["code"],
                env_code=env_code,
                email_meta=email_meta,
                defaults=defaults,
                received_at=received_at,
                source='EXCEL_SALES_ORDERS_DETAILS',
                overwrite=True,
            )
            _log_ing_email_message_process(
                cur,
                client_code=tenant["code"],
                graph_message_id=graph_message_id,
                env_code=env_code,
                event_type='INGEST_ENS_HEADER',
                target_table='STG.BKD_ENS_Headers',
                target_record_id=ens_id,
                target_ref=email_meta.conveyance_ref or email_meta.identity_no_of_transport or str(ens_id),
                transform_status='SUCCESS',
            )
    except Exception as exc:
        friendly_error = _friendly_ingest_error(exc)
        log.exception('Sales Orders DETAILS staging failed: %s', friendly_error)
        try:
            with db_cursor() as cur:
                _log_ing_email_message_process(
                    cur,
                    client_code=tenant["code"],
                    graph_message_id=graph_message_id,
                    env_code=env_code,
                    event_type='INGEST_ENS_HEADER',
                    target_table='STG.BKD_ENS_Headers',
                    transform_status='FAILED',
                    transform_error=friendly_error,
                )
        except Exception as _trace_exc:
            log.warning('DETAILS failure process trace failed: %s', _trace_exc)
        _log_receive(
            subject[:500],
            'EMAIL',
            tenant.get('code'),
            (
                f"{request.form.get('source', 'graph_email_details') or 'graph_email_details'}: "
                f"DETAILS body staging failed: {friendly_error}"
            ),
            'SALES_ORDER_DETAILS',
            'FAILED',
            friendly_error,
        )
        try:
            from app.ingestion.automation_notify import notify_pipeline_error
            notify_pipeline_error(
                'staging_exception',
                friendly_error,
                tenant_code=tenant.get('code') or 'BKD',
                filename=subject,
            )
        except Exception as _notify_exc:
            log.warning('notify_pipeline_error DETAILS staging error: %s', _notify_exc)
        return jsonify({
            'ok': False,
            'mode': 'sales_orders_details',
            'tenant_code': tenant["code"],
            'env_code': env_code,
            'error': friendly_error,
            'warnings': email_meta.parse_warnings,
        }), 500

    auto_submit = _start_ens_header_auto_submit_worker(
        ens_id,
        tenant_code=tenant["code"],
        env_code=env_code,
        graph_message_id=graph_message_id,
        subject=subject,
        summary={
            'ens_inserted': inserted,
            'conveyance_ref': email_meta.conveyance_ref,
            'arrival_date_time': email_meta.arrival_date_time,
            'transport_identity': email_meta.identity_no_of_transport,
            'send_ingest_success_notification': True,
        },
    )

    _log_receive(
        subject[:500],
        'EMAIL',
        tenant.get('code'),
        (
            f"{request.form.get('source', 'graph_email_details') or 'graph_email_details'}: "
            f"DETAILS body staged ENS={ens_id}; auto_submit={auto_submit.get('stage')}"
        ),
        'SALES_ORDER_DETAILS',
        'STAGED',
    )

    return jsonify({
        'ok': True,
        'mode': 'sales_orders_details',
        'tenant_code': tenant["code"],
        'env_code': env_code,
        'ens_staging_id': ens_id,
        'ens_inserted': inserted,
        'auto_submit': auto_submit,
        'warnings': email_meta.parse_warnings,
    }), 200


@ingest_bp.route('/receive-sales-orders', methods=['POST'])
def receive_sales_orders():
    """Accept one email body + Excel sales-orders attachment and stage ENS+consignments+goods.

    Headers:
      X-API-Key (required if INGEST_WEBHOOK_KEY set)
      X-Tenant-Code (optional override)

    Form/multipart fields:
      tenant_code     (optional)
      email_body      (text, the email body containing the carrier `DETAILS FOR` block)
      received_at     (ISO datetime, optional; defaults to now())
      creation_mode   ("review_required"/"auto_create_if_clean"; defaults to INGEST_AUTO.MODE)
      auto_create     legacy boolean alias for creation_mode=auto_create_if_clean
      file            (the .xlsx attachment)

    Response: JSON with staging ids + diff_flags + blockers + warnings + needs_review.
    """
    auth_error = _check_webhook_auth()
    if auth_error:
        return auth_error
    if not _local_autostage_enabled():
        return _ingest_disabled_json()

    from datetime import datetime as _dt, timezone as _tz

    tenant = _active_tenant()
    if tenant is None:
        return jsonify({'ok': False, 'error': 'Unknown tenant'}), 400

    email_body = request.form.get('email_body', '') or ''
    received_at_raw = request.form.get('received_at', '').strip()
    try:
        received_at = (
            _dt.fromisoformat(received_at_raw.replace('Z', '+00:00'))
            if received_at_raw else _dt.now(_tz.utc)
        )
    except ValueError:
        return jsonify({'ok': False, 'error': f'Bad received_at: {received_at_raw!r}'}), 400

    defaults = resolve_ingest_defaults(tenant_code=tenant["code"])
    auto_create = _email_sales_orders_auto_create_requested(defaults)

    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return jsonify({'ok': False, 'error': "No 'file' field in request"}), 400
    if not upload.filename.lower().endswith('.xlsx'):
        return jsonify({'ok': False, 'error': 'Only .xlsx accepted'}), 400

    xlsx_bytes = upload.read()

    try:
        resolution = _resolve_sales_orders_email_target(tenant["code"], email_body, received_at)
        if resolution['error']:
            _log_receive(
                upload.filename,
                'EMAIL',
                tenant.get('code'),
                'Sales Orders Excel ingestion blocked',
                'SALES_ORDER_STAGE',
                'FAILED',
                resolution['error'],
            )
            try:
                from app.ingestion.automation_notify import notify_pipeline_error
                notify_pipeline_error(
                    resolution['resolution_mode'],
                    resolution['error'],
                    tenant_code=tenant.get('code') or 'BKD',
                    filename=upload.filename,
                )
            except Exception as _pe:
                log.warning('notify_pipeline_error error: %s', _pe)
            return jsonify({
                'ok': False,
                'error': resolution['error'],
                'mode': 'sales_orders_stage',
                'email_resolution_mode': resolution['resolution_mode'],
            }), 409

        tenant_code, env_code, parsed, result = _stage_sales_orders_excel(
            upload.filename,
            xlsx_bytes,
            email_body,
            received_at,
            auto_create=auto_create,
            no_sfd_reason=_selected_no_sfd_reason(),
            create_header_from_email_body=resolution['create_header_from_email_body'],
            existing_ens_staging_id=resolution['existing_ens_staging_id'],
            master_schema_override=_sales_orders_master_schema(tenant),
        )
        _log_sales_orders_receive(
            upload.filename,
            tenant_code,
            parsed,
            result,
            source=request.form.get('source', 'graph_email') or 'graph_email',
        )
        graph_message_id = (
            request.form.get('graph_message_id')
            or request.form.get('graph_id')
            or ''
        ).strip()
        subject = (request.form.get('subject') or upload.filename or 'Sales Orders email').strip()
        sender_raw = request.form.get('from') or request.form.get('sender') or ''
        mailbox = request.form.get('mailbox') or request.form.get('mailbox_address') or ''
        try:
            from app.db import db_cursor
            with db_cursor() as cur:
                _ensure_ing_email_message_from_request(
                    cur,
                    client_code=tenant_code,
                    env_code=env_code,
                    graph_message_id=graph_message_id,
                    subject=subject,
                    sender_raw=sender_raw,
                    received_at=received_at,
                    mailbox=mailbox,
                )
                _log_ing_email_message_process(
                    cur,
                    client_code=tenant_code,
                    graph_message_id=graph_message_id,
                    env_code=env_code,
                    event_type='INGEST_CONSIGNMENTS',
                    target_table='STG.BKD_ENS_Headers',
                    target_record_id=result.ens_staging_id,
                    target_ref=str(result.ens_staging_id or ''),
                    transform_status='SUCCESS',
                )
        except Exception as _trace_exc:
            log.warning('Sales Orders ING process trace failed: %s', _trace_exc)
    except Exception as exc:
        _log_receive(
            upload.filename,
            'EMAIL',
            tenant.get('code'),
            'Sales Orders Excel ingestion failed',
            'SALES_ORDER_STAGE',
            'FAILED',
            str(exc),
        )
        try:
            from app.ingestion.automation_notify import notify_pipeline_error
            notify_pipeline_error(
                'staging_exception',
                str(exc),
                tenant_code=tenant.get('code') or 'BKD',
                filename=upload.filename,
            )
        except Exception as _notify_exc:
            log.warning('notify_pipeline_error staging_exception error: %s', _notify_exc)
        return jsonify({'ok': False, 'error': str(exc)}), 400

    blocking_warnings = _blocking_sales_orders_warnings(result.warnings)

    # Best-effort failure/warning notification - fires on hard blockers AND on
    # goods-level warnings (missing commodity, weight, product master) that need
    # operator visibility. Never blocks the response.
    if result.blockers or blocking_warnings:
        try:
            from app.ingestion.automation_notify import notify_staging_failures
            notify_staging_failures(
                _sales_orders_result_for_notification(result, blocking_warnings),
                ens_staging_id=result.ens_staging_id,
                tenant_code=tenant_code,
                filename=upload.filename,
            )
        except Exception as _notify_exc:
            log.warning('Staging failure notification error: %s', _notify_exc)

    try:
        from app.ingestion.automation_notify import notify_ingest_success
        notify_ingest_success(
            'consignments_email_received',
            tenant_code=tenant_code,
            stg_header_id=result.ens_staging_id,
            filename=upload.filename,
            subject_hint=request.form.get('subject', '').strip(),
            summary={
                'ens_local_draft_id': result.ens_staging_id,
                'email_resolution_mode': resolution['resolution_mode'],
                'consignment_count': len(parsed.consignments),
                'goods_count': sum(len(c.goods) for c in parsed.consignments),
                'needs_review': bool(result.blockers or blocking_warnings),
                'blockers': len(result.blockers or []),
                'warnings': len(blocking_warnings),
            },
        )
    except Exception as _notify_exc:
        log.warning('notify_ingest_success Sales Orders error: %s', _notify_exc)

    auto_cargo = None
    if result.ens_staging_id:
        if result.blockers or blocking_warnings:
            detail = '; '.join((result.blockers or blocking_warnings or [])[:6])
            _log_auto_cargo_process(
                tenant_code=tenant_code,
                env_code=env_code,
                graph_message_id=request.form.get('graph_message_id') or request.form.get('graph_id') or '',
                event_type='AUTO_SUBMIT_CARGO_FAILED',
                stg_header_id=result.ens_staging_id,
                status='FAILED',
                error=detail or 'Sales Orders staging produced local validation warnings.',
            )
        else:
            auto_cargo = _start_prd_cargo_auto_submit_worker(
                result.ens_staging_id,
                tenant_code=tenant_code,
                env_code=env_code,
                graph_message_id=request.form.get('graph_message_id') or request.form.get('graph_id') or '',
                subject=request.form.get('subject', '').strip(),
                filename=upload.filename,
            )

    payload = _sales_orders_json_payload(tenant_code, env_code, parsed, result)
    payload['email_resolution_mode'] = resolution['resolution_mode']
    payload['target_ens_staging_id'] = resolution['existing_ens_staging_id']
    payload['auto_cargo_submit'] = auto_cargo
    return jsonify(payload), 200


@ingest_bp.route('/receive-batch', methods=['POST'])
def receive_batch():
    """Accept one email batch with many invoice PDFs and stage a single ENS."""
    auth_error = _check_webhook_auth()
    if auth_error:
        return auth_error

    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        return jsonify({'ok': False, 'error': 'No files field in request'}), 400
    if not _local_autostage_enabled():
        return _ingest_disabled_json()

    uploaded = []
    for f in files:
        if not _allowed(f.filename):
            return jsonify({'ok': False, 'error': f'File type not allowed: {Path(f.filename).suffix}'}), 400
        uploaded.append({
            'filename': f.filename,
            'bytes': f.read(),
        })

    email_meta = {
        'subject': request.form.get('subject', '').strip(),
        'from': request.form.get('from', '').strip(),
        'message_id': request.form.get('message_id', '').strip(),
        'date': request.form.get('date', '').strip(),
        'body': request.form.get('email_body', '') or '',
    }
    source = request.form.get('source', 'imap_email')
    parsed_invoices, route_info, stage_result = _stage_uploaded_invoices(
        uploaded,
        source=source,
        channel='EMAIL',
        email_meta=email_meta,
        no_sfd_reason=_selected_no_sfd_reason(),
    )

    for item, route in zip(uploaded, route_info):
        _log_receive(
            item['filename'],
            route.get('channel'),
            route.get('customer_code'),
            route.get('routing_notes'),
            'LOCAL_STAGE_BATCH',
            'STAGED',
        )

    return jsonify({
        'ok': True,
        'mode': 'local_batch_autostage',
        'files_received': len(uploaded),
        'parsed_invoice_count': len(parsed_invoices),
        'declaration_id': stage_result.get('declaration_id'),
        'staging_ens_id': stage_result.get('staging_ens_id'),
        'consignment_ids': stage_result.get('consignment_ids'),
        'goods_ids': stage_result.get('goods_ids'),
        'warnings': stage_result.get('warnings') or [],
        'auto_pipeline': [],
    }), 200


@ingest_bp.route('/receive-email', methods=['POST'])
def receive_email():
    """Accept a raw RFC822 email and auto-stage all PDF invoices into one ENS."""
    auth_error = _check_webhook_auth()
    if auth_error:
        return auth_error
    if not _local_autostage_enabled():
        return _ingest_disabled_json()

    message_bytes = b''
    if 'email' in request.files:
        email_file = request.files['email']
        message_bytes = email_file.read()
    elif 'message' in request.files:
        email_file = request.files['message']
        message_bytes = email_file.read()
    elif request.data:
        message_bytes = request.data

    if not message_bytes:
        return jsonify({'ok': False, 'error': 'No raw email payload supplied'}), 400

    message = parse_email_bytes(message_bytes)
    all_attachments = extract_supported_attachments(message)
    sales_order_attachments = [
        item for item in all_attachments
        if is_sales_order_workbook_attachment(item)
    ]
    if sales_order_attachments:
        email_meta, staged = _stage_sales_order_email_attachments(
            message,
            sales_order_attachments,
            source=request.form.get('source', 'smtp_inject'),
            no_sfd_reason=_selected_no_sfd_reason(),
            auto_create=_email_sales_orders_auto_create_requested(),
        )
        return jsonify({
            'ok': True,
            'mode': 'email_sales_orders',
            'subject': email_meta.get('subject'),
            'from': email_meta.get('from'),
            'attachment_count': len(sales_order_attachments),
            'sales_orders': staged,
        }), 200

    message, attachments, email_meta, parsed_invoices, route_info, stage_result = _stage_email_message(
        message_bytes,
        source=request.form.get('source', 'smtp_inject'),
        no_sfd_reason=_selected_no_sfd_reason(),
    )

    for item, route in zip(attachments, route_info):
        _log_receive(
            item['filename'],
            route.get('channel'),
            route.get('customer_code'),
            route.get('routing_notes'),
            'EMAIL_INJECT',
            'STAGED',
        )

    return jsonify({
        'ok': True,
        'mode': 'email_inject',
        'subject': email_meta.get('subject'),
        'from': email_meta.get('from'),
        'attachment_count': len(attachments),
        'parsed_invoice_count': len(parsed_invoices),
        'declaration_id': stage_result.get('declaration_id'),
        'staging_ens_id': stage_result.get('staging_ens_id'),
        'consignment_ids': stage_result.get('consignment_ids'),
        'goods_ids': stage_result.get('goods_ids'),
        'warnings': stage_result.get('warnings') or [],
        'auto_pipeline': [],
    }), 200


def _stage_sales_orders_batch_from_upload(
    xlsx_item,
    email_body='',
    no_sfd_reason='',
    creation_mode=None,
    preview_edits=None,
):
    """Shared sales-orders staging path used by preview_batch (xlsx detected)
    and the receive-sales-orders webhook. Review mode keeps editable drafts;
    auto-create mode marks clean batches ready for submission."""
    from datetime import datetime as _dt, timezone as _tz
    from app.db import db_cursor
    from app.ingestion.sales_orders_stage import stage_sales_orders_batch

    if not _local_autostage_enabled():
        _flash_ingest_disabled()
        return redirect(url_for('ingest.queue', panel='upload'))

    tenant = get_tenant()
    tenant_code = tenant['code']
    schema = tenant['schema']
    received_at = _dt.now(_tz.utc)
    try:
        email_meta, parsed = _parse_sales_orders_workbook(xlsx_item['bytes'], email_body, received_at)
        parsed.source_filename = xlsx_item['filename']
        _apply_no_sfd_reason_to_parsed(parsed, no_sfd_reason)
        _apply_sales_orders_preview_edits(parsed, preview_edits)
        defaults = resolve_ingest_defaults(tenant_code=tenant_code)
        selected_mode = creation_mode or _selected_creation_mode(defaults)
        env_code = _ing_env_code()
        with db_cursor() as cur:
            result = stage_sales_orders_batch_stg(
                cur,
                tenant_code=tenant_code, schema=schema, master_schema=schema,
                env_code=env_code, email_meta=email_meta, parsed=parsed,
                defaults=defaults, received_at=received_at,
                auto_create_if_clean=selected_mode == INGEST_CREATION_AUTO,
            )
        cons_count = len(result.consignments)
        goods_count = sum(len(c.get('goods', [])) for c in result.consignments)
        if result.ens_staging_id:
            verb = 'Created' if result.ens_inserted else 'Updated'
            state = 'ready for submission' if not result.needs_review else 'PENDING_REVIEW'
            flash(
                f"{verb} ENS #{result.ens_staging_id} from {xlsx_item['filename']} — "
                f"{cons_count} consignments, {goods_count} goods ({state}).",
                'success',
            )
        if result.diff_flags:
            flash('Email metadata differs from tenant fallback settings: ' + ' | '.join(result.diff_flags[:3]), 'warning')
        if result.warnings:
            flash('Warnings: ' + ' | '.join(result.warnings[:3]), 'warning')
        if result.blockers:
            flash('Blockers: ' + ' | '.join(result.blockers[:3]), 'danger')
        if result.ens_staging_id and not result.needs_review:
            _trigger_auto_pipeline(**_sales_orders_pipeline_scope(result))
    except Exception as exc:
        log.exception('sales-orders staging failed')
        friendly_error = _friendly_ingest_error(exc)
        _flash_ingest_exception(
            f'Sales-orders staging failed: {friendly_error}',
            RuntimeError(f"{friendly_error} Raw error: {exc}") if friendly_error != str(exc) else exc,
            call_type='SALES_ORDERS_UPLOAD',
            payload={'filename': xlsx_item['filename']},
        )
    return redirect(url_for('ingest.queue'))


def _stage_sales_orders_existing_ens_from_upload(
    xlsx_item,
    *,
    ens_staging_id,
    no_sfd_reason='',
    creation_mode=None,
    return_url=None,
    preview_edits=None,
    suppress_auto_pipeline=False,
):
    from datetime import timezone as _tz
    from app.db import db_cursor
    from app.ingestion.sales_orders_stage import stage_sales_orders_batch

    return_url = _safe_ingest_next_url(return_url)
    workbook_format, allowed_for_tenant = _sales_orders_workbook_allowed_for_tenant(xlsx_item.get('bytes') or b'')
    if not allowed_for_tenant:
        _flash_sales_orders_tenant_block()
        return redirect(return_url)
    if not _local_autostage_enabled():
        _flash_ingest_disabled()
        return redirect(return_url)

    tenant = _active_tenant()
    schema = tenant["schema"]
    selected_header = _load_bkd_sales_orders_ens_header(schema, ens_staging_id)
    if not selected_header:
        flash('The selected ENS header could not be found or is locked by its current TSS/local status.', 'danger')
        return redirect(return_url)

    received_at = datetime.now(_tz.utc)
    try:
        email_meta, parsed = _parse_sales_orders_workbook(
            xlsx_item['bytes'],
            '',
            received_at,
            require_email_body=False,
        )
        parsed.source_filename = xlsx_item['filename']
        _apply_no_sfd_reason_to_parsed(parsed, no_sfd_reason)
        _apply_sales_orders_preview_edits(parsed, preview_edits)
        defaults = resolve_ingest_defaults(tenant_code=tenant["code"])
        selected_mode = creation_mode or _selected_creation_mode(defaults)
        header_warnings = _bkd_sales_orders_existing_ens_warnings(selected_header)
        env_code = _ing_env_code()
        with db_cursor() as cur:
            result = stage_sales_orders_batch(
                cur,
                tenant_code=tenant["code"],
                schema=schema,
                master_schema=schema,
                env_code=env_code,
                email_meta=email_meta,
                parsed=parsed,
                defaults=defaults,
                received_at=received_at,
                auto_create_if_clean=selected_mode == INGEST_CREATION_AUTO and not header_warnings,
                existing_ens_staging_id=ens_staging_id,
            )
        if header_warnings:
            result.warnings = header_warnings + result.warnings
            result.needs_review = True
        cons_count = len(result.consignments)
        goods_count = sum(len(c.get('goods', [])) for c in result.consignments)
        state = 'ready for submission' if not result.needs_review else 'PENDING_REVIEW'
        flash(
            f"Imported {xlsx_item['filename']} into ENS header #{ens_staging_id} - "
            f"{cons_count} consignments, {goods_count} goods ({state}).",
            'success',
        )
        if result.warnings:
            flash('Warnings: ' + ' | '.join(result.warnings[:3]), 'warning')
        if result.blockers:
            flash('Blockers: ' + ' | '.join(result.blockers[:3]), 'danger')
        if result.ens_staging_id and not result.needs_review and not suppress_auto_pipeline:
            _trigger_auto_pipeline(**_sales_orders_pipeline_scope(result))
    except Exception as exc:
        log.exception('sales-order selected-ENS import failed')
        _flash_ingest_exception(
            f'Sales Orders import failed: {exc}',
            exc,
            call_type='SALES_ORDERS_IMPORT',
            payload={
                'filename': xlsx_item['filename'],
                'ens_staging_id': ens_staging_id,
                'workbook_format': workbook_format,
            },
        )
    return redirect(return_url)


def _sales_orders_raw_float(raw, key):
    value = (raw or {}).get(key)
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sales_orders_raw_text(raw, key):
    value = (raw or {}).get(key)
    return str(value or '').strip()


def _sales_orders_default(defaults, key, fallback=''):
    return getattr(defaults, key, fallback)


def _sales_orders_preview_float(value):
    text = str(value or '').strip().replace(',', '')
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _sales_orders_preview_int(value):
    number = _sales_orders_preview_float(value)
    if number is None:
        return None
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return None


def _sales_orders_preview_field(form_data, prefix, field):
    if not form_data:
        return None, False
    key = f'{prefix}__{field}'
    if key not in form_data:
        return None, False
    return form_data.get(key), True


def _apply_sales_orders_preview_edits(parsed, form_data=None):
    """Apply operator edits from the Sales Orders preview before staging."""
    if not form_data:
        return parsed

    consignment_fields = (
        'document_no',
        'sell_to_customer_no',
        'ship_to_name',
        'ship_to_address',
        'ship_to_address_2',
        'ship_to_city',
        'ship_to_county',
        'ship_to_postcode',
        'ship_to_country',
        'ship_to_phone',
        'ship_to_email',
    )
    raw_text_fields = (
        'goods_description',
        'commodity_code',
        'taric_code',
        'country_of_origin',
        'type_of_packages',
        'package_marks',
        'procedure_code',
        'additional_procedure_code',
        'valuation_method',
        'preference',
        'item_invoice_currency',
        'controlled_goods',
    )
    raw_number_fields = (
        'gross_mass_kg',
        'net_mass_kg',
        'number_of_packages',
        'number_of_individual_pieces',
    )

    for cons_idx, consignment in enumerate(parsed.consignments):
        cons_prefix = f'so_cons__{cons_idx}'
        for field in consignment_fields:
            value, present = _sales_orders_preview_field(form_data, cons_prefix, field)
            if present:
                setattr(consignment, field, str(value or '').strip())

        generate_sd_value, generate_sd_present = _sales_orders_preview_field(form_data, cons_prefix, 'generate_SD')
        if generate_sd_present:
            generate_sd = 'yes' if str(generate_sd_value or '').strip().lower() in {'yes', 'y', 'true', '1', 'on'} else 'no'
            for line in consignment.goods or []:
                line.raw = dict(line.raw or {})
                line.raw['generate_SD'] = generate_sd

        for goods_idx, line in enumerate(consignment.goods):
            goods_prefix = f'so_goods__{cons_idx}__{goods_idx}'
            line.raw = dict(line.raw or {})

            value, present = _sales_orders_preview_field(form_data, goods_prefix, 'line_no')
            if present:
                line.line_no = _sales_orders_preview_int(value)

            value, present = _sales_orders_preview_field(form_data, goods_prefix, 'sku')
            if present:
                line.sku = str(value or '').strip()

            value, present = _sales_orders_preview_field(form_data, goods_prefix, 'quantity')
            if present:
                qty = _sales_orders_preview_float(value)
                line.quantity = qty
                line.quantity_base = qty

            value, present = _sales_orders_preview_field(form_data, goods_prefix, 'item_invoice_amount')
            if present:
                line.line_amount_excl_vat = _sales_orders_preview_float(value)
                line.raw['item_invoice_amount'] = line.line_amount_excl_vat

            line.document_no = consignment.document_no
            for field in raw_text_fields:
                value, present = _sales_orders_preview_field(form_data, goods_prefix, field)
                if present:
                    line.raw[field] = str(value or '').strip()
            for field in raw_number_fields:
                value, present = _sales_orders_preview_field(form_data, goods_prefix, field)
                if present:
                    line.raw[field] = _sales_orders_preview_float(value)
    return parsed


def _sales_orders_goods_preview(cursor, master_schema, parsed, defaults):
    from app.ingestion.excel_sales_orders import resolve_product_master
    from app.ingestion.sales_orders_stage import (
        _builtin_sales_order_product_fallback,
        _compute_weight,
        _line_base_quantity,
        _package_type_text,
    )

    consignments = []
    flat_warnings = []
    goods_warning_count = 0
    missing_weight_count = 0
    missing_product_count = 0
    missing_commodity_count = 0

    for cons in parsed.consignments:
        cons_raw = {}
        for line in cons.goods:
            if line.raw:
                cons_raw = line.raw
                break
        generate_sd_value = _sales_orders_raw_text(cons_raw, 'generate_SD').lower()
        cons_preview = {
            'document_no': cons.document_no,
            'sell_to_customer_no': cons.sell_to_customer_no,
            'ship_to_name': cons.ship_to_name,
            'ship_to_address': cons.ship_to_address,
            'ship_to_address_2': cons.ship_to_address_2,
            'ship_to_city': cons.ship_to_city,
            'ship_to_county': cons.ship_to_county,
            'ship_to_postcode': cons.ship_to_postcode,
            'ship_to_country': cons.ship_to_country,
            'ship_to_phone': cons.ship_to_phone,
            'ship_to_email': cons.ship_to_email,
            'generate_SD': 'yes' if generate_sd_value in {'yes', 'y', 'true', '1', 'on'} else 'no',
            'goods': [],
            'warning_count': 0,
        }
        for line in cons.goods:
            raw = line.raw or {}
            product = (
                resolve_product_master(cursor, master_schema, cons.sell_to_customer_no, line.sku)
                or _builtin_sales_order_product_fallback(line.sku)
            )
            missing_master = not product
            product = product or {}
            commodity = _sales_orders_raw_text(raw, 'commodity_code') or product.get('commodity_code')
            description = (
                _sales_orders_raw_text(raw, 'goods_description')
                or product.get('description')
                or product.get('goods_description')
                or line.sku
            )

            gross_mass_kg = _sales_orders_raw_float(raw, 'gross_mass_kg')
            if gross_mass_kg is None:
                gross_mass_kg = _compute_weight(product, line, 'gross')
            net_mass_kg = _sales_orders_raw_float(raw, 'net_mass_kg')
            if net_mass_kg is None:
                net_mass_kg = _compute_weight(product, line, 'net')

            warnings = []
            template_ready = bool(
                commodity
                and _sales_orders_raw_text(raw, 'goods_description')
                and _sales_orders_raw_float(raw, 'gross_mass_kg') is not None
            )
            if missing_master and not template_ready:
                warnings.append('no product master match')
                missing_product_count += 1
            if not commodity:
                warnings.append('missing commodity_code')
                missing_commodity_count += 1

            missing_weight = []
            if gross_mass_kg is None or gross_mass_kg <= 0:
                missing_weight.append('gross')
            if net_mass_kg is None or net_mass_kg <= 0:
                missing_weight.append('net')
            if missing_weight:
                warnings.append('missing ' + '/'.join(missing_weight) + ' weight')
                missing_weight_count += 1

            if warnings:
                goods_warning_count += 1
                cons_preview['warning_count'] += 1
                flat_warnings.append(f"{cons.document_no}/{line.sku}: " + '; '.join(warnings))

            cons_preview['goods'].append({
                'document_no': cons.document_no,
                'line_no': line.line_no,
                'sku': line.sku,
                'description': description,
                'commodity_code': commodity,
                'quantity': _line_base_quantity(line),
                'number_of_packages': _sales_orders_raw_float(raw, 'number_of_packages') or line.quantity or 1,
                'number_of_individual_pieces': _sales_orders_raw_float(raw, 'number_of_individual_pieces'),
                'gross_mass_kg': gross_mass_kg,
                'net_mass_kg': net_mass_kg,
                'country_of_origin': _sales_orders_raw_text(raw, 'country_of_origin') or product.get('country_of_origin') or _sales_orders_default(defaults, 'country_of_origin'),
                'type_of_packages': _package_type_text(
                    _sales_orders_raw_text(raw, 'type_of_packages')
                    or product.get('package_type')
                    or line.uom_code,
                    _sales_orders_default(defaults, 'package_type'),
                ),
                'package_marks': _sales_orders_raw_text(raw, 'package_marks') or cons.ship_to_name[:140] or 'ADDR',
                'procedure_code': _sales_orders_raw_text(raw, 'procedure_code') or product.get('procedure_code') or _sales_orders_default(defaults, 'procedure_code'),
                'additional_procedure_code': _sales_orders_raw_text(raw, 'additional_procedure_code') or _sales_orders_default(defaults, 'additional_procedure_code'),
                'valuation_method': _sales_orders_raw_text(raw, 'valuation_method') or product.get('valuation_method') or _sales_orders_default(defaults, 'valuation_method'),
                'preference': _sales_orders_raw_text(raw, 'preference') or product.get('preference_code') or '',
                'taric_code': _sales_orders_raw_text(raw, 'taric_code'),
                'item_invoice_amount': line.line_amount_excl_vat,
                'item_invoice_currency': _sales_orders_raw_text(raw, 'item_invoice_currency') or _sales_orders_default(defaults, 'invoice_currency'),
                'controlled_goods': _sales_orders_raw_text(raw, 'controlled_goods') or _sales_orders_default(defaults, 'controlled_goods'),
                'warnings': warnings,
                'status': 'Review' if warnings else 'Ready',
            })
        consignments.append(cons_preview)

    goods_count = sum(len(c.goods) for c in parsed.consignments)
    return {
        'consignments': consignments,
        'warnings': flat_warnings,
        'summary': {
            'consignment_count': len(parsed.consignments),
            'goods_count': goods_count,
            'goods_warning_count': goods_warning_count,
            'missing_weight_count': missing_weight_count,
            'missing_product_count': missing_product_count,
            'missing_commodity_count': missing_commodity_count,
        },
    }


def _build_sales_orders_preview(
    xlsx_item,
    email_body='',
    no_sfd_reason='',
    creation_mode=None,
    *,
    require_email_body=True,
    existing_ens_staging_id=None,
    selected_header=None,
    return_url='',
    suppress_auto_pipeline=False,
):
    from datetime import datetime as _dt, timezone as _tz
    from app.db import db_cursor
    from app.ingestion.sales_orders_stage import _diff_email_vs_defaults, _transport_metadata_warnings

    tenant = get_tenant()
    tenant_code = tenant['code']
    schema = tenant['schema']
    received_at = _dt.now(_tz.utc)
    email_meta, parsed = _parse_sales_orders_workbook(
        xlsx_item['bytes'],
        email_body,
        received_at,
        require_email_body=require_email_body,
    )
    parsed.source_filename = xlsx_item['filename']
    _apply_no_sfd_reason_to_parsed(parsed, no_sfd_reason)
    defaults = resolve_ingest_defaults(tenant_code=tenant_code)
    selected_mode = creation_mode or _selected_creation_mode(defaults)

    blockers = []
    if not parsed.consignments:
        blockers.append('No consignments parsed from Excel')

    header_warnings = []
    header_warnings.extend(parsed.parse_warnings)
    if existing_ens_staging_id:
        header_warnings.extend(_bkd_sales_orders_existing_ens_warnings(selected_header))
    else:
        header_warnings.extend(email_meta.parse_warnings)
        header_warnings.extend(_diff_email_vs_defaults(email_meta, defaults))
        header_warnings.extend(_transport_metadata_warnings(email_meta, defaults))

    with db_cursor() as cur:
        goods_preview = _sales_orders_goods_preview(cur, schema, parsed, defaults)

    return {
        'filename': xlsx_item['filename'],
        'xlsx_payload': base64.b64encode(xlsx_item['bytes']).decode('ascii'),
        'email_body': email_body,
        'no_sfd_reason': no_sfd_reason,
        'creation_mode': selected_mode,
        'mode_label': 'Auto-create clean' if selected_mode == INGEST_CREATION_AUTO else 'Review required',
        'existing_ens_staging_id': existing_ens_staging_id,
        'target_ens': selected_header or None,
        'return_url': return_url,
        'suppress_auto_pipeline': bool(suppress_auto_pipeline),
        'blockers': blockers,
        'header_warnings': header_warnings,
        'goods_warnings': goods_preview['warnings'],
        'consignments': goods_preview['consignments'],
        'summary': goods_preview['summary'],
    }


def _render_sales_orders_preview(preview):
    return render_template('ingest/sales_orders_preview.html', preview=preview)


@ingest_bp.route('/upload-batch', methods=['POST'])
def upload_batch():
    """Portal form helper for manual Birkdale invoice upload."""
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        flash('Choose one or more Birkdale invoice PDFs first.', 'warning')
        return redirect(url_for('ingest.queue'))

    uploaded = [{'filename': f.filename, 'bytes': f.read()} for f in files]
    try:
        _parsed_invoices, route_info, stage_result = _stage_uploaded_invoices(
            uploaded,
            source='portal_upload',
            channel='UPLOAD',
            email_meta={'subject': request.form.get('batch_label', '').strip()},
            no_sfd_reason=_selected_no_sfd_reason(),
        )
        for item, route in zip(uploaded, route_info):
            _log_receive(
                item['filename'],
                route.get('channel'),
                route.get('customer_code'),
                route.get('routing_notes'),
                'PORTAL_UPLOAD',
                'STAGED',
            )
        flash(
            f"Created ENS #{stage_result.get('declaration_id')} with "
            f"{len(stage_result.get('consignment_ids') or [])} consignments and "
            f"{len(stage_result.get('goods_ids') or [])} goods items.",
            'success',
        )
        if stage_result.get('warnings'):
            flash(' | '.join(stage_result['warnings'][:4]), 'warning')
    except Exception as exc:
        if 'INGEST_AUTO.ENABLED' in str(exc):
            _flash_ingest_disabled()
        else:
            _flash_ingest_exception(
                f'Birkdale upload failed: {exc}',
                exc,
                call_type='INGEST_UPLOAD',
                payload={'filenames': [item['filename'] for item in uploaded]},
            )
    return redirect(url_for('ingest.queue'))


@ingest_bp.route('/preview-batch', methods=['POST'])
def preview_batch():
    """Parse uploaded Birkdale files and show editable review before staging."""
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        flash('Choose one or more Birkdale invoice PDFs or mapped CSVs first.', 'warning')
        return redirect(url_for('ingest.queue', panel='upload'))

    uploaded = [{'filename': f.filename, 'bytes': f.read()} for f in files]
    attach_consignment = _load_attach_consignment(request.form.get('consignment_id'))
    creation_mode = _selected_creation_mode()

    # Branch: any .xlsx in the batch → sales-orders pipeline (skips invoice parser).
    # Only routes when not attaching to an existing consignment, since
    # sales-orders staging always creates new ENS+consignments.
    xlsx_items = [u for u in uploaded if u['filename'].lower().endswith('.xlsx')]
    if xlsx_items and attach_consignment:
        flash(
            'Sales Orders Excel creates ENS and consignments, so it cannot be attached as goods to an existing consignment. '
            'Use Import Sales Orders from the ENS/consignment workflow, or upload invoice PDF/CSV/ZIP files here.',
            'warning',
        )
        return redirect(url_for('goods.create', cons_id=attach_consignment['staging_id']))
    if xlsx_items and not attach_consignment:
        try:
            preview = _build_sales_orders_preview(
                xlsx_items[0],
                email_body=request.form.get('email_body', '') or '',
                no_sfd_reason=_selected_no_sfd_reason(),
                creation_mode=creation_mode,
            )
            return _render_sales_orders_preview(preview)
        except Exception as exc:
            log.exception('sales-orders preview failed')
            _flash_ingest_exception(
                f'Sales-orders preview failed: {exc}', exc,
                call_type='SALES_ORDERS_PREVIEW',
                payload={'filename': xlsx_items[0]['filename']},
            )
            return redirect(url_for('ingest.queue', panel='upload'))

    try:
        if attach_consignment:
            if not attach_consignment.get('can_attach_goods'):
                flash(
                    attach_consignment.get('lock_reason') or 'This consignment is locked for new goods.',
                    'warning',
                )
                return redirect(url_for('consignments.detail', sid=attach_consignment['staging_id']))
            _parsed, _route_info, review = _review_uploaded_invoices_for_attach(
                uploaded,
                attach_consignment,
                source='portal_review_attach',
                channel='UPLOAD',
                email_meta={'subject': request.form.get('batch_label', '').strip()},
            )
        else:
            _parsed, _route_info, review = _review_uploaded_invoices(
                uploaded,
                source='portal_review',
                channel='UPLOAD',
                email_meta={'subject': request.form.get('batch_label', '').strip()},
            )
            review = _apply_no_sfd_reason_to_review(review, _selected_no_sfd_reason())
        return _render_review(review) if attach_consignment else _render_or_auto_create_review(review, creation_mode)
    except Exception as exc:
        if 'INGEST_AUTO.ENABLED' in str(exc):
            _flash_ingest_disabled()
        else:
            _flash_ingest_exception(
                f'Birkdale review failed: {exc}',
                exc,
                call_type='INGEST_REVIEW_UPLOAD',
                payload={
                    'filenames': [item['filename'] for item in uploaded],
                    'batch_label': request.form.get('batch_label', '').strip(),
                },
            )
        return redirect(url_for('ingest.queue', panel='upload'))


@ingest_bp.route('/sales-orders/confirm-preview', methods=['POST'])
def confirm_sales_orders_preview():
    filename = (request.form.get('filename') or 'Sales Orders.xlsx').strip()
    return_url = _safe_ingest_next_url(request.form.get('return_url'))
    payload = request.form.get('xlsx_payload') or ''
    try:
        xlsx_bytes = base64.b64decode(payload.encode('ascii'), validate=True)
    except Exception:
        flash('Sales Orders preview payload could not be read. Upload the Excel file again.', 'danger')
        return redirect(return_url)

    try:
        ens_staging_id = int(request.form.get('existing_ens_staging_id') or 0)
    except (TypeError, ValueError):
        ens_staging_id = 0

    if ens_staging_id > 0:
        return _stage_sales_orders_existing_ens_from_upload(
            {'filename': filename, 'bytes': xlsx_bytes},
            ens_staging_id=ens_staging_id,
            no_sfd_reason=request.form.get('no_sfd_reason', '') or '',
            creation_mode=request.form.get('creation_mode') or None,
            return_url=return_url,
            preview_edits=request.form,
            suppress_auto_pipeline=str(request.form.get('suppress_auto_pipeline') or '').strip().lower()
            in {'1', 'true', 'yes', 'on'},
        )

    return _stage_sales_orders_batch_from_upload(
        {'filename': filename, 'bytes': xlsx_bytes},
        email_body=request.form.get('email_body', '') or '',
        no_sfd_reason=request.form.get('no_sfd_reason', '') or '',
        creation_mode=request.form.get('creation_mode') or None,
        preview_edits=request.form,
    )


@ingest_bp.route('/upload-email', methods=['POST'])
def upload_email():
    """Portal form helper for uploading a raw email (.eml/.msg exported as RFC822)."""
    email_file = request.files.get('email')
    if not email_file or not email_file.filename:
        flash('Choose an .eml email file first.', 'warning')
        return redirect(url_for('ingest.queue'))
    try:
        message_bytes = email_file.read()
        message = parse_email_bytes(message_bytes)
        all_attachments = extract_supported_attachments(message)
        sales_order_attachments = [
            item for item in all_attachments
            if is_sales_order_workbook_attachment(item)
        ]
        if sales_order_attachments:
            email_meta, staged = _stage_sales_order_email_attachments(
                message,
                sales_order_attachments,
                source='portal_email_upload',
                no_sfd_reason=_selected_no_sfd_reason(),
                auto_create=_email_sales_orders_auto_create_requested(),
            )
            ens_ids = [item.get('ens_staging_id') for item in staged if item.get('ens_staging_id')]
            blockers = [b for item in staged for b in (item.get('blockers') or [])]
            if blockers:
                flash(
                    f"Email '{email_meta.get('subject') or email_file.filename}' contains Sales Orders Excel but needs review: "
                    + ' | '.join(blockers[:3]),
                    'warning',
                )
            else:
                flash(
                    f"Email '{email_meta.get('subject') or email_file.filename}' staged "
                    f"{len(sales_order_attachments)} Sales Orders workbook(s).",
                    'success',
                )
            if len(ens_ids) == 1:
                return redirect(url_for('declarations.detail', dec_id=ens_ids[0]))
            return redirect(url_for('ingest.queue'))

        _message, attachments, email_meta, _parsed, route_info, stage_result = _stage_email_message(
            message_bytes,
            source='portal_email_upload',
            no_sfd_reason=_selected_no_sfd_reason(),
        )
        for item, route in zip(attachments, route_info):
            _log_receive(
                item['filename'],
                route.get('channel'),
                route.get('customer_code'),
                route.get('routing_notes'),
                'PORTAL_EMAIL_UPLOAD',
                'STAGED',
            )
        flash(
            f"Email '{email_meta.get('subject') or email_file.filename}' created ENS #{stage_result.get('declaration_id')} "
            f"from {len(attachments)} PDF/CSV/ZIP attachments.",
            'success',
        )
        if stage_result.get('warnings'):
            flash(' | '.join(stage_result['warnings'][:4]), 'warning')
    except Exception as exc:
        if 'INGEST_AUTO.ENABLED' in str(exc):
            _flash_ingest_disabled()
        else:
            _flash_ingest_exception(
                f'Email injection failed: {exc}',
                exc,
                call_type='INGEST_EMAIL_UPLOAD',
                payload={'filename': email_file.filename},
            )
    return redirect(url_for('ingest.queue'))


@ingest_bp.route('/preview-email', methods=['POST'])
def preview_email():
    """Parse an uploaded email and show editable review before staging."""
    email_file = request.files.get('email')
    if not email_file or not email_file.filename:
        flash('Choose an .eml email file first.', 'warning')
        return redirect(url_for('ingest.queue', panel='upload'))
    attach_consignment = _load_attach_consignment(request.form.get('consignment_id'))
    creation_mode = _selected_creation_mode()
    try:
        message = parse_email_bytes(email_file.read())
        attachments = extract_supported_attachments(message)
        if not attachments:
            raise ValueError('No supported PDF/CSV/ZIP/XLSX attachments found in the email.')
        sales_order_attachments = [
            item for item in attachments
            if is_sales_order_workbook_attachment(item)
        ]
        if sales_order_attachments:
            if attach_consignment:
                raise ValueError('Sales Orders Excel creates ENS/consignments and cannot be attached to an existing consignment.')
            email_meta, staged = _stage_sales_order_email_attachments(
                message,
                sales_order_attachments,
                source='portal_email_review',
                no_sfd_reason=_selected_no_sfd_reason(),
                auto_create=creation_mode == INGEST_CREATION_AUTO,
            )
            ens_ids = [item.get('ens_staging_id') for item in staged if item.get('ens_staging_id')]
            blockers = [b for item in staged for b in (item.get('blockers') or [])]
            warnings = [w for item in staged for w in (item.get('warnings') or [])]
            if blockers:
                flash(
                    f"Sales Orders Excel from '{email_meta.get('subject') or email_file.filename}' needs review: "
                    + ' | '.join(blockers[:3]),
                    'warning',
                )
            else:
                flash(
                    f"Sales Orders Excel staged from '{email_meta.get('subject') or email_file.filename}'.",
                    'success',
                )
            if warnings:
                flash(' | '.join(warnings[:4]), 'warning')
            if len(ens_ids) == 1:
                return redirect(url_for('declarations.detail', dec_id=ens_ids[0]))
            return redirect(url_for('ingest.queue'))

        attachments = [
            item for item in attachments
            if not is_sales_order_workbook_attachment(item)
        ]
        email_meta = build_email_metadata(message)
        if attach_consignment:
            if not attach_consignment.get('can_attach_goods'):
                flash(
                    attach_consignment.get('lock_reason') or 'This consignment is locked for new goods.',
                    'warning',
                )
                return redirect(url_for('consignments.detail', sid=attach_consignment['staging_id']))
            _parsed, _route_info, review = _review_uploaded_invoices_for_attach(
                attachments,
                attach_consignment,
                source='portal_email_review_attach',
                channel='EMAIL',
                email_meta=email_meta,
            )
        else:
            _parsed, _route_info, review = _review_uploaded_invoices(
                attachments,
                source='portal_email_review',
                channel='EMAIL',
                email_meta=email_meta,
            )
            review = _apply_no_sfd_reason_to_review(review, _selected_no_sfd_reason())
        return _render_review(review) if attach_consignment else _render_or_auto_create_review(review, creation_mode)
    except Exception as exc:
        if 'INGEST_AUTO.ENABLED' in str(exc):
            _flash_ingest_disabled()
        else:
            _flash_ingest_exception(
                f'Email review failed: {exc}',
                exc,
                call_type='INGEST_REVIEW_EMAIL',
                payload={'filename': email_file.filename},
            )
        return redirect(url_for('ingest.queue', panel='upload'))


@ingest_bp.route('/review/attach-existing', methods=['POST'])
def review_attach_existing():
    """Switch a parsed invoice review into goods-only attach mode for an existing consignment."""
    raw_review = request.form.get('review_json') or '{}'
    try:
        review = json.loads(raw_review)
    except json.JSONDecodeError:
        flash('Review payload could not be read. Please upload the batch again.', 'danger')
        return redirect(url_for('ingest.queue', panel='upload'))

    review = _apply_review_form(review, request.form)
    target = (request.form.get('attach_target') or '').strip()
    try:
        invoice_idx_text, consignment_id_text = target.split(':', 1)
        invoice_index = int(invoice_idx_text)
    except (ValueError, AttributeError):
        return _render_review(review, error='Could not switch this invoice to an existing consignment.')

    consignment = _load_attach_consignment(consignment_id_text)
    if not consignment:
        return _render_review(review, error='The selected consignment could not be found.')
    if not consignment.get('can_attach_goods'):
        return _render_review(
            review,
            error=consignment.get('lock_reason') or 'This consignment is locked for new goods.',
        )

    try:
        attach_review = build_goods_attach_review_from_review(review, consignment, invoice_indexes=[invoice_index])
    except Exception as exc:
        return _render_review(review, error=str(exc))
    return _render_review(attach_review)


@ingest_bp.route('/review/confirm', methods=['POST'])
def confirm_review():
    """Create staging records from an approved review payload."""
    raw_review = request.form.get('review_json') or '{}'
    try:
        review = json.loads(raw_review)
    except json.JSONDecodeError:
        flash('Review payload could not be read. Please upload the batch again.', 'danger')
        return redirect(url_for('ingest.queue', panel='upload'))

    try:
        review = _apply_review_form(review, request.form)
        if review.get('review_mode') == REVIEW_MODE_ATTACH_GOODS:
            result = stage_goods_attach_review(
                review,
                tenant_code=_active_tenant()["code"],
                approved_by=session.get('username') or 'portal',
            )
            flash(
                f"Created {len(result.get('goods_ids') or [])} goods item(s) on "
                f"{(review.get('attach_target') or {}).get('reference_label') or 'the selected consignment'}.",
                'success',
            )
            if result.get('warnings'):
                flash(' | '.join(result['warnings'][:4]), 'warning')
            return redirect(url_for('consignments.detail', sid=result.get('staging_cons_id')))

        result = stage_invoice_review(
            review,
            tenant_code=_active_tenant()["code"],
            approved_by=session.get('username') or 'portal',
        )
        flash(
            f"Created ENS #{result.get('declaration_id')} with "
            f"{len(result.get('consignment_ids') or [])} consignments and "
            f"{len(result.get('goods_ids') or [])} goods items.",
            'success',
        )
        if result.get('warnings'):
            flash(' | '.join(result['warnings'][:4]), 'warning')
        _trigger_auto_pipeline(
            consignment_ids=result.get('consignment_ids'),
            goods_ids=result.get('goods_ids'),
        )
        return redirect(url_for('declarations.detail', dec_id=result.get('declaration_id')))
    except Exception as exc:
        if 'required field' in str(exc).lower():
            return _render_review(review, error=str(exc))
        if 'INGEST_AUTO.ENABLED' in str(exc):
            _flash_ingest_disabled()
            return redirect(url_for('ingest.queue', panel='upload'))
        return _render_review(review, error=f'Could not create staging records: {exc}')


def _scope_id_csv(values):
    seen = set()
    ids = []
    for value in values or []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        ids.append(str(parsed))
    return ','.join(ids)


def _sales_orders_pipeline_scope(result):
    consignment_ids = []
    goods_ids = []
    for cons in getattr(result, 'consignments', []) or []:
        if cons.get('staging_id'):
            consignment_ids.append(cons.get('staging_id'))
        for goods in cons.get('goods', []) or []:
            if goods.get('staging_id'):
                goods_ids.append(goods.get('staging_id'))
    return {
        'consignment_ids': consignment_ids,
        'goods_ids': goods_ids,
    }


def _trigger_auto_pipeline(consignment_ids=None, goods_ids=None, supdec_ids=None):
    """Fire validate + submit pipelines in a background thread after staging."""
    import subprocess
    import sys
    import threading

    tenant = _active_tenant()
    run_env = os.environ.copy()
    run_env['TENANT_CODE'] = tenant['code']
    run_env['TENANT_SCHEMA'] = tenant['schema']
    run_env.setdefault('PYTHONIOENCODING', 'utf-8')
    scope_env = {
        'VALIDATE_PIPELINE_CONSIGNMENT_IDS': _scope_id_csv(consignment_ids),
        'VALIDATE_PIPELINE_GOODS_IDS': _scope_id_csv(goods_ids),
        'VALIDATE_PIPELINE_SUPDEC_IDS': _scope_id_csv(supdec_ids),
        'SUBMIT_PIPELINE_CONSIGNMENT_IDS': _scope_id_csv(consignment_ids),
        'SUBMIT_PIPELINE_GOODS_IDS': _scope_id_csv(goods_ids),
        'SUBMIT_PIPELINE_SUPDEC_IDS': _scope_id_csv(supdec_ids),
    }
    for key, value in scope_env.items():
        if value:
            run_env[key] = value

    try:
        from app.tss_api import resolve_tss_settings
        resolved = resolve_tss_settings()
        for k, v in {
            'TSS_API_BASE_URL': (resolved.get('base_url') or '').rstrip('/'),
            'TSS_API_USERNAME': resolved.get('username') or '',
            'TSS_API_PASSWORD': resolved.get('password') or '',
        }.items():
            if v and not run_env.get(k):
                run_env[k] = v
    except Exception:
        pass

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _run():
        # validate → submit (creates ENS/DEC/goods) → sync (discovers SFDs) → submit (SFD update+submit)
        for script in (
            'scripts/validate_pipeline.py',
            'scripts/submit_pipeline.py',
            'scripts/sync_pipeline.py',
            'scripts/submit_pipeline.py',
        ):
            try:
                subprocess.run(
                    [sys.executable, os.path.join(project_root, script)],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=180,
                    cwd=project_root,
                    env=run_env,
                )
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


def _simple_prefix(filename):
    """Fallback prefix extractor when ingest_service router isn't importable."""
    import re
    stem = Path(filename).stem
    m = re.match(r'^([A-Z]{2,4})[\s\-_]', stem, re.I)
    if m:
        return m.group(1).upper()
    first = re.split(r'[\s\-_]', stem)[0]
    if re.match(r'^[A-Za-z]{2,4}$', first):
        return first.upper()
    return None


@ingest_bp.route('/test-smtp')
def test_smtp():
    """Quick smoke-test: sends a test email to confirm SMTP is working.

    Returns timing breakdown and the active SMTP target so operators can tell
    cold-start latency apart from SMTP auth/config errors without leaving the
    browser. Pass ?to=your@email.com (skip the send with ?dry=1)."""
    import time as _time
    to_addr = (request.args.get('to') or '').strip()
    dry_run = (request.args.get('dry') or '').lower() in {'1', 'true', 'yes'}
    if not dry_run and (not to_addr or '@' not in to_addr):
        return jsonify({'ok': False, 'error': 'Pass ?to=your@email.com (or ?dry=1 to inspect config without sending)'}), 400

    from app.email_utils import resolve_smtp_config

    smtp_cfg = resolve_smtp_config()
    target = {
        'server': smtp_cfg['server'],
        'port': smtp_cfg['port'],
        'sender': smtp_cfg['sender'],
        'username_set': bool(smtp_cfg['username']),
        'password_set': bool(smtp_cfg['password']),
        'smtp_enabled': smtp_cfg['enabled'],
    }

    if dry_run:
        return jsonify({'ok': True, 'dry_run': True, 'smtp': target})

    t0 = _time.monotonic()
    from app.email_utils import send_email
    html = '<h2>SMTP Test</h2><p>Fusion Flow V2 — SMTP is working correctly.</p>'
    ok, err = send_email(to_addr, 'Fusion Flow — SMTP Test', html)
    duration_ms = int((_time.monotonic() - t0) * 1000)
    return jsonify({
        'ok': ok,
        'to': to_addr,
        'error': err,
        'duration_ms': duration_ms,
        'smtp': target,
    })


@ingest_bp.route('/test-graph')
def test_graph():
    """Smoke-test for Microsoft Graph mailbox polling.

    Walks the same path the cron uses, but stages nothing:
      1. Load GRAPH.* config from AppConfiguration for active tenant.
      2. Acquire access_token via client_credentials.
      3. List top messages (with attachments) from configured folder.

    Returns JSON with status of each step. Use to debug Entra app permissions,
    consent, secret expiry, mailbox name, and folder access without writing
    anything to the DB.

    Query args:
      ?limit=N        cap message preview (default 5)
      ?folder=NAME    override configured folder for this test
      ?unread_only=1  override unread filter
    """
    from app.ingestion.graph_mail import GraphMailClient
    from dataclasses import replace

    tenant = get_tenant()
    settings = resolve_graph_mail_settings(tenant_code=tenant.get('code'))

    steps: list[dict] = []

    def step(name, ok, **kw):
        entry = {'step': name, 'ok': ok, **kw}
        steps.append(entry)
        return entry

    # 1. Config
    masked_secret = ('***' + settings.client_secret[-4:]) if settings.client_secret else ''
    step('config_loaded', settings.enabled, **{
        'enabled': settings.enabled,
        'tenant_id': settings.tenant_id or '(missing)',
        'client_id': settings.client_id or '(missing)',
        'client_secret': masked_secret or '(missing)',
        'mailbox': settings.mailbox or '(missing)',
        'folder': settings.folder or 'INBOX',
        'processed_folder': settings.processed_folder or '(none)',
        'unread_only': settings.unread_only,
    })
    if not settings.enabled:
        return jsonify({'ok': False, 'error': 'GRAPH.ENABLED is false', 'steps': steps}), 400

    # Optional overrides for this run only
    folder_override = (request.args.get('folder') or '').strip()
    unread_override = request.args.get('unread_only')
    limit = int(request.args.get('limit', '5'))
    runtime_settings = replace(
        settings,
        folder=folder_override or settings.folder,
        unread_only=(unread_override == '1') if unread_override is not None else settings.unread_only,
    )

    client = GraphMailClient(runtime_settings)
    if not client.is_configured():
        return jsonify({
            'ok': False,
            'error': 'GRAPH config incomplete (need TENANT_ID, CLIENT_ID, CLIENT_SECRET, MAILBOX)',
            'steps': steps,
        }), 400

    # 2. Token
    try:
        token = client._access_token()  # noqa: SLF001 — test endpoint, internal access OK
        step('token_acquired', True, token_prefix=token[:20] + '…' if token else '')
    except Exception as exc:
        step('token_acquired', False, error=str(exc))
        return jsonify({
            'ok': False,
            'error': f'Token request failed: {exc}',
            'hint': 'Check tenant_id, client_id, client_secret. Verify admin consent for Mail.Read application permission.',
            'steps': steps,
        }), 502

    # 3. List messages
    try:
        messages = client.scan_messages(limit=limit)
        preview = [
            {
                'id': m.get('id'),
                'subject': m.get('subject'),
                'from': m.get('from'),
                'receivedDateTime': m.get('received'),
                'attachment_count': len(m.get('attachments') or []),
                'body_present': bool(m.get('body')),
            }
            for m in messages
        ]
        step('messages_listed', True, count=len(messages), preview=preview)
    except Exception as exc:
        step('messages_listed', False, error=str(exc))
        return jsonify({
            'ok': False,
            'error': f'Listing messages failed: {exc}',
            'hint': (
                'Common causes: 403 = missing admin consent or app not granted Mail.Read. '
                '404 = mailbox name wrong or mailbox not in tenant. '
                'If Application Access Policy is set, ensure mailbox is in the policy scope.'
            ),
            'steps': steps,
        }), 502

    return jsonify({
        'ok': True,
        'mailbox': runtime_settings.mailbox,
        'folder': runtime_settings.folder,
        'unread_only': runtime_settings.unread_only,
        'steps': steps,
    })
