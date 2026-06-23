"""
NOT FOR PRD: reads/writes legacy BKD.Staging* tables removed by migration 078. Do not run against Fusion_TSS_Automation_PRD.

Synovia Flow — Pipeline Submitter (Consignments + Goods + Sup Decs)
Takes VALIDATED records, calls TSS API, captures response.
Updates records to CREATED / FAILED, and then lets sync jobs advance
consignment TSS status from the live environment.

Usage:
    python scripts/submit_pipeline.py
"""
import os, sys, json, time, base64
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone
import pyodbc, requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string
from app.pipeline_validation import normalise_package_type
from app.tenant import get_tenant, tenant_aware_cursor
from app.sdi_payloads import build_sdi_goods_update_payload, build_sdi_update_payload, normalise_taric_code
from app.tss_api import build_cfg_client, build_tss_api_url, resolve_tss_settings

RATE_LIMIT = 0.3
TIMEOUT = 30
S = get_tenant()["schema"]


def configure_console_output():
    for stream in (getattr(sys, 'stdout', None), getattr(sys, 'stderr', None)):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')


configure_console_output()


def table_columns(cur, table_name):
    cur.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
    """, [S, table_name])
    return {row[0].lower() for row in cur.fetchall()}


def first_existing(cur, table_name, *candidates):
    columns = table_columns(cur, table_name)
    for candidate in candidates:
        if candidate and candidate.lower() in columns:
            return candidate
    return None


def table_exists(cur, table_name):
    cur.execute("""
        SELECT 1
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
    """, [S, table_name])
    return cur.fetchone() is not None


def csv_int_ids(env_name):
    ids = []
    for raw in (os.environ.get(env_name) or '').split(','):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ids.append(int(raw))
        except ValueError:
            continue
    return ids


def id_filter_sql(column_expr, ids, force_empty=False):
    if ids:
        placeholders = ','.join('?' for _ in ids)
        return f" AND {column_expr} IN ({placeholders})", list(ids)
    if force_empty:
        return " AND 1 = 0", []
    return "", []


def get_connection():
    from dotenv import load_dotenv
    load_dotenv()
    return pyodbc.connect(build_connection_string(timeout=30), autocommit=False)

def get_api_session():
    resolved = resolve_tss_settings()
    if resolved.get('demo_enabled'):
        client = build_cfg_client()
        return client.session, client.api_url, resolved
    base_url = (resolved.get('base_url') or '').rstrip('/')
    if not base_url:
        raise RuntimeError('TSS API base URL is not configured.')
    api_url = build_tss_api_url(base_url)
    u = resolved.get('username') or ''
    p = resolved.get('password') or ''
    if not u or not p:
        raise RuntimeError('TSS API credentials are not configured.')
    b64 = base64.b64encode(f"{u}:{p}".encode()).decode()
    session = requests.Session()
    session.headers.update({
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Basic {b64}',
    })
    return session, api_url, resolved

def api_post(session, url, payload, params=None):
    t0 = time.time()
    try:
        r = session.post(url, params=params, json=payload, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        body = r.json() if r.status_code == 200 else {}
        result = body.get('result', {})
        return {
            'success': r.status_code == 200 and result.get('process_message') == 'SUCCESS',
            'http_status': r.status_code,
            'reference': result.get('reference', ''),
            'status': result.get('status', ''),
            'message': result.get('process_message', r.text[:200]),
            'duration_ms': ms,
            'raw': r.text[:4000],
        }
    except Exception as e:
        return {'success': False, 'http_status': 0, 'reference': '',
                'message': str(e)[:500], 'duration_ms': int((time.time()-t0)*1000), 'raw': ''}
    finally:
        time.sleep(RATE_LIMIT)

def log_call(cur, conn, staging_id, call_type, result, url='', payload=None):
    try:
        cur.execute("""INSERT INTO BKD.ApiCallLog
            (staging_id, call_type, http_method, url, request_payload, http_status,
             response_status, response_message, response_json, duration_ms)
            VALUES (?,?,'POST',?,?,?,?,?,?,?)""",
            [staging_id, call_type, (url or '')[:500],
             json.dumps(payload or {}, ensure_ascii=False)[:4000],
             result['http_status'], (result.get('status') or '')[:50],
             (result.get('message') or '')[:500],
             (result.get('raw') or '')[:4000], result['duration_ms']])
        conn.commit()
    except Exception as exc:
        print(f"    [warn] ApiCallLog insert failed for {call_type} #{staging_id}: {exc}")


def format_tss_decimal(value, max_dp=2):
    if value in (None, ''):
        return ''
    text = str(value).strip()
    if not text:
        return ''
    try:
        dec = Decimal(text)
        if max_dp is not None:
            quant = Decimal('1').scaleb(-max_dp)
            dec = dec.quantize(quant, rounding=ROUND_HALF_UP)
        normalized = format(dec.normalize(), 'f')
    except (InvalidOperation, ValueError):
        return text
    if '.' in normalized:
        normalized = normalized.rstrip('0').rstrip('.')
    return normalized or '0'


def consignment_ready_for_submit(cur, consignment_id):
    row = cur.execute(f"""
        SELECT
            COUNT(*) AS total_goods,
            SUM(CASE WHEN status IN ('CREATED', 'SYNCED', 'SUBMITTED') THEN 1 ELSE 0 END) AS ready_goods,
            SUM(CASE WHEN status IN ('PENDING', 'VALIDATED', 'FAILED', 'INVALID') THEN 1 ELSE 0 END) AS blocked_goods
        FROM {S}.StagingGoodsItems
        WHERE staging_cons_id = ?
    """, [consignment_id]).fetchone()
    if not row:
        return False, 0, 0
    total_goods = row[0] or 0
    ready_goods = row[1] or 0
    blocked_goods = row[2] or 0
    return total_goods > 0 and ready_goods == total_goods and blocked_goods == 0, total_goods, blocked_goods


def consignment_has_successful_submit(cur, consignment_id):
    try:
        row = cur.execute(f"""
            SELECT TOP 1 1
            FROM {S}.ApiCallLog
            WHERE staging_id = ?
              AND call_type = 'SUBMIT_CONSIGNMENT'
              AND http_status BETWEEN 200 AND 299
              AND (
                    UPPER(COALESCE(response_message, '')) = 'SUCCESS'
                 OR UPPER(COALESCE(response_status, '')) IN ('SUBMITTED', 'PROCESSING')
              )
            ORDER BY called_at DESC, id DESC
        """, [consignment_id]).fetchone()
        return bool(row)
    except Exception:
        return False


def draft_like_tss_status(status):
    normalized = (status or '').strip().upper().replace(' ', '_')
    return normalized in {'', 'DRAFT', 'CREATED', 'UPDATED', 'IMPORTED'}


def local_status_after_consignment_update(already_submitted):
    return 'SUBMITTED' if already_submitted else 'CREATED'


def normalise_yes_no(value, default='yes'):
    text = str(value or '').strip().lower()
    if not text:
        return default
    if text in {'yes', 'y', 'true', '1', 'on'}:
        return 'yes'
    if text in {'no', 'n', 'false', '0', 'off'}:
        return 'no'
    return text


def build_consignment_submit_payload(dec_ref):
    return {
        'op_type': 'submit',
        'consignment_number': dec_ref,
    }


def build_consignment_payload(row, *, op_type, ens_ref=None, dec_ref=None):
    payload = {
        'op_type': op_type,
        'goods_description': row.get('goods_description', ''),
        'transport_document_number': row.get('transport_document_number', ''),
        'controlled_goods': normalise_yes_no(row.get('controlled_goods'), default='no'),
        'consignor_eori': row.get('consignor_eori', ''),
        'consignee_eori': row.get('consignee_eori', ''),
        'importer_eori': row.get('importer_eori', ''),
        'exporter_eori': row.get('exporter_eori', ''),
        'consignor_name': row.get('consignor_name', ''),
        'consignee_name': row.get('consignee_name', ''),
        'importer_name': row.get('importer_name', ''),
        'exporter_name': row.get('exporter_name', ''),
        'buyer_same_as_importer': normalise_yes_no(row.get('buyer_same_as_importer'), default='yes'),
        'seller_same_as_exporter': normalise_yes_no(row.get('seller_same_as_exporter'), default='yes'),
    }
    if ens_ref:
        payload['declaration_number'] = ens_ref
    if dec_ref is not None:
        payload['consignment_number'] = dec_ref

    optional_fields = [
        'trader_reference', 'goods_domestic_status', 'destination_country',
        'supervising_customs_office', 'customs_warehouse_identifier',
        'ducr', 'no_sfd_reason', 'align_ukims', 'use_importer_sde',
        'declaration_choice', 'container_indicator',
        'consignor_street_number', 'consignor_city', 'consignor_postcode',
        'consignor_country', 'consignee_street_number', 'consignee_city',
        'consignee_postcode', 'consignee_country', 'importer_street_number',
        'importer_city', 'importer_postcode', 'importer_country',
        'exporter_street_number', 'exporter_city', 'exporter_postcode',
        'exporter_country', 'buyer_eori', 'buyer_name',
        'buyer_street_and_number', 'buyer_city', 'buyer_postcode',
        'buyer_country', 'seller_eori', 'seller_name',
        'seller_street_and_number', 'seller_city', 'seller_postcode',
        'seller_country',
    ]
    for field in optional_fields:
        if field.startswith('buyer_') and payload.get('buyer_same_as_importer') == 'yes':
            continue
        if field.startswith('seller_') and payload.get('seller_same_as_exporter') == 'yes':
            continue
        if row.get(field):
            if field == 'use_importer_sde':
                payload[field] = normalise_yes_no(row.get(field), default='')
            else:
                payload[field] = row[field]
    if row.get('generate_SD'):
        payload['generate_SD'] = normalise_yes_no(row.get('generate_SD'), default='')
    cleaned = {k: v for k, v in payload.items() if v not in (None, '')}
    if dec_ref is not None:
        cleaned['consignment_number'] = dec_ref
    return cleaned


def build_sfd_update_payload(row, sfd_ref):
    payload = {
        'op_type': 'update',
        'sfd_number': sfd_ref,
        'goods_description': row.get('goods_description', ''),
        'transport_document_number': row.get('transport_document_number', ''),
        'controlled_goods': normalise_yes_no(row.get('controlled_goods'), default='no'),
        'consignor_eori': row.get('consignor_eori', ''),
        'consignee_eori': row.get('consignee_eori', ''),
        'importer_eori': row.get('importer_eori', ''),
        'exporter_eori': row.get('exporter_eori', ''),
    }
    if payload['controlled_goods'] == 'yes' and row.get('goods_domestic_status'):
        payload['goods_domestic_status'] = row['goods_domestic_status']
    for prefix in ('consignor', 'consignee', 'importer', 'exporter'):
        if not payload.get(f'{prefix}_eori'):
            for suffix in ('name', 'street_number', 'city', 'postcode', 'country'):
                v = row.get(f'{prefix}_{suffix}')
                if v:
                    payload[f'{prefix}_{suffix}'] = v
    for field in ('ducr', 'destination_country', 'supervising_customs_office',
                  'customs_warehouse_identifier'):
        if row.get(field):
            payload[field] = row[field]
    return {k: v for k, v in payload.items() if v not in (None, '')}


def tss_status_needs_consignment_update(status):
    normalized = (status or '').strip().upper().replace('_', ' ')
    return normalized in {'TRADER INPUT REQUIRED', 'AMENDMENT REQUIRED', 'ERROR', 'DO NOT LOAD'}


def is_unsupported_consignment_submit(result):
    text = f"{result.get('message') or ''}\n{result.get('raw') or ''}".lower()
    return 'invalid op_type' in text and 'submit' in text


def main():
    print("Synovia Flow - Pipeline Submitter")
    print("=" * 55)

    conn = get_connection()
    cur = tenant_aware_cursor(conn.cursor())
    session, api_url, resolved_settings = get_api_session()
    print(f"API: {api_url}")

    ok_total = 0
    fail_total = 0
    filter_consignment_ids = csv_int_ids('SUBMIT_PIPELINE_CONSIGNMENT_IDS')
    filter_goods_ids = csv_int_ids('SUBMIT_PIPELINE_GOODS_IDS')
    filter_supdec_ids = csv_int_ids('SUBMIT_PIPELINE_SUPDEC_IDS')
    scoped_pipeline = bool(filter_consignment_ids or filter_goods_ids or filter_supdec_ids)
    skip_consignments = (os.environ.get('SUBMIT_PIPELINE_SKIP_CONSIGNMENTS') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    skip_goods = (os.environ.get('SUBMIT_PIPELINE_SKIP_GOODS') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    skip_consignment_submit = (os.environ.get('SUBMIT_PIPELINE_SKIP_CONSIGNMENT_SUBMIT') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    skip_supdecs = (os.environ.get('SUBMIT_PIPELINE_SKIP_SUPDECS') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    if scoped_pipeline:
        print(
            "Scope: "
            f"consignments={','.join(str(i) for i in filter_consignment_ids) or '-'}; "
            f"goods={','.join(str(i) for i in filter_goods_ids) or '-'}; "
            f"supdecs={','.join(str(i) for i in filter_supdec_ids) or '-'}"
        )
    skipped_steps = [
        label
        for label, skipped in (
            ('consignments', skip_consignments),
            ('goods', skip_goods),
            ('consignment submit', skip_consignment_submit),
            ('sup decs', skip_supdecs),
        )
        if skipped
    ]
    if skipped_steps:
        print(f"Skip: {', '.join(skipped_steps)}")

    # ══════════════════════════════════════════════════════════
    #  0. CREATE ENS HEADERS IN TSS (VALIDATED, no ens_reference yet)
    # ══════════════════════════════════════════════════════════
    ENS_PAYLOAD_FIELDS = [
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
    # Filter to columns that actually exist in this tenant's schema
    ens_schema_cols = table_columns(cur, 'StagingEnsHeaders')
    ENS_PAYLOAD_FIELDS = [f for f in ENS_PAYLOAD_FIELDS if f.lower() in ens_schema_cols]
    ens_fallback_cols = [
        f for f in ('identity_no_transport', 'vehicle_registration')
        if f.lower() in ens_schema_cols
    ]
    extra_select = (', ' + ', '.join(ens_fallback_cols)) if ens_fallback_cols else ''

    cur.execute(f"""
        SELECT staging_id, {', '.join(ENS_PAYLOAD_FIELDS)}{extra_select}
        FROM {S}.StagingEnsHeaders
        WHERE status = 'VALIDATED'
          AND (ens_reference IS NULL OR ens_reference = '')
        ORDER BY staging_id
    """)
    ens_cols = [d[0] for d in cur.description]
    ens_rows = [dict(zip(ens_cols, r)) for r in cur.fetchall()]
    print(f"\nENS Headers: {len(ens_rows)} VALIDATED without TSS ref")

    ens_url = f"{api_url}/headers"
    for row in ens_rows:
        sid = row['staging_id']
        print(f"  #{sid}: CREATE ENS header...", end=' ')

        payload = {}
        for f in ENS_PAYLOAD_FIELDS:
            v = row.get(f)
            if v not in (None, ''):
                payload[f] = str(v).strip()
        if not payload.get('identity_no_of_transport'):
            fb = row.get('identity_no_transport') or row.get('vehicle_registration') or ''
            if fb:
                payload['identity_no_of_transport'] = fb

        # arrival_date_time: normalise to TSS format
        if payload.get('arrival_date_time'):
            adt = payload['arrival_date_time']
            try:
                from datetime import datetime as _dt
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%d/%m/%Y %H:%M:%S'):
                    try:
                        payload['arrival_date_time'] = _dt.strptime(str(adt), fmt).strftime('%d/%m/%Y %H:%M:%S')
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        payload = {k: v for k, v in payload.items() if v not in (None, '')}
        result = api_post(session, ens_url, {'op_type': 'create', **payload})
        log_call(cur, conn, sid, 'CREATE_ENS_HEADER', result, url=ens_url, payload=payload)

        if result['success']:
            ens_ref = result['reference']
            cur.execute(f"""UPDATE {S}.StagingEnsHeaders
                SET ens_reference=?, tss_status=?, status='SUBMITTED',
                    error_message=NULL, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?""", [ens_ref, result.get('status', ''), sid])
            conn.commit()
            ok_total += 1
            print(f"OK -> {ens_ref} ({result['duration_ms']}ms)")
        else:
            cur.execute(f"""UPDATE {S}.StagingEnsHeaders
                SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?""", [result['message'][:4000], sid])
            conn.commit()
            fail_total += 1
            print(f"FAILED: {result['message'][:80]}")

    # ══════════════════════════════════════════════════════════
    #  1. SUBMIT CONSIGNMENTS (VALIDATED → create on TSS)
    # ══════════════════════════════════════════════════════════
    cons_create_filter, cons_create_params = id_filter_sql(
        'c.staging_id',
        filter_consignment_ids,
        force_empty=skip_consignments or (scoped_pipeline and not filter_consignment_ids),
    )
    cur.execute(f"""
        SELECT c.staging_id, c.staging_ens_id, c.goods_description,
               c.transport_document_number, c.controlled_goods,
               c.goods_domestic_status, c.destination_country,
               c.consignor_eori, c.consignee_eori,
               c.importer_eori, c.exporter_eori,
               c.consignor_name, c.consignee_name,
               c.importer_name, c.exporter_name,
               c.consignor_street_number, c.consignor_city,
               c.consignor_postcode, c.consignor_country,
               c.consignee_street_number, c.consignee_city,
               c.consignee_postcode, c.consignee_country,
               c.importer_street_number, c.importer_city,
               c.importer_postcode, c.importer_country,
               c.exporter_street_number, c.exporter_city,
               c.exporter_postcode, c.exporter_country,
               c.buyer_same_as_importer, c.seller_same_as_exporter,
               c.buyer_eori, c.buyer_name, c.buyer_street_and_number,
               c.buyer_city, c.buyer_postcode, c.buyer_country,
               c.seller_eori, c.seller_name, c.seller_street_and_number,
               c.seller_city, c.seller_postcode, c.seller_country,
               c.trader_reference, c.supervising_customs_office,
               c.customs_warehouse_identifier, c.ducr, c.no_sfd_reason,
               c.align_ukims, c.use_importer_sde, c.declaration_choice,
               c.generate_SD, c.container_indicator,
               e.ens_reference
        FROM {S}.StagingConsignments c
        JOIN {S}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
        WHERE c.status = 'VALIDATED'
          AND c.dec_reference IS NULL
          AND e.ens_reference IS NOT NULL
          {cons_create_filter}
        ORDER BY c.staging_id
    """, cons_create_params)
    cols = [d[0] for d in cur.description]
    cons_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"\nConsignments: {len(cons_rows)} VALIDATED")

    for row in cons_rows:
        sid = row['staging_id']
        ens_ref = row['ens_reference']
        print(f"  #{sid}: CREATE on {ens_ref}...", end=' ')

        payload = build_consignment_payload(row, op_type='create', ens_ref=ens_ref, dec_ref='')

        consignment_url = f"{api_url}/consignments"
        result = api_post(session, consignment_url, payload)
        log_call(cur, conn, sid, 'CREATE_CONSIGNMENT', result, url=consignment_url, payload=payload)

        if result['success']:
            dec_ref = result['reference']
            cur.execute(f"""UPDATE {S}.StagingConsignments
                SET status='CREATED', dec_reference=?, tss_status=?,
                    error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                WHERE staging_id=?""", [dec_ref, result['status'], sid])
            conn.commit()
            ok_total += 1
            print(f"OK -> {dec_ref} ({result['duration_ms']}ms)")
        else:
            cur.execute(f"""UPDATE {S}.StagingConsignments
                SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?""", [result['message'][:4000], sid])
            conn.commit()
            fail_total += 1
            print(f"FAILED: {result['message'][:80]}")

    # Existing DEC records in a TSS repair state need update, not create.
    cons_update_filter, cons_update_params = id_filter_sql(
        'c.staging_id',
        filter_consignment_ids,
        force_empty=skip_consignments or (scoped_pipeline and not filter_consignment_ids),
    )
    cur.execute(f"""
        SELECT c.staging_id, c.dec_reference, c.goods_description,
               c.transport_document_number, c.controlled_goods,
               c.goods_domestic_status, c.destination_country,
               c.consignor_eori, c.consignee_eori,
               c.importer_eori, c.exporter_eori,
               c.consignor_name, c.consignee_name,
               c.importer_name, c.exporter_name,
               c.consignor_street_number, c.consignor_city,
               c.consignor_postcode, c.consignor_country,
               c.consignee_street_number, c.consignee_city,
               c.consignee_postcode, c.consignee_country,
               c.importer_street_number, c.importer_city,
               c.importer_postcode, c.importer_country,
               c.exporter_street_number, c.exporter_city,
               c.exporter_postcode, c.exporter_country,
               c.buyer_same_as_importer, c.seller_same_as_exporter,
               c.buyer_eori, c.buyer_name, c.buyer_street_and_number,
               c.buyer_city, c.buyer_postcode, c.buyer_country,
               c.seller_eori, c.seller_name, c.seller_street_and_number,
               c.seller_city, c.seller_postcode, c.seller_country,
               c.trader_reference, c.supervising_customs_office,
               c.customs_warehouse_identifier, c.ducr, c.no_sfd_reason,
               c.align_ukims, c.use_importer_sde, c.declaration_choice,
               c.generate_SD, c.container_indicator, c.tss_status
        FROM {S}.StagingConsignments c
        WHERE c.status = 'VALIDATED'
          AND c.dec_reference IS NOT NULL
          AND UPPER(REPLACE(COALESCE(c.tss_status, ''), '_', ' ')) IN (
              'TRADER INPUT REQUIRED', 'AMENDMENT REQUIRED', 'ERROR', 'DO NOT LOAD'
          )
          {cons_update_filter}
        ORDER BY c.staging_id
    """, cons_update_params)
    cols = [d[0] for d in cur.description]
    cons_update_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"\nConsignment updates: {len(cons_update_rows)} existing DEC")

    consignment_url = f"{api_url}/consignments"
    for row in cons_update_rows:
        sid = row['staging_id']
        dec_ref = row['dec_reference']
        already_submitted = consignment_has_successful_submit(cur, sid)
        print(f"  #{sid}: UPDATE {dec_ref}...", end=' ')
        payload = build_consignment_payload(row, op_type='update', dec_ref=dec_ref)
        result = api_post(session, consignment_url, payload)
        log_call(cur, conn, sid, 'UPDATE_CONSIGNMENT', result, url=consignment_url, payload=payload)

        if result['success']:
            cur.execute(f"""UPDATE {S}.StagingConsignments
                SET status=?, tss_status=?,
                    error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                WHERE staging_id=?""", [local_status_after_consignment_update(already_submitted), result['status'] or 'UPDATED', sid])
            conn.commit()
            ok_total += 1
            suffix = 'already submitted; sync next' if already_submitted else 'ready for submit'
            print(f"OK ({result['duration_ms']}ms, {suffix})")
        else:
            cur.execute(f"""UPDATE {S}.StagingConsignments
                SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?""", [result['message'][:4000], sid])
            conn.commit()
            fail_total += 1
            print(f"FAILED: {result['message'][:80]}")

    # ══════════════════════════════════════════════════════════
    #  2. SUBMIT GOODS ITEMS (VALIDATED → create on TSS)
    # ══════════════════════════════════════════════════════════
    goods_filter_parts = []
    goods_params = []
    goods_id_filter, goods_id_params = id_filter_sql('g.staging_id', filter_goods_ids)
    cons_goods_filter, cons_goods_params = id_filter_sql('g.staging_cons_id', filter_consignment_ids)
    goods_filter_parts.extend([goods_id_filter, cons_goods_filter])
    goods_params.extend(goods_id_params + cons_goods_params)
    if skip_goods or (scoped_pipeline and not filter_goods_ids and not filter_consignment_ids):
        goods_filter_parts.append(' AND 1 = 0')
    cur.execute(f"""
        SELECT g.staging_id, g.goods_description, g.type_of_packages,
               g.number_of_packages, g.package_marks,
               g.gross_mass_kg, g.net_mass_kg,
               g.controlled_goods, g.controlled_goods_type,
               g.commodity_code, g.procedure_code,
               g.additional_procedure_code, g.country_of_origin, g.taric_code,
               g.item_invoice_amount, g.item_invoice_currency,
               c.dec_reference
        FROM {S}.StagingGoodsItems g
        JOIN {S}.StagingConsignments c ON c.staging_id = g.staging_cons_id
        WHERE g.status = 'VALIDATED' AND c.dec_reference IS NOT NULL
          {''.join(goods_filter_parts)}
        ORDER BY g.staging_id
    """, goods_params)
    cols = [d[0] for d in cur.description]
    goods_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"\nGoods Items: {len(goods_rows)} VALIDATED")

    for row in goods_rows:
        sid = row['staging_id']
        dec_ref = row['dec_reference']
        print(f"  #{sid}: CREATE goods on {dec_ref}...", end=' ')

        payload = {
            'op_type': 'create',
            'consignment_number': dec_ref,
            'goods_id': '',
            'goods_description': row.get('goods_description', ''),
            'type_of_packages': normalise_package_type(row.get('type_of_packages')),
            'number_of_packages': str(row.get('number_of_packages', 1)),
            'package_marks': row.get('package_marks', 'ADDR'),
            'gross_mass_kg': format_tss_decimal(row.get('gross_mass_kg', 0), max_dp=2),
        }
        decimal_fields = {'net_mass_kg', 'item_invoice_amount'}
        for f in ['net_mass_kg','controlled_goods','controlled_goods_type','commodity_code',
                   'procedure_code','additional_procedure_code','country_of_origin',
                   'taric_code',
                   'item_invoice_amount','item_invoice_currency']:
            v = row.get(f)
            if v is None or not str(v).strip():
                continue
            if f == 'taric_code':
                payload[f] = normalise_taric_code(v)
            elif f == 'controlled_goods':
                payload[f] = normalise_yes_no(v, default='no')
            else:
                payload[f] = format_tss_decimal(v, max_dp=2) if f in decimal_fields else str(v)

        payload = {k: v for k, v in payload.items() if v not in (None, '')}
        goods_url = f"{api_url}/goods"
        result = api_post(session, goods_url, payload)
        log_call(cur, conn, sid, 'CREATE_GOODS', result, url=goods_url, payload=payload)

        if result['success']:
            goods_id = result['reference']
            try:
                cur.execute(f"""UPDATE {S}.StagingGoodsItems
                    SET status='CREATED', goods_id=?, tss_status=?,
                        error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                    WHERE staging_id=?""", [goods_id, result['status'], sid])
                conn.commit()
                ok_total += 1
                print(f"OK -> {goods_id[:16]}... ({result['duration_ms']}ms)")
            except pyodbc.IntegrityError as dup_err:
                if '23000' not in str(dup_err):
                    raise
                conn.rollback()
                # goods_id already on another staging row (duplicate local entry — TSS idempotent).
                # Mark CREATED without setting goods_id; existing row is canonical.
                conflict = cur.execute(
                    f"SELECT staging_id FROM {S}.StagingGoodsItems WHERE goods_id=? AND staging_id!=?",
                    [goods_id, sid]
                ).fetchone()
                conflict_note = (
                    f"goods_id {goods_id} already on staging_id={conflict[0]}; "
                    "deduplicate manually"
                ) if conflict else f"goods_id {goods_id} duplicate conflict"
                cur.execute(f"""UPDATE {S}.StagingGoodsItems
                    SET status='CREATED', tss_status=?,
                        error_message=?,
                        submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                    WHERE staging_id=?""", [result['status'], conflict_note[:500], sid])
                conn.commit()
                ok_total += 1
                print(f"WARN #{sid} goods_id conflict — {conflict_note[:80]}")
        else:
            cur.execute(f"""UPDATE {S}.StagingGoodsItems
                SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?""", [result['message'][:4000], sid])
            conn.commit()
            fail_total += 1
            print(f"FAILED: {result['message'][:80]}")

    # ══════════════════════════════════════════════════════════
    #  3. SUBMIT READY CONSIGNMENTS (CREATED/Draft -> submit on TSS)
    # ══════════════════════════════════════════════════════════
    cons_submit_filter, cons_submit_params = id_filter_sql(
        'staging_id',
        filter_consignment_ids,
        force_empty=skip_consignment_submit or (scoped_pipeline and not filter_consignment_ids),
    )
    cur.execute(f"""
        SELECT COUNT(*)
        FROM {S}.StagingConsignments
        WHERE status = 'CREATED'
          AND dec_reference IS NOT NULL
          AND error_message LIKE '%Invalid op_type%'
          AND error_message LIKE '%submit%'
          {cons_submit_filter}
    """, cons_submit_params)
    skipped_rejected_submits = cur.fetchone()[0] or 0

    cons_submit_row_filter, cons_submit_row_params = id_filter_sql(
        'c.staging_id',
        filter_consignment_ids,
        force_empty=skip_consignment_submit or (scoped_pipeline and not filter_consignment_ids),
    )
    cur.execute(f"""
        SELECT c.staging_id, c.dec_reference, c.tss_status
        FROM {S}.StagingConsignments c
        WHERE c.status IN ('CREATED', 'VALIDATED') AND c.dec_reference IS NOT NULL
          AND (
              c.error_message IS NULL
              OR c.error_message NOT LIKE '%Invalid op_type%'
              OR c.error_message NOT LIKE '%submit%'
          )
          {cons_submit_row_filter}
        ORDER BY c.staging_id
    """, cons_submit_row_params)
    consignment_submit_rows = cur.fetchall()
    print(f"\nConsignment submit candidates: {len(consignment_submit_rows)} CREATED")
    if skipped_rejected_submits:
        print(f"  skipped {skipped_rejected_submits} consignment(s) previously rejected by TSS for submit op_type")

    consignment_url = f"{api_url}/consignments"
    for sid, dec_ref, prev_tss_status in consignment_submit_rows:
        if consignment_has_successful_submit(cur, sid):
            cur.execute(f"""
                UPDATE {S}.StagingConsignments
                SET status='SUBMITTED', error_message=NULL, updated_at=SYSUTCDATETIME()
                WHERE staging_id=? AND status='CREATED'
            """, [sid])
            conn.commit()
            print(f"  #{sid}: skipped submit (DEC was already submitted successfully; run Sync Cargo Statuses)")
            continue
        ready, total_goods, blocked_goods = consignment_ready_for_submit(cur, sid)
        if not ready:
            print(f"  #{sid}: awaiting submit ({total_goods} goods, {blocked_goods} not ready)")
            continue
        if not draft_like_tss_status(prev_tss_status):
            print(f"  #{sid}: skipped submit (TSS status already {prev_tss_status})")
            continue

        print(f"  #{sid}: SUBMIT {dec_ref}...", end=' ')
        payload = build_consignment_submit_payload(dec_ref)
        result = api_post(session, consignment_url, payload)
        log_call(cur, conn, sid, 'SUBMIT_CONSIGNMENT', result, url=consignment_url, payload=payload)

        if result['success']:
            cur.execute(f"""UPDATE {S}.StagingConsignments
                SET status='SUBMITTED', tss_status=?,
                    error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                WHERE staging_id=?""", [result['status'] or 'SUBMITTED', sid])
            conn.commit()
            ok_total += 1
            print(f"OK ({result['duration_ms']}ms)")
        elif is_unsupported_consignment_submit(result):
            cur.execute(f"""UPDATE {S}.StagingConsignments
                SET status='CREATED', error_message=?, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?""", [result['message'][:4000], sid])
            conn.commit()
            fail_total += 1
            print(f"FAILED: TSS rejected consignment submit ({result['message'][:80]})")
        else:
            cur.execute(f"""UPDATE {S}.StagingConsignments
                SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                WHERE staging_id=?""", [result['message'][:4000], sid])
            conn.commit()
            fail_total += 1
            print(f"FAILED: {result['message'][:80]}")

    # ══════════════════════════════════════════════════════════
    #  4. SFD UPDATE + SUBMIT (Draft SFDs that have been discovered by sync)
    # ══════════════════════════════════════════════════════════
    sfd_cols = table_columns(cur, 'StagingConsignments')
    has_sfd_reference = 'sfd_reference' in sfd_cols
    has_sfd_status = 'sfd_status' in sfd_cols
    if has_sfd_reference:
        sfd_status_guard = (
            "AND UPPER(COALESCE(sfd_status, '')) NOT IN "
            "('SUBMITTED','AUTHORISED FOR MOVEMENT','AUTHORISED_FOR_MOVEMENT',"
            "'ARRIVED','CANCELLED','PROCESSING')"
        ) if has_sfd_status else ''
        cur.execute(f"""
            SELECT c.staging_id, c.dec_reference, c.sfd_reference,
                   c.goods_description, c.transport_document_number, c.controlled_goods,
                   c.goods_domestic_status, c.destination_country,
                   c.consignor_eori, c.consignor_name, c.consignor_street_number,
                   c.consignor_city, c.consignor_postcode, c.consignor_country,
                   c.consignee_eori, c.consignee_name, c.consignee_street_number,
                   c.consignee_city, c.consignee_postcode, c.consignee_country,
                   c.importer_eori, c.importer_name, c.importer_street_number,
                   c.importer_city, c.importer_postcode, c.importer_country,
                   c.exporter_eori, c.exporter_name, c.exporter_street_number,
                   c.exporter_city, c.exporter_postcode, c.exporter_country,
                   c.ducr, c.supervising_customs_office
            FROM {S}.StagingConsignments c
            WHERE c.sfd_reference IS NOT NULL
              AND c.dec_reference IS NOT NULL
              {sfd_status_guard}
            ORDER BY c.staging_id
        """)
        sfd_cols_desc = [d[0] for d in cur.description]
        sfd_rows = [dict(zip(sfd_cols_desc, r)) for r in cur.fetchall()]
        print(f"\nSFDs: {len(sfd_rows)} with Draft/unknown status to update+submit")
        sfd_url = f"{api_url}/simplified_frontier_declarations"
        for row in sfd_rows:
            sid = row['staging_id']
            sfd_ref = (row.get('sfd_reference') or '').strip()
            if not sfd_ref:
                continue
            print(f"  #{sid}: UPDATE SFD {sfd_ref[:20]}...", end=' ')
            update_payload = build_sfd_update_payload(row, sfd_ref)
            update_result = api_post(session, sfd_url, update_payload)
            log_call(cur, conn, sid, 'UPDATE_SFD', update_result, url=sfd_url, payload=update_payload)
            if not update_result['success']:
                print(f"FAILED UPDATE: {update_result['message'][:80]}")
                continue
            print("submit...", end=' ')
            submit_result = api_post(session, sfd_url, {'op_type': 'submit', 'sfd_number': sfd_ref})
            log_call(cur, conn, sid, 'SUBMIT_SFD', submit_result, url=sfd_url,
                     payload={'op_type': 'submit', 'sfd_number': sfd_ref})
            if submit_result['success']:
                if has_sfd_status:
                    cur.execute(f"""UPDATE {S}.StagingConsignments
                        SET sfd_status='Submitted', updated_at=SYSUTCDATETIME()
                        WHERE staging_id=?""", [sid])
                conn.commit()
                ok_total += 1
                print(f"OK ({update_result['duration_ms']}ms + {submit_result['duration_ms']}ms)")
            else:
                print(f"FAILED SUBMIT: {submit_result['message'][:80]}")
    else:
        print("\nSFDs: skipped (sfd_reference column not present)")

    # ══════════════════════════════════════════════════════════
    #  5. SUBMIT SUP DECS (VALIDATED -> update then submit on TSS)
    # ══════════════════════════════════════════════════════════
    try:
        header_id_col = first_existing(cur, 'StagingSupDecHeaders', 'staging_id', 'id')
        goods_parent_col = first_existing(cur, 'StagingSupDecGoods', 'staging_supdec_id', 'supdec_header_id')
        goods_id_col = first_existing(cur, 'StagingSupDecGoods', 'staging_id', 'id')
        goods_item_col = first_existing(cur, 'StagingSupDecGoods', 'item_number', 'goods_item_number')
        goods_remote_col = first_existing(cur, 'StagingSupDecGoods', 'sup_goods_id', 'tss_goods_id_sdi', 'goods_id')
        if not header_id_col or not goods_parent_col or not goods_id_col or not goods_remote_col:
            raise RuntimeError("SDI schema columns are missing")

        supdec_filter, supdec_params = id_filter_sql(
            header_id_col,
            filter_supdec_ids,
            force_empty=skip_supdecs or (scoped_pipeline and not filter_supdec_ids),
        )
        cur.execute(f"""
            SELECT {header_id_col} AS staging_id, sup_dec_number, declaration_choice,
                   goods_domestic_status, exporter_eori,
                   incoterm, delivery_location_country, delivery_location_town,
                   freight_charge, freight_charge_currency,
                   insurance, insurance_currency,
                   postponed_vat, trader_reference, transport_document_number,
                   controlled_goods, goods_description, vat_adjustment,
                   vat_adjust_currency, exchange_rate, vat_number
                   {', act_as' if first_existing(cur, 'StagingSupDecHeaders', 'act_as') else ''}
            FROM {S}.StagingSupDecHeaders
            WHERE status = 'VALIDATED' AND sup_dec_number IS NOT NULL
              {supdec_filter}
            ORDER BY {header_id_col}
        """, supdec_params)
        cols = [d[0] for d in cur.description]
        sd_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        print(f"\nSup Decs: {len(sd_rows)} VALIDATED")

        for row in sd_rows:
            sid = row['staging_id']
            sup_ref = row['sup_dec_number']
            print(f"  #{sid}: UPDATE goods for {sup_ref}...", end=' ')
            act_as = (row.get('act_as') or resolved_settings.get('act_as') or '').strip() or None
            act_as_params = {'actAs': act_as} if act_as else None
            header_payload_row = dict(row)

            if table_exists(cur, 'StagingSupDecAddDed'):
                add_ded_currency_col = first_existing(cur, 'StagingSupDecAddDed', 'addition_deduction_currency')
                add_ded_currency_select = (
                    f", {add_ded_currency_col} AS addition_deduction_currency"
                    if add_ded_currency_col else
                    ", CAST(NULL AS varchar(3)) AS addition_deduction_currency"
                )
                cur.execute(f"""
                    SELECT op_type,
                           addition_deduction_code,
                           addition_deduction_value
                           {add_ded_currency_select}
                    FROM {S}.StagingSupDecAddDed
                    WHERE staging_supdec_id=?
                    ORDER BY add_ded_id
                """, [sid])
                add_ded_cols = [d[0] for d in cur.description]
                add_ded_rows = [dict(zip(add_ded_cols, r)) for r in cur.fetchall()]
                if add_ded_rows:
                    header_payload_row['header_additions_deductions'] = json.dumps(add_ded_rows, ensure_ascii=False)

            cur.execute(f"""
                SELECT {goods_id_col} AS staging_id,
                       {goods_item_col} AS item_number,
                       {goods_remote_col} AS sup_goods_id,
                       goods_description, commodity_code, procedure_code,
                       number_of_packages,
                       {first_existing(cur, 'StagingSupDecGoods', 'type_of_packages', 'type_of_package')} AS type_of_packages,
                       package_marks,
                       {first_existing(cur, 'StagingSupDecGoods', 'gross_mass_kg', 'gross_weight_kg')} AS gross_mass_kg,
                       {first_existing(cur, 'StagingSupDecGoods', 'net_mass_kg', 'net_weight_kg')} AS net_mass_kg,
                       country_of_origin, item_invoice_amount, item_invoice_currency,
                       valuation_method, valuation_indicator, invoice_number,
                       nature_of_transaction, preference, additional_procedure_code,
                       {first_existing(cur, 'StagingSupDecGoods', 'ni_additional_information_codes', 'national_additional_codes')} AS ni_additional_information_codes,
                       country_of_preferential_origin, statistical_value, supplementary_units,
                       {first_existing(cur, 'StagingSupDecGoods', 'taric_code') or 'CAST(NULL AS varchar(20))'} AS taric_code
                FROM {S}.StagingSupDecGoods
                WHERE {goods_parent_col}=?
                ORDER BY {goods_item_col}, {goods_id_col}
            """, [sid])
            goods_cols = [d[0] for d in cur.description]
            goods_rows = [dict(zip(goods_cols, r)) for r in cur.fetchall()]
            if not goods_rows:
                cur.execute(f"""UPDATE {S}.StagingSupDecHeaders
                    SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                    WHERE {header_id_col}=?""", ['No SDI goods rows available for submit', sid])
                conn.commit()
                fail_total += 1
                print("FAILED: no goods")
                continue

            goods_failed = False
            for goods_row in goods_rows:
                remote_goods_id = (goods_row.get('sup_goods_id') or '').strip()
                if not remote_goods_id:
                    cur.execute(f"""UPDATE {S}.StagingSupDecHeaders
                        SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                        WHERE {header_id_col}=?""", [
                            f"SDI goods item {goods_row.get('item_number') or goods_row.get('staging_id')} has no TSS goods id",
                            sid,
                        ])
                    conn.commit()
                    fail_total += 1
                    print("FAILED: missing goods id")
                    goods_failed = True
                    break

                goods_payload = build_sdi_goods_update_payload(goods_row)
                goods_payload['goods_id'] = remote_goods_id
                goods_result = api_post(
                    session,
                    f"{api_url}/goods",
                    {'op_type': 'update', **goods_payload},
                    params=act_as_params,
                )
                log_call(cur, conn, sid, 'UPDATE_SUPDEC_GOODS', goods_result)
                if not goods_result['success']:
                    cur.execute(f"""UPDATE {S}.StagingSupDecHeaders
                        SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                        WHERE {header_id_col}=?""", [
                            goods_result['message'][:4000],
                            sid,
                        ])
                    conn.commit()
                    fail_total += 1
                    print(f"FAILED GOODS: {goods_result['message'][:60]}")
                    goods_failed = True
                    break

            if goods_failed:
                continue

            print("header...", end=' ')

            update_result = api_post(
                session,
                f"{api_url}/supplementary_declarations",
                build_sdi_update_payload(header_payload_row),
                params=act_as_params,
            )
            log_call(cur, conn, sid, 'UPDATE_SUPDEC', update_result)

            if not update_result['success']:
                cur.execute(f"""UPDATE {S}.StagingSupDecHeaders
                    SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME()
                    WHERE {header_id_col}=?""", [update_result['message'][:4000], sid])
                conn.commit()
                fail_total += 1
                print(f"FAILED UPDATE: {update_result['message'][:80]}")
                continue

            print("submit...", end=' ')
            submit_result = api_post(
                session,
                f"{api_url}/supplementary_declarations",
                {'op_type': 'submit', 'sup_dec_number': sup_ref},
                params=act_as_params,
            )
            log_call(cur, conn, sid, 'SUBMIT_SUPDEC', submit_result)

            if submit_result['success']:
                cur.execute(f"""UPDATE {S}.StagingSupDecHeaders
                    SET status='SUBMITTED', tss_status=?,
                        error_message=NULL, submitted_at=SYSUTCDATETIME(), updated_at=SYSUTCDATETIME()
                    WHERE {header_id_col}=?""", [submit_result['status'] or 'SUBMITTED', sid])
                conn.commit()
                ok_total += 1
                print(f"OK ({update_result['duration_ms']}ms + {submit_result['duration_ms']}ms)")
            else:
                cur.execute(f"""UPDATE {S}.StagingSupDecHeaders
                    SET status='FAILED', tss_status=?, error_message=?, updated_at=SYSUTCDATETIME()
                    WHERE {header_id_col}=?""", [
                        update_result.get('status') or 'UPDATED',
                        submit_result['message'][:4000],
                        sid,
                    ])
                conn.commit()
                fail_total += 1
                print(f"FAILED SUBMIT: {submit_result['message'][:80]}")
    except Exception as e:
        print(f"\nSup Decs: skipped ({e})")

    print(f"\n{'=' * 55}")
    print(f"Submitted: {ok_total}  |  Failed: {fail_total}  |  Total: {ok_total + fail_total}")
    conn.close()
    sys.exit(1 if fail_total > 0 else 0)

if __name__ == '__main__':
    main()
