"""
NOT FOR PRD: writes BKD.StagingDeclarations removed by migration 078.
             Use STG.BKD_* or ING.BKD_* for new pipeline work.

Poll Microsoft Graph for Primeline ENS notification emails and auto-create
ENS Headers in BKD.StagingDeclarations.

Emails are plain-text / HTML structured key-value pairs sent to @birkdalesales.com
with subjects like "DETAILS FOR 30.04.26". No attachments.

Run:
    python scripts/ingest_primeline_ens.py
    python scripts/ingest_primeline_ens.py --dry-run
    python scripts/ingest_primeline_ens.py --limit 5

Requires GRAPH.* in BKD.AppConfiguration or env vars:
    GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_MAILBOX
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pyodbc
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion.defaults import resolve_graph_mail_settings
from config.db_connection import build_connection_string

# ── CV / field mappings ──────────────────────────────────────────────────────
# Map plain-English email text → TSS CV codes used in StagingDeclarations.
# Extend these as new carriers / port names appear.

NATIONALITY_MAP = {
    'united kingdom': 'GB',
    'great britain': 'GB',
    'ireland': 'IE',
    'france': 'FR',
    'germany': 'DE',
    'netherlands': 'NL',
    'belgium': 'BE',
    'spain': 'ES',
    'italy': 'IT',
    'poland': 'PL',
    'denmark': 'DK',
}

ARRIVAL_PORT_MAP = {
    'belfast': 'GBBEL',
    'larne': 'GBLAR',
    'warrenpoint': 'GBWPT',
    'dublin': 'IEDUB',
    'rosslare': 'IERSL',
}

MOVEMENT_TYPE_MAP = {
    'roro accompanied': '1',
    'roro unaccompanied': '2',
    'ro-ro accompanied': '1',
    'ro-ro unaccompanied': '2',
}

TRANSPORT_TYPE_MAP = {
    'truck': '30',
    'lorry': '30',
    'tautliner': '30',
    'trailer': '20',
    'van': '31',
}

TRANSPORT_CHARGES_MAP = {
    'account holder with carrier': 'A',
    'account': 'A',
    'cash': 'C',
    'prepaid': 'P',
    'freight prepaid': 'P',
}

# ── label patterns ───────────────────────────────────────────────────────────
# (regex for label line, extracted dict key)
LABEL_PATTERNS: list[tuple[str, str]] = [
    (r'type\s+of\s+movement', '_raw_movement_type'),
    (r'type\s+of\s+passive\s+transport', '_raw_type_of_passive_transport'),
    (r'identity\s+(?:no\.?\s+)?(?:number\s+)?of\s+(?:means\s+of\s+)?transport', 'identity_no_of_transport'),
    (r'nationality\s+of\s+(?:means\s+of\s+)?transport', '_raw_nationality'),
    (r'carrier\s+eori', 'carrier_eori'),
    (r'transport\s+document\s+number', 'conveyance_ref'),
    (r'arrival\s+date(?:[/\s]*time)?', '_raw_arrival'),
    (r'port\s+of\s+arrival', '_raw_arrival_port'),
    (r'is[/\s]*are\s+the\s+place(?:s|\(s\))?\s+of\s+acceptance\s+same\s+as\s+place(?:s|\(s\))?\s+of\s+loading', '_raw_acceptance_same'),
    (r'place(?:s|\(s\))?\s+of\s+loading', 'place_of_loading'),
    (r'is[/\s]*are\s+the\s+place(?:s|\(s\))?\s+of\s+delivery\s+same\s+as\s+place(?:s|\(s\))?\s+of\s+unloading', '_raw_delivery_same'),
    (r'place(?:s|\(s\))?\s+of\s+unloading', 'place_of_unloading'),
    (r'transport\s+charges', '_raw_transport_charges'),
]

# Pre-compile label regexes
_COMPILED_LABELS = [(re.compile(pat, re.IGNORECASE), key) for pat, key in LABEL_PATTERNS]


# ── HTML → plain text ────────────────────────────────────────────────────────

def _strip_html(body: str) -> str:
    text = re.sub(r'<br\s*/?>', '\n', body, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|tr|td|li|h[1-6])[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── email body parser ────────────────────────────────────────────────────────

def _is_label_line(line: str) -> bool:
    norm = line.strip().lower()
    return any(pat.search(norm) for pat, _ in _COMPILED_LABELS)


def parse_primeline_email(body_html: str) -> dict[str, str]:
    """
    Extract ENS header fields from a Primeline notification email body.
    Returns a dict keyed by extracted field name. Raw fields (_raw_*) are
    converted to TSS values by build_ens_payload().
    """
    plain = _strip_html(body_html)
    lines = [ln.strip() for ln in plain.splitlines()]

    extracted: dict[str, str] = {}

    for i, line in enumerate(lines):
        if not line:
            continue
        norm = line.lower()
        for pattern, key in _COMPILED_LABELS:
            if pattern.search(norm):
                # Value = next non-empty, non-label line within 4 lines
                for j in range(i + 1, min(i + 5, len(lines))):
                    candidate = lines[j].strip()
                    if candidate and not _is_label_line(candidate):
                        extracted[key] = candidate
                        break
                break

    return extracted


# ── field mappers ────────────────────────────────────────────────────────────

def _map(raw: str, table: dict[str, str], fallback: str = '') -> str:
    norm = (raw or '').lower()
    for key, val in table.items():
        if key in norm:
            return val
    return fallback if fallback else raw


def _yes_no(raw: str) -> str:
    return 'yes' if 'yes' in (raw or '').lower() else 'no'


def _resolve_arrival(raw: str, received_dt: datetime) -> str:
    """
    Resolve arrival date/time.
    'Tomorrow's Date / 06:30' → received_dt + 1 day at parsed time.
    Explicit dates like '01/05/2026 06:30' → parsed directly.
    """
    raw = (raw or '').strip()
    time_match = re.search(r'(\d{1,2})[:\.](\d{2})', raw)
    hour = int(time_match.group(1)) if time_match else 6
    minute = int(time_match.group(2)) if time_match else 30

    if 'tomorrow' in raw.lower() or not re.search(r'\d{1,2}[/\-.]\d{1,2}', raw.lower()):
        dt = (received_dt + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0,
        )
        return dt.strftime('%d/%m/%Y %H:%M:%S')

    for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%Y-%m-%d %H:%M', '%d-%m-%Y %H:%M'):
        try:
            return datetime.strptime(raw, fmt).strftime('%d/%m/%Y %H:%M:%S')
        except ValueError:
            pass

    dt = (received_dt + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    return dt.strftime('%d/%m/%Y %H:%M:%S')


def build_ens_payload(extracted: dict[str, str], received_dt: datetime) -> dict[str, str]:
    """Map raw extracted fields → BKD.StagingDeclarations column values."""
    return {
        'movement_type': _map(extracted.get('_raw_movement_type', ''), MOVEMENT_TYPE_MAP, '1'),
        'type_of_passive_transport': _map(extracted.get('_raw_type_of_passive_transport', ''), TRANSPORT_TYPE_MAP, extracted.get('_raw_type_of_passive_transport', '')),
        'identity_no_of_transport': extracted.get('identity_no_of_transport', ''),
        'nationality_of_transport': _map(extracted.get('_raw_nationality', ''), NATIONALITY_MAP, 'GB'),
        'carrier_eori': extracted.get('carrier_eori', ''),
        'carrier_name': '',
        'carrier_street_number': '',
        'carrier_city': '',
        'carrier_postcode': '',
        'carrier_country': 'GB',
        'haulier_eori': '',
        'conveyance_ref': extracted.get('conveyance_ref', ''),
        'arrival_date_time': _resolve_arrival(extracted.get('_raw_arrival', ''), received_dt),
        'arrival_port': _map(extracted.get('_raw_arrival_port', ''), ARRIVAL_PORT_MAP, extracted.get('_raw_arrival_port', '')),
        'place_of_loading': extracted.get('place_of_loading', ''),
        'place_of_acceptance_same_as_loading': _yes_no(extracted.get('_raw_acceptance_same', '')),
        'place_of_acceptance': '',
        'place_of_unloading': extracted.get('place_of_unloading', ''),
        'place_of_delivery_same_as_unloading': _yes_no(extracted.get('_raw_delivery_same', '')),
        'place_of_delivery': '',
        'seal_number': '',
        'transport_charges': _map(extracted.get('_raw_transport_charges', ''), TRANSPORT_CHARGES_MAP, 'A'),
    }


# ── DB helpers ───────────────────────────────────────────────────────────────

def _icr_exists(cursor, icr: str) -> bool:
    cursor.execute(
        "SELECT TOP 1 1 FROM BKD.StagingDeclarations"
        " WHERE conveyance_ref = ? AND declaration_type = 'ENS_HEADER'",
        [icr],
    )
    return cursor.fetchone() is not None


def insert_ens_header(conn, payload: dict, dry_run: bool = False) -> int | None:
    sql = """
        INSERT INTO BKD.StagingDeclarations
            (declaration_type, status, source,
             movement_type, arrival_port, arrival_date_time,
             carrier_name, carrier_eori, identity_no_of_transport,
             nationality_of_transport, seal_number, transport_charges,
             type_of_passive_transport, conveyance_ref,
             place_of_loading, place_of_unloading,
             place_of_acceptance_same, place_of_acceptance,
             place_of_delivery_same, place_of_delivery,
             carrier_street_number, carrier_city, carrier_postcode,
             carrier_country, haulier_eori,
             payload_json, created_by)
        VALUES ('ENS_HEADER', 'Inserted', 'graph_email',
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?)
    """
    params = [
        payload['movement_type'],
        payload['arrival_port'],
        payload['arrival_date_time'],
        payload['carrier_name'],
        payload['carrier_eori'],
        payload['identity_no_of_transport'],
        payload['nationality_of_transport'],
        payload['seal_number'],
        payload['transport_charges'],
        payload['type_of_passive_transport'],
        payload['conveyance_ref'],
        payload['place_of_loading'],
        payload['place_of_unloading'],
        payload['place_of_acceptance_same_as_loading'],
        payload['place_of_acceptance'],
        payload['place_of_delivery_same_as_unloading'],
        payload['place_of_delivery'],
        payload['carrier_street_number'],
        payload['carrier_city'],
        payload['carrier_postcode'],
        payload['carrier_country'],
        payload['haulier_eori'],
        json.dumps(payload),
        'graph_ingest',
    ]

    if dry_run:
        icr = payload.get('conveyance_ref') or '(no ICR)'
        print(f'  [DRY RUN] Would INSERT ENS header — ICR: {icr}')
        return None

    cursor = conn.cursor()
    cursor.execute(sql, params)
    conn.commit()
    cursor.execute('SELECT @@IDENTITY AS id')
    row = cursor.fetchone()
    return int(row[0]) if row else None


# ── Graph mail helpers ───────────────────────────────────────────────────────

def _graph_token(settings) -> str:
    resp = requests.post(
        f'https://login.microsoftonline.com/{quote(settings.tenant_id, safe="")}/oauth2/v2.0/token',
        data={
            'grant_type': 'client_credentials',
            'client_id': settings.client_id,
            'client_secret': settings.client_secret,
            'scope': 'https://graph.microsoft.com/.default',
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()['access_token']


def scan_primeline_messages(settings, limit: int = 50) -> list[dict]:
    """
    Return all messages in the dedicated Primeline mailbox.
    The mailbox itself is the filter — everything arriving here is a Primeline ENS email.
    Filters to unread only when GRAPH.UNREAD_ONLY is true (default).
    """
    token = _graph_token(settings)
    mailbox = quote(settings.mailbox, safe='@._-')
    hdrs = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}

    filter_parts = []
    if settings.unread_only:
        filter_parts.append('isRead eq false')

    params: dict = {
        '$top': str(limit),
        '$select': 'id,subject,receivedDateTime,from,body',
    }
    if filter_parts:
        params['$filter'] = ' and '.join(filter_parts)

    resp = requests.get(
        f'https://graph.microsoft.com/v1.0/users/{mailbox}/messages',
        headers=hdrs,
        params=params,
        timeout=30,
    )
    resp.raise_for_status()

    results = []
    for msg in resp.json().get('value') or []:
        body = msg.get('body') or {}
        results.append({
            'id': msg.get('id'),
            'subject': msg.get('subject') or '',
            'received': msg.get('receivedDateTime') or '',
            'from': ((msg.get('from') or {}).get('emailAddress') or {}).get('address', ''),
            'body_html': body.get('content') or '',
        })
    return results


def _mark_read(settings, message_id: str) -> None:
    try:
        token = _graph_token(settings)
        mailbox = quote(settings.mailbox, safe='@._-')
        requests.patch(
            f'https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{quote(message_id, safe="")}',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            },
            json={'isRead': True},
            timeout=30,
        )
    except Exception as exc:
        print(f'  WARN: could not mark message as read: {exc}')


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description='Ingest Primeline ENS notification emails → BKD.StagingDeclarations',
    )
    ap.add_argument('--dry-run', action='store_true', help='Parse and print without writing to DB')
    ap.add_argument('--limit', type=int, default=20, help='Max emails to process per run (default 20)')
    args = ap.parse_args()

    settings = resolve_graph_mail_settings(tenant_code='BKD')

    if not all([settings.tenant_id, settings.client_id, settings.client_secret, settings.mailbox]):
        print(
            'ERROR: Graph mail not configured.\n'
            'Set GRAPH.TENANT_ID, CLIENT_ID, CLIENT_SECRET, MAILBOX in BKD.AppConfiguration\n'
            'or as env vars GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET / GRAPH_MAILBOX.'
        )
        sys.exit(1)

    print(f'Scanning {settings.mailbox} for Primeline ENS emails '
          f'(limit={args.limit}, dry_run={args.dry_run}) ...')

    messages = scan_primeline_messages(settings, limit=args.limit)
    print(f'Found {len(messages)} matching message(s).')

    if not messages:
        return

    conn = pyodbc.connect(build_connection_string(timeout=30), autocommit=False)
    cursor = conn.cursor()

    created = skipped = errors = 0

    for msg in messages:
        subject = msg['subject']
        print(f'\n  [{msg["from"]}] {subject}')

        try:
            received_str = msg['received']  # e.g. "2026-04-30T08:00:00Z"
            received_dt = datetime.fromisoformat(received_str.replace('Z', '+00:00'))
        except Exception:
            received_dt = datetime.now(timezone.utc)

        extracted = parse_primeline_email(msg['body_html'])

        if not extracted.get('identity_no_of_transport') and not extracted.get('carrier_eori'):
            print('  SKIP: key fields not found in email body — check email format.')
            skipped += 1
            continue

        icr = extracted.get('conveyance_ref', '').strip()
        if icr and not args.dry_run and _icr_exists(cursor, icr):
            print(f'  SKIP: ICR {icr} already in StagingDeclarations.')
            skipped += 1
            continue

        payload = build_ens_payload(extracted, received_dt)

        print(f'    movement_type:            {payload["movement_type"]}')
        print(f'    type_of_passive_transport:{payload["type_of_passive_transport"]}')
        print(f'    identity_no_of_transport: {payload["identity_no_of_transport"]}')
        print(f'    carrier_eori:             {payload["carrier_eori"]}')
        print(f'    conveyance_ref (ICR):     {payload["conveyance_ref"]}')
        print(f'    arrival_date_time:        {payload["arrival_date_time"]}')
        print(f'    arrival_port:             {payload["arrival_port"]}')
        print(f'    place_of_loading:         {payload["place_of_loading"]}')
        print(f'    place_of_unloading:       {payload["place_of_unloading"]}')

        try:
            new_id = insert_ens_header(conn, payload, dry_run=args.dry_run)
            if new_id:
                print(f'  CREATED id={new_id}')
                _mark_read(settings, msg['id'])
                created += 1
            elif args.dry_run:
                created += 1
        except Exception as exc:
            print(f'  ERROR inserting: {exc}')
            errors += 1

    cursor.close()
    conn.close()
    print(f'\nDone. created={created}  skipped={skipped}  errors={errors}')


if __name__ == '__main__':
    main()
