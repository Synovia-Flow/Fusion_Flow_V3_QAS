from __future__ import annotations

import html
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any


SUPPORTED_ATTACHMENT_SUFFIXES = {'pdf', 'csv', 'zip', 'xlsx'}
SUPPORTED_ATTACHMENT_TYPES = {
    'application/pdf',
    'text/csv',
    'application/csv',
    'application/zip',
    'application/x-zip-compressed',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

SALES_ORDER_WORKBOOK_TYPES = {
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}


def parse_email_bytes(message_bytes: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(message_bytes)


def extract_pdf_attachments(message: EmailMessage) -> list[dict]:
    attachments = []
    for part in message.iter_attachments():
        filename = part.get_filename() or ''
        content_type = (part.get_content_type() or '').lower()
        if not filename.lower().endswith('.pdf') and content_type != 'application/pdf':
            continue
        payload = part.get_payload(decode=True) or b''
        attachments.append({
            'filename': filename or 'attachment.pdf',
            'content_type': content_type or 'application/pdf',
            'bytes': payload,
        })
    return attachments


def extract_supported_attachments(message: EmailMessage) -> list[dict]:
    attachments = []
    for part in message.iter_attachments():
        filename = part.get_filename() or ''
        content_type = (part.get_content_type() or '').lower()
        suffix = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
        if suffix not in SUPPORTED_ATTACHMENT_SUFFIXES and content_type not in SUPPORTED_ATTACHMENT_TYPES:
            continue
        payload = part.get_payload(decode=True) or b''
        attachments.append({
            'filename': filename or f'attachment.{suffix or "bin"}',
            'content_type': content_type or 'application/octet-stream',
            'bytes': payload,
        })
    return attachments


def is_sales_order_workbook_attachment(attachment: dict[str, Any]) -> bool:
    filename = str(attachment.get('filename') or '').strip().lower()
    content_type = str(attachment.get('content_type') or '').strip().lower()
    return filename.endswith('.xlsx') or content_type in SALES_ORDER_WORKBOOK_TYPES


def extract_email_body_text(message: EmailMessage) -> str:
    """Return readable body text from an RFC822 message, ignoring attachments."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in message.walk():
        if part.get_content_disposition() == 'attachment':
            continue
        content_type = (part.get_content_type() or '').lower()
        if content_type not in {'text/plain', 'text/html'}:
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b''
            charset = part.get_content_charset() or 'utf-8'
            content = payload.decode(charset, errors='replace')
        if content_type == 'text/plain':
            plain_parts.append(str(content))
        else:
            html_parts.append(_html_to_text(str(content)))

    if plain_parts:
        return '\n'.join(part.strip() for part in plain_parts if part and part.strip())
    return '\n'.join(part.strip() for part in html_parts if part and part.strip())


def _html_to_text(content: str) -> str:
    text = re.sub(r'(?i)<\s*br\s*/?\s*>', '\n', content or '')
    text = re.sub(r'(?i)</\s*(p|div|tr|li|h[1-6])\s*>', '\n', text)
    text = re.sub(r'(?is)<\s*(script|style)[^>]*>.*?</\s*\1\s*>', '', text)
    text = re.sub(r'(?s)<[^>]+>', ' ', text)
    text = html.unescape(text).replace('\xa0', ' ')
    return re.sub(r'[ \t]+', ' ', text)


def build_email_metadata(message: EmailMessage) -> dict:
    return {
        'subject': str(message.get('Subject') or '').strip(),
        'from': str(message.get('From') or '').strip(),
        'message_id': str(message.get('Message-ID') or '').strip(),
        'date': str(message.get('Date') or '').strip(),
    }
