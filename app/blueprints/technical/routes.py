"""
Technical Blueprint — Job execution logs, API call history, status change timeline.
Separates technical/operational noise from the main Jobs orchestrator page.
"""
import json
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from app.db import execute, insert_api_call_log, query_all, query_one
from app.tenant import get_tenant
from app.tss_guidance import REFERENCE_RE, explain_tss_error, format_error_explanation

technical_bp = Blueprint(
    'technical', __name__,
    template_folder='../../templates/technical'
)

def _schema():
    return get_tenant()["schema"]


def _json_reference_candidates(value):
    if not value:
        return []
    try:
        payload = json.loads(str(value))
    except Exception:
        return []

    candidates = []
    stack = [payload]
    interesting_keys = {
        'reference',
        'declaration_number',
        'declaration_header_number',
        'declaration_header_reference',
        'ens_reference',
        'ens_consignment_number',
        'consignment_number',
        'dec_reference',
        'sfd_number',
        'sup_dec_number',
        'gmr_id',
        'goods_id',
    }
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, nested in current.items():
                if key in interesting_keys and nested:
                    candidates.append(str(nested))
                elif isinstance(nested, (dict, list)):
                    stack.append(nested)
                elif isinstance(nested, str):
                    match = REFERENCE_RE.search(nested)
                    if match:
                        candidates.append(match.group(0))
        elif isinstance(current, list):
            stack.extend(current)
    return candidates


def _extract_reference(row):
    for key in ('request_payload', 'response_message', 'response_json', 'error_detail', 'output_snippet', 'url'):
        for candidate in _json_reference_candidates(row.get(key)):
            match = REFERENCE_RE.search(candidate)
            if match:
                return match.group(0)
        text = str(row.get(key) or '')
        match = REFERENCE_RE.search(text)
        if match:
            return match.group(0)
    return None


def _reference_url(ref):
    # TSS ref → PRD staging_id mapping requires a DB lookup we don't have here.
    # All detail_by_ref routes are stubs in the PRD branch.
    return None


def _staging_url(row):
    staging_id = row.get('staging_id')
    if not staging_id:
        return None
    call_type = (row.get('call_type') or '').upper()
    try:
        if 'ENS' in call_type or 'HEADER' in call_type:
            return url_for('declarations.header_detail', staging_id=staging_id)
        if 'CONSIGNMENT' in call_type or 'CARGO' in call_type:
            return url_for('consignments.detail', sid=staging_id)
        # GOODS rows live inside consignment detail; no standalone goods page in PRD.
        # GMR, SUP/SDI detail routes are stubs in PRD.
    except Exception:
        return None
    return None


def _entity_label(row, ref):
    if ref:
        return ref
    if row.get('staging_id'):
        call_type = (row.get('call_type') or '').replace('_', ' ').title()
        return f"{call_type or 'Record'} #{row['staging_id']}"
    return ''


def _friendly_error_entity_label(row):
    call_type = (row.get('call_type') or '').upper()
    url = (row.get('url') or '').lower()
    endpoint_hint = f'{call_type} {url}'

    if 'CONSIGNMENT' in endpoint_hint or 'CARGO' in endpoint_hint:
        return 'this consignment'
    if 'GOODS' in endpoint_hint:
        return 'this goods item'
    if 'GMR' in endpoint_hint or 'GVMS' in endpoint_hint:
        return 'this GMR'
    if 'SUP' in endpoint_hint or 'SDI' in endpoint_hint:
        return 'this supplementary declaration'
    if 'ENS' in endpoint_hint or 'HEADER' in endpoint_hint:
        return 'this ENS header'
    return 'this record'


def _enrich_log_row(row):
    row = dict(row)
    explanation = explain_tss_error(
        row.get('error_detail'),
        row.get('response_message'),
        row.get('response_json'),
        row.get('output_snippet'),
        row.get('request_payload'),
        http_status=row.get('http_status'),
        entity_label=_friendly_error_entity_label(row),
    )
    ref = _extract_reference(row)
    row['friendly_error'] = explanation
    row['friendly_error_text'] = format_error_explanation(explanation)
    row['entity_ref'] = ref
    row['entity_url'] = _reference_url(ref) or _staging_url(row)
    row['entity_label'] = _entity_label(row, ref)
    return row


def _enrich_rows(rows):
    return [_enrich_log_row(row) for row in (rows or [])]


def _selected_ids_from_form():
    ids = []
    for raw in request.form.getlist('selected_ids'):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    return ids


def _redirect_tab_from_form(default='jobs'):
    allowed = {'jobs', 'ingest', 'api', 'notifications', 'alerts', 'local_errors', 'latency'}
    tab = (request.form.get('tab') or default).strip().lower()
    return tab if tab in allowed else default


def _cleanup_before_date_from_form():
    raw = (request.form.get('before_date') or '').strip()
    try:
        return datetime.strptime(raw, '%Y-%m-%d'), raw
    except (TypeError, ValueError):
        return None, raw


def _job_runs(schema_name, n=100):
    return _enrich_rows(query_all(f"""
        SELECT TOP {n}
            ApiExchangeId AS id,
            CallType AS call_type,
            Url AS url,
            HttpStatus AS http_status,
            ResponseMessage AS result,
            CAST(DurationMs AS FLOAT) / 1000.0 AS duration_sec,
            ResponseJson AS output_snippet,
            ErrorDetail AS error_detail,
            CalledAt AS called_at
        FROM TSS.BKD_API_Exchanges
        WHERE CallType LIKE 'JOB[_]%'
          AND ClientCode = ?
        ORDER BY CalledAt DESC
    """, [schema_name]))


def _api_calls(schema_name, n=200, call_type_filter=None):
    extra = f"AND CallType = ?" if call_type_filter else ""
    params = [schema_name] + ([call_type_filter] if call_type_filter else [])
    return _enrich_rows(query_all(f"""
        SELECT TOP {n}
            ApiExchangeId AS id,
            EntityId AS staging_id,
            CallType AS call_type,
            HttpMethod AS http_method,
            Url AS url,
            RequestPayloadJson AS request_payload,
            ResponseStatus AS response_status,
            ResponseMessage AS response_message,
            ResponseJson AS response_json,
            HttpStatus AS http_status,
            CAST(DurationMs AS FLOAT) / 1000.0 AS duration_sec,
            ErrorDetail AS error_detail,
            CalledAt AS called_at
        FROM TSS.BKD_API_Exchanges
        WHERE CallType NOT LIKE 'JOB[_]%'
          AND COALESCE(Flow, '') <> 'EMAIL_AUTOMATION'
          AND COALESCE(EntityKind, '') <> 'EMAIL'
          AND ClientCode = ?
          {extra}
        ORDER BY CalledAt DESC
    """, params))


def _notification_logs(schema_name, n=200):
    """Email notification audit rows, kept separate from real TSS API calls."""
    return _enrich_rows(query_all(f"""
        SELECT TOP {n}
            ApiExchangeId AS id,
            EntityId AS staging_id,
            CallType AS call_type,
            HttpMethod AS http_method,
            Url AS url,
            RequestPayloadJson AS request_payload,
            ResponseStatus AS response_status,
            ResponseMessage AS response_message,
            ResponseJson AS response_json,
            HttpStatus AS http_status,
            CAST(DurationMs AS FLOAT) / 1000.0 AS duration_sec,
            ErrorDetail AS error_detail,
            CalledAt AS called_at
        FROM TSS.BKD_API_Exchanges
        WHERE ClientCode = ?
          AND (
              Flow = 'EMAIL_AUTOMATION'
              OR EntityKind = 'EMAIL'
              OR CallType LIKE '%EMAIL%'
              OR HttpMethod = 'SMTP'
          )
        ORDER BY CalledAt DESC
    """, [schema_name]) or [])


def _api_call_by_id(schema_name, log_id):
    row = query_one(
        """
        SELECT
            ApiExchangeId AS id,
            EntityId AS staging_id,
            CallType AS call_type,
            HttpMethod AS http_method,
            Url AS url,
            RequestPayloadJson AS request_payload,
            ResponseStatus AS response_status,
            ResponseMessage AS response_message,
            ResponseJson AS response_json,
            HttpStatus AS http_status,
            CAST(DurationMs AS FLOAT) / 1000.0 AS duration_sec,
            ErrorDetail AS error_detail,
            CalledAt AS called_at
        FROM TSS.BKD_API_Exchanges
        WHERE ApiExchangeId = ?
          AND ClientCode = ?
        """,
        [log_id, schema_name],
    )
    return _enrich_log_row(row) if row else None


def _local_errors(schema_name, n=200):
    """Failures that originate locally (SQL/template/script crashes), not TSS API responses."""
    return _enrich_rows(query_all(f"""
        SELECT TOP {n}
            ApiExchangeId AS id,
            EntityId AS staging_id,
            CallType AS call_type,
            HttpMethod AS http_method,
            Url AS url,
            RequestPayloadJson AS request_payload,
            ResponseStatus AS response_status,
            ResponseMessage AS response_message,
            ResponseJson AS response_json,
            HttpStatus AS http_status,
            CAST(DurationMs AS FLOAT) / 1000.0 AS duration_sec,
            ErrorDetail AS error_detail,
            CalledAt AS called_at
        FROM TSS.BKD_API_Exchanges
        WHERE (CallType LIKE '%\\_FAILED' ESCAPE '\\'
            OR CallType LIKE 'LOCAL\\_%' ESCAPE '\\'
            OR ResponseStatus IN ('sql_error','template_error','script_error'))
          AND ClientCode = ?
        ORDER BY CalledAt DESC
    """, [schema_name]))


def _classify_status_change_entity(call_type):
    """Map an ApiCallLog call_type to the entity kind so the technical panel
    can link to the right detail page for non-ENS rows."""
    ct = (call_type or '').upper()
    if 'GVMS_GMR' in ct or 'GMR' in ct:
        return 'gmr'
    if 'SUP' in ct or 'SDI' in ct:
        return 'sdi'
    if 'GOODS' in ct:
        return 'goods'
    if 'SFD' in ct or 'SIMPLIFIED' in ct or 'FRONTIER' in ct:
        return 'sfd'
    if 'CONSIGN' in ct or 'CARGO' in ct:
        return 'consignment'
    if 'HEADER' in ct or ct in {'STATUS_CHANGE', 'STATUS_CHANGE_ALERT', 'CRITICAL_STATUS'}:
        return 'ens'
    return 'other'


def _status_changes(schema_name, n=200):
    """TSS mutation and status-change activity from TSS.BKD_API_Exchanges."""
    rows = query_all(f"""
        SELECT TOP {n}
            ApiExchangeId AS id,
            EntityId AS staging_id,
            CallType AS call_type,
            HttpMethod AS http_method,
            ResponseMessage AS change_detail,
            ResponseStatus AS response_status,
            CalledAt AS called_at,
            NULL AS external_ref,
            NULL AS dec_status
        FROM TSS.BKD_API_Exchanges
        WHERE (CallType IN ('STATUS_CHANGE','STATUS_CHANGE_ALERT','CRITICAL_STATUS')
           OR (HttpMethod = 'POST' AND (
                CallType LIKE 'CREATE[_]%'
                OR CallType LIKE 'SUBMIT[_]%'
                OR CallType LIKE 'UPDATE[_]%'
                OR CallType LIKE 'CANCEL[_]%'
                OR CallType LIKE 'RECALL[_]%'
           ))
           OR (CallType LIKE 'READ[_]%STATUS%' AND (
                ResponseMessage LIKE '% -> %'
                OR ResponseMessage LIKE '%->%'
                OR CHARINDEX(N'→', ResponseMessage) > 0
           )))
          AND ClientCode = ?
        ORDER BY CalledAt DESC
    """, [schema_name]) or []
    enriched = []
    for row in rows:
        item = dict(row) if not isinstance(row, dict) else dict(row)
        item['entity_kind'] = _classify_status_change_entity(item.get('call_type'))
        enriched.append(item)
    return enriched


def _ingestion_runs(schema_name, n=100):
    """Recent Graph/email ingestion events with attachment and staging trace."""
    try:
        return query_all(f"""
            SELECT TOP {n}
                m.EmailMessageId AS id,
                m.GraphMessageId AS graph_message_id,
                m.Mailbox AS mailbox,
                m.SenderEmail AS customer_code,
                m.SenderName AS sender_name,
                m.Subject AS original_filename,
                'EMAIL' AS doc_type,
                CASE
                    WHEN COALESCE(att.error_count, 0) > 0
                      OR latest_process.TransformStatus = 'FAILED' THEN 'FAILED'
                    WHEN m.Skipped = 1 THEN 'SKIPPED'
                    WHEN m.AllowedSender = 0 THEN 'BLOCKED'
                    WHEN latest_process.TransformStatus = 'SUCCESS' THEN 'STAGED'
                    ELSE 'PROCESSED'
                END AS status,
                'EMAIL_GRAPH' AS channel,
                m.SkipReason AS routing_notes,
                COALESCE(latest_process.TransformError, m.SkipReason) AS error_message,
                NULL AS overall_confidence,
                m.AttachmentCount AS page_count,
                NULL AS staging_ens_id,
                NULL AS staging_cons_id,
                m.ReceivedAt AS created_at,
                COALESCE(latest_process.TransformedAt, m.MarkedReadAt, m.LoadedAt) AS processing_completed_at,
                NULL AS ens_reference,
                NULL AS dec_reference,
                NULL AS sfd_reference,
                latest_process.ProcessLogId AS integration_log_id,
                'ING.BKD_ProcessLog' AS target_service,
                latest_process.TargetTable AS target_table,
                latest_process.TargetRecordId AS target_record_id,
                latest_process.TargetRef AS target_ref,
                latest_process.TransformStatus AS integration_status,
                latest_process.TransformError AS integration_error,
                latest_process.TransformedAt AS integrated_at,
                COALESCE(att.saved_count, 0) AS saved_attachments,
                COALESCE(att.error_count, 0) AS error_attachments,
                COALESCE(att.skipped_count, 0) AS skipped_attachments,
                m.AllowedSender AS allowed_sender,
                m.Skipped AS skipped,
                m.MarkedReadAt AS marked_read_at,
                m.LoadedAt AS loaded_at
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
            ORDER BY COALESCE(m.ReceivedAt, m.LoadedAt) DESC, m.EmailMessageId DESC
        """, [schema_name]) or []
    except Exception:
        try:
            return query_all(f"""
                SELECT TOP {n}
                    EmailMessageId AS id,
                    SenderEmail AS customer_code,
                    Subject AS original_filename,
                    'EMAIL' AS doc_type,
                    CASE WHEN Skipped=1 THEN 'Skipped' ELSE 'Processed' END AS status,
                    'EMAIL_GRAPH' AS channel,
                    SkipReason AS routing_notes,
                    SkipReason AS error_message,
                    NULL AS overall_confidence,
                    AttachmentCount AS page_count,
                    NULL AS staging_ens_id,
                    NULL AS staging_cons_id,
                    ReceivedAt AS created_at,
                    ReceivedAt AS processing_completed_at
                FROM ING.BKD_EmailMessage
                WHERE ClientCode = ?
                ORDER BY ReceivedAt DESC
            """, [schema_name]) or []
        except Exception:
            return []


def _api_stats(schema_name):
    return query_one("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN HttpStatus = 200 THEN 1 ELSE 0 END) AS ok,
            SUM(CASE WHEN HttpStatus != 200 OR HttpStatus IS NULL THEN 1 ELSE 0 END) AS failed,
            CAST(AVG(CAST(DurationMs AS FLOAT)) AS INT) AS avg_ms,
            MAX(DurationMs) AS max_ms
        FROM TSS.BKD_API_Exchanges
        WHERE CalledAt > DATEADD(day, -1, GETUTCDATE())
          AND COALESCE(Flow, '') <> 'EMAIL_AUTOMATION'
          AND COALESCE(EntityKind, '') <> 'EMAIL'
          AND ClientCode = ?
    """, [schema_name]) or {}


def _latency_by_type(schema_name):
    try:
        return query_all("""
            SELECT
                CallType AS call_type,
                COUNT(*) AS cnt,
                CAST(AVG(CAST(DurationMs AS FLOAT)) AS INT) AS avg_ms,
                MAX(DurationMs) AS max_ms
            FROM TSS.BKD_API_Exchanges
            WHERE CalledAt > DATEADD(day, -7, GETUTCDATE())
              AND DurationMs IS NOT NULL
              AND ClientCode = ?
            GROUP BY CallType
            ORDER BY avg_ms DESC
        """, [schema_name]) or []
    except Exception:
        return []


@technical_bp.route('/')
def index():
    tab = request.args.get('tab', 'ingest')
    if tab not in {'ingest', 'api', 'notifications', 'local_errors', 'alerts', 'jobs', 'latency'}:
        tab = 'ingest'
    tenant = get_tenant()
    schema_name = tenant["schema"]
    focus_log_id = request.args.get('log_id', type=int)
    api_calls = _api_calls(schema_name)
    focused_api_call = None
    if focus_log_id:
        focused_api_call = _api_call_by_id(schema_name, focus_log_id)
        if focused_api_call and not any((row.get("id") == focus_log_id) for row in api_calls):
            api_calls = [focused_api_call] + api_calls
    return render_template(
        'technical/index.html',
        tab=tab,
        tenant=tenant,
        focus_log_id=focus_log_id,
        focused_api_call=focused_api_call,
        job_runs=_job_runs(schema_name),
        api_calls=api_calls,
        notification_logs=_notification_logs(schema_name),
        ingestion_runs=_ingestion_runs(schema_name),
        status_changes=_status_changes(schema_name),
        local_errors=_local_errors(schema_name),
        api_stats=_api_stats(schema_name),
        latency_by_type=_latency_by_type(schema_name),
    )


@technical_bp.route('/clear-tenant-logs', methods=['POST'])
def clear_tenant_logs():
    schema_name = _schema()
    scope = (request.form.get('scope') or 'jobs').strip().lower()
    try:
        if scope == 'all':
            deleted = execute(
                "DELETE FROM TSS.BKD_API_Exchanges WHERE ClientCode = ?",
                [schema_name],
            )
        else:
            scope = 'jobs'
            deleted = execute(
                "DELETE FROM TSS.BKD_API_Exchanges WHERE CallType LIKE 'JOB[_]%' AND ClientCode = ?",
                [schema_name],
            )
        deleted_count = deleted if isinstance(deleted, int) and deleted >= 0 else None

        if scope == 'jobs':
            insert_api_call_log(
                schema_name,
                'ADMIN_CLEAR_TENANT_JOB_LOGS',
                http_method='POST',
                url=url_for('technical.clear_tenant_logs'),
                request_payload={'scope': scope},
                response_status='ok',
                response_message=(
                    f"Deleted {deleted_count} tenant job log rows."
                    if deleted_count is not None
                    else "Tenant job logs cleared."
                ),
            )

        if deleted_count is not None and scope == 'all':
            flash(f"All Tenant Logs cleared ({deleted_count} rows deleted).", "success")
        elif deleted_count is not None:
            flash(f"Tenant Job Logs cleared ({deleted_count} rows deleted).", "success")
        elif scope == 'all':
            flash("All Tenant Logs cleared.", "success")
        else:
            flash("Tenant Job Logs cleared.", "success")
    except Exception as exc:
        insert_api_call_log(
            schema_name,
            'LOCAL_CLEAR_TENANT_LOGS_FAILED',
            http_method='POST',
            url=url_for('technical.clear_tenant_logs'),
            request_payload={'scope': scope},
            response_status='local_error',
            response_message='Tenant Logs cleanup failed.',
            error_detail=str(exc),
        )
        flash(f"Could not clear Tenant Logs: {exc}", "danger")
    return redirect(url_for('technical.index', tab='jobs'))


@technical_bp.route('/bulk-delete-selected', methods=['POST'])
def bulk_delete_selected():
    schema_name = _schema()
    source = (request.form.get('source') or 'api').strip().lower()
    tab = _redirect_tab_from_form('jobs')
    selected_ids = _selected_ids_from_form()

    if not selected_ids:
        flash('No technical log rows selected.', 'warning')
        return redirect(url_for('technical.index', tab=tab))

    placeholders = ','.join(['?'] * len(selected_ids))

    try:
        if source == 'ingest':
            # Remove child attachments first to satisfy the FK from BKD_EmailAttachment → BKD_EmailMessage.
            try:
                execute(
                    f"DELETE FROM ING.BKD_EmailAttachment WHERE EmailMessageId IN ({placeholders})",
                    selected_ids,
                )
            except Exception:
                pass  # attachment table absent or rows already gone — message delete will raise if still blocked
            deleted = execute(
                f"DELETE FROM ING.BKD_EmailMessage WHERE EmailMessageId IN ({placeholders}) AND ClientCode = ?",
                selected_ids + [schema_name],
            )
            deleted_count = deleted if isinstance(deleted, int) and deleted >= 0 else len(selected_ids)
            flash(
                f'Deleted {deleted_count} ingestion log row(s).',
                'success',
            )
        else:
            deleted = execute(
                f"""
                DELETE FROM TSS.BKD_API_Exchanges
                WHERE ApiExchangeId IN ({placeholders})
                  AND ClientCode = ?
                  AND NOT (CallType LIKE 'JOB[_]%' AND ResponseMessage = 'STARTED')
                """,
                selected_ids + [schema_name],
            )
            deleted_count = deleted if isinstance(deleted, int) and deleted >= 0 else None
            if deleted_count is None:
                flash('Selected technical log rows deleted.', 'success')
            elif deleted_count < len(selected_ids):
                flash(
                    f'Deleted {deleted_count} technical log row(s). Running job rows were left in place; cancel them first.',
                    'warning',
                )
            else:
                flash(f'Deleted {deleted_count} technical log row(s).', 'success')
    except Exception as exc:
        try:
            insert_api_call_log(
                schema_name,
                'LOCAL_BULK_DELETE_TECHNICAL_LOGS_FAILED',
                http_method='POST',
                url=url_for('technical.bulk_delete_selected'),
                request_payload={'source': source, 'tab': tab, 'selected_count': len(selected_ids)},
                response_status='local_error',
                response_message='Bulk technical log cleanup failed.',
                error_detail=str(exc),
            )
        except Exception:
            pass
        flash(f'Could not delete selected technical log rows: {exc}', 'danger')

    return redirect(url_for('technical.index', tab=tab))


@technical_bp.route('/cleanup-email-intake-before', methods=['POST'])
def cleanup_email_intake_before():
    """Delete test email intake logs before a go-live date.

    Scope is intentionally narrow: ING email capture rows and, optionally,
    email-automation audit rows in TSS.BKD_API_Exchanges. STG business drafts
    and TSS entity mirror tables are never touched here.
    """
    schema_name = _schema()
    cutoff, raw_date = _cleanup_before_date_from_form()
    include_audit = (request.form.get('include_technical_audit') or '').strip().lower() in {'1', 'true', 'on', 'yes'}

    if cutoff is None:
        flash('Choose a valid cutoff date before deleting email automation test logs.', 'warning')
        return redirect(url_for('technical.index', tab='ingest'))

    try:
        # ProcessLog has no FK; remove message/attachment transform rows first.
        process_deleted = execute(
            """
            DELETE p
            FROM ING.BKD_ProcessLog p
            JOIN ING.BKD_EmailMessage m
              ON (
                  p.SourceTable = 'ING.BKD_EmailMessage'
                  AND p.SourceRecordId = m.EmailMessageId
              )
              OR EXISTS (
                  SELECT 1 FROM ING.BKD_EmailAttachment a
                  WHERE a.EmailMessageId = m.EmailMessageId
                    AND p.SourceTable = 'ING.BKD_EmailAttachment'
                    AND p.SourceRecordId = a.EmailAttachmentId
              )
            WHERE m.ClientCode = ?
              AND COALESCE(m.ReceivedAt, m.LoadedAt) < ?
            """,
            [schema_name, cutoff],
        )
        attachments_deleted = execute(
            """
            DELETE a
            FROM ING.BKD_EmailAttachment a
            JOIN ING.BKD_EmailMessage m
              ON m.EmailMessageId = a.EmailMessageId
            WHERE m.ClientCode = ?
              AND COALESCE(m.ReceivedAt, m.LoadedAt) < ?
            """,
            [schema_name, cutoff],
        )
        emails_deleted = execute(
            """
            DELETE FROM ING.BKD_EmailMessage
            WHERE ClientCode = ?
              AND COALESCE(ReceivedAt, LoadedAt) < ?
            """,
            [schema_name, cutoff],
        )

        audit_deleted = 0
        if include_audit:
            audit_deleted = execute(
                """
                DELETE FROM TSS.BKD_API_Exchanges
                WHERE ClientCode = ?
                  AND CalledAt < ?
                  AND (
                      Flow = 'EMAIL_AUTOMATION'
                      OR CallType LIKE '%EMAIL%'
                      OR CallType LIKE '%INGEST%'
                      OR CallType LIKE '%GRAPH%'
                      OR Url LIKE '%/ingest/%'
                      OR Url LIKE '%graph%'
                  )
                """,
                [schema_name, cutoff],
            )

        insert_api_call_log(
            schema_name,
            'ADMIN_CLEAN_EMAIL_AUTOMATION_TEST_LOGS',
            http_method='POST',
            url=url_for('technical.cleanup_email_intake_before'),
            request_payload={
                'before_date': raw_date,
                'include_technical_audit': include_audit,
            },
            response_status='ok',
            response_message=(
                f"Deleted email automation test logs before {raw_date}: "
                f"{emails_deleted or 0} emails, {attachments_deleted or 0} attachments, "
                f"{process_deleted or 0} process rows"
                + (f", {audit_deleted or 0} audit rows" if include_audit else "")
                + "."
            ),
        )
        flash(
            (
                f"Email automation test logs before {raw_date} deleted: "
                f"{emails_deleted or 0} emails, {attachments_deleted or 0} attachments, "
                f"{process_deleted or 0} process rows"
                + (f", {audit_deleted or 0} audit rows" if include_audit else "")
                + ". STG/TSS business records were not touched."
            ),
            'success',
        )
    except Exception as exc:
        insert_api_call_log(
            schema_name,
            'LOCAL_CLEAN_EMAIL_AUTOMATION_TEST_LOGS_FAILED',
            http_method='POST',
            url=url_for('technical.cleanup_email_intake_before'),
            request_payload={'before_date': raw_date, 'include_technical_audit': include_audit},
            response_status='local_error',
            response_message='Email automation test log cleanup failed.',
            error_detail=str(exc),
        )
        flash(f'Could not delete email automation test logs: {exc}', 'danger')

    return redirect(url_for('technical.index', tab='ingest'))


@technical_bp.route('/jobs/<int:log_id>/cancel', methods=['POST'])
def cancel_job_run(log_id):
    """Mark one stuck tenant job log as cancelled.

    This does not terminate an OS process. It fixes the common case where a
    background worker died or disconnected without updating ApiCallLog, leaving
    Technical > Jobs permanently showing RUNNING.
    """
    schema_name = _schema()
    try:
        affected = execute(
            """
            UPDATE TSS.BKD_API_Exchanges
               SET ResponseMessage = 'CANCELLED',
                   ResponseStatus = 'cancelled',
                   DurationMs = COALESCE(DurationMs, DATEDIFF(millisecond, CalledAt, GETUTCDATE())),
                   ErrorDetail = CONCAT(
                       COALESCE(NULLIF(ErrorDetail, ''), ResponseJson, ''),
                       CASE
                           WHEN COALESCE(NULLIF(ErrorDetail, ''), ResponseJson, '') = '' THEN ''
                           ELSE CHAR(13) + CHAR(10) + CHAR(13) + CHAR(10)
                       END,
                       'Cancelled by user from Technical > Jobs at ',
                       CONVERT(varchar(19), GETUTCDATE(), 120),
                       ' UTC.'
                   )
             WHERE ApiExchangeId = ?
               AND CallType LIKE 'JOB[_]%'
               AND ResponseMessage = 'STARTED'
               AND ClientCode = ?
            """,
            [log_id, schema_name],
        )
        if affected and affected > 0:
            insert_api_call_log(
                schema_name,
                'ADMIN_CANCEL_JOB_RUN',
                http_method='POST',
                url=url_for('technical.cancel_job_run', log_id=log_id),
                request_payload={'log_id': log_id},
                response_status='cancelled',
                response_message=f'Marked job log #{log_id} as cancelled.',
            )
            flash(f'Job run #{log_id} marked as cancelled.', 'success')
        else:
            flash(f'Job run #{log_id} is no longer running or was not found.', 'warning')
    except Exception as exc:
        insert_api_call_log(
            schema_name,
            'LOCAL_CANCEL_JOB_RUN_FAILED',
            http_method='POST',
            url=url_for('technical.cancel_job_run', log_id=log_id),
            request_payload={'log_id': log_id},
            response_status='local_error',
            response_message='Cancel job run failed.',
            error_detail=str(exc),
        )
        flash(f'Could not cancel job run #{log_id}: {exc}', 'danger')
    return redirect(url_for('technical.index', tab='jobs'))


@technical_bp.route('/jobs/cancel-running', methods=['POST'])
def cancel_running_job_runs():
    """Mark all stuck tenant job logs as cancelled.

    Like the single-run cancel action, this clears stale RUNNING rows in the
    portal. It does not terminate OS processes that may still exist.
    """
    schema_name = _schema()
    try:
        affected = execute(
            """
            UPDATE TSS.BKD_API_Exchanges
               SET ResponseMessage = 'CANCELLED',
                   ResponseStatus = 'cancelled',
                   DurationMs = COALESCE(DurationMs, DATEDIFF(millisecond, CalledAt, GETUTCDATE())),
                   ErrorDetail = CONCAT(
                       COALESCE(NULLIF(ErrorDetail, ''), ResponseJson, ''),
                       CASE
                           WHEN COALESCE(NULLIF(ErrorDetail, ''), ResponseJson, '') = '' THEN ''
                           ELSE CHAR(13) + CHAR(10) + CHAR(13) + CHAR(10)
                       END,
                       'Cancelled by user from Technical > Jobs bulk action at ',
                       CONVERT(varchar(19), GETUTCDATE(), 120),
                       ' UTC.'
                   )
             WHERE CallType LIKE 'JOB[_]%'
               AND ResponseMessage = 'STARTED'
               AND ClientCode = ?
            """,
            [schema_name],
        )
        insert_api_call_log(
            schema_name,
            'ADMIN_CANCEL_RUNNING_JOB_RUNS',
            http_method='POST',
            url=url_for('technical.cancel_running_job_runs'),
            request_payload={'scope': 'running_jobs'},
            response_status='cancelled',
            response_message=f'Marked {affected or 0} running job log(s) as cancelled.',
        )
        flash(f'{affected or 0} running job run(s) marked as cancelled.', 'success' if affected else 'info')
    except Exception as exc:
        insert_api_call_log(
            schema_name,
            'LOCAL_CANCEL_RUNNING_JOB_RUNS_FAILED',
            http_method='POST',
            url=url_for('technical.cancel_running_job_runs'),
            request_payload={'scope': 'running_jobs'},
            response_status='local_error',
            response_message='Cancel running job runs failed.',
            error_detail=str(exc),
        )
        flash(f'Could not cancel running job runs: {exc}', 'danger')
    return redirect(url_for('technical.index', tab='jobs'))
