"""
Pull invoice emails and auto-stage them into one ENS per email batch.

Supported mailbox providers:
  - IMAP
  - Microsoft Graph

Sales Orders automation is intentionally two-step:
  - a body-only `DETAILS FOR ...` email creates/updates the ENS header draft
  - a later Sales Orders XLSX email links consignments/goods to that ENS draft

Other supported files keep the legacy batch path:
  - one PDF/CSV/ZIP attachment => one consignment
  - one invoice line or mapped row => one goods item
"""

from __future__ import annotations

import argparse
import hashlib
import imaplib
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from email.utils import parseaddr
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DEFAULT_ATTACHMENT_ARCHIVE_DIR = r'\\PL-AZ-Fusion-co\FusionProduction\FusionFlow V2\BKD\files'

from app.db import get_standalone_connection
from app.ingestion.defaults import resolve_graph_mail_settings, resolve_imap_settings
from app.ingestion.email_batch import (
    build_email_metadata,
    extract_email_body_text,
    extract_supported_attachments,
    is_sales_order_workbook_attachment,
    parse_email_bytes,
)
from app.ingestion.excel_sales_orders import parse_email_carrier_block
from app.ingestion.graph_mail import GraphMailClient
from app.tenant import TENANT_REGISTRY


def _portal_url() -> str:
    return os.environ.get('PORTAL_URL', 'http://localhost:10000').rstrip('/')


def _headers(tenant_code: str | None = None) -> dict:
    headers = {}
    webhook_key = os.environ.get('INGEST_WEBHOOK_KEY', '')
    if webhook_key:
        headers['X-API-Key'] = webhook_key
    resolved_tenant_code = (tenant_code or os.environ.get('TENANT_CODE', '')).strip().upper()
    if resolved_tenant_code:
        headers['X-Tenant-Code'] = resolved_tenant_code
    return headers


def _search_terms(raw_search: str) -> list[str]:
    return shlex.split(raw_search or 'UNSEEN')


def _response_preview(resp: requests.Response) -> str:
    text = (resp.text or '').strip()
    if not text:
        return '(empty body)'
    return text[:500]


def _post_portal_form(url: str, *, files, data: dict, tenant_code: str | None = None) -> tuple[bool, dict]:
    try:
        resp = requests.post(
            url,
            files=files,
            data=data,
            headers=_headers(tenant_code),
            timeout=180,
            allow_redirects=False,
        )
    except Exception as exc:
        return False, {'error': str(exc)}

    if resp.status_code >= 400:
        return False, {
            'error': (
                f'HTTP {resp.status_code} from {url}: '
                f'{_response_preview(resp)}'
            )
        }

    try:
        return True, resp.json()
    except ValueError:
        return False, {
            'error': (
                f'Non-JSON response from {url}: HTTP {resp.status_code}, '
                f'content-type={resp.headers.get("content-type", "(missing)")}, '
                f'body={_response_preview(resp)}'
            )
        }


def _post_portal_data(url: str, *, data: dict, tenant_code: str | None = None) -> tuple[bool, dict]:
    try:
        resp = requests.post(
            url,
            data=data,
            headers=_headers(tenant_code),
            timeout=180,
            allow_redirects=False,
        )
    except Exception as exc:
        return False, {'error': str(exc)}

    if resp.status_code >= 400:
        return False, {
            'error': (
                f'HTTP {resp.status_code} from {url}: '
                f'{_response_preview(resp)}'
            )
        }

    try:
        return True, resp.json()
    except ValueError:
        return False, {
            'error': (
                f'Non-JSON response from {url}: HTTP {resp.status_code}, '
                f'content-type={resp.headers.get("content-type", "(missing)")}, '
                f'body={_response_preview(resp)}'
            )
        }


def _parse_message_datetime(raw_value: str | None) -> datetime:
    raw = str(raw_value or '').strip()
    if raw:
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    return datetime.now(timezone.utc)


def _message_has_sales_order_details(message: dict) -> bool:
    return bool(_sales_order_details_meta(message).raw_block)


def _sales_order_details_meta(message: dict):
    body = str(message.get('body') or '')
    return parse_email_carrier_block(
        body,
        received_at=_parse_message_datetime(message.get('received') or message.get('date')),
    )


def _sales_order_details_body(message: dict) -> str:
    """Return only the parsed carrier DETAILS block for portal posts.

    Graph/Outlook threads can include months of quoted replies and signatures.
    The portal only needs the current carrier block; keeping the payload short
    avoids slow form parsing and prevents old quoted DETAILS blocks from leaking
    into ingestion.
    """
    meta = _sales_order_details_meta(message)
    return meta.raw_block or ''


# Step 02 - email classification (docs/README.md)
def _classify_inbound_message(message: dict, attachments: list[dict] | None = None) -> dict:
    attachments = list(attachments if attachments is not None else (message.get('attachments') or []))
    details_meta = _sales_order_details_meta(message)
    sales_order_attachments = [item for item in attachments if _is_sales_order_workbook(item)]
    batch_attachments = [item for item in attachments if not _is_sales_order_workbook(item)]

    if details_meta.raw_block and not attachments:
        action = 'sales_orders_details'
    elif sales_order_attachments:
        action = 'sales_orders_xlsx'
    elif batch_attachments:
        action = 'batch'
    else:
        action = 'skip'

    return {
        'action': action,
        'has_details': bool(details_meta.raw_block),
        'details_conveyance_ref': details_meta.conveyance_ref,
        'details_movement_type': details_meta.movement_type,
        'details_warnings': details_meta.parse_warnings,
        'sales_order_attachments': sales_order_attachments,
        'batch_attachments': batch_attachments,
        'attachment_count': len(attachments),
    }


def _print_dry_run_message(message_id: str, classification: dict) -> None:
    details = 'yes' if classification['has_details'] else 'no'
    warnings = '; '.join(classification.get('details_warnings') or []) or 'none'
    print(
        f"DRY_RUN {message_id}: "
        f"action={classification['action']} "
        f"details={details} "
        f"sales_order_xlsx={len(classification['sales_order_attachments'])} "
        f"batch_attachments={len(classification['batch_attachments'])} "
        f"conveyance_ref={classification.get('details_conveyance_ref') or '-'} "
        f"movement_type={classification.get('details_movement_type') or '-'} "
        f"warnings={warnings}"
    )


def _source_tenant_from_mailbox(settings, tenant_code: str | None = None) -> str:
    active = str(tenant_code or os.environ.get('TENANT_CODE') or '').strip().upper()
    folder = str(getattr(settings, 'folder', '') or '').strip()
    first_part = re.split(r'[\\/]+', folder, maxsplit=1)[0].strip().upper()
    if active == 'SYD' and first_part in TENANT_REGISTRY and first_part != 'SYD':
        return first_part
    return active


def _attachment_archive_dir() -> Path:
    return Path(os.environ.get('INGEST_ATTACHMENT_ARCHIVE_DIR') or DEFAULT_ATTACHMENT_ARCHIVE_DIR)


def _safe_archive_part(value: str, *, fallback: str = 'attachment', max_length: int = 120) -> str:
    text = str(value or '').replace('\\', '/').rsplit('/', 1)[-1]
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', text)
    text = re.sub(r'\s+', ' ', text).strip(' ._')
    if not text:
        text = fallback
    return text[:max_length].strip(' ._') or fallback


def _archive_file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ''


def _sales_order_archive_path(archive_dir: Path, filename: str, received_dt: datetime, data: bytes) -> Path:
    """Return a readable, stable Sales Orders archive path.

    Store files in one flat archive folder with the received date appended to
    the attachment stem. If two different files arrive on the same date, append
    a simple numeric suffix.
    """

    safe_filename = _safe_archive_part(filename, fallback='attachment.xlsx')
    stem, ext = os.path.splitext(safe_filename)
    if not ext:
        ext = '.bin'
    stem = re.sub(r'\s+\(\d+\)$', '', stem).strip() or 'attachment'
    date_str = received_dt.strftime('%d.%m.%Y')
    wanted_digest = hashlib.sha256(data or b'').hexdigest()

    index = 1
    while True:
        suffix = '' if index == 1 else f' {index}'
        candidate = archive_dir / f'{stem} {date_str}{suffix}{ext}'
        if not candidate.exists():
            return candidate
        if wanted_digest and _archive_file_digest(candidate) == wanted_digest:
            return candidate
        index += 1


def _record_sales_order_archive_path(message: dict, attachment: dict, archive_result: dict) -> None:
    """Best-effort ING trace update for the physical Sales Orders archive path."""

    if not archive_result.get('ok') or not archive_result.get('path'):
        return

    graph_message_id = str(message.get('id') or message.get('message_id') or '')[:200]
    if not graph_message_id:
        return

    payload = attachment.get('bytes') or b''
    sha256 = hashlib.sha256(payload).hexdigest() if payload else ''
    graph_attachment_id = str(
        attachment.get('attachment_id') or sha256 or attachment.get('filename') or ''
    )[:200]
    original_name = str(attachment.get('filename') or '')[:260]
    client_code = (os.environ.get('CLIENT_CODE') or os.environ.get('TENANT_CODE') or 'BKD').strip().upper()
    archived_path = str(archive_result.get('path') or '')
    archived_name = os.path.basename(archived_path)

    try:
        conn = get_standalone_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE a
                   SET DownloadedName = ?,
                       DownloadedPath = ?,
                       Status = 'Saved',
                       ErrorText = NULL
                FROM ING.BKD_EmailAttachment a
                JOIN ING.BKD_EmailMessage m
                  ON m.EmailMessageId = a.EmailMessageId
                WHERE m.GraphMessageId = ?
                  AND a.ClientCode = ?
                  AND (
                        (? <> '' AND a.GraphAttachmentId = ?)
                     OR (? <> '' AND a.Sha256 = ?)
                     OR (? <> '' AND a.OriginalName = ?)
                  )
                """,
                [
                    archived_name[:260],
                    archived_path[:500],
                    graph_message_id,
                    client_code,
                    graph_attachment_id,
                    graph_attachment_id,
                    sha256,
                    sha256,
                    original_name,
                    original_name,
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
    except Exception as exc:
        print(f"WARN could not update ING archive path for {original_name or graph_message_id}: {exc}")


def _archive_sales_order_attachment(message: dict, attachment: dict) -> dict:
    """Archive a sales-order attachment for source-data traceability.

    Archive failures never block ingestion.
    """

    filename = str(attachment.get('filename') or 'attachment.xlsx')
    data = attachment.get('bytes') or b''
    received_dt = _parse_message_datetime(message.get('received') or message.get('date'))
    archive_dir = _attachment_archive_dir()
    dest = _sales_order_archive_path(archive_dir, filename, received_dt, data)

    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            return {'ok': True, 'path': str(dest), 'created': False}
        dest.write_bytes(data)
        return {'ok': True, 'path': str(dest), 'created': True}
    except Exception as exc:
        return {'ok': False, 'path': str(dest), 'error': str(exc)}


def _post_batch(
    message_meta: dict,
    attachments: list[dict],
    source: str,
    tenant_code: str | None = None,
    source_tenant_code: str | None = None,
) -> tuple[bool, dict]:
    url = f"{_portal_url()}/ingest/receive-batch"
    files = []
    for item in attachments:
        files.append(
            (
                'files',
                (item['filename'], item['bytes'], item.get('content_type') or 'application/pdf'),
            )
        )
    data = {
        'source': source,
        'subject': message_meta.get('subject', ''),
        'from': message_meta.get('from', ''),
        'message_id': message_meta.get('message_id', ''),
        'date': message_meta.get('date', ''),
        'email_body': message_meta.get('body', ''),
        'tenant_code': (tenant_code or os.environ.get('TENANT_CODE', '')).strip().upper(),
        'source_tenant_code': (source_tenant_code or '').strip().upper(),
    }
    return _post_portal_form(url, files=files, data=data, tenant_code=tenant_code)


def _mark_processed(conn: imaplib.IMAP4_SSL, msg_id: bytes, processed_folder: str):
    if processed_folder:
        conn.copy(msg_id, processed_folder)
        conn.store(msg_id, '+FLAGS', r'(\Deleted \Seen)')
    else:
        conn.store(msg_id, '+FLAGS', r'(\Seen)')


def _post_graph_batch(
    message: dict,
    tenant_code: str | None = None,
    source_tenant_code: str | None = None,
) -> tuple[bool, dict]:
    attachments = message.get('attachments') or []
    return _post_batch(
        {
            'subject': message.get('subject', ''),
            'from': message.get('from', ''),
            'message_id': message.get('message_id', ''),
            'date': message.get('received', ''),
            'body': message.get('body', ''),
        },
        attachments,
        source='graph_email',
        tenant_code=tenant_code,
        source_tenant_code=source_tenant_code,
    )


def _post_graph_sales_orders(
    message: dict,
    attachment: dict,
    *,
    source: str = 'graph_email',
    tenant_code: str | None = None,
    source_tenant_code: str | None = None,
) -> tuple[bool, dict]:
    url = f"{_portal_url()}/ingest/receive-sales-orders"

    archive_result = _archive_sales_order_attachment(message, attachment)
    if archive_result.get('ok'):
        _record_sales_order_archive_path(message, attachment, archive_result)
        action = 'Saved' if archive_result.get('created') else 'Already archived'
        print(f"[files] {action}: {archive_result.get('path')}")
    else:
        print(
            f"[files] WARNING: could not archive {attachment.get('filename')!r}: "
            f"{archive_result.get('error') or 'unknown error'}"
        )

    files = {
        'file': (
            attachment['filename'],
            attachment['bytes'],
            attachment.get('content_type') or 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
    }
    data = {
        'source': source,
        'subject': message.get('subject', ''),
        'from': message.get('from', ''),
        'message_id': message.get('message_id', ''),
        'graph_message_id': message.get('id', '') or message.get('message_id', ''),
        'received_at': message.get('received', ''),
        'email_body': _sales_order_details_body(message),
        'tenant_code': (tenant_code or os.environ.get('TENANT_CODE', '')).strip().upper(),
        'source_tenant_code': (source_tenant_code or '').strip().upper(),
        'mailbox': message.get('mailbox') or os.environ.get('GRAPH_MAILBOX', ''),
    }
    return _post_portal_form(url, files=files, data=data, tenant_code=tenant_code)


def _post_graph_sales_orders_details(
    message: dict,
    *,
    source: str = 'graph_email_details',
    tenant_code: str | None = None,
    source_tenant_code: str | None = None,
) -> tuple[bool, dict]:
    url = f"{_portal_url()}/ingest/receive-sales-orders-details"
    data = {
        'source': source,
        'subject': message.get('subject', ''),
        'from': message.get('from', ''),
        'message_id': message.get('message_id', ''),
        'graph_message_id': message.get('id', '') or message.get('message_id', ''),
        'received_at': message.get('received', '') or message.get('date', ''),
        'email_body': _sales_order_details_body(message),
        'tenant_code': (tenant_code or os.environ.get('TENANT_CODE', '')).strip().upper(),
        'source_tenant_code': (source_tenant_code or '').strip().upper(),
        'mailbox': message.get('mailbox') or os.environ.get('GRAPH_MAILBOX', ''),
    }
    return _post_portal_data(url, data=data, tenant_code=tenant_code)


def _notify_graph_portal_failure(
    error_type: str,
    detail: str,
    *,
    tenant_code: str | None,
    message: dict | None = None,
) -> None:
    """Best-effort alert when the mailbox worker cannot hand a message to Flask."""
    try:
        from app.ingestion.automation_notify import notify_pipeline_error
        notify_pipeline_error(
            error_type,
            detail,
            tenant_code=(tenant_code or os.environ.get('TENANT_CODE') or 'BKD').strip().upper(),
            filename=(message or {}).get('subject') or (message or {}).get('id') or 'Graph mailbox worker',
        )
    except Exception as exc:
        print(f'WARN Graph failure notification skipped: {exc}')


def _is_sales_order_workbook(attachment: dict) -> bool:
    return is_sales_order_workbook_attachment(attachment)


def _graph_ing_env_code() -> str:
    """Environment code used for ING email trace rows."""
    return 'PRD'


def _allowed_sender_domains(settings) -> tuple[str, ...]:
    domains = getattr(settings, 'allowed_sender_domains', ()) or ()
    cleaned = []
    for item in domains:
        domain = str(item or '').strip().lower()
        if domain.startswith('@'):
            domain = domain[1:]
        if domain:
            cleaned.append(domain)
    return tuple(dict.fromkeys(cleaned))


def _sender_allowed_by_domain(message: dict, settings) -> bool:
    domains = _allowed_sender_domains(settings)
    if not domains:
        return True

    sender_raw = str(message.get('from') or '')
    _name, sender_email = parseaddr(sender_raw)
    sender_email = (sender_email or sender_raw).strip().lower()
    if '@' not in sender_email:
        return False
    sender_domain = sender_email.rsplit('@', 1)[-1].strip()
    return any(
        sender_domain == allowed
        or sender_domain.endswith(f'.{allowed}')
        for allowed in domains
    )


def _allowed_sender_domains_label(settings) -> str:
    return ', '.join(_allowed_sender_domains(settings)) or '(none)'


def _log_graph_message_to_ing(settings, message: dict, attachments: list[dict]) -> None:
    """Best-effort ING mailbox trace. Never blocks message processing."""
    try:
        env_code = _graph_ing_env_code()
        client_code = (os.environ.get('CLIENT_CODE') or os.environ.get('TENANT_CODE') or 'BKD').strip().upper()
        graph_message_id = str(message.get('id') or message.get('message_id') or '')[:200]
        if not graph_message_id:
            return

        conn = get_standalone_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT OBJECT_ID('ING.BKD_EmailMessage', 'U')")
            if not cursor.fetchone()[0]:
                return

            cursor.execute(
                """
                SELECT TOP 1 EmailMessageId, EnvCode
                FROM ING.BKD_EmailMessage
                WHERE GraphMessageId = ? AND ClientCode = ?
                ORDER BY CASE WHEN EnvCode = ? THEN 0 ELSE 1 END, EmailMessageId DESC
                """,
                [graph_message_id, client_code, env_code],
            )
            row = cursor.fetchone()
            if row:
                email_message_id = int(row[0])
                cursor.execute(
                    """
                    UPDATE ING.BKD_EmailMessage
                    SET EnvCode = ?, ClientCode = ?, Mailbox = ?, SenderEmail = ?, Subject = ?, ReceivedAt = ?,
                        HasAttachments = ?, AttachmentCount = ?
                    WHERE EmailMessageId = ?
                    """,
                    [
                        env_code,
                        client_code,
                        settings.mailbox,
                        message.get('from') or None,
                        (message.get('subject') or '')[:500],
                        message.get('received') or None,
                        1 if attachments else 0,
                        len(attachments),
                        email_message_id,
                    ],
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO ING.BKD_EmailMessage (
                        EnvCode, ClientCode, Mailbox, GraphMessageId,
                        SenderEmail, Subject, ReceivedAt,
                        HasAttachments, AttachmentCount, AllowedSender, Skipped
                    )
                    OUTPUT INSERTED.EmailMessageId
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
                    """,
                    [
                        env_code,
                        client_code,
                        settings.mailbox,
                        graph_message_id,
                        message.get('from') or None,
                        (message.get('subject') or '')[:500],
                        message.get('received') or None,
                        1 if attachments else 0,
                        len(attachments),
                    ],
                )
                email_message_id = int(cursor.fetchone()[0])

            cursor.execute("SELECT OBJECT_ID('ING.BKD_EmailAttachment', 'U')")
            if not cursor.fetchone()[0]:
                conn.commit()
                return

            for attachment in attachments:
                payload = attachment.get('bytes') or b''
                sha256 = hashlib.sha256(payload).hexdigest() if payload else None
                graph_attachment_id = str(
                    attachment.get('attachment_id') or sha256 or attachment.get('filename') or ''
                )[:200]
                if not graph_attachment_id:
                    continue
                cursor.execute(
                    """
                    SELECT EmailAttachmentId
                    FROM ING.BKD_EmailAttachment
                    WHERE EmailMessageId = ? AND GraphAttachmentId = ?
                    """,
                    [email_message_id, graph_attachment_id],
                )
                if cursor.fetchone():
                    continue
                cursor.execute(
                    """
                    INSERT INTO ING.BKD_EmailAttachment (
                        EmailMessageId, GraphAttachmentId, EnvCode, ClientCode,
                        OriginalName, ContentType, SizeBytes, Sha256, Status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Saved')
                    """,
                    [
                        email_message_id,
                        graph_attachment_id,
                        env_code,
                        client_code,
                        (attachment.get('filename') or 'attachment.bin')[:260],
                        (attachment.get('content_type') or '')[:100],
                        len(payload) if payload else None,
                        sha256,
                    ],
                )
            cursor.execute(
                """
                UPDATE ING.BKD_EmailAttachment
                SET EnvCode = ?, ClientCode = ?
                WHERE EmailMessageId = ? AND (EnvCode <> ? OR ClientCode <> ?)
                """,
                [env_code, client_code, email_message_id, env_code, client_code],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
    except Exception as exc:
        print(f'WARN Graph ING trace failed: {exc}')


def _process_imap_messages(
    limit: int | None = None,
    tenant_code: str | None = None,
    *,
    dry_run: bool = False,
) -> int:
    settings = resolve_imap_settings(tenant_code=tenant_code)
    if not settings.enabled:
        print('IMAP is disabled (IMAP.ENABLED=false).')
        return 1
    if not settings.host or not settings.username or not settings.password:
        print('IMAP settings incomplete: need host, username, password.')
        return 1
    source_tenant_code = _source_tenant_from_mailbox(settings, tenant_code)

    processed = 0
    with imaplib.IMAP4_SSL(settings.host, settings.port) as conn:
        conn.login(settings.username, settings.password)
        conn.select(settings.folder)
        status, data = conn.search(None, *_search_terms(settings.search))
        if status != 'OK':
            print('IMAP search failed.')
            return 1
        msg_ids = [msg_id for msg_id in (data[0] or b'').split() if msg_id]
        if limit:
            msg_ids = msg_ids[:limit]

        had_failures = False
        if not msg_ids:
            print('NO_MESSAGES IMAP scan returned 0 matching messages.')

        for msg_id in msg_ids:
            status, payload = conn.fetch(msg_id, '(RFC822)')
            if status != 'OK' or not payload:
                continue
            raw_bytes = b''.join(
                part[1]
                for part in payload
                if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], (bytes, bytearray))
            )
            if not raw_bytes:
                continue

            message = parse_email_bytes(raw_bytes)
            attachments = extract_supported_attachments(message)
            email_meta = build_email_metadata(message)
            email_meta['body'] = extract_email_body_text(message)
            classification = _classify_inbound_message(
                {
                    'subject': email_meta.get('subject', ''),
                    'from': email_meta.get('from', ''),
                    'message_id': email_meta.get('message_id', ''),
                    'date': email_meta.get('date', ''),
                    'body': email_meta.get('body', ''),
                },
                attachments,
            )
            if dry_run:
                _print_dry_run_message(
                    msg_id.decode(errors='ignore'),
                    classification,
                )
                continue

            if not attachments:
                details_message = {
                    'subject': email_meta.get('subject', ''),
                    'from': email_meta.get('from', ''),
                    'message_id': email_meta.get('message_id', ''),
                    'received': email_meta.get('date', ''),
                    'body': email_meta.get('body', ''),
                }
                if classification['action'] == 'sales_orders_details':
                    ok, result = _post_graph_sales_orders_details(
                        details_message,
                        source='imap_email_details',
                        tenant_code=tenant_code,
                        source_tenant_code=source_tenant_code,
                    )
                    if not ok:
                        had_failures = True
                        print(f"FAILED {msg_id.decode(errors='ignore')}: {result.get('error', 'unknown error')}")
                        continue
                    print(
                        f"OK {msg_id.decode(errors='ignore')}: "
                        f"mode={result.get('mode')} ENS={result.get('ens_staging_id')}"
                    )
                else:
                    print(f"SKIPPED {msg_id.decode(errors='ignore')}: no supported PDF/CSV/ZIP/XLSX attachments or DETAILS body")
                _mark_processed(conn, msg_id, settings.processed_folder)
                processed += 1
                continue

            sales_order_attachments = classification['sales_order_attachments']
            batch_attachments = classification['batch_attachments']
            failures = []
            staged_ens = None
            staged_consignment_count = 0
            staged_goods_count = 0

            for attachment in sales_order_attachments:
                ok, result = _post_graph_sales_orders(
                    {
                        'subject': email_meta.get('subject', ''),
                        'from': email_meta.get('from', ''),
                        'message_id': email_meta.get('message_id', ''),
                        'received': email_meta.get('date', ''),
                        'body': email_meta.get('body', ''),
                    },
                    attachment,
                    source='imap_email',
                    tenant_code=tenant_code,
                    source_tenant_code=source_tenant_code,
                )
                if not ok:
                    failures.append(result.get('error', 'unknown error'))
                    continue
                staged_ens = result.get('ens_staging_id') or staged_ens
                staged_consignment_count += len(result.get('consignments') or [])
                staged_goods_count += sum(len(c.get('goods') or []) for c in (result.get('consignments') or []))

            if batch_attachments:
                ok, result = _post_batch(
                    email_meta,
                    batch_attachments,
                    source='imap_email',
                    tenant_code=tenant_code,
                    source_tenant_code=source_tenant_code,
                )
                if not ok:
                    failures.append(result.get('error', 'unknown error'))
                else:
                    staged_ens = result.get('staging_ens_id') or staged_ens
                    staged_consignment_count += len(result.get('consignment_ids') or [])
                    staged_goods_count += len(result.get('goods_ids') or [])

            if failures:
                had_failures = True
                print(f"FAILED {msg_id.decode(errors='ignore')}: {' | '.join(failures)}")
                continue

            _mark_processed(conn, msg_id, settings.processed_folder)
            processed += 1
            print(
                f"OK {msg_id.decode(errors='ignore')}: "
                f"ENS={staged_ens} "
                f"consignments={staged_consignment_count} "
                f"goods={staged_goods_count}"
            )

        conn.expunge()

    return 1 if had_failures else 0


def _process_graph_messages(
    limit: int | None = None,
    tenant_code: str | None = None,
    *,
    dry_run: bool = False,
) -> int:
    settings = resolve_graph_mail_settings(tenant_code=tenant_code)
    if not settings.enabled:
        print('Microsoft Graph mailbox polling is disabled (GRAPH.ENABLED=false).')
        return 1

    client = GraphMailClient(settings)
    if not client.is_configured():
        print('Graph settings incomplete: need tenant_id, client_id, client_secret, mailbox.')
        return 1

    print(
        f"CONFIG Graph tenant={tenant_code or '(env/default)'} "
        f"mailbox={getattr(settings, 'mailbox', '') or '(missing)'} "
        f"folder={getattr(settings, 'folder', '') or 'INBOX'} "
        f"unread_only={getattr(settings, 'unread_only', True)} "
        f"limit={limit or getattr(settings, 'max_messages', 50)}"
    )

    processed = 0
    had_failures = False
    source_tenant_code = _source_tenant_from_mailbox(settings, tenant_code)

    try:
        messages = client.scan_messages(limit=limit, include_body_only=True)
    except Exception as exc:
        print(f'Graph scan failed: {exc}')
        return 1
    if not messages:
        print(f'NO_MESSAGES Graph scan returned 0 unread messages from folder {settings.folder or "INBOX"}.')
    for message in messages:
        message.setdefault('mailbox', getattr(settings, 'mailbox', '') or '')
        if not _sender_allowed_by_domain(message, settings):
            sender = message.get('from') or '(missing sender)'
            message_id = str(message.get('id') or '')
            print(
                f"IGNORED {message_id}: sender {sender} outside allowed Graph domains "
                f"{_allowed_sender_domains_label(settings)}; no ING received trace created"
            )
            if dry_run:
                continue
            if message_id:
                try:
                    client.mark_processed(message_id)
                except Exception as exc:
                    had_failures = True
                    print(f"WARN {message_id}: failed to mark ignored message processed: {exc}")
            continue

        attachments = message.get('attachments') or []
        classification = _classify_inbound_message(message, attachments)
        if dry_run:
            _print_dry_run_message(str(message.get('id') or ''), classification)
            continue

        _log_graph_message_to_ing(settings, message, attachments)
        if not attachments:
            if classification['action'] == 'sales_orders_details':
                ok, result = _post_graph_sales_orders_details(
                    message,
                    tenant_code=tenant_code,
                    source_tenant_code=source_tenant_code,
                )
                if not ok:
                    had_failures = True
                    error = result.get('error', 'unknown error')
                    _notify_graph_portal_failure(
                        'graph_details_post_failed',
                        error,
                        tenant_code=tenant_code,
                        message=message,
                    )
                    print(f"FAILED {message.get('id')}: {error}")
                    continue
                print(
                    f"OK {message.get('id')}: "
                    f"mode={result.get('mode')} ENS={result.get('ens_staging_id')}"
                )
            else:
                print(f"SKIPPED {message.get('id')}: no supported PDF/CSV/ZIP/XLSX attachments or DETAILS body")
            client.mark_processed(str(message.get('id') or ''))
            processed += 1
            continue

        sales_order_attachments = classification['sales_order_attachments']
        batch_attachments = classification['batch_attachments']
        failures = []

        for attachment in sales_order_attachments:
            ok, result = _post_graph_sales_orders(
                message,
                attachment,
                tenant_code=tenant_code,
                source_tenant_code=source_tenant_code,
            )
            if not ok:
                failures.append(result.get('error', 'unknown error'))
                continue
            print(
                f"OK {message.get('id')} {attachment.get('filename')}: "
                f"mode={result.get('mode')} "
                f"ENS={result.get('ens_staging_id')} "
                f"consignments={len(result.get('consignments') or [])} "
                f"needs_review={result.get('needs_review')} "
                f"blockers={len(result.get('blockers') or [])}"
            )

        if batch_attachments:
            ok, result = _post_graph_batch(
                {**message, 'attachments': batch_attachments},
                tenant_code=tenant_code,
                source_tenant_code=source_tenant_code,
            )
            if not ok:
                failures.append(result.get('error', 'unknown error'))
            else:
                print(
                    f"OK {message.get('id')}: "
                    f"ENS={result.get('staging_ens_id')} "
                    f"consignments={len(result.get('consignment_ids') or [])} "
                    f"goods={len(result.get('goods_ids') or [])}"
                )

        if failures:
            had_failures = True
            detail = ' | '.join(failures)
            _notify_graph_portal_failure(
                'graph_attachment_post_failed',
                detail,
                tenant_code=tenant_code,
                message=message,
            )
            print(f"FAILED {message.get('id')}: {detail}")
            continue

        client.mark_processed(str(message.get('id') or ''))
        processed += 1

    return 1 if had_failures else 0


def process_messages(
    limit: int | None = None,
    provider: str = 'auto',
    tenant_code: str | None = None,
    *,
    dry_run: bool = False,
) -> int:
    tenant_code = (
        tenant_code
        or os.environ.get('TENANT_CODE')
        or os.environ.get('CLIENT_CODE')
        or 'BKD'
    ).strip().upper()
    provider = (provider or 'auto').strip().lower()

    if provider == 'graph':
        return _process_graph_messages(limit=limit, tenant_code=tenant_code, dry_run=dry_run)
    if provider == 'imap':
        return _process_imap_messages(limit=limit, tenant_code=tenant_code, dry_run=dry_run)

    graph_settings = resolve_graph_mail_settings(tenant_code=tenant_code)
    if graph_settings.enabled:
        return _process_graph_messages(limit=limit, tenant_code=tenant_code, dry_run=dry_run)
    return _process_imap_messages(limit=limit, tenant_code=tenant_code, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(description='Pull invoice emails and auto-stage them into Fusion Flow.')
    parser.add_argument('--limit', type=int, default=0, help='Process only the first N matched emails.')
    parser.add_argument(
        '--provider',
        choices=['auto', 'imap', 'graph'],
        default='auto',
        help='Mailbox provider to use. auto prefers Microsoft Graph when enabled, otherwise IMAP.',
    )
    parser.add_argument(
        '--tenant-code',
        default='',
        help='Tenant code whose AppConfiguration should be used for mailbox polling.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Scan and classify emails without posting to the portal or marking messages processed.',
    )
    args = parser.parse_args()
    sys.exit(
        process_messages(
            limit=args.limit or None,
            provider=args.provider,
            tenant_code=args.tenant_code or None,
            dry_run=args.dry_run,
        )
    )


if __name__ == '__main__':
    main()
