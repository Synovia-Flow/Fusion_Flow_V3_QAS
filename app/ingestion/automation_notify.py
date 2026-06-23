"""
Headless email automation notifications for the production pipeline.

Two notification types:
  staging_failures    - sent after Sales Orders XLSX staging when blockers or
                        goods-level warnings exist. Tells operators which
                        consignments/goods failed or need review, with portal
                        links to correct them before/retry TSS submission.
  movement_authorised - sent when ENS header + all active consignments reach
                        AUTHORISED_FOR_MOVEMENT in TSS. Deduped via
                        STG.BKD_ENS_Headers.movement_notified_at.
  ingest_success      - temporary positive test notification for email automation
                        smoke testing (ENS received/created, consignments staged).

Pre-staging errors:
  pipeline_error      - sent for missing_details_ens, parse_failure,
                        staging_exception, no_consignments before staging runs.

Data sources (production model only - no BKD-Staging queries):
  STG.BKD_ENS_Headers      local data, dedup columns
  STG.BKD_ENS_Consignments active consignment list + local sub_status
  STG.BKD_GoodsItems       goods error_message probe
  TSS.BKD_ENS_Headers      remote ENS TssStatus (via DeclarationNumber)
  TSS.BKD_ENS_Consignments remote consignment TssStatus (via ConsignmentReference)
  TSS.BKD_API_Exchanges    email audit log (Flow='EMAIL_AUTOMATION', EntityKind='EMAIL')

Recipient resolution (in priority order):
  1. AppConfiguration: NOTIFY.STAGING_FAILURES_TO / NOTIFY.MOVEMENT_AUTHORISED_TO
     Optional CC for final movement: NOTIFY.MOVEMENT_AUTHORISED_CC
     Optional automatic ENS pack: NOTIFY.ENS_PACK_AUTO_TO / NOTIFY.ENS_PACK_AUTO_CC
  2. Env var: SMTP_NOTIFY_TO (or NOTIFY_TO fallback)

All sends are best-effort and never raise. Works in Flask context and headless
(sync pipeline, cron jobs) - tries Flask send_email first, falls back to a raw
SMTP send from env vars when no app context is available.
"""
from __future__ import annotations

import logging
import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

_AUTHORISED_STATUS = 'AUTHORISED_FOR_MOVEMENT'
# Hardcoded operational fallback — receives all error notifications when
# NOTIFY.STAGING_FAILURES_TO / SMTP_NOTIFY_TO are not configured.
_DEFAULT_NOTIFY_TO = 'alvaro.molina@synoviadigital.com'
_TEST_NOTIFY_DEFAULT_TO = _DEFAULT_NOTIFY_TO
_DEFAULT_PORTAL_URL = 'https://fusion-flow-v2-bkd-5w83.onrender.com'


def _normalise_tss_status(raw: str | None) -> str:
    """Uppercase, strip, replace spaces and hyphens with underscore."""
    if not raw:
        return ''
    return raw.strip().upper().replace(' ', '_').replace('-', '_')


def _portal_url(tenant_code: str | None = None) -> str:
    """Portal base URL used in all automation emails.

    Render cron jobs do not run inside the web service, so the default must be
    the public app URL rather than localhost.
    """
    raw = (
        os.environ.get('PORTAL_URL', '')
        or _config_db_value('NOTIFY', 'PORTAL_URL', tenant_code=tenant_code)
        or _DEFAULT_PORTAL_URL
    )
    return raw.rstrip('/') or _DEFAULT_PORTAL_URL


def _ens_url(stg_header_id: int | None, tenant_code: str | None = None) -> str:
    base = _portal_url(tenant_code)
    return f'{base}/ens/header/{stg_header_id}' if stg_header_id else f'{base}/ingest/'


def _consignment_url(stg_consignment_id: int | None, tenant_code: str | None = None) -> str:
    base = _portal_url(tenant_code)
    return f'{base}/consignments/{stg_consignment_id}' if stg_consignment_id else base


# -- Config helpers -------------------------------------------------------------

def _config_db_value(category: str, key: str, tenant_code: str | None = None) -> str:
    try:
        from app import config_store
        return config_store.get_db_value(category, key, tenant_code=tenant_code) or ''
    except Exception:
        return ''


def _resolve_notify_recipients(config_key: str, tenant_code: str | None = None) -> list[str]:
    from app.email_utils import _normalise_address_list
    db_val = _config_db_value('NOTIFY', config_key, tenant_code=tenant_code)
    if db_val.strip():
        return _normalise_address_list(db_val)
    env_val = os.environ.get('SMTP_NOTIFY_TO', '') or os.environ.get('NOTIFY_TO', '')
    return _normalise_address_list(env_val)


def _resolve_error_recipients(config_key: str, tenant_code: str | None = None) -> list[str]:
    """Resolve error notification recipients with guaranteed ops-team fallback.

    Unlike _resolve_notify_recipients, always falls back to _DEFAULT_NOTIFY_TO
    so error emails are never silently dropped when env vars are unconfigured.
    """
    from app.email_utils import _normalise_address_list
    db_val = _config_db_value('NOTIFY', config_key, tenant_code=tenant_code)
    if db_val.strip():
        return _normalise_address_list(db_val)
    env_val = os.environ.get('SMTP_NOTIFY_TO', '') or os.environ.get('NOTIFY_TO', '')
    return _normalise_address_list(env_val or _DEFAULT_NOTIFY_TO)


def _resolve_notify_cc_recipients(config_key: str, tenant_code: str | None = None) -> list[str]:
    """Resolve optional CC recipients without falling back to the main To list."""
    from app.email_utils import _normalise_address_list
    db_val = _config_db_value('NOTIFY', config_key, tenant_code=tenant_code)
    if db_val.strip():
        return _normalise_address_list(db_val)
    env_val = (
        os.environ.get(f'NOTIFY_{config_key}', '')
        or os.environ.get(config_key, '')
        or os.environ.get('SMTP_NOTIFY_CC', '')
    )
    return _normalise_address_list(env_val)


def _resolve_explicit_notify_recipients(
    config_key: str,
    tenant_code: str | None = None,
) -> list[str]:
    """Resolve recipients only from a matching AppConfiguration/env key.

    This avoids the broad SMTP_NOTIFY_TO fallback for optional customer-facing
    sends that must not start emailing just because the global ops fallback
    exists.
    """
    from app.email_utils import _normalise_address_list
    db_val = _config_db_value('NOTIFY', config_key, tenant_code=tenant_code)
    if db_val.strip():
        return _normalise_address_list(db_val)
    env_val = (
        os.environ.get(f'NOTIFY_{config_key}', '')
        or os.environ.get(config_key, '')
    )
    return _normalise_address_list(env_val)


def _dedupe_cc(to_list: list[str], cc_list: list[str]) -> list[str]:
    to_keys = {addr.lower() for addr in to_list}
    out: list[str] = []
    seen: set[str] = set()
    for addr in cc_list:
        key = addr.lower()
        if key in to_keys or key in seen:
            continue
        seen.add(key)
        out.append(addr)
    return out


def _movement_authorised_header_recipients(ens_header: dict) -> list[str]:
    """Prefer a recipient carried by the ENS/header context when available."""
    from app.email_utils import _normalise_address_list
    for key in (
        'movement_authorised_to',
        'notification_to',
        'reply_to_email',
        'response_email',
        'contact_email',
        'carrier_email',
        'to_email',
    ):
        recipients = _normalise_address_list(ens_header.get(key))
        if recipients:
            return recipients
    return []


def _resolve_movement_authorised_recipients(
    ens_header: dict,
    tenant_code: str | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve final movement To + configured always-CC recipients."""
    to_list = (
        _movement_authorised_header_recipients(ens_header)
        or _resolve_notify_recipients('MOVEMENT_AUTHORISED_TO', tenant_code)
    )
    cc_list = _resolve_notify_cc_recipients('MOVEMENT_AUTHORISED_CC', tenant_code)
    return to_list, _dedupe_cc(to_list, cc_list)


def _resolve_test_notify_recipients(tenant_code: str | None = None) -> list[str]:
    """Temporary smoke-test recipients for positive email automation events."""
    from app.email_utils import _normalise_address_list
    db_val = _config_db_value('NOTIFY', 'EMAIL_AUTOMATION_TEST_TO', tenant_code=tenant_code)
    if db_val.strip():
        return _normalise_address_list(db_val)
    env_val = os.environ.get('EMAIL_AUTOMATION_TEST_NOTIFY_TO', '')
    return _normalise_address_list(env_val or _TEST_NOTIFY_DEFAULT_TO)


def _is_notify_enabled(config_key: str, tenant_code: str | None = None) -> bool:
    """Check NOTIFY.<config_key> in AppConfiguration. Defaults to True if not set."""
    raw = _config_db_value('NOTIFY', config_key, tenant_code=tenant_code).strip()
    if not raw:
        return True
    return raw.lower() in ('1', 'true', 'yes', 'on', 'enabled')


def _is_notify_explicitly_enabled(config_key: str, tenant_code: str | None = None) -> bool:
    """Check a NOTIFY flag that must be explicitly enabled.

    Existing notification toggles default to enabled for backwards compatibility.
    New optional sends, such as the automatic ENS movement pack, must default to
    off so deploying the code never starts emailing a new audience unexpectedly.
    """
    raw = _config_db_value('NOTIFY', config_key, tenant_code=tenant_code).strip()
    if not raw:
        raw = os.environ.get(f'NOTIFY_{config_key}', '').strip()
    return raw.lower() in ('1', 'true', 'yes', 'on', 'enabled')


_INGEST_SUCCESS_TOGGLES: dict[str, str] = {
    'ens_email_received': 'ENS_RECEIVED_ENABLED',
    'consignments_email_received': 'CONSIGNMENTS_RECEIVED_ENABLED',
}


# -- SMTP send (works with or without Flask app context) -----------------------

def _send_email_headless(
    to_list: list[str],
    subject: str,
    html: str,
    text: str | None = None,
    *,
    cc_list: list[str] | None = None,
    inline_images: list[tuple[str, str, str]] | None = None,
) -> tuple[bool, str | None]:
    """SMTP send without Flask app context - reads config from env vars only."""
    enabled_raw = os.environ.get('SMTP_ENABLED', 'true').strip().lower()
    if enabled_raw in ('0', 'false', 'no', 'n', 'off', 'disabled'):
        return False, 'SMTP_ENABLED=false'

    server = os.environ.get('SMTP_SERVER', 'smtp.office365.com')
    try:
        port = int(os.environ.get('SMTP_PORT', '587'))
    except (TypeError, ValueError):
        port = 587
    sender = os.environ.get('SMTP_SENDER') or os.environ.get('SMTP_USERNAME', '')
    username = os.environ.get('SMTP_USERNAME') or sender
    password = os.environ.get('SMTP_PASSWORD', '')

    if not username or not password:
        return False, 'SMTP credentials not configured (SMTP_USERNAME/SMTP_PASSWORD)'

    cc_list = _dedupe_cc(to_list, list(cc_list or []))
    all_recipients = to_list + cc_list

    inline_images = list(inline_images or [])
    if inline_images:
        msg = MIMEMultipart('related')
        msg['Subject'] = subject
        msg['From'] = f'Synovia Integration <{sender or username}>'
        msg['To'] = ', '.join(to_list)
        if cc_list:
            msg['Cc'] = ', '.join(cc_list)
        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        if text:
            alt.attach(MIMEText(text, 'plain', 'utf-8'))
        alt.attach(MIMEText(html, 'html', 'utf-8'))
        for cid, image_path, subtype in inline_images:
            try:
                with open(image_path, 'rb') as fh:
                    image_part = MIMEImage(fh.read(), _subtype=(subtype or 'jpeg'))
                image_part.add_header('Content-ID', f'<{cid}>')
                image_part.add_header(
                    'Content-Disposition', 'inline', filename=os.path.basename(image_path),
                )
                msg.attach(image_part)
            except Exception as exc:
                log.warning('Failed to attach inline image %s (%s): %s', cid, image_path, exc)
    else:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f'Synovia Integration <{sender or username}>'
        msg['To'] = ', '.join(to_list)
        if cc_list:
            msg['Cc'] = ', '.join(cc_list)
        if text:
            msg.attach(MIMEText(text, 'plain', 'utf-8'))
        msg.attach(MIMEText(html, 'html', 'utf-8'))

    try:
        with smtplib.SMTP(server, port, timeout=30) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(username, password)
            srv.sendmail(sender or username, all_recipients, msg.as_string())
        log.info(
            'Headless email sent: %s -> %s%s',
            subject[:60],
            ', '.join(to_list),
            f' (cc {", ".join(cc_list)})' if cc_list else '',
        )
        return True, None
    except Exception as exc:
        log.error('Headless SMTP error: %s', exc)
        return False, str(exc)


def _send_email_auto(
    to_list: list[str],
    subject: str,
    html: str,
    text: str | None = None,
    *,
    cc_list: list[str] | None = None,
    inline_images: list[tuple[str, str, str]] | None = None,
) -> tuple[bool, str | None]:
    """Try Flask send_email (with Admin Settings SMTP), fall back to headless."""
    try:
        from flask import current_app
        _ = current_app.name  # raises RuntimeError outside app context
        from app.email_utils import send_email
        return send_email(
            to_list,
            subject,
            html,
            text,
            cc_addresses=cc_list,
            inline_images=inline_images,
        )
    except RuntimeError:
        return _send_email_headless(
            to_list, subject, html, text, cc_list=cc_list, inline_images=inline_images,
        )
    except Exception as exc:
        log.warning('send_email_auto Flask path failed, trying headless: %s', exc)
        return _send_email_headless(
            to_list, subject, html, text, cc_list=cc_list, inline_images=inline_images,
        )


def _log_notify(
    entity_id: int | None,
    to_address: str,
    subject: str,
    ok: bool,
    error: str | None = None,
    *,
    client_code: str = 'BKD',
) -> None:
    """Audit email send to TSS.BKD_API_Exchanges. Never raises."""
    sql = """
        INSERT INTO TSS.BKD_API_Exchanges
          (ClientCode, Flow, EntityKind, EntityId,
           CallType, HttpMethod, Url, RequestPayloadJson,
           HttpStatus, ResponseStatus, ResponseMessage)
        VALUES (?, 'EMAIL_AUTOMATION', 'EMAIL', ?,
                'SMTP', 'SMTP', ?, ?,
                ?, ?, ?)
    """
    params = [
        client_code,
        entity_id,
        (to_address or '')[:500],
        (subject or '')[:500],
        200 if ok else 500,
        'SENT' if ok else 'FAILED',
        (str(error))[:1000] if error else None,
    ]
    try:
        from app.db import execute
        execute(sql, params)
    except Exception as exc:
        log.debug('_log_notify db write failed: %s', exc)


def _log_email_automation_event(
    entity_id: int | None,
    to_address: str,
    subject: str,
    ok: bool,
    error: str | None = None,
    *,
    client_code: str = 'BKD',
    call_type: str = 'SMTP',
    event_payload: dict | None = None,
) -> None:
    """Audit an automation email with optional JSON metadata. Never raises."""
    sql = """
        INSERT INTO TSS.BKD_API_Exchanges
          (ClientCode, Flow, EntityKind, EntityId,
           CallType, HttpMethod, Url, RequestPayloadJson,
           HttpStatus, ResponseStatus, ResponseMessage, ResponseJson)
        VALUES (?, 'EMAIL_AUTOMATION', 'EMAIL', ?,
                ?, 'SMTP', ?, ?,
                ?, ?, ?, ?)
    """
    response_json = None
    if event_payload is not None:
        response_json = json.dumps(event_payload, default=str)[:4000]
    params = [
        client_code,
        entity_id,
        call_type,
        (to_address or '')[:500],
        (subject or '')[:500],
        200 if ok else 500,
        'SENT' if ok else 'FAILED',
        (str(error))[:1000] if error else None,
        response_json,
    ]
    try:
        from app.db import execute
        execute(sql, params)
    except Exception as exc:
        log.debug('_log_email_automation_event db write failed: %s', exc)


def _notification_event_sent(
    entity_id: int | None,
    *,
    client_code: str,
    call_type: str,
    dedupe_key: str,
) -> bool:
    """Return True when a successful email for this dedupe key already exists."""
    if entity_id is None or not dedupe_key:
        return False
    try:
        from app.db import query_one
        row = query_one(
            """
            SELECT TOP 1 1 AS found
            FROM TSS.BKD_API_Exchanges
            WHERE ClientCode = ?
              AND Flow = 'EMAIL_AUTOMATION'
              AND EntityKind = 'EMAIL'
              AND EntityId = ?
              AND CallType = ?
              AND ResponseStatus = 'SENT'
              AND ResponseJson LIKE ?
            ORDER BY ApiExchangeId DESC
            """,
            [client_code, entity_id, call_type, f'%{dedupe_key}%'],
        )
        return bool(row)
    except Exception as exc:
        log.debug('_notification_event_sent failed: %s', exc)
        return False


def _stamp_stg_header(
    stg_header_id: int,
    column: str,
    cursor=None,
    conn=None,
) -> None:
    """Stamp a DATETIME2 column on STG.BKD_ENS_Headers. Never raises."""
    _valid = {'movement_notified_at', 'staging_failures_notified_at'}
    if column not in _valid:
        return
    sql = (
        f"UPDATE STG.BKD_ENS_Headers SET {column} = SYSUTCDATETIME()"
        " WHERE stg_header_id = ?"
    )
    try:
        if cursor is not None:
            cursor.execute(sql, [stg_header_id])
            if conn:
                conn.commit()
        else:
            from app.db import db_cursor
            with db_cursor(commit=True) as cur:
                cur.execute(sql, [stg_header_id])
    except Exception as exc:
        log.warning('_stamp_stg_header %s=%s failed: %s', column, stg_header_id, exc)


# -- Public notification functions ----------------------------------------------

def notify_ingest_success(
    event_type: str,
    *,
    tenant_code: str,
    stg_header_id: int | None = None,
    filename: str = '',
    subject_hint: str = '',
    summary: dict | None = None,
) -> tuple[bool, str | None]:
    """Temporary positive notification for smoke-testing email automation.

    This is intentionally separate from operational failure/final movement
    notifications so it can be disabled/removed once the flow is proven.
    Best-effort. Never raises.
    """
    try:
        summary = dict(summary or {})
        event_label = event_type.replace('_', ' ').title()
        ens_ref = (
            summary.get('ens_reference')
            or summary.get('tss_ens_header_ref')
            or summary.get('ens_staging_id')
            or stg_header_id
            or '?'
        )
        subject = f'[TEST] Email automation - {event_label} (ENS {ens_ref})'

        toggle_key = _INGEST_SUCCESS_TOGGLES.get(event_type)
        if toggle_key and not _is_notify_enabled(toggle_key, tenant_code):
            err = f'NOTIFY.{toggle_key}=false'
            _log_notify(stg_header_id, '', subject, False, err, client_code=tenant_code)
            return False, err

        recipients = _resolve_test_notify_recipients(tenant_code)
        if not recipients:
            err = 'No recipients configured (NOTIFY.EMAIL_AUTOMATION_TEST_TO)'
            _log_notify(stg_header_id, '', subject, False, err, client_code=tenant_code)
            return False, err

        html = _build_ingest_success_html(
            event_type=event_type,
            event_label=event_label,
            tenant_code=tenant_code,
            stg_header_id=stg_header_id,
            filename=filename,
            subject_hint=subject_hint,
            summary=summary,
        )
        text = _build_ingest_success_text(
            event_label=event_label,
            tenant_code=tenant_code,
            stg_header_id=stg_header_id,
            filename=filename,
            subject_hint=subject_hint,
            summary=summary,
        )
        ok, err = _send_email_auto(recipients, subject, html, text)
        _log_notify(stg_header_id, ', '.join(recipients), subject, ok, err, client_code=tenant_code)
        return ok, err
    except Exception as exc:
        log.warning('notify_ingest_success (%s): %s', event_type, exc)
        return False, str(exc)


# Step 09 - error notification (docs/README.md)
def notify_pipeline_error(
    error_type: str,
    detail: str,
    *,
    tenant_code: str,
    stg_header_id: int | None = None,
    filename: str = '',
) -> tuple[bool, str | None]:
    """Send failure email for pre-staging pipeline errors.

    error_type values: 'missing_details_ens' | 'parse_failure' |
                       'staging_exception'   | 'no_consignments'
    Best-effort. Never raises.
    """
    try:
        if not _is_notify_enabled('STAGING_FAILURES_ENABLED', tenant_code):
            return False, 'NOTIFY.STAGING_FAILURES_ENABLED=false'

        recipients = _resolve_error_recipients('STAGING_FAILURES_TO', tenant_code)
        if not recipients:
            return False, 'No recipients configured (NOTIFY.STAGING_FAILURES_TO)'

        label = error_type.replace('_', ' ').title()
        subject = f'Pipeline Error - {label} ({filename or "?"})'
        html = _build_pipeline_error_html(
            error_type=error_type,
            detail=detail,
            stg_header_id=stg_header_id,
            filename=filename,
            tenant_code=tenant_code,
        )
        text = _build_pipeline_error_text(
            error_type=error_type,
            detail=detail,
            filename=filename,
        )
        ok, err = _send_email_auto(recipients, subject, html, text)
        _log_notify(stg_header_id, ', '.join(recipients), subject, ok, err, client_code=tenant_code)
        return ok, err
    except Exception as exc:
        log.warning('notify_pipeline_error (%s): %s', error_type, exc)
        return False, str(exc)


# Step 09 - staging failure notification (docs/README.md)
def notify_staging_failures(
    result,
    *,
    ens_staging_id: int | None,
    tenant_code: str,
    filename: str = '',
    cursor=None,
    conn=None,
) -> tuple[bool, str | None]:
    """Send actionable failure email after Sales Orders staging has issues.

    ens_staging_id is STG.BKD_ENS_Headers.stg_header_id.
    If cursor + conn provided and email sends OK, stamps staging_failures_notified_at.
    Best-effort. Never raises.
    """
    try:
        if not _is_notify_enabled('STAGING_FAILURES_ENABLED', tenant_code):
            return False, 'NOTIFY.STAGING_FAILURES_ENABLED=false'

        recipients = _resolve_error_recipients('STAGING_FAILURES_TO', tenant_code)
        if not recipients:
            return False, 'No recipients configured (NOTIFY.STAGING_FAILURES_TO)'

        blockers = list(result.blockers or [])
        warnings = list(result.warnings or [])
        all_cons = list(result.consignments or [])

        subject = (
            f'Action Required - Sales Orders staging failed '
            f'(ENS #{ens_staging_id or "?"})'
            if blockers else
            f'Action Required - Sales Orders goods need attention '
            f'(ENS #{ens_staging_id or "?"})'
        )
        html = _build_staging_failure_html(
            ens_staging_id=ens_staging_id,
            filename=filename,
            tenant_code=tenant_code,
            blockers=blockers,
            warnings=warnings,
            all_consignments=all_cons,
        )
        text = _build_staging_failure_text(
            ens_staging_id=ens_staging_id,
            filename=filename,
            blockers=blockers,
            warnings=warnings,
            all_consignments=all_cons,
        )

        ok, err = _send_email_auto(recipients, subject, html, text)
        _log_notify(ens_staging_id, ', '.join(recipients), subject, ok, err, client_code=tenant_code)

        if ok and ens_staging_id:
            _stamp_stg_header(ens_staging_id, 'staging_failures_notified_at', cursor, conn)

        return ok, err
    except Exception as exc:
        log.warning('notify_staging_failures error: %s', exc)
        return False, str(exc)


def notify_cargo_submitted(
    ens_header: dict,
    consignments: list[dict],
    *,
    tenant_code: str,
    summary: dict | None = None,
) -> tuple[bool, str | None]:
    """Send positive operational email when cargo was sent to TSS.

    This is the second-mail checkpoint: ENS exists in TSS and the staged
    consignments/goods were created/submitted to TSS. Deduped through
    TSS.BKD_API_Exchanges so retries do not spam operators.
    """
    try:
        recipients = (
            _resolve_notify_recipients('CARGO_SUBMITTED_TO', tenant_code)
            or _resolve_test_notify_recipients(tenant_code)
        )
        if not recipients:
            return False, 'No recipients configured (NOTIFY.CARGO_SUBMITTED_TO)'

        stg_header_id = ens_header.get('stg_header_id')
        ens_ref = (
            ens_header.get('tss_ens_header_ref')
            or ens_header.get('ens_reference')
            or ens_header.get('conveyance_ref')
            or f"#{stg_header_id or '?'}"
        )
        dec_refs = [
            str(c.get('tss_consignment_ref') or c.get('dec_reference') or '').strip()
            for c in consignments or []
        ]
        dec_refs = [ref for ref in dec_refs if ref]
        dedupe_key = f'CARGO_SUBMITTED:{stg_header_id}:{ens_ref}:{"|".join(dec_refs)}'
        call_type = 'SMTP_CARGO_SUBMITTED'
        if _notification_event_sent(
            stg_header_id,
            client_code=tenant_code,
            call_type=call_type,
            dedupe_key=dedupe_key,
        ):
            return True, 'already_sent'

        subject = f'Cargo submitted to TSS - {ens_ref}'
        html = _build_cargo_submitted_html(ens_header, consignments, summary or {}, tenant_code=tenant_code)
        text = _build_cargo_submitted_text(ens_header, consignments, summary or {}, tenant_code=tenant_code)
        ok, err = _send_email_auto(recipients, subject, html, text)
        _log_email_automation_event(
            stg_header_id,
            ', '.join(recipients),
            subject,
            ok,
            err,
            client_code=tenant_code,
            call_type=call_type,
            event_payload={
                'dedupe_key': dedupe_key,
                'ens_ref': ens_ref,
                'dec_refs': dec_refs,
                'summary': summary or {},
            },
        )
        return ok, err
    except Exception as exc:
        log.warning('notify_cargo_submitted error: %s', exc)
        return False, str(exc)


def notify_sdi_autosubmit_issue(
    summary: dict | None = None,
    errors: list[str] | None = None,
    *,
    tenant_code: str,
    manual: bool = False,
) -> tuple[bool, str | None]:
    """Send internal TEST attention email when SDI autosubmit leaves issues."""
    try:
        summary = dict(summary or {})
        errors = [str(item) for item in (errors or []) if str(item or '').strip()]
        try:
            blocked = int(summary.get('blocked') or 0)
        except (TypeError, ValueError):
            blocked = 0
        if blocked <= 0 and not errors:
            return False, 'no_issue'

        recipients = _resolve_test_notify_recipients(tenant_code)
        if not recipients:
            return False, 'No recipients configured (NOTIFY.EMAIL_AUTOMATION_TEST_TO)'

        submitted = summary.get('submitted') or 0
        subject = f'[TEST] SDI autosubmit attention - {blocked} blocked / {submitted} submitted'
        html = _build_sdi_autosubmit_issue_html(summary, errors, tenant_code=tenant_code, manual=manual)
        text = _build_sdi_autosubmit_issue_text(summary, errors, tenant_code=tenant_code, manual=manual)
        ok, err = _send_email_auto(recipients, subject, html, text)
        _log_email_automation_event(
            None,
            ', '.join(recipients),
            subject,
            ok,
            err,
            client_code=tenant_code,
            call_type='SMTP_SDI_AUTOSUBMIT_TEST',
            event_payload={
                'summary': summary,
                'errors': errors[:20],
                'manual': manual,
            },
        )
        return ok, err
    except Exception as exc:
        log.warning('notify_sdi_autosubmit_issue error: %s', exc)
        return False, str(exc)


def notify_tss_status_attention(
    ens_header: dict,
    status_items: list[dict],
    *,
    tenant_code: str,
) -> tuple[bool, str | None]:
    """Notify operators when TSS asks for manual action.

    Currently focused on TRADER_INPUT_REQUIRED. This is an operational attention
    notification, so it uses error recipients and is not controlled by the
    positive smoke-test toggles.
    """
    try:
        actionable = [
            item for item in (status_items or [])
            if _normalise_tss_status(item.get('tss_status')) == 'TRADER_INPUT_REQUIRED'
        ]
        if not actionable:
            return False, 'no_actionable_status'

        recipients = _resolve_error_recipients('TSS_STATUS_ATTENTION_TO', tenant_code)
        if not recipients:
            return False, 'No recipients configured (NOTIFY.TSS_STATUS_ATTENTION_TO)'

        stg_header_id = ens_header.get('stg_header_id')
        ens_ref = (
            ens_header.get('tss_ens_header_ref')
            or ens_header.get('ens_reference')
            or ens_header.get('conveyance_ref')
            or f"#{stg_header_id or '?'}"
        )
        keys = []
        for item in actionable:
            ref = (
                item.get('tss_ref')
                or item.get('tss_consignment_ref')
                or item.get('tss_ens_header_ref')
                or item.get('stg_consignment_id')
                or stg_header_id
            )
            keys.append(f"{item.get('entity_kind', 'TSS')}:{ref}:TRADER_INPUT_REQUIRED")
        dedupe_key = 'TSS_STATUS_ATTENTION:' + '|'.join(sorted(str(k) for k in keys))
        call_type = 'SMTP_TSS_STATUS_ATTENTION'
        if _notification_event_sent(
            stg_header_id,
            client_code=tenant_code,
            call_type=call_type,
            dedupe_key=dedupe_key,
        ):
            return True, 'already_sent'

        subject = f'Action Required - TSS Trader Input Required ({ens_ref})'
        html = _build_tss_status_attention_html(ens_header, actionable, tenant_code=tenant_code)
        text = _build_tss_status_attention_text(ens_header, actionable, tenant_code=tenant_code)
        ok, err = _send_email_auto(recipients, subject, html, text)
        _log_email_automation_event(
            stg_header_id,
            ', '.join(recipients),
            subject,
            ok,
            err,
            client_code=tenant_code,
            call_type=call_type,
            event_payload={
                'dedupe_key': dedupe_key,
                'ens_ref': ens_ref,
                'items': actionable,
            },
        )
        return ok, err
    except Exception as exc:
        log.warning('notify_tss_status_attention error: %s', exc)
        return False, str(exc)


# Step 12 - Authorised for Movement email (docs/README.md)
def notify_movement_authorised(
    ens_header: dict,
    consignments: list[dict],
    *,
    tenant_code: str,
) -> tuple[bool, str | None]:
    """Send confirmation email when all consignments reach AUTHORISED_FOR_MOVEMENT.

    ens_header must contain stg_header_id and tss_ens_header_ref.
    Best-effort. Never raises.
    """
    try:
        if not _is_notify_enabled('MOVEMENT_AUTHORISED_ENABLED', tenant_code):
            return False, 'NOTIFY.MOVEMENT_AUTHORISED_ENABLED=false'

        recipients, cc_recipients = _resolve_movement_authorised_recipients(ens_header, tenant_code)
        if not recipients:
            return False, 'No recipients configured (NOTIFY.MOVEMENT_AUTHORISED_TO)'

        ens_ref = (
            ens_header.get('tss_ens_header_ref')
            or ens_header.get('conveyance_ref')
            or f"#{ens_header.get('stg_header_id', '?')}"
        )
        subject = f'Authorised for Movement - {ens_ref}'
        html = _build_movement_authorised_html(ens_header, consignments)
        text = _build_movement_authorised_text(ens_header, consignments)
        inline_images = _movement_authorised_pack_inline_images()

        ok, err = _send_email_auto(
            recipients,
            subject,
            html,
            text,
            cc_list=cc_recipients,
            inline_images=inline_images,
        )
        _log_notify(
            ens_header.get('stg_header_id'),
            ', '.join(recipients + cc_recipients),
            subject,
            ok,
            err,
            client_code=tenant_code,
        )
        return ok, err
    except Exception as exc:
        log.warning('notify_movement_authorised error: %s', exc)
        return False, str(exc)


def notify_ens_movement_pack_auto(
    ens_header: dict,
    consignments: list[dict],
    *,
    tenant_code: str,
) -> tuple[bool, str | None]:
    """Optionally send the ENS movement pack automatically after authorisation.

    This is deliberately separate from notify_movement_authorised:
      - MOVEMENT_AUTHORISED_ENABLED keeps the current notification behaviour.
      - ENS_PACK_AUTO_ENABLED controls the extra customer/operator pack send.
      - TSS.BKD_API_Exchanges dedupes successful pack sends per ENS header.
    """
    try:
        if not _is_notify_explicitly_enabled('ENS_PACK_AUTO_ENABLED', tenant_code):
            return False, 'NOTIFY.ENS_PACK_AUTO_ENABLED=false'

        recipients = _resolve_explicit_notify_recipients('ENS_PACK_AUTO_TO', tenant_code)
        if not recipients:
            return False, 'No recipients configured (NOTIFY.ENS_PACK_AUTO_TO)'

        cc_recipients = _dedupe_cc(
            recipients,
            _resolve_explicit_notify_recipients('ENS_PACK_AUTO_CC', tenant_code),
        )
        stg_header_id = ens_header.get('stg_header_id')
        ens_ref = (
            ens_header.get('tss_ens_header_ref')
            or ens_header.get('conveyance_ref')
            or f"#{stg_header_id or '?'}"
        )
        dec_refs = [
            str(c.get('dec_reference') or c.get('tss_consignment_ref') or '').strip()
            for c in consignments
            if c.get('dec_reference') or c.get('tss_consignment_ref')
        ]
        dedupe_key = f"ENS_PACK_AUTO:{stg_header_id or ''}:{ens_ref}:{'|'.join(dec_refs)}"
        call_type = 'SMTP_ENS_MOVEMENT_PACK_AUTO'
        if _notification_event_sent(
            stg_header_id,
            client_code=tenant_code,
            call_type=call_type,
            dedupe_key=dedupe_key,
        ):
            return True, 'already_sent'

        subject = f'ENS Movement Pack - {ens_ref}'
        html = _build_movement_authorised_html(ens_header, consignments)
        text = _build_movement_authorised_text(ens_header, consignments)
        inline_images = _movement_authorised_pack_inline_images()

        ok, err = _send_email_auto(
            recipients,
            subject,
            html,
            text,
            cc_list=cc_recipients,
            inline_images=inline_images,
        )
        _log_email_automation_event(
            stg_header_id,
            ', '.join(recipients + cc_recipients),
            subject,
            ok,
            err,
            client_code=tenant_code,
            call_type=call_type,
            event_payload={
                'dedupe_key': dedupe_key,
                'ens_ref': ens_ref,
                'dec_refs': dec_refs,
                'source': 'auto_after_movement_authorised',
            },
        )
        return ok, err
    except Exception as exc:
        log.warning('notify_ens_movement_pack_auto error: %s', exc)
        return False, str(exc)


# -- ENS movement pack rendering ------------------------------------------------

def _movement_authorised_pack_inline_images() -> list[tuple[str, str, str]]:
    """Return the same inline logo attachments used by manual ENS pack emails."""
    try:
        from app.blueprints.declarations.routes import (
            ENS_PACK_LOGO_CID,
            ENS_PACK_SYNOVIA_LOGO_CID,
            ENS_PACK_TENANT_LOGO_CID,
            _ens_pack_logo_path,
            _ens_pack_synovia_logo_path,
            _ens_pack_tenant_logo_path,
        )
    except Exception as exc:
        log.debug('ENS movement pack inline image helpers unavailable: %s', exc)
        return []

    images: list[tuple[str, str, str]] = []
    for cid, path_func in (
        (ENS_PACK_LOGO_CID, _ens_pack_logo_path),
        (ENS_PACK_SYNOVIA_LOGO_CID, _ens_pack_synovia_logo_path),
        (ENS_PACK_TENANT_LOGO_CID, _ens_pack_tenant_logo_path),
    ):
        try:
            path = path_func()
            if not path or not os.path.exists(path):
                continue
            ext = os.path.splitext(path)[1].lower().lstrip('.') or 'jpeg'
            images.append((cid, path, 'jpeg' if ext in {'jpg', 'jpeg'} else ext))
        except Exception as exc:
            log.debug('ENS movement pack inline image %s unavailable: %s', cid, exc)
    return images


# Step 12 - movement authorisation gate + send (docs/README.md)
def check_and_notify_ens_authorised(
    stg_header_id: int,
    *,
    cursor,
    conn,
    client_code: str = 'BKD',
) -> tuple[bool, str | None]:
    """Check ENS header + all active consignments against TSS remote status.

    Reads from STG (local data / dedup) and TSS (remote status) only.
    No BKD-Staging queries.

    Authorization gate (ALL must pass):
      1. STG.BKD_ENS_Headers.movement_notified_at IS NULL (dedup)
      2. TSS.BKD_ENS_Headers.TssStatus normalised == AUTHORISED_FOR_MOVEMENT
      3. ALL active STG consignments have TSS.BKD_ENS_Consignments.TssStatus
         normalised == AUTHORISED_FOR_MOVEMENT
      4. No STG.BKD_GoodsItems.error_message IS NOT NULL for those consignments

    Stamps movement_notified_at ONLY if email send returns ok=True.

    Return codes on False:
      'already_notified' | 'no_tss_ref' | 'header_not_found' |
      'header_not_authorized' | 'no_consignments' |
      'not_all_consignments_authorized' | 'goods_have_errors'
    """
    try:
        # -- 1. Load STG header + dedup check ------------------------------
        try:
            cursor.execute(
                """
                SELECT stg_header_id, tss_ens_header_ref, movement_notified_at,
                       conveyance_ref, arrival_date_time, arrival_port, sub_status
                FROM STG.BKD_ENS_Headers
                WHERE stg_header_id = ?
                """,
                [stg_header_id],
            )
        except Exception as probe_exc:
            return False, f'stg_header_probe_failed: {probe_exc}'

        hrow = cursor.fetchone()
        if not hrow:
            return False, 'header_not_found'

        hcols = [d[0] for d in cursor.description]
        ens_header = dict(zip(hcols, hrow))

        if ens_header.get('movement_notified_at') is not None:
            return False, 'already_notified'

        tss_ens_ref = ens_header.get('tss_ens_header_ref')
        if not tss_ens_ref:
            return False, 'no_tss_ref'

        # -- 2. Check remote ENS header TSS status -------------------------
        cursor.execute(
            """
            SELECT TssStatus
            FROM TSS.BKD_ENS_Headers
            WHERE ClientCode = ? AND DeclarationNumber = ?
            """,
            [client_code, tss_ens_ref],
        )
        tss_hrow = cursor.fetchone()
        header_tss_status = _normalise_tss_status(tss_hrow[0] if tss_hrow else None)
        if header_tss_status != _AUTHORISED_STATUS:
            return False, 'header_not_authorized'

        # -- 3. Load active STG consignments -------------------------------
        cursor.execute(
            """
            SELECT stg_consignment_id, tss_consignment_ref, sub_status
            FROM STG.BKD_ENS_Consignments
            WHERE stg_header_id = ?
              AND UPPER(COALESCE(sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
            """,
            [stg_header_id],
        )
        cons_rows = cursor.fetchall()
        if not cons_rows:
            return False, 'no_consignments'

        ccols = [d[0] for d in cursor.description]
        cons_list_stg = [dict(zip(ccols, r)) for r in cons_rows]

        # -- 4. Check TSS status for each consignment ----------------------
        cons_with_status = []
        for cons in cons_list_stg:
            tss_cons_ref = cons.get('tss_consignment_ref')
            cons_tss_status = ''
            if tss_cons_ref:
                cursor.execute(
                    """
                    SELECT TssStatus
                    FROM TSS.BKD_ENS_Consignments
                    WHERE ClientCode = ? AND ConsignmentReference = ?
                    """,
                    [client_code, tss_cons_ref],
                )
                trow = cursor.fetchone()
                cons_tss_status = _normalise_tss_status(trow[0] if trow else None)
            cons_with_status.append({**cons, '_tss_status': cons_tss_status})

        not_authorised = [
            c for c in cons_with_status
            if c['_tss_status'] != _AUTHORISED_STATUS
        ]
        if not_authorised:
            return False, 'not_all_consignments_authorized'

        # -- 5. Check goods errors -----------------------------------------
        stg_cons_ids = [c['stg_consignment_id'] for c in cons_list_stg]
        placeholders = ','.join('?' * len(stg_cons_ids))
        cursor.execute(
            f"""
            SELECT COUNT(*)
            FROM STG.BKD_GoodsItems
            WHERE stg_consignment_id IN ({placeholders})
              AND error_message IS NOT NULL
            """,
            stg_cons_ids,
        )
        goods_error_count = (cursor.fetchone() or [0])[0]
        if goods_error_count:
            return False, 'goods_have_errors'

        # -- 6. Build consignment list for email (with goods) --------------
        cons_for_email = []
        for cons in cons_with_status:
            goods_cur = conn.cursor()
            goods_cur.execute(
                """
                SELECT stg_item_id, item_seq, goods_description
                FROM STG.BKD_GoodsItems
                WHERE stg_consignment_id = ?
                """,
                [cons['stg_consignment_id']],
            )
            gcols = [d[0] for d in goods_cur.description]
            goods = [dict(zip(gcols, gr)) for gr in goods_cur.fetchall()]
            goods_cur.close()
            cons_for_email.append({
                'stg_consignment_id': cons['stg_consignment_id'],
                'dec_reference': cons.get('tss_consignment_ref'),
                'tss_status': cons['_tss_status'],
                'goods': goods,
            })

        ens_header['stg_header_id'] = stg_header_id
        ok, err = notify_movement_authorised(
            ens_header, cons_for_email, tenant_code=client_code,
        )

        # -- 7. Optional ENS movement pack email ---------------------------
        # This is independent and deduped via TSS.BKD_API_Exchanges. It must
        # not change the existing movement notification contract.
        if ok:
            pack_ok, pack_err = notify_ens_movement_pack_auto(
                ens_header, cons_for_email, tenant_code=client_code,
            )
            if not pack_ok and pack_err != 'NOTIFY.ENS_PACK_AUTO_ENABLED=false':
                log.warning(
                    'Automatic ENS movement pack email did not send for %s: %s',
                    stg_header_id,
                    pack_err,
                )

        # -- 8. Stamp ONLY if movement email sent successfully --------------
        if ok:
            _stamp_stg_header(stg_header_id, 'movement_notified_at', cursor, conn)

        return ok, err

    except Exception as exc:
        log.warning(
            'check_and_notify_ens_authorised error (stg_header_id=%s): %s',
            stg_header_id, exc,
        )
        return False, str(exc)


# -- HTML/text builders ---------------------------------------------------------

_BRAND = '#0b1d3a'
_ACCENT = '#2563eb'
_SUCCESS = '#15803d'
_DANGER = '#991b1b'
_BORDER = '#e5e7eb'
_MUTED = '#6b7280'


def _summary_row(label: str, value) -> str:
    return (
        f'<tr>'
        f'<td style="color:{_MUTED};font-weight:600;padding:5px 8px;'
        f'border-bottom:1px solid {_BORDER};">{_esc(label)}</td>'
        f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};">'
        f'{_esc(str(value))}</td>'
        f'</tr>'
    )


def _build_summary_rows(summary: dict) -> str:
    rows = ''
    for key, value in (summary or {}).items():
        if value is None or value == '':
            continue
        rows += _summary_row(str(key).replace('_', ' ').title(), value)
    return rows


def _build_sdi_autosubmit_issue_html(
    summary: dict,
    errors: list[str],
    *,
    tenant_code: str,
    manual: bool,
) -> str:
    portal = _portal_url(tenant_code)
    mode_label = 'Manual fallback' if manual else 'Automation'
    summary_rows = _build_summary_rows({
        'Tenant': tenant_code,
        'Source': mode_label,
        'Candidates': (summary or {}).get('candidates', 0),
        'Discovered': (summary or {}).get('discovered', 0),
        'Staged Headers': (summary or {}).get('staged_headers', 0),
        'Staged Goods': (summary or {}).get('staged_goods', 0),
        'Ready': (summary or {}).get('ready', 0),
        'Blocked': (summary or {}).get('blocked', 0),
        'Submitted': (summary or {}).get('submitted', 0),
    })
    error_rows = ''.join(
        f'<li style="margin:5px 0;"><code style="font-family:monospace;'
        f'background:#f3f4f6;padding:2px 5px;border-radius:4px;">{_esc(item)}</code></li>'
        for item in (errors or [])[:12]
    )
    if not error_rows:
        error_rows = '<li style="margin:5px 0;">No detailed error text was returned by the worker.</li>'

    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>SDI autosubmit attention</title></head>'
        f'<body style="margin:0;padding:0;background:#f3f4f6;'
        f'font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;color:#1f2937;">'
        f'<div style="max-width:680px;margin:32px auto;background:#fff;'
        f'border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.12);">'
        f'<div style="background:{_BRAND};padding:24px 32px;">'
        f'<div style="font-size:11px;color:#93c5fd;text-transform:uppercase;'
        f'letter-spacing:1px;font-weight:700;">SYNOVIA INTEGRATION - TEST</div>'
        f'<h1 style="color:#fff;font-size:18px;margin:8px 0 0;">'
        f'SDI autosubmit needs attention</h1>'
        f'<div style="color:#93c5fd;font-size:12px;margin:2px 0 0;">'
        f'{_esc(mode_label)} - internal operator notification</div></div>'
        f'<div style="padding:28px 32px;">'
        f'<p>Fusion attempted to submit supplementary declaration data and TSS returned a response that needs review.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">'
        f'{summary_rows}</table>'
        f'<div style="background:#fffbeb;border-left:4px solid #d97706;'
        f'padding:12px 16px;border-radius:0 6px 6px 0;margin:16px 0;">'
        f'<strong>Operator action:</strong> open the SDI detail page, inspect the latest TSS/API response JSON '
        f'and technical log, then retry or cancel the SDI in TSS as appropriate.</div>'
        f'<p style="font-weight:700;margin:18px 0 8px;">TSS/API responses</p>'
        f'<ul style="padding-left:20px;margin:0 0 18px;">{error_rows}</ul>'
        f'<a href="{portal}/supdec/" style="display:inline-block;background:{_ACCENT};color:#fff;'
        f'text-decoration:none;padding:10px 14px;border-radius:6px;font-weight:700;">Open SDI worklist</a>'
        f'</div>'
        f'<div style="background:#f9fafb;padding:16px 32px;border-top:1px solid {_BORDER};'
        f'font-size:12px;color:{_MUTED};">Synovia Integration - Fusion Flow</div>'
        f'</div></body></html>'
    )


def _build_sdi_autosubmit_issue_text(
    summary: dict,
    errors: list[str],
    *,
    tenant_code: str,
    manual: bool,
) -> str:
    portal = _portal_url(tenant_code)
    mode_label = 'manual fallback' if manual else 'automation'
    lines = [
        'SDI autosubmit needs attention',
        f'Tenant: {tenant_code}',
        f'Source: {mode_label}',
        f"Candidates: {(summary or {}).get('candidates', 0)}",
        f"Ready: {(summary or {}).get('ready', 0)}",
        f"Blocked: {(summary or {}).get('blocked', 0)}",
        f"Submitted: {(summary or {}).get('submitted', 0)}",
        f'Open: {portal}/supdec/',
        '',
        'TSS/API responses:',
    ]
    lines.extend((errors or ['No detailed error text was returned by the worker.'])[:12])
    return '\n'.join(lines)


def _build_ingest_success_html(
    *,
    event_type: str,
    event_label: str,
    tenant_code: str,
    stg_header_id: int | None,
    filename: str,
    subject_hint: str,
    summary: dict,
) -> str:
    ens_url = _ens_url(stg_header_id, tenant_code)
    summary_rows = _build_summary_rows(summary)
    if not summary_rows:
        summary_rows = (
            f'<tr><td style="padding:5px 8px;color:{_MUTED};" colspan="2">'
            f'No extra summary provided.</td></tr>'
        )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Email Automation Test - {_esc(event_label)}</title></head>'
        f'<body style="margin:0;padding:0;background:#f3f4f6;'
        f'font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;color:#1f2937;">'
        f'<div style="max-width:640px;margin:32px auto;background:#fff;'
        f'border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.12);">'
        f'<div style="background:{_BRAND};padding:24px 32px;">'
        f'<div style="font-size:11px;color:#93c5fd;text-transform:uppercase;'
        f'letter-spacing:1px;font-weight:700;">SYNOVIA INTEGRATION - TEST</div>'
        f'<h1 style="color:#fff;font-size:18px;margin:8px 0 0;">'
        f'Email Automation - {_esc(event_label)}</h1>'
        f'<div style="color:#93c5fd;font-size:12px;margin:2px 0 0;">'
        f'Temporary smoke-test notification</div>'
        f'</div>'
        f'<div style="padding:28px 32px;">'
        f'<p>The email automation flow received and staged this item successfully.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">'
        f'{_summary_row("Tenant", tenant_code)}'
        f'{_summary_row("Event", event_type)}'
        f'{_summary_row("ENS STG ID", stg_header_id) if stg_header_id else ""}'
        f'{_summary_row("File", filename) if filename else ""}'
        f'{_summary_row("Subject", subject_hint) if subject_hint else ""}'
        f'{summary_rows}</table>'
        f'<div style="background:#ecfdf5;border-left:4px solid {_SUCCESS};'
        f'padding:12px 16px;border-radius:0 6px 6px 0;margin:16px 0;">'
        f'<strong>For testing only:</strong> once the automation is proven,'
        f' positive smoke-test emails can be disabled and only errors/final movement'
        f' notifications should remain.<br>'
        f'<a href="{ens_url}" style="color:{_SUCCESS};">Open related Fusion Flow record</a>'
        f'</div>'
        f'</div>'
        f'<div style="background:#f9fafb;padding:16px 32px;border-top:1px solid {_BORDER};'
        f'font-size:11px;color:{_MUTED};">'
        f'<p>Sent by Synovia Integration on behalf of Birkdale Sales Ltd.</p>'
        f'</div></div></body></html>'
    )


def _build_ingest_success_text(
    *,
    event_label: str,
    tenant_code: str,
    stg_header_id: int | None,
    filename: str,
    subject_hint: str,
    summary: dict,
) -> str:
    lines = [
        'SYNOVIA INTEGRATION - Fusion Flow',
        '=' * 50,
        f'[TEST] Email Automation - {event_label}',
        '',
        f'Tenant: {tenant_code}',
    ]
    if stg_header_id:
        lines.append(f'ENS STG ID: {stg_header_id}')
    if filename:
        lines.append(f'File: {filename}')
    if subject_hint:
        lines.append(f'Subject: {subject_hint}')
    for key, value in (summary or {}).items():
        if value is not None and value != '':
            lines.append(f'{str(key).replace("_", " ").title()}: {value}')
    lines += [
        '',
        'Temporary smoke-test notification. Later we should keep only errors and final Authorised for Movement notifications.',
        '',
        'Synovia Integration - Fusion Flow',
    ]
    return '\n'.join(lines)


def _build_pipeline_error_html(
    *,
    error_type: str,
    detail: str,
    stg_header_id: int | None,
    filename: str,
    tenant_code: str,
) -> str:
    ens_url = _ens_url(stg_header_id, tenant_code)
    label = error_type.replace('_', ' ').title()
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Pipeline Error - {_esc(label)}</title></head>'
        f'<body style="margin:0;padding:0;background:#f3f4f6;'
        f'font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;color:#1f2937;">'
        f'<div style="max-width:640px;margin:32px auto;background:#fff;'
        f'border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.12);">'
        f'<div style="background:{_BRAND};padding:24px 32px;">'
        f'<div style="font-size:11px;color:#93c5fd;text-transform:uppercase;'
        f'letter-spacing:1px;font-weight:700;">SYNOVIA INTEGRATION</div>'
        f'<h1 style="color:#fff;font-size:18px;margin:8px 0 0;">'
        f'Pipeline Error - {_esc(label)}</h1>'
        f'<div style="color:#93c5fd;font-size:12px;margin:2px 0 0;">'
        f'Fusion Flow - Declaration Management Portal</div>'
        f'</div>'
        f'<div style="padding:28px 32px;">'
        f'<p>File: <strong>{_esc(filename or "?")}</strong>'
        f'{f" (ENS #{stg_header_id})" if stg_header_id else ""}'
        f' - Tenant: {_esc(tenant_code)}</p>'
        f'<div style="background:#fef2f2;border-left:4px solid #dc2626;'
        f'padding:12px 16px;border-radius:0 6px 6px 0;margin:16px 0;">'
        f'<strong>{_esc(label)}</strong><br>'
        f'<span style="font-size:12px;color:{_MUTED};">{_esc(str(detail or ""))}</span>'
        f'</div>'
        f'<div style="background:#eff6ff;border-left:4px solid {_ACCENT};'
        f'padding:12px 16px;border-radius:0 6px 6px 0;margin:16px 0;">'
        f'<strong>What to do:</strong> Log in to the portal to investigate and'
        f' resubmit the file after correcting the issue.<br>'
        f'<a href="{ens_url}" style="color:{_ACCENT};">Open in Fusion Flow portal</a>'
        f'</div>'
        f'</div>'
        f'<div style="background:#f9fafb;padding:16px 32px;border-top:1px solid {_BORDER};'
        f'font-size:11px;color:{_MUTED};">'
        f'<p>Sent by Synovia Integration on behalf of Birkdale Sales Ltd.</p>'
        f'</div></div></body></html>'
    )


def _build_pipeline_error_text(
    *,
    error_type: str,
    detail: str,
    filename: str,
) -> str:
    label = error_type.replace('_', ' ').title()
    return '\n'.join([
        'SYNOVIA INTEGRATION - Fusion Flow',
        '=' * 50,
        f'Pipeline Error - {label}',
        '',
        f'File: {filename or "?"}',
        f'Error: {detail or ""}',
        '',
        'Log in to the portal to investigate and resubmit.',
        '',
        'Synovia Integration - Fusion Flow',
    ])


def _linked_consignment_issue_html(
    issue: str,
    cons_by_doc: dict[str, dict],
    *,
    portal_url: str,
    ens_url: str,
) -> str:
    text = str(issue or "")
    for doc_no, cons in cons_by_doc.items():
        if not doc_no or doc_no not in text:
            continue
        cons_id = cons.get('staging_id')
        href = f'{portal_url}/consignments/{cons_id}' if cons_id else ens_url
        before, after = text.split(doc_no, 1)
        return (
            f'{_esc(before)}'
            f'<a href="{href}" style="color:{_ACCENT};font-weight:600;">{_esc(doc_no)}</a>'
            f'{_esc(after)}'
        )
    return _esc(text)


def _build_staging_failure_html(
    *,
    ens_staging_id: int | None,
    filename: str,
    tenant_code: str,
    blockers: list[str],
    warnings: list[str] | None = None,
    all_consignments: list[dict],
) -> str:
    warnings = list(warnings or [])
    portal_url = _portal_url(tenant_code)
    ens_url = _ens_url(ens_staging_id, tenant_code)
    cons_by_doc = {
        str(c.get('document_no') or '').strip(): c
        for c in all_consignments or []
        if str(c.get('document_no') or '').strip()
    }

    blocker_items = ''.join(
        f'<li style="margin:4px 0;">'
        f'{_linked_consignment_issue_html(b, cons_by_doc, portal_url=portal_url, ens_url=ens_url)}'
        f'</li>'
        for b in blockers[:10]
    )
    blocker_block = (
        f'<div style="background:#fef2f2;border-left:4px solid #dc2626;'
        f'padding:12px 16px;border-radius:0 6px 6px 0;margin:16px 0;">'
        f'<strong>Blockers:</strong><ul style="margin:8px 0 0 16px;">'
        f'{blocker_items}</ul></div>'
    ) if blocker_items else ''

    # Goods-level warnings that require operator review
    warning_items = ''.join(
        f'<li style="margin:4px 0;">'
        f'{_linked_consignment_issue_html(w, cons_by_doc, portal_url=portal_url, ens_url=ens_url)}'
        f'</li>'
        for w in warnings[:15]
    )
    warning_block = (
        f'<div style="background:#fffbeb;border-left:4px solid #d97706;'
        f'padding:12px 16px;border-radius:0 6px 6px 0;margin:16px 0;">'
        f'<strong>Goods requiring attention:</strong>'
        f'<ul style="margin:8px 0 0 16px;">'
        f'{warning_items}</ul></div>'
    ) if warning_items else ''

    cons_rows = ''
    for c in all_consignments[:25]:
        doc_no = _esc(str(c.get('document_no') or c.get('staging_id') or '?'))
        hard_fail = bool(c.get('failed') or c.get('hard_blockers') or c.get('error'))
        soft_fail = bool(c.get('blockers') or c.get('warnings'))
        if hard_fail:
            status_label, color = 'FAILED', _DANGER
        elif soft_fail:
            status_label, color = 'NEEDS REVIEW', '#d97706'
        else:
            status_label, color = 'OK', _SUCCESS
        error = _esc(str(
            c.get('error')
            or '; '.join(c.get('hard_blockers') or [])
            or '; '.join(c.get('blockers') or [])
            or '; '.join(c.get('warnings') or [])
            or ''
        )[:200])
        goods_ok = sum(1 for g in (c.get('goods') or []) if not g.get('failed') and not g.get('warnings'))
        goods_warn = sum(1 for g in (c.get('goods') or []) if g.get('warnings') and not g.get('failed'))
        goods_fail = sum(1 for g in (c.get('goods') or []) if g.get('failed'))
        goods_summary = f'{goods_ok} ok'
        if goods_warn:
            goods_summary += f' / {goods_warn} review'
        if goods_fail:
            goods_summary += f' / {goods_fail} failed'
        # Per-consignment portal link when staging_id is available
        cons_staging_id = c.get('staging_id')
        cons_link = (
            f'{portal_url}/consignments/{cons_staging_id}'
            if cons_staging_id else ens_url
        )
        doc_cell = (
            f'<a href="{cons_link}" style="color:{_ACCENT};">{doc_no}</a>'
            if cons_staging_id else doc_no
        )
        cons_rows += (
            f'<tr>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};">{doc_cell}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};'
            f'color:{color};font-weight:600;">{status_label}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};">'
            f'{goods_summary}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};'
            f'color:{_MUTED};font-size:12px;">{error}</td>'
            f'</tr>'
        )

    cons_table = (
        f'<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">Consignment</th>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">Status</th>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">Goods</th>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">Issue</th>'
        f'</tr></thead><tbody>{cons_rows}</tbody></table>'
    ) if cons_rows else ''

    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Sales Orders Staging Failed - ENS #{ens_staging_id or "?"}</title></head>'
        f'<body style="margin:0;padding:0;background:#f3f4f6;'
        f'font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;color:#1f2937;">'
        f'<div style="max-width:640px;margin:32px auto;background:#fff;'
        f'border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.12);">'
        f'<div style="background:{_BRAND};padding:24px 32px;">'
        f'<div style="font-size:11px;color:#93c5fd;text-transform:uppercase;'
        f'letter-spacing:1px;font-weight:700;">SYNOVIA INTEGRATION</div>'
        f'<h1 style="color:#fff;font-size:18px;margin:8px 0 0;">'
        f'Action Required - Sales Orders Staging Failed</h1>'
        f'<div style="color:#93c5fd;font-size:12px;margin:2px 0 0;">'
        f'Fusion Flow - Declaration Management Portal</div>'
        f'</div>'
        f'<div style="padding:28px 32px;">'
        f'<p>Sales Orders staging from <strong>{_esc(filename or "Excel workbook")}</strong>'
        f' could not complete for ENS <strong>#{ens_staging_id or "?"}</strong>'
        f' ({_esc(tenant_code)}).</p>'
        f'{blocker_block}'
        f'{warning_block}'
        f'{cons_table}'
        f'<div style="background:#eff6ff;border-left:4px solid {_ACCENT};'
        f'padding:12px 16px;border-radius:0 6px 6px 0;margin:16px 0;">'
        f'<strong>What to do:</strong> Log in to the portal and correct the affected '
        f'consignments / goods before submitting to TSS.<br>'
        f'<a href="{ens_url}" style="color:{_ACCENT};">Open in Fusion Flow portal</a>'
        f'</div>'
        f'</div>'
        f'<div style="background:#f9fafb;padding:16px 32px;border-top:1px solid {_BORDER};'
        f'font-size:11px;color:{_MUTED};">'
        f'<p>Sent by Synovia Integration on behalf of Birkdale Sales Ltd.</p>'
        f'</div></div></body></html>'
    )


def _build_staging_failure_text(
    *,
    ens_staging_id: int | None,
    filename: str,
    blockers: list[str],
    warnings: list[str] | None = None,
    all_consignments: list[dict],
) -> str:
    warnings = list(warnings or [])
    heading = 'Action Required - Sales Orders Staging Failed' if blockers else 'Action Required - Sales Orders Goods Need Attention'
    lines = [
        'SYNOVIA INTEGRATION - Fusion Flow',
        '=' * 50,
        heading,
        '',
        f'File: {filename or "Excel workbook"}',
        f'ENS staging ID: #{ens_staging_id or "?"}',
        '',
    ]
    if blockers:
        lines.append('BLOCKERS:')
        lines.extend(f'  - {b}' for b in blockers[:10])
        lines.append('')
    if warnings:
        lines.append('GOODS REQUIRING ATTENTION:')
        lines.extend(f'  - {w}' for w in warnings[:15])
        lines.append('')
    failed = [
        c for c in all_consignments
        if c.get('failed') or c.get('hard_blockers') or c.get('error')
        or c.get('blockers') or c.get('warnings')
    ]
    if failed:
        lines.append('CONSIGNMENTS NEEDING ACTION:')
        for c in failed[:10]:
            err = (
                c.get('error')
                or '; '.join(c.get('hard_blockers') or [])
                or '; '.join(c.get('blockers') or [])
                or '; '.join(c.get('warnings') or [])
                or 'review required'
            )
            lines.append(
                f"  - {c.get('document_no') or c.get('staging_id') or '?'}: {err}"
            )
        lines.append('')
    lines += [
        'Log in to the portal to correct these before submitting to TSS.',
        '',
        'Synovia Integration - Fusion Flow',
    ]
    return '\n'.join(lines)


def _build_cargo_submitted_html(
    ens_header: dict,
    consignments: list[dict],
    summary: dict,
    *,
    tenant_code: str,
) -> str:
    stg_header_id = ens_header.get('stg_header_id')
    ens_ref = (
        ens_header.get('tss_ens_header_ref')
        or ens_header.get('ens_reference')
        or ens_header.get('conveyance_ref')
        or f"#{stg_header_id or '?'}"
    )
    ens_link = _ens_url(stg_header_id, tenant_code)
    rows = ''
    total_goods = 0
    for cons in (consignments or [])[:40]:
        cons_id = cons.get('stg_consignment_id') or cons.get('staging_id')
        dec_ref = cons.get('tss_consignment_ref') or cons.get('dec_reference') or '(pending)'
        doc_no = cons.get('document_no') or cons.get('trader_reference') or cons.get('transport_document_number') or ''
        goods_count = int(cons.get('goods_count') or 0)
        total_goods += goods_count
        cons_link = _consignment_url(cons_id, tenant_code)
        rows += (
            f'<tr>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};">'
            f'<a href="{cons_link}" style="color:{_ACCENT};font-weight:600;">'
            f'{_esc(str(dec_ref))}</a></td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};">'
            f'{_esc(str(doc_no or "-"))}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};">'
            f'{goods_count}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};color:{_SUCCESS};font-weight:600;">'
            f'{_esc(str(cons.get("sub_status") or "SUBMITTED"))}</td>'
            f'</tr>'
        )
    if not rows:
        rows = (
            f'<tr><td colspan="4" style="padding:8px;color:{_MUTED};">'
            f'No consignment rows were available for the notification.</td></tr>'
        )

    summary_rows = (
        _summary_row('ENS Reference', ens_ref)
        + _summary_row('Consignments', len(consignments or []))
        + _summary_row('Goods', total_goods or summary.get('goods_created') or 0)
        + _summary_row('Consignments Submitted', summary.get('cons_submitted', 0))
        + _summary_row('Goods Created', summary.get('goods_created', 0))
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Cargo submitted to TSS - {_esc(str(ens_ref))}</title></head>'
        f'<body style="margin:0;padding:0;background:#f3f4f6;'
        f'font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;color:#1f2937;">'
        f'<div style="max-width:680px;margin:32px auto;background:#fff;'
        f'border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.12);">'
        f'<div style="background:{_BRAND};padding:24px 32px;">'
        f'<div style="font-size:11px;color:#93c5fd;text-transform:uppercase;'
        f'letter-spacing:1px;font-weight:700;">SYNOVIA INTEGRATION</div>'
        f'<h1 style="color:#fff;font-size:18px;margin:8px 0 0;">'
        f'Cargo submitted to TSS</h1>'
        f'<div style="color:#93c5fd;font-size:12px;margin:2px 0 0;">'
        f'ENS {_esc(str(ens_ref))}</div></div>'
        f'<div style="padding:28px 32px;">'
        f'<p>The Sales Orders cargo has been validated locally and sent to TSS.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">'
        f'{summary_rows}</table>'
        f'<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">DEC</th>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">Document</th>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">Goods</th>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">Local Status</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
        f'<div style="background:#ecfdf5;border-left:4px solid {_SUCCESS};'
        f'padding:12px 16px;border-radius:0 6px 6px 0;margin:16px 0;">'
        f'<strong>Next:</strong> Fusion Flow will keep syncing this ENS until TSS reaches '
        f'Authorised for Movement or requests action.<br>'
        f'<a href="{ens_link}" style="color:{_SUCCESS};">Open ENS in Fusion Flow</a>'
        f'</div></div>'
        f'<div style="background:#f9fafb;padding:16px 32px;border-top:1px solid {_BORDER};'
        f'font-size:11px;color:{_MUTED};">'
        f'<p>Sent by Synovia Integration on behalf of Birkdale Sales Ltd.</p>'
        f'</div></div></body></html>'
    )


def _build_cargo_submitted_text(
    ens_header: dict,
    consignments: list[dict],
    summary: dict,
    *,
    tenant_code: str,
) -> str:
    stg_header_id = ens_header.get('stg_header_id')
    ens_ref = (
        ens_header.get('tss_ens_header_ref')
        or ens_header.get('ens_reference')
        or ens_header.get('conveyance_ref')
        or f"#{stg_header_id or '?'}"
    )
    lines = [
        'SYNOVIA INTEGRATION - Fusion Flow',
        '=' * 50,
        f'Cargo submitted to TSS - {ens_ref}',
        '',
        f'ENS link: {_ens_url(stg_header_id, tenant_code)}',
        f'Consignments submitted: {summary.get("cons_submitted", 0)}',
        f'Goods created: {summary.get("goods_created", 0)}',
        '',
        'CONSIGNMENTS:',
    ]
    for cons in consignments or []:
        cons_id = cons.get('stg_consignment_id') or cons.get('staging_id')
        dec_ref = cons.get('tss_consignment_ref') or cons.get('dec_reference') or '(pending)'
        doc_no = cons.get('document_no') or cons.get('trader_reference') or cons.get('transport_document_number') or ''
        lines.append(
            f'  - {dec_ref} / {doc_no}: {cons.get("goods_count") or 0} goods '
            f'({_consignment_url(cons_id, tenant_code)})'
        )
    lines += ['', 'Fusion Flow will keep syncing TSS statuses automatically.']
    return '\n'.join(lines)


def _build_tss_status_attention_html(
    ens_header: dict,
    status_items: list[dict],
    *,
    tenant_code: str,
) -> str:
    stg_header_id = ens_header.get('stg_header_id')
    ens_ref = (
        ens_header.get('tss_ens_header_ref')
        or ens_header.get('ens_reference')
        or ens_header.get('conveyance_ref')
        or f"#{stg_header_id or '?'}"
    )
    ens_link = _ens_url(stg_header_id, tenant_code)
    rows = ''
    for item in status_items[:30]:
        kind = str(item.get('entity_kind') or 'TSS')
        ref = (
            item.get('tss_ref')
            or item.get('tss_consignment_ref')
            or item.get('tss_ens_header_ref')
            or item.get('stg_consignment_id')
            or stg_header_id
            or '?'
        )
        cons_id = item.get('stg_consignment_id')
        href = _consignment_url(cons_id, tenant_code) if cons_id else ens_link
        rows += (
            f'<tr>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};">{_esc(kind)}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};">'
            f'<a href="{href}" style="color:{_ACCENT};font-weight:600;">{_esc(str(ref))}</a></td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};color:#d97706;font-weight:700;">'
            f'{_esc(str(item.get("tss_status") or "TRADER_INPUT_REQUIRED"))}</td>'
            f'</tr>'
        )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Action Required - TSS Trader Input Required</title></head>'
        f'<body style="margin:0;padding:0;background:#f3f4f6;'
        f'font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;color:#1f2937;">'
        f'<div style="max-width:680px;margin:32px auto;background:#fff;'
        f'border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.12);">'
        f'<div style="background:{_BRAND};padding:24px 32px;">'
        f'<div style="font-size:11px;color:#93c5fd;text-transform:uppercase;'
        f'letter-spacing:1px;font-weight:700;">SYNOVIA INTEGRATION</div>'
        f'<h1 style="color:#fff;font-size:18px;margin:8px 0 0;">'
        f'TSS needs trader input</h1>'
        f'<div style="color:#93c5fd;font-size:12px;margin:2px 0 0;">'
        f'ENS {_esc(str(ens_ref))}</div></div>'
        f'<div style="padding:28px 32px;">'
        f'<div style="background:#fffbeb;border-left:4px solid #d97706;'
        f'padding:12px 16px;border-radius:0 6px 6px 0;margin:0 0 16px;">'
        f'TSS returned <strong>Trader Input Required</strong>. Open the ENS, review the highlighted '
        f'consignment or header, correct the data, then retry from Fusion Flow.</div>'
        f'<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">Type</th>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">Reference</th>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">TSS Status</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
        f'<a href="{ens_link}" style="display:inline-block;background:{_ACCENT};color:#fff;'
        f'text-decoration:none;padding:10px 14px;border-radius:6px;font-weight:600;">'
        f'Open ENS in Fusion Flow</a>'
        f'</div>'
        f'<div style="background:#f9fafb;padding:16px 32px;border-top:1px solid {_BORDER};'
        f'font-size:11px;color:{_MUTED};">'
        f'<p>Sent by Synovia Integration on behalf of Birkdale Sales Ltd.</p>'
        f'</div></div></body></html>'
    )


def _build_tss_status_attention_text(
    ens_header: dict,
    status_items: list[dict],
    *,
    tenant_code: str,
) -> str:
    stg_header_id = ens_header.get('stg_header_id')
    ens_ref = (
        ens_header.get('tss_ens_header_ref')
        or ens_header.get('ens_reference')
        or ens_header.get('conveyance_ref')
        or f"#{stg_header_id or '?'}"
    )
    lines = [
        'SYNOVIA INTEGRATION - Fusion Flow',
        '=' * 50,
        f'Action Required - TSS Trader Input Required ({ens_ref})',
        '',
        f'ENS link: {_ens_url(stg_header_id, tenant_code)}',
        '',
        'ITEMS:',
    ]
    for item in status_items:
        cons_id = item.get('stg_consignment_id')
        ref = item.get('tss_ref') or item.get('tss_consignment_ref') or item.get('stg_consignment_id') or ens_ref
        link = _consignment_url(cons_id, tenant_code) if cons_id else _ens_url(stg_header_id, tenant_code)
        lines.append(f'  - {item.get("entity_kind", "TSS")} {ref}: {item.get("tss_status")} ({link})')
    lines += ['', 'Open the ENS, correct the data, then retry from Fusion Flow.']
    return '\n'.join(lines)


def _movement_pack_context(ens_header: dict, consignments: list[dict]) -> tuple[dict, dict, list[dict], dict]:
    """Adapt STG/TSS notification data to the existing ENS pack template shape."""
    ens_ref = (
        ens_header.get('tss_ens_header_ref')
        or ens_header.get('ens_reference')
        or ens_header.get('conveyance_ref')
        or f"Draft #{ens_header.get('stg_header_id', '?')}"
    )
    pipeline_header = {
        'staging_id': ens_header.get('stg_header_id'),
        'ens_reference': ens_ref,
        'tss_status': _AUTHORISED_STATUS,
        'created_at': ens_header.get('created_at') or ens_header.get('stg_created_at'),
        'movement_type': ens_header.get('movement_type') or '',
        'arrival_port': ens_header.get('arrival_port') or '',
        'arrival_date_time': ens_header.get('arrival_date_time') or '',
        'identity_no_of_transport': ens_header.get('identity_no_of_transport') or '',
        'nationality_of_transport': ens_header.get('nationality_of_transport') or '',
        'carrier_eori': ens_header.get('carrier_eori') or '',
        'carrier_name': ens_header.get('carrier_name') or '',
        'haulier_eori': ens_header.get('haulier_eori') or '',
        'place_of_loading': ens_header.get('place_of_loading') or '',
        'place_of_unloading': ens_header.get('place_of_unloading') or '',
    }
    dec = {'external_status': _AUTHORISED_STATUS, 'status': _AUTHORISED_STATUS}

    pack_consignments: list[dict] = []
    goods_by_cons: dict = {}
    for idx, cons in enumerate(consignments, start=1):
        cons_id = cons.get('stg_consignment_id') or cons.get('staging_id') or idx
        goods = cons.get('goods') or []
        pack_cons = {
            **cons,
            'staging_id': cons_id,
            'dec_reference': cons.get('dec_reference') or cons.get('tss_consignment_ref') or '',
            'tss_status': cons.get('tss_status') or _AUTHORISED_STATUS,
            'goods_description': cons.get('goods_description') or (
                goods[0].get('goods_description') if goods else ''
            ),
        }
        pack_consignments.append(pack_cons)
        pack_goods = []
        for gidx, goods_item in enumerate(goods, start=1):
            pack_goods.append({
                **goods_item,
                'staging_id': goods_item.get('stg_item_id') or goods_item.get('staging_id') or gidx,
                'item_number': goods_item.get('item_seq') or goods_item.get('item_number') or gidx,
            })
        goods_by_cons[cons_id] = pack_goods

    return pipeline_header, dec, pack_consignments, goods_by_cons


def _build_movement_authorised_pack_html(
    ens_header: dict,
    consignments: list[dict],
) -> str | None:
    """Render the ENS Movement Pack template, including from cron/background jobs."""
    try:
        from flask import has_app_context
        if has_app_context():
            return _render_movement_authorised_pack_html_in_context(ens_header, consignments)

        try:
            from app import create_app
            app_obj = create_app()
            with app_obj.app_context():
                return _render_movement_authorised_pack_html_in_context(ens_header, consignments)
        except Exception as exc:
            log.debug('ENS movement pack app context unavailable, using fallback email: %s', exc)
            return None
    except Exception as exc:
        log.debug('ENS movement pack render unavailable, using fallback email: %s', exc)
        return None


def _render_movement_authorised_pack_html_in_context(
    ens_header: dict,
    consignments: list[dict],
) -> str | None:
    """Render the ENS Movement Pack template inside an active Flask app context."""
    try:
        from flask import render_template
        stg_header_id = ens_header.get('stg_header_id')

        if stg_header_id:
            try:
                from app.blueprints.declarations.routes import (
                    _build_ens_pack_context,
                    _render_ens_pack_body,
                )
                (
                    pipeline_header,
                    dec,
                    pack_consignments,
                    goods_by_cons,
                    can_email,
                ) = _build_ens_pack_context(int(stg_header_id))
                if can_email:
                    return _render_ens_pack_body(
                        pipeline_header,
                        dec,
                        pack_consignments,
                        goods_by_cons,
                        note='',
                        logo_mode='cid',
                    )
            except Exception as exc:
                log.debug('ENS movement pack DB context unavailable, using adapted context: %s', exc)

        pipeline_header, dec, pack_consignments, goods_by_cons = _movement_pack_context(
            ens_header, consignments,
        )
        return render_template(
            'declarations/_email_pack_body.html',
            pipeline_header=pipeline_header,
            dec=dec,
            consignments=pack_consignments,
            goods_by_cons=goods_by_cons,
            logo_data_uri='',
            synovia_logo_uri='',
            tenant_logo_uri='',
            tenant_label='Birkdale',
            note='',
        )
    except Exception as exc:
        log.debug('ENS movement pack render unavailable, using fallback email: %s', exc)
        return None


def _build_movement_authorised_html(
    ens_header: dict,
    consignments: list[dict],
) -> str:
    pack_html = _build_movement_authorised_pack_html(ens_header, consignments)
    if pack_html:
        return pack_html

    ens_ref = (
        ens_header.get('tss_ens_header_ref')
        or ens_header.get('conveyance_ref')
        or f"#{ens_header.get('stg_header_id', '?')}"
    )
    arrival_dt = str(ens_header.get('arrival_date_time') or '')[:16]
    arrival_port = _esc(str(ens_header.get('arrival_port') or ''))
    portal_url = _portal_url()
    stg_header_id = ens_header.get('stg_header_id')
    ens_url = f'{portal_url}/ens/header/{stg_header_id}' if stg_header_id else f'{portal_url}/ingest/'

    cons_rows = ''
    total_goods = 0
    for c in consignments[:30]:
        dec_ref = _esc(c.get('dec_reference') or '(pending)')
        gcount = len(c.get('goods') or [])
        total_goods += gcount
        cons_rows += (
            f'<tr>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};">'
            f'<code style="font-family:monospace;background:#f3f4f6;padding:1px 5px;'
            f'border-radius:3px;">{dec_ref}</code></td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};'
            f'color:{_SUCCESS};font-weight:600;">AUTHORISED</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid {_BORDER};">{gcount}</td>'
            f'</tr>'
        )

    meta_rows = (
        f'<tr><td style="color:{_MUTED};font-weight:600;padding:5px 8px;">ENS Reference</td>'
        f'<td style="padding:5px 8px;"><code style="font-family:monospace;background:#f3f4f6;'
        f'padding:1px 5px;border-radius:3px;">{_esc(ens_ref)}</code></td></tr>'
    )
    if arrival_dt:
        meta_rows += (
            f'<tr><td style="color:{_MUTED};font-weight:600;padding:5px 8px;">Arrival Date</td>'
            f'<td style="padding:5px 8px;">{_esc(arrival_dt)}</td></tr>'
        )
    if arrival_port:
        meta_rows += (
            f'<tr><td style="color:{_MUTED};font-weight:600;padding:5px 8px;">Arrival Port</td>'
            f'<td style="padding:5px 8px;">{arrival_port}</td></tr>'
        )
    meta_rows += (
        f'<tr><td style="color:{_MUTED};font-weight:600;padding:5px 8px;">Total Goods</td>'
        f'<td style="padding:5px 8px;">{total_goods}</td></tr>'
    )

    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Authorised for Movement - {_esc(ens_ref)}</title></head>'
        f'<body style="margin:0;padding:0;background:#f3f4f6;'
        f'font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;color:#1f2937;">'
        f'<div style="max-width:640px;margin:32px auto;background:#fff;'
        f'border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.12);">'
        f'<div style="background:{_BRAND};padding:24px 32px;">'
        f'<div style="font-size:11px;color:#93c5fd;text-transform:uppercase;'
        f'letter-spacing:1px;font-weight:700;">SYNOVIA INTEGRATION</div>'
        f'<h1 style="color:#fff;font-size:18px;margin:8px 0 0;">'
        f'Authorised for Movement &#10003;</h1>'
        f'<div style="color:#93c5fd;font-size:12px;margin:2px 0 0;">'
        f'Fusion Flow - Declaration Management Portal</div>'
        f'</div>'
        f'<div style="padding:28px 32px;">'
        f'<p>ENS <strong>{_esc(ens_ref)}</strong> and all {len(consignments)} '
        f'consignment(s) are <strong style="color:{_SUCCESS};">'
        f'AUTHORISED FOR MOVEMENT</strong>.</p>'
        f'<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">'
        f'<thead><tr>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">DEC Reference</th>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">TSS Status</th>'
        f'<th style="text-align:left;padding:5px 8px;border-bottom:2px solid {_BORDER};">Goods</th>'
        f'</tr></thead><tbody>{cons_rows}</tbody></table>'
        f'<table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">'
        f'{meta_rows}</table>'
        f'<div style="background:#dcfce7;border-left:4px solid #16a34a;'
        f'padding:12px 16px;border-radius:0 6px 6px 0;margin:16px 0;">'
        f'<strong>Next step:</strong> Your haulier needs a GMR (Goods Movement Reference) '
        f'to present at the port. The GMR workflow is now unlocked in Fusion Flow.'
        f'<br><a href="{ens_url}" style="color:{_SUCCESS};">Open ENS movement details</a>'
        f'</div>'
        f'</div>'
        f'<div style="background:#f9fafb;padding:16px 32px;border-top:1px solid {_BORDER};'
        f'font-size:11px;color:{_MUTED};">'
        f'<p>Sent by Synovia Integration on behalf of Birkdale Sales Ltd.</p>'
        f'</div></div></body></html>'
    )


def _build_movement_authorised_text(
    ens_header: dict,
    consignments: list[dict],
) -> str:
    ens_ref = (
        ens_header.get('tss_ens_header_ref')
        or ens_header.get('conveyance_ref')
        or f"#{ens_header.get('stg_header_id', '?')}"
    )
    lines = [
        'SYNOVIA INTEGRATION - Fusion Flow',
        '=' * 50,
        'Authorised for Movement',
        '',
        f'ENS Reference: {ens_ref}',
        f'Consignments:  {len(consignments)}',
        '',
        'CONSIGNMENTS:',
    ]
    total_goods = 0
    for c in consignments:
        gcount = len(c.get('goods') or [])
        total_goods += gcount
        lines.append(
            f"  {c.get('dec_reference') or '(pending)'}: "
            f"AUTHORISED FOR MOVEMENT - {gcount} goods"
        )
    lines += [
        '',
        f'Total goods: {total_goods}',
        '',
        'Next: haulier needs a GMR. The GMR workflow is now unlocked in Fusion Flow.',
        '',
        'Synovia Integration - Fusion Flow',
    ]
    return '\n'.join(lines)


def _esc(value: str) -> str:
    """Minimal HTML escape for user-supplied values."""
    return (
        str(value)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )
