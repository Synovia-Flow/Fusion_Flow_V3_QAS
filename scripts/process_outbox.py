#!/usr/bin/env python3
"""
NOT FOR PRD: reads/writes legacy BKD.Staging* tables removed by migration 078. Do not run against Fusion_TSS_Automation_PRD.

process_outbox.py — Unified Message Outbox Processor
Synovia Digital Ltd · Fusion Flow V2 BKD · 2026-04-09

THE DESIGN: DB as message broker (Transactional Outbox pattern).
Every outgoing TSS API call is a row in BKD.MessageOutbox.
This worker polls for PENDING rows, respects the depends_on_id chain,
dispatches to the TSS API, and writes results back to DB.

SEQUENCING: A consignment submit message DEPENDS ON the ENS create message
being SENT first. The worker query enforces this automatically.

USAGE:
  python scripts/process_outbox.py               # process one batch then exit
  python scripts/process_outbox.py --loop 30     # loop every 30 seconds
  python scripts/process_outbox.py --dry-run     # preview without calling API
  python scripts/process_outbox.py --type ENS_CREATE  # only process one type

MESSAGE TYPES HANDLED:
  PERMISSION_GRANT_CHECK  → GET permission_grant
  ENS_CREATE              → POST declaration_headers (create)
  ENS_UPDATE              → POST declaration_headers (update)
  CONSIGNMENT_CREATE      → POST consignments (create)
  CONSIGNMENT_SUBMIT      → POST consignments (submit)
  GOODS_CREATE            → POST goods (create)
  GOODS_UPDATE            → POST goods (update)
  GMR_CREATE              → POST gvms_gmr (create)
  GMR_SUBMIT              → POST gvms_gmr (submit)
  SDI_UPDATE              → POST supplementary_declarations (update)
  SDI_SUBMIT              → POST supplementary_declarations (submit)
  STATUS_POLL             → GET {resource}?reference=...
  SFD_LOOKUP              → GET consignments?ens_consignment_number=...
  SDI_LOOKUP              → GET supplementary_declarations?sfd_number=...
"""

import os
import sys
import json
import time
import uuid
import logging
import argparse
from datetime import datetime, timezone
try:
    from _console_output import configure_console_output
except ModuleNotFoundError:
    from scripts._console_output import configure_console_output

configure_console_output()

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.db import query_all, query_one, execute, db_cursor
from app.tenant import get_tenant

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('outbox_worker')

S = get_tenant()["schema"]
WORKER_ID = f'worker-{uuid.uuid4().hex[:8]}'


# ── TSS API client (minimal, self-contained) ──────────────────────────────────

def _tss_client():
    """Return a configured TssApiClient if available."""
    try:
        from app.tss_api import build_cfg_client
        return build_cfg_client()
    except Exception as exc:
        logger.error('Cannot load TssApiClient: %s', exc)
        return None


# ── Claim next ready message ──────────────────────────────────────────────────

def _claim_next(message_type_filter: str = None) -> dict | None:
    """
    Atomically claim the next ready message:
      - status IN (PENDING, FAILED)
      - attempts < max_attempts
      - schedule_after is past (or NULL)
      - dependency is SENT (or no dependency)
    Sets status=PROCESSING + claimed_at + claimed_by to prevent double-dispatch.
    """
    type_clause = f"AND o.message_type = '{message_type_filter}'" if message_type_filter else ''
    try:
        with db_cursor() as cursor:
            # Read the top candidate
            cursor.execute(f"""
                SELECT TOP 1 o.*
                FROM {S}.MessageOutbox o
                WHERE o.status IN ('PENDING', 'FAILED')
                  AND o.attempts < o.max_attempts
                  AND (o.schedule_after IS NULL OR o.schedule_after <= GETUTCDATE())
                  AND (
                      o.depends_on_id IS NULL
                      OR EXISTS (
                          SELECT 1 FROM {S}.MessageOutbox dep
                          WHERE dep.id = o.depends_on_id
                            AND dep.status = 'SENT'
                      )
                  )
                  {type_clause}
                ORDER BY o.priority DESC, o.created_at ASC
            """)
            columns = [col[0] for col in cursor.description]
            row = cursor.fetchone()
            if not row:
                return None
            msg = dict(zip(columns, row))

            # Atomically claim it — update only if status hasn't changed
            cursor.execute(f"""
                UPDATE {S}.MessageOutbox SET
                    status      = 'PROCESSING',
                    claimed_at  = GETUTCDATE(),
                    claimed_by  = ?,
                    updated_at  = GETUTCDATE()
                WHERE id = ?
                  AND status IN ('PENDING', 'FAILED')
            """, [WORKER_ID, msg['id']])
            cursor.connection.commit()
            if cursor.rowcount == 0:
                return None  # Race condition — another worker got it
            return msg
    except Exception as exc:
        logger.error('Failed to claim message: %s', exc)
        return None


# ── Mark message SENT / FAILED ────────────────────────────────────────────────

def _mark_sent(msg_id: int, tss_reference: str, http_status: int, response_json: str):
    execute(f"""
        UPDATE {S}.MessageOutbox SET
            status         = 'SENT',
            tss_reference  = ?,
            http_status    = ?,
            response_json  = ?,
            processed_at   = GETUTCDATE(),
            updated_at     = GETUTCDATE(),
            attempts       = attempts + 1,
            last_error     = NULL
        WHERE id = ?
    """, [tss_reference, http_status, (response_json or '')[:4000], msg_id])


def _mark_failed(msg_id: int, error: str, http_status: int = None, backoff_seconds: int = 60):
    msg = query_one(f"SELECT attempts, max_attempts FROM {S}.MessageOutbox WHERE id=?", [msg_id])
    if not msg:
        return
    new_attempts = (msg.get('attempts') or 0) + 1
    new_status = 'DEAD_LETTER' if new_attempts >= (msg.get('max_attempts') or 3) else 'FAILED'
    # Exponential backoff: 60s, 120s, 240s
    schedule_after_sql = f"DATEADD(second, {backoff_seconds * (2 ** (new_attempts - 1))}, GETUTCDATE())"
    execute(f"""
        UPDATE {S}.MessageOutbox SET
            status        = ?,
            attempts      = ?,
            last_error    = ?,
            http_status   = ?,
            schedule_after = {schedule_after_sql},
            updated_at    = GETUTCDATE()
        WHERE id = ?
    """, [new_status, new_attempts, str(error)[:2000], http_status, msg_id])
    if new_status == 'DEAD_LETTER':
        logger.error('Message %d moved to DEAD_LETTER after %d attempts', msg_id, new_attempts)


# ── Update staging entity from TSS response ───────────────────────────────────

def _update_entity(entity_table: str, entity_id: int, updates: dict):
    """Write TSS response data back to the originating staging record."""
    if not entity_table or not entity_id or not updates:
        return
    set_clause = ', '.join(f"{k}=?" for k in updates.keys())
    values = list(updates.values()) + [entity_id]
    try:
        execute(f"UPDATE {entity_table} SET {set_clause}, updated_at=GETUTCDATE() WHERE staging_id=?", values)
    except Exception as exc:
        # Some tables use 'id' not 'staging_id'
        try:
            execute(f"UPDATE {entity_table} SET {set_clause}, updated_at=GETUTCDATE() WHERE id=?", values)
        except Exception:
            logger.warning('Could not update entity %s#%s: %s', entity_table, entity_id, exc)


# ── Message type handlers ─────────────────────────────────────────────────────

def _handle_permission_grant(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    eori = payload.get('importer_eori', '')
    if dry_run:
        return 'DRY_RUN', 200, {}
    result = api._request('GET', f'permission_grant?importer_eori={eori}')
    if result.get('success'):
        return result.get('reference', 'OK'), result.get('http_status', 200), result
    raise ValueError(result.get('message', 'Permission grant failed'))


def _handle_ens_create(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    if dry_run:
        return f'ENS000DRY{msg["id"]:08d}', 200, {}
    result = api._request('POST', 'declaration_headers', payload={'op_type': 'create', **payload})
    if result.get('success'):
        ref = result.get('reference', '')
        _update_entity(msg.get('entity_table'), msg.get('entity_id'),
                       {'ens_reference': ref, 'status': 'CREATED'})
        return ref, result.get('http_status', 200), result
    raise ValueError(result.get('message', 'ENS create failed'))


def _handle_consignment_create(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    if dry_run:
        return f'DEC000DRY{msg["id"]:08d}', 200, {}
    result = api._request('POST', 'consignments', payload={'op_type': 'create', **payload})
    if result.get('success'):
        ref = result.get('reference', '')
        _update_entity(msg.get('entity_table'), msg.get('entity_id'),
                       {'dec_reference': ref, 'status': 'CREATED', 'tss_status': 'DRAFT'})
        return ref, result.get('http_status', 200), result
    raise ValueError(result.get('message', 'Consignment create failed'))


def _handle_consignment_submit(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    if dry_run:
        return payload.get('consignment_number', 'DRY'), 200, {}
    result = api._request('POST', 'consignments', payload={'op_type': 'submit', **payload})
    if result.get('success'):
        _update_entity(msg.get('entity_table'), msg.get('entity_id'),
                       {'status': 'SUBMITTED', 'tss_status': 'SUBMITTED',
                        'submitted_at': datetime.now(timezone.utc).isoformat()})
        return payload.get('consignment_number', ''), result.get('http_status', 200), result
    raise ValueError(result.get('message', 'Consignment submit failed'))


def _handle_goods_create(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    if dry_run:
        return f'goodsDRY{msg["id"]:016x}', 200, {}
    result = api._request('POST', 'goods', payload={'op_type': 'create', **payload})
    if result.get('success'):
        ref = result.get('reference', '')
        _update_entity(msg.get('entity_table'), msg.get('entity_id'),
                       {'goods_id': ref, 'status': 'CREATED'})
        return ref, result.get('http_status', 200), result
    raise ValueError(result.get('message', 'Goods create failed'))


def _handle_goods_update(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    if dry_run:
        return payload.get('goods_id', 'DRY'), 200, {}
    result = api._request('POST', 'goods', payload={'op_type': 'update', **payload})
    if result.get('success'):
        _update_entity(msg.get('entity_table'), msg.get('entity_id'),
                       {'status': 'UPDATED'})
        return payload.get('goods_id', ''), result.get('http_status', 200), result
    raise ValueError(result.get('message', 'Goods update failed'))


def _handle_gmr_create(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    if dry_run:
        return f'GMR000DRY{msg["id"]:08d}', 200, {}
    result = api._request('POST', 'gvms_gmr', payload={'op_type': 'create', **payload})
    if result.get('success'):
        ref = result.get('reference', '')
        _update_entity(msg.get('entity_table'), msg.get('entity_id'),
                       {'status': 'SUBMITTED', 'gmr_id': ref})
        return ref, result.get('http_status', 200), result
    raise ValueError(result.get('message', 'GMR create failed'))


def _handle_gmr_submit(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    if dry_run:
        return payload.get('declaration_header_number', 'DRY'), 200, {}
    # RULE 4: submit must follow create immediately
    result = api._request('POST', 'gvms_gmr', payload={'op_type': 'submit', **payload})
    if result.get('success'):
        _update_entity(msg.get('entity_table'), msg.get('entity_id'),
                       {'gvms_status': 'Open', 'submitted_at': datetime.now(timezone.utc).isoformat()})
        return payload.get('declaration_header_number', ''), result.get('http_status', 200), result
    raise ValueError(result.get('message', 'GMR submit failed'))


def _handle_sdi_submit(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    if dry_run:
        return payload.get('sup_dec_number', 'DRY'), 200, {}
    result = api.submit_sdi(payload.get('sup_dec_number', ''))
    if result.get('success'):
        _update_entity(msg.get('entity_table'), msg.get('entity_id'),
                       {'status': 'SUBMITTED', 'tss_status': 'Submitted'})
        return payload.get('sup_dec_number', ''), result.get('http_status', 200), result
    raise ValueError(result.get('message', 'SDI submit failed'))


def _handle_sdi_update(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    sup_dec_number = payload.get('sup_dec_number', '')
    if dry_run:
        return sup_dec_number or 'DRY', 200, {}
    goods_updates = payload.pop('goods_updates', []) or []
    for goods_update in goods_updates:
        goods_id = goods_update.get('goods_id', '')
        if not goods_id:
            raise ValueError('SDI goods update is missing goods_id')
        goods_payload = {k: v for k, v in goods_update.items() if k != 'goods_id'}
        goods_result = api.update_sdi_goods(sup_dec_number, goods_id, goods_payload)
        if not goods_result.get('success'):
            raise ValueError(goods_result.get('message', f'SDI goods update failed for {goods_id}'))
    result = api.update_sdi(sup_dec_number, payload)
    if result.get('success'):
        _update_entity(
            msg.get('entity_table'),
            msg.get('entity_id'),
            {
                'status': 'VALIDATED',
                'tss_status': result.get('status') or 'Draft',
                'error_message': None,
            },
        )
        return sup_dec_number, result.get('http_status', 200), result
    raise ValueError(result.get('message', 'SDI update failed'))


def _handle_status_poll(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    resource = payload.get('resource', 'consignments')
    reference = payload.get('reference', '')
    fields = payload.get('fields', 'status,mrn')
    if dry_run:
        return reference, 200, {}
    result = api._request('GET', f'{resource}?reference={reference}&fields={fields}')
    if result.get('success'):
        # Write back status to entity
        data = result.get('data', {})
        update = {}
        if data.get('status'):
            update['tss_status'] = data['status']
        if data.get('mrn') or data.get('movement_reference_number'):
            update['movement_reference_number'] = data.get('mrn') or data.get('movement_reference_number')
        if update:
            _update_entity(msg.get('entity_table'), msg.get('entity_id'), update)
        return reference, result.get('http_status', 200), result
    raise ValueError(result.get('message', 'Status poll failed'))


def _handle_sfd_lookup(msg: dict, api, dry_run: bool) -> tuple:
    """GET consignments?ens_consignment_number=DEC... to get SFD ref + MRN."""
    payload = json.loads(msg.get('payload_json') or '{}')
    ens_cons_number = payload.get('ens_consignment_number', '')
    if dry_run:
        return ens_cons_number, 200, {}
    result = api._request('GET', f'consignments?ens_consignment_number={ens_cons_number}')
    if result.get('success'):
        data = result.get('data', {})
        sfd_ref = data.get('declaration_number') or data.get('sfd_number') or ''
        mrn = data.get('mrn') or data.get('movement_reference_number') or ''
        if sfd_ref and msg.get('entity_id') and msg.get('entity_table'):
            _update_entity(msg.get('entity_table'), msg.get('entity_id'),
                           {'sfd_reference': sfd_ref, 'sfd_mrn': mrn,
                            'sfd_status': data.get('status', '')})
        return sfd_ref, result.get('http_status', 200), result
    raise ValueError(result.get('message', 'SFD lookup failed'))


def _handle_sdi_lookup(msg: dict, api, dry_run: bool) -> tuple:
    payload = json.loads(msg.get('payload_json') or '{}')
    sfd_number = payload.get('sfd_number', '')
    if dry_run:
        return 'SUP000DRY', 200, {}
    result = api.lookup_sdi(sfd_number)
    if result.get('success'):
        items = api.lookup_sdi_items(sfd_number)
        first = items[0] if items else {}
        sup_ref = (
            first.get('sup_dec_number')
            or first.get('reference')
            or first.get('supplementary_declaration_number')
            or first.get('number')
            or result.get('reference', '')
        )
        if sup_ref:
            _update_entity(
                msg.get('entity_table'),
                msg.get('entity_id'),
                {'sup_dec_number': sup_ref, 'tss_status': first.get('status') or first.get('state') or result.get('status', '')},
            )
        return sup_ref, result.get('http_status', 200), result
    raise ValueError(result.get('message', 'SDI lookup failed'))


# ── Handler dispatch table ────────────────────────────────────────────────────

HANDLERS = {
    'PERMISSION_GRANT_CHECK': _handle_permission_grant,
    'ENS_CREATE':             _handle_ens_create,
    'ENS_UPDATE':             _handle_ens_create,       # same structure, op_type in payload
    'CONSIGNMENT_CREATE':     _handle_consignment_create,
    'CONSIGNMENT_SUBMIT':     _handle_consignment_submit,
    'GOODS_CREATE':           _handle_goods_create,
    'GOODS_UPDATE':           _handle_goods_update,
    'GMR_CREATE':             _handle_gmr_create,
    'GMR_SUBMIT':             _handle_gmr_submit,
    'SDI_UPDATE':             _handle_sdi_update,
    'SDI_SUBMIT':             _handle_sdi_submit,
    'STATUS_POLL':            _handle_status_poll,
    'SFD_LOOKUP':             _handle_sfd_lookup,
    'SDI_LOOKUP':             _handle_sdi_lookup,
}


# ── Main processing loop ──────────────────────────────────────────────────────

def process_batch(max_messages: int = 20,
                  dry_run: bool = False,
                  message_type_filter: str = None) -> dict:
    """
    Process one batch of ready messages.
    Returns summary dict: {processed, sent, failed, skipped}
    """
    api = None if dry_run else _tss_client()
    if not dry_run and api is None:
        logger.error('TSS API client unavailable — aborting batch')
        return {'processed': 0, 'sent': 0, 'failed': 0, 'skipped': 0}

    stats = {'processed': 0, 'sent': 0, 'failed': 0, 'skipped': 0}

    for _ in range(max_messages):
        msg = _claim_next(message_type_filter)
        if not msg:
            break  # Queue empty or all dependencies unmet

        msg_id = msg['id']
        msg_type = msg.get('message_type', 'UNKNOWN')
        stats['processed'] += 1

        logger.info('[%s] Processing message %d type=%s entity=%s#%s',
                    WORKER_ID, msg_id, msg_type,
                    msg.get('entity_table', ''), msg.get('entity_id', ''))

        handler = HANDLERS.get(msg_type)
        if not handler:
            logger.warning('No handler for message type %s — marking as DEAD_LETTER', msg_type)
            _mark_failed(msg_id, f'No handler registered for type: {msg_type}', backoff_seconds=0)
            execute(f"UPDATE {S}.MessageOutbox SET status='DEAD_LETTER' WHERE id=?", [msg_id])
            stats['skipped'] += 1
            continue

        try:
            tss_ref, http_status, response = handler(msg, api, dry_run)
            response_str = json.dumps(response) if isinstance(response, dict) else str(response)
            _mark_sent(msg_id, str(tss_ref or ''), http_status, response_str)
            logger.info('[%s] Message %d SENT → %s', WORKER_ID, msg_id, tss_ref)
            stats['sent'] += 1

            # Log to ApiCallLog for audit trail
            try:
                execute(f"""
                    INSERT INTO {S}.ApiCallLog
                        (staging_id, call_type, http_method, url,
                         request_payload, http_status, response_status,
                         response_message, response_json, duration_ms)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, [
                    msg.get('entity_id'), msg_type,
                    msg.get('http_method', 'POST'),
                    msg.get('api_endpoint', ''),
                    msg.get('payload_json', '')[:4000],
                    http_status, 'SUCCESS', str(tss_ref), response_str[:4000], 0
                ])
            except Exception:
                pass  # ApiCallLog insert failure is non-critical

        except Exception as exc:
            logger.error('[%s] Message %d FAILED: %s', WORKER_ID, msg_id, exc)
            _mark_failed(msg_id, str(exc), backoff_seconds=60)
            stats['failed'] += 1

    logger.info('[%s] Batch complete: %s', WORKER_ID, stats)
    return stats


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Fusion Flow — Message Outbox Processor')
    parser.add_argument('--loop', type=int, default=0,
                        help='Loop interval in seconds (0 = single run)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simulate dispatches without calling TSS API')
    parser.add_argument('--type', dest='message_type',
                        help='Only process this message type')
    parser.add_argument('--max', type=int, default=20,
                        help='Max messages per batch')
    args = parser.parse_args()

    logger.info('Outbox processor starting — worker=%s dry_run=%s loop=%ds',
                WORKER_ID, args.dry_run, args.loop)

    if args.loop > 0:
        while True:
            try:
                stats = process_batch(args.max, args.dry_run, args.message_type)
                if stats['processed'] == 0:
                    logger.debug('Queue empty, sleeping %ds', args.loop)
            except Exception as exc:
                logger.exception('Batch error: %s', exc)
            time.sleep(args.loop)
    else:
        process_batch(args.max, args.dry_run, args.message_type)


if __name__ == '__main__':
    main()
