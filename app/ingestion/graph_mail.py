from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from app.ingestion.defaults import GraphMailSettings


SUPPORTED_SUFFIXES = {'.pdf', '.csv', '.zip', '.xlsx'}
SUPPORTED_CONTENT_TYPES = {
    'application/pdf',
    'text/csv',
    'application/csv',
    'application/zip',
    'application/x-zip-compressed',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}


@dataclass
class GraphAttachment:
    filename: str
    content_type: str
    payload: bytes
    attachment_id: str = ''

    def as_upload(self) -> dict[str, Any]:
        return {
            'filename': self.filename,
            'content_type': self.content_type,
            'bytes': self.payload,
            'attachment_id': self.attachment_id,
        }


def _supported_attachment(filename: str, content_type: str) -> bool:
    filename = filename or ''
    content_type = (content_type or '').lower()
    suffix = ''
    if '.' in filename:
        suffix = '.' + filename.rsplit('.', 1)[-1].lower()
    return suffix in SUPPORTED_SUFFIXES or content_type in SUPPORTED_CONTENT_TYPES


def extract_supported_graph_attachments(attachments: list[dict[str, Any]]) -> list[GraphAttachment]:
    supported: list[GraphAttachment] = []
    for item in attachments or []:
        if (item.get('@odata.type') or '').lower() != '#microsoft.graph.fileattachment':
            continue
        if item.get('isInline'):
            continue
        filename = str(item.get('name') or '').strip()
        content_type = str(item.get('contentType') or 'application/octet-stream').strip()
        if not _supported_attachment(filename, content_type):
            continue
        content_b64 = item.get('contentBytes') or ''
        if not content_b64:
            continue
        try:
            payload = base64.b64decode(content_b64)
        except Exception:
            continue
        supported.append(
            GraphAttachment(
                filename=filename or 'attachment.bin',
                content_type=content_type or 'application/octet-stream',
                payload=payload,
                attachment_id=str(item.get('id') or ''),
            )
        )
    return supported


def normalise_graph_body(content: str, content_type: str = '') -> str:
    """Return readable plain text from Graph body content."""
    text = str(content or '')
    if 'html' not in str(content_type or '').lower():
        return html.unescape(text).replace('\xa0', ' ')

    text = re.sub(r'(?i)<\s*br\s*/?\s*>', '\n', text)
    text = re.sub(r'(?i)</\s*(p|div|tr|li|h[1-6])\s*>', '\n', text)
    text = re.sub(r'(?is)<\s*(script|style)[^>]*>.*?</\s*\1\s*>', '', text)
    text = re.sub(r'(?s)<[^>]+>', ' ', text)
    text = html.unescape(text).replace('\xa0', ' ')
    return re.sub(r'[ \t]+', ' ', text)


class GraphMailClient:
    def __init__(self, settings: GraphMailSettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self._token: str | None = None
        self._folder_cache: dict[str, str] = {}

    def is_configured(self) -> bool:
        return all([
            self.settings.tenant_id,
            self.settings.client_id,
            self.settings.client_secret,
            self.settings.mailbox,
        ])

    def scan_messages(self, limit: int | None = None, *, include_body_only: bool = False) -> list[dict[str, Any]]:
        folder_ref = self._resolve_folder_reference(self.settings.folder or 'INBOX')
        filter_parts = [] if include_body_only else ['hasAttachments eq true']
        if self.settings.unread_only:
            filter_parts.append('isRead eq false')
        params = {
            '$top': str(limit or self.settings.max_messages),
            '$select': 'id,subject,receivedDateTime,from,internetMessageId,isRead,hasAttachments,body',
        }
        if filter_parts:
            params['$filter'] = ' and '.join(filter_parts)
        response = self._request(
            'GET',
            f"{self._user_root()}/mailFolders/{folder_ref}/messages",
            params=params,
        )
        messages = response.json().get('value') or []
        results = []
        for message in messages:
            attachments = self._list_attachments(str(message.get('id') or '')) if message.get('hasAttachments') else []
            body = message.get('body') or {}
            body_content_type = body.get('contentType') or ''
            results.append({
                'id': message.get('id'),
                'subject': message.get('subject') or '',
                'received': message.get('receivedDateTime') or '',
                'from': ((message.get('from') or {}).get('emailAddress') or {}).get('address', ''),
                'message_id': message.get('internetMessageId') or '',
                'body': normalise_graph_body(body.get('content') or '', body_content_type),
                'body_content_type': body_content_type,
                'attachments': [item.as_upload() for item in attachments],
            })
        return results

    def get_message_by_id(self, message_id: str) -> dict[str, Any]:
        """Fetch one message, including supported attachments, by Graph message id."""
        message_id = str(message_id or '').strip()
        if not message_id:
            raise ValueError('Graph message id is required')
        response = self._request(
            'GET',
            f"{self._user_root()}/messages/{quote(message_id, safe='')}",
            params={
                '$select': 'id,subject,receivedDateTime,from,internetMessageId,isRead,hasAttachments,body',
            },
        )
        message = response.json()
        attachments = self._list_attachments(message_id) if message.get('hasAttachments') else []
        body = message.get('body') or {}
        body_content_type = body.get('contentType') or ''
        return {
            'id': message.get('id') or message_id,
            'subject': message.get('subject') or '',
            'received': message.get('receivedDateTime') or '',
            'from': ((message.get('from') or {}).get('emailAddress') or {}).get('address', ''),
            'message_id': message.get('internetMessageId') or '',
            'body': normalise_graph_body(body.get('content') or '', body_content_type),
            'body_content_type': body_content_type,
            'attachments': [item.as_upload() for item in attachments],
        }

    def mark_processed(self, message_id: str) -> None:
        processed_folder = (self.settings.processed_folder or '').strip()
        if processed_folder:
            destination = self._resolve_folder_reference(processed_folder)
            self._request(
                'POST',
                f"{self._user_root()}/messages/{quote(message_id, safe='')}/move",
                json={'destinationId': destination},
                expected_status=(201,),
            )
            return

        self._request(
            'PATCH',
            f"{self._user_root()}/messages/{quote(message_id, safe='')}",
            json={'isRead': True},
            expected_status=(200,),
        )

    def _list_attachments(self, message_id: str) -> list[GraphAttachment]:
        response = self._request(
            'GET',
            f"{self._user_root()}/messages/{quote(message_id, safe='')}/attachments",
        )
        items = response.json().get('value') or []
        for item in items:
            if (item.get('@odata.type') or '').lower() != '#microsoft.graph.fileattachment':
                continue
            if item.get('contentBytes') or not item.get('id'):
                continue
            if not _supported_attachment(str(item.get('name') or ''), str(item.get('contentType') or '')):
                continue
            detail = self._request(
                'GET',
                f"{self._user_root()}/messages/{quote(message_id, safe='')}/attachments/{quote(str(item['id']), safe='')}",
            ).json()
            item.update(detail)
        return extract_supported_graph_attachments(items)

    def _resolve_folder_reference(self, folder_ref: str) -> str:
        folder_ref = (folder_ref or '').strip()
        if not folder_ref:
            return 'inbox'
        cached = self._folder_cache.get(folder_ref.casefold())
        if cached:
            return cached

        direct_url = f"{self._user_root()}/mailFolders/{quote(folder_ref, safe='')}"
        direct_resp = self._request('GET', direct_url, expected_status=(200, 400, 404), allow_not_found=True)
        if direct_resp.status_code == 200:
            folder_id = str(direct_resp.json().get('id') or folder_ref)
            self._folder_cache[folder_ref.casefold()] = folder_id
            return folder_id

        folder_id = self._find_folder_by_display_reference(folder_ref)
        if folder_id:
            self._folder_cache[folder_ref.casefold()] = folder_id
            return folder_id
        raise ValueError(
            f"Graph mail folder '{folder_ref}' was not found. Use a well-known folder name, folder id, root display name, or path such as Inbox/Target."
        )

    def _find_folder_by_display_reference(self, folder_ref: str) -> str:
        parts = [part.strip() for part in re.split(r'[\\/]+', folder_ref) if part.strip()]
        if not parts:
            return ''
        if len(parts) > 1:
            parent_id = self._find_folder_by_display_reference(parts[0])
            if not parent_id:
                return ''
            current_id = parent_id
            for part in parts[1:]:
                current_id = self._find_child_folder_by_display_name(current_id, part)
                if not current_id:
                    return ''
            return current_id

        root_match = self._find_root_folder_by_display_name(parts[0])
        if root_match:
            return root_match
        return self._find_descendant_folder_by_display_name(parts[0])

    def _folder_pages(self, url: str, params: dict[str, Any] | None = None):
        while url:
            response = self._request('GET', url, params=params)
            body = response.json()
            for item in body.get('value') or []:
                yield item
            url = body.get('@odata.nextLink') or ''
            params = None

    def _root_folders(self):
        response = self._request(
            'GET',
            f"{self._user_root()}/mailFolders",
            params={
                '$top': '200',
                '$select': 'id,displayName',
                'includeHiddenFolders': 'true',
            },
        )
        return response.json().get('value') or []

    def _find_root_folder_by_display_name(self, display_name: str) -> str:
        for item in self._root_folders():
            if str(item.get('displayName') or '').strip().casefold() != display_name.casefold():
                continue
            return str(item.get('id') or '')
        return ''

    def _child_folders(self, parent_id: str):
        return self._folder_pages(
            f"{self._user_root()}/mailFolders/{quote(parent_id, safe='')}/childFolders",
            params={
                '$top': '200',
                '$select': 'id,displayName',
                'includeHiddenFolders': 'true',
            },
        )

    def _find_child_folder_by_display_name(self, parent_id: str, display_name: str) -> str:
        for item in self._child_folders(parent_id):
            if str(item.get('displayName') or '').strip().casefold() == display_name.casefold():
                return str(item.get('id') or '')
        return ''

    def _find_descendant_folder_by_display_name(self, display_name: str) -> str:
        queue = [str(item.get('id') or '') for item in self._root_folders()]
        seen = set()
        while queue:
            parent_id = queue.pop(0)
            if not parent_id or parent_id in seen:
                continue
            seen.add(parent_id)
            for item in self._child_folders(parent_id):
                folder_id = str(item.get('id') or '')
                if str(item.get('displayName') or '').strip().casefold() == display_name.casefold():
                    return folder_id
                if folder_id and folder_id not in seen:
                    queue.append(folder_id)
        return ''

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expected_status: tuple[int, ...] = (200,),
        allow_not_found: bool = False,
    ) -> requests.Response:
        response = self.session.request(
            method,
            url,
            headers=self._headers(),
            params=params,
            json=json,
            timeout=30,
        )
        if allow_not_found and response.status_code == 404:
            return response
        if response.status_code not in expected_status:
            response.raise_for_status()
        return response

    def _headers(self) -> dict[str, str]:
        return {
            'Authorization': f'Bearer {self._access_token()}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    def _access_token(self) -> str:
        if self._token:
            return self._token
        token_url = (
            f"https://login.microsoftonline.com/{quote(self.settings.tenant_id, safe='')}"
            "/oauth2/v2.0/token"
        )
        response = self.session.post(
            token_url,
            data={
                'grant_type': 'client_credentials',
                'client_id': self.settings.client_id,
                'client_secret': self.settings.client_secret,
                'scope': 'https://graph.microsoft.com/.default',
            },
            timeout=30,
        )
        response.raise_for_status()
        self._token = response.json()['access_token']
        return self._token

    def _user_root(self) -> str:
        mailbox = quote(self.settings.mailbox, safe='@._-')
        return f'https://graph.microsoft.com/v1.0/users/{mailbox}'
