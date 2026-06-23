"""
NOT FOR PRD: reads/writes BKD.Staging* tables removed by migration 078.
             Migrate all phases to STG.BKD_* before running on Fusion_TSS_Automation_PRD.

Synovia Flow — Pipeline Status Sync (All Entity Types)
Polls TSS API for status updates on all CREATED/SUBMITTED records.
Updates staging tables + logs changes.

Usage:
    python scripts/sync_pipeline.py

Environment flags (all optional):
    SYNC_PIPELINE_ENS_IDS           Comma-separated StagingEnsHeaders ids to
                                    scope the ENS header status pass.
    SYNC_PIPELINE_CONSIGNMENT_IDS   Comma-separated staging_id list to scope
                                    the consignment + goods passes. Other
                                    phases (ENS, SFD, SDI) run full unless
                                    SYNC_PIPELINE_ONLY_GOODS is set.
    SYNC_PIPELINE_ONLY_GOODS        Truthy value -> skip ENS, SFD and SDI
                                    phases, run only the consignment loop
                                    (which also backfills missing goods).
"""
import os, sys, json, time, base64
from datetime import date, datetime, timezone
import pyodbc, requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string
from app.status_utils import local_goods_status_after_parent_sync
from app.tenant import get_tenant, tenant_aware_cursor
from app.tss_api import build_cfg_client, build_tss_api_url, resolve_tss_settings

RATE_LIMIT = 0.25
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

def get_connection():
    from dotenv import load_dotenv
    load_dotenv()
    return pyodbc.connect(build_connection_string(timeout=30), autocommit=False)

def get_api_session():
    resolved = resolve_tss_settings()
    if resolved.get('demo_enabled'):
        client = build_cfg_client()
        return client.session, client.api_url
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
    session.headers.update({'Accept':'application/json', 'Authorization':f'Basic {b64}'})
    return session, api_url


def first_result_item(result):
    if isinstance(result, list):
        return result[0] if result else {}
    if isinstance(result, dict):
        for key in ('items', 'results', 'data'):
            value = result.get(key)
            if isinstance(value, list):
                return value[0] if value else {}
        return result
    return {}

def api_get(session, url, params):
    t0 = time.time()
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            result = r.json().get('result', {})
            item = first_result_item(result)
            return {
                'success': True,
                'http_status': r.status_code,
                'status': item.get('status', ''),
                'error': item.get('error_message', '') or item.get('process_message', ''),
                'mrn': item.get('movement_reference_number', ''),
                'eidr': item.get('eori_for_eidr', ''),
                'reference': item.get('reference', '') or item.get('sfd_number', '') or item.get('declaration_number', ''),
                'data': result,
                'item': item,
                'duration_ms': ms,
            }
        return {'success': False, 'http_status': r.status_code, 'status': '', 'error': r.text[:200], 'duration_ms': ms}
    except Exception as e:
        return {'success': False, 'http_status': 0, 'status': '', 'error': str(e)[:200],
                'duration_ms': int((time.time()-t0)*1000)}
    finally:
        time.sleep(RATE_LIMIT)


def coerce_tss_datetime(value):
    """Return a pyodbc-safe datetime for TSS date strings, or None.

    TSS v2.9.5 returns arrival_date_time as UTC text in dd/mm/yyyy HH:MM:SS.
    Binding a Python datetime avoids SQL Server DATEFORMAT-dependent parsing
    when tenant schemas use DATETIME2.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    raw = str(value).strip()
    if not raw:
        return None

    normalized = raw.replace('Z', '+00:00')
    if '.' in normalized:
        head, tail = normalized.split('.', 1)
        for marker in ('+', '-'):
            if marker in tail:
                frac, tz = tail.split(marker, 1)
                tail = f"{frac[:6]}{marker}{tz}"
                break
        else:
            tail = tail[:6]
        normalized = f"{head}.{tail}"

    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass

    for fmt in (
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
    ):
        try:
            return datetime.strptime(raw.split('.')[0], fmt)
        except ValueError:
            continue
    return None


def log_call(cur, conn, staging_id, call_type, url, params, result):
    try:
        cur.execute("""
            INSERT INTO BKD.ApiCallLog
                (staging_id, call_type, http_method, url, request_payload,
                 http_status, response_status, response_message, response_json, duration_ms)
            VALUES (?, ?, 'GET', ?, ?, ?, ?, ?, ?, ?)
        """, [
            staging_id,
            call_type,
            (url or '')[:500],
            json.dumps(params or {}, ensure_ascii=False)[:4000],
            result.get('http_status') or 0,
            (result.get('status') or '')[:50],
            (result.get('error') or result.get('status') or '')[:500],
            json.dumps(result, default=str)[:4000],
            result.get('duration_ms') or 0,
        ])
        conn.commit()
    except Exception as exc:
        print(f"    [warn] ApiCallLog insert failed for {call_type} #{staging_id}: {exc}")


def ensure_parent_ens(cur, conn, session, api_url, cons_staging_id, ens_ref):
    """If parent ENS not in local DB, fetch its header from TSS and upsert.
    Also link the consignment to it. Returns 1 if a change was made, 0 otherwise."""
    if not ens_ref or not ens_ref.startswith('ENS'):
        return 0
    cur.execute(
        f"SELECT TOP 1 staging_id FROM {S}.StagingEnsHeaders WHERE ens_reference = ?",
        [ens_ref],
    )
    row = cur.fetchone()
    if row:
        ens_staging_id = row[0]
    else:
        # Fetch header from TSS — only request fields supported by TSS v2.9.5
        url = f"{api_url}/headers"
        all_fields = [
            'status', 'error_message', 'movement_type', 'arrival_date_time',
            'arrival_port', 'carrier_eori', 'carrier_name',
            'identity_no_of_transport', 'nationality_of_transport',
            'seal_number', 'route',
        ]
        params = {'reference': ens_ref, 'fields': ','.join(all_fields)}
        result = api_get(session, url, params)
        log_call(cur, conn, cons_staging_id, 'READ_PARENT_ENS', url, params, result)
        item = result.get('item') or {}

        # Build INSERT dynamically — only columns that exist in the local schema.
        ens_cols = table_columns(cur, 'StagingEnsHeaders')
        ens_max_lens = column_max_lengths(cur, 'StagingEnsHeaders')
        candidate = [
            ('ens_reference', ens_ref),
            ('status', 'IMPORTED'),
            ('tss_status', (item.get('status') or 'PENDING_SYNC') if result.get('success') else 'PENDING_SYNC'),
            ('source', 'TSS_SYNC'),
            ('label', f'Synced parent for {ens_ref}'),
            ('movement_type', item.get('movement_type')),
            ('arrival_date_time', coerce_tss_datetime(item.get('arrival_date_time'))),
            ('arrival_port', item.get('arrival_port')),
            ('carrier_eori', item.get('carrier_eori')),
            ('carrier_name', item.get('carrier_name')),
            ('identity_no_of_transport', item.get('identity_no_of_transport')),
            ('nationality_of_transport', item.get('nationality_of_transport')),
            ('seal_number', item.get('seal_number')),
            ('route', item.get('route')),
            ('error_message', item.get('error_message') or None),
        ]
        values = [(k, clip_to_column(k, v, ens_max_lens)) for k, v in candidate if k.lower() in ens_cols]
        if not values:
            print(f"    [warn] StagingEnsHeaders schema has no usable columns for parent ENS {ens_ref}; skipping")
            return 0

        col_sql = ", ".join(f"[{k}]" for k, _ in values)
        ph_sql = ", ".join("?" for _ in values)
        suffix_cols = []
        suffix_vals = []
        if 'created_at' in ens_cols:
            suffix_cols.append('created_at')
            suffix_vals.append('SYSUTCDATETIME()')
        if 'updated_at' in ens_cols:
            suffix_cols.append('updated_at')
            suffix_vals.append('SYSUTCDATETIME()')
        if suffix_cols:
            col_sql += ", " + ", ".join(f"[{c}]" for c in suffix_cols)
            ph_sql += ", " + ", ".join(suffix_vals)

        cur.execute(
            f"INSERT INTO {S}.StagingEnsHeaders ({col_sql}) "
            f"OUTPUT INSERTED.staging_id VALUES ({ph_sql})",
            [v for _, v in values],
        )
        ens_staging_id = cur.fetchone()[0]
        conn.commit()
        print(f"    + ENS parent inserted: {ens_ref}")

    # Ensure consignment's staging_ens_id is set
    cur.execute(
        f"""UPDATE {S}.StagingConsignments
            SET staging_ens_id = ?, ens_reference = COALESCE(NULLIF(ens_reference,''), ?),
                updated_at = SYSUTCDATETIME()
            WHERE staging_id = ? AND (staging_ens_id IS NULL OR staging_ens_id <> ?)""",
        [ens_staging_id, ens_ref, cons_staging_id, ens_staging_id],
    )
    conn.commit()
    return 1


def column_max_lengths(cur, table_name):
    """Return {column_name_lower: char_length} for VARCHAR/NVARCHAR columns."""
    cur.execute("""
        SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
          AND CHARACTER_MAXIMUM_LENGTH IS NOT NULL
    """, [S, table_name])
    return {row[0].lower(): row[1] for row in cur.fetchall() if row[1] and row[1] > 0}


def clip_to_column(col, value, max_lens):
    """Truncate string values to fit declared NVARCHAR length, leave others alone."""
    if value is None or not isinstance(value, str):
        return value
    limit = max_lens.get(col.lower())
    if limit and limit > 0 and len(value) > limit:
        return value[:limit]
    return value


def _to_decimal(value):
    """Convert TSS string/number to float, or None if blank/invalid."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _to_int(value):
    """Convert TSS string/number to int, or None if blank/invalid."""
    if value is None:
        return None
    try:
        return int(str(value).strip().split('.')[0])
    except (ValueError, TypeError):
        return None


def sync_goods_for_consignment(cur, conn, session, api_url, cons_staging_id, dec_ref):
    """Pull goods items for a consignment if missing locally. Returns number upserted."""
    goods_cols = table_columns(cur, 'StagingGoodsItems')
    if not goods_cols:
        return 0
    parent_col = next((c for c in ('staging_cons_id', 'consignment_staging_id') if c in goods_cols), None)
    remote_col = next((c for c in ('tss_goods_id', 'tss_goods_id_ens', 'goods_id') if c in goods_cols), None)
    if not parent_col or not remote_col:
        return 0
    goods_max_lens = column_max_lengths(cur, 'StagingGoodsItems')

    cur.execute(
        f"SELECT COUNT(*) FROM {S}.StagingGoodsItems WHERE {parent_col} = ?",
        [cons_staging_id],
    )
    if (cur.fetchone()[0] or 0) > 0:
        return 0  # already have goods locally

    url = f"{api_url}/goods"
    for parent_param in ('ens_number', 'consignment_number'):
        result = api_get(session, url, {parent_param: dec_ref})
        log_call(cur, conn, cons_staging_id, f'LOOKUP_GOODS_{parent_param.upper()}', url, {parent_param: dec_ref}, result)
        items = []
        data = result.get('data') if result.get('success') else None
        if isinstance(data, dict):
            for key in ('goods', 'items', 'results'):
                value = data.get(key)
                if isinstance(value, list) and value:
                    items = value
                    break
        if items:
            break
    else:
        return 0

    upserted = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        goods_id = (item.get('goods_id') or item.get('reference') or '').strip()
        if not goods_id:
            continue
        # Read full goods detail
        read_result = api_get(session, url, {'reference': goods_id, 'fields': 'goods_description,commodity_code,procedure_code,country_of_origin,gross_mass_kg,net_mass_kg,number_of_packages,type_of_packages,package_marks,item_invoice_amount,item_invoice_currency'})
        log_call(cur, conn, cons_staging_id, 'READ_GOODS_ITEM', url, {'reference': goods_id}, read_result)
        detail = read_result.get('item') if read_result.get('success') else {}
        merged = {**item, **(detail or {})}

        cols = []
        vals = []

        def add(col, value):
            if col in goods_cols:
                cols.append(col)
                vals.append(clip_to_column(col, value, goods_max_lens))

        add(parent_col, cons_staging_id)
        add(remote_col, goods_id)
        add('consignment_number', dec_ref)
        add('status', 'IMPORTED')
        add('tss_status', merged.get('status'))
        add('source', 'TSS_SYNC')
        add('goods_description', merged.get('goods_description'))
        add('commodity_code', merged.get('commodity_code'))
        add('procedure_code', merged.get('procedure_code'))
        add('country_of_origin', merged.get('country_of_origin'))
        add('gross_mass_kg', _to_decimal(merged.get('gross_mass_kg')))
        add('net_mass_kg', _to_decimal(merged.get('net_mass_kg')))
        add('number_of_packages', _to_int(merged.get('number_of_packages')))
        add('type_of_packages', merged.get('type_of_packages'))
        add('package_marks', merged.get('package_marks'))
        add('item_invoice_amount', _to_decimal(merged.get('item_invoice_amount')))
        add('item_invoice_currency', merged.get('item_invoice_currency'))

        col_sql = ', '.join(f'[{c}]' for c in cols)
        ph_sql = ', '.join('?' for _ in cols)

        # MERGE for atomic upsert. Wrap in try/except so one bad row does not
        # crash the entire sync run, and log the failure to ApiCallLog so it
        # surfaces in /technical/.
        try:
            cur.execute(
                f"""MERGE {S}.StagingGoodsItems WITH (HOLDLOCK) AS target
                    USING (SELECT ? AS k) AS src
                    ON target.[{remote_col}] = src.k
                    WHEN MATCHED THEN UPDATE SET
                        {parent_col} = ?, source='TSS_SYNC',
                        goods_description = COALESCE(?, goods_description),
                        commodity_code = COALESCE(?, commodity_code),
                        updated_at = SYSUTCDATETIME()
                    WHEN NOT MATCHED THEN INSERT ({col_sql}) VALUES ({ph_sql});""",
                [
                    goods_id, cons_staging_id,
                    clip_to_column('goods_description', merged.get('goods_description'), goods_max_lens),
                    clip_to_column('commodity_code', merged.get('commodity_code'), goods_max_lens),
                    *vals,
                ],
            )
            upserted += 1
        except Exception as merge_err:
            conn.rollback()
            err_msg = str(merge_err)[:500]
            print(f"    [warn] goods upsert failed for {goods_id}: {err_msg}")
            log_call(
                cur, conn, cons_staging_id,
                'GOODS_UPSERT_FAILED',
                f'{S}.StagingGoodsItems',
                {'goods_id': goods_id, 'dec_ref': dec_ref, 'merged_keys': sorted(merged.keys())},
                {
                    'success': False,
                    'http_status': 0,
                    'status': 'sql_error',
                    'error': err_msg,
                    'duration_ms': 0,
                },
            )
            continue

    if upserted:
        conn.commit()
        print(f"    + {upserted} goods items pulled for {dec_ref}")
    return upserted


def reconcile_goods_local_status_for_parent(cur, conn, cons_staging_id, parent_local_status='', parent_tss_status=''):
    """Mark child goods as CREATED once the parent consignment is already in TSS."""
    synced_status = local_goods_status_after_parent_sync(
        'PENDING',
        parent_local_status=parent_local_status,
        parent_tss_status=parent_tss_status,
    )
    if synced_status != 'CREATED':
        return 0

    cur.execute(
        f"""
        UPDATE {S}.StagingGoodsItems
           SET status='CREATED',
               updated_at=SYSUTCDATETIME()
         WHERE staging_cons_id=?
           AND UPPER(REPLACE(COALESCE(CAST(status AS NVARCHAR(100)), ''), '_', ' '))
               NOT IN ('IMPORTED', 'INGESTED', 'CREATED')
        """,
        [cons_staging_id],
    )
    changed = cur.rowcount or 0
    if changed:
        conn.commit()
    return changed


def sync_header_status_to_declaration(cur, conn, staging_declaration_id, ens_ref, tss_status, error_message=''):
    """Keep ENS UI cards aligned with the live TSS header status."""
    status = (tss_status or '').strip()
    error = (error_message or '').strip()
    ens_ref = (ens_ref or '').strip()

    if not status and not error:
        return False
    if not staging_declaration_id and not ens_ref:
        return False

    if staging_declaration_id:
        where_clause = "(id = ? OR external_ref = ?)"
        where_params = [staging_declaration_id, ens_ref]
    else:
        where_clause = "external_ref = ?"
        where_params = [ens_ref]

    cur.execute(f"""
        UPDATE {S}.StagingDeclarations
        SET status = CASE
                WHEN external_ref IS NOT NULL
                 AND UPPER(ISNULL(status, '')) IN ('DRAFT', 'CREATED', 'UPDATED', 'SUBMIT_ERROR', 'SUBMIT ERROR')
                    THEN 'Submitted'
                ELSE status
            END,
            external_status = COALESCE(NULLIF(?, ''), external_status),
            external_error_message = NULLIF(?, ''),
            error_message = CASE
                WHEN NULLIF(?, '') IS NULL AND error_message LIKE '%Invalid op_type%' THEN NULL
                ELSE error_message
            END,
            api_error_message = CASE
                WHEN NULLIF(?, '') IS NULL AND api_error_message LIKE '%Invalid op_type%' THEN NULL
                ELSE api_error_message
            END,
            updated_at = GETUTCDATE()
        WHERE {where_clause}
          AND (
              ISNULL(external_status, '') <> ISNULL(?, '')
              OR ISNULL(external_error_message, '') <> ISNULL(?, '')
              OR (
                  NULLIF(?, '') IS NULL
                  AND (
                      error_message LIKE '%Invalid op_type%'
                      OR api_error_message LIKE '%Invalid op_type%'
                  )
              )
              OR (
                  external_ref IS NOT NULL
                  AND UPPER(ISNULL(status, '')) IN ('DRAFT', 'CREATED', 'UPDATED', 'SUBMIT_ERROR', 'SUBMIT ERROR')
              )
          )
    """, [status, error, error, error, *where_params, status, error, error])

    if (cur.rowcount or 0) > 0:
        conn.commit()
        return True
    return False


def _csv_int_ids(env_name):
    raw = os.environ.get(env_name, '') or ''
    ids = []
    for token in raw.split(','):
        token = token.strip()
        if not token:
            continue
        try:
            ids.append(int(token))
        except ValueError:
            continue
    return ids


def _truthy(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on', 'enabled'}


def main():
    print("Synovia Flow — Pipeline Status Sync")
    print("=" * 55)

    conn = get_connection()
    cur = tenant_aware_cursor(conn.cursor())
    session, api_url = get_api_session()

    scope_ens_ids = _csv_int_ids('SYNC_PIPELINE_ENS_IDS')
    scope_cons_ids = _csv_int_ids('SYNC_PIPELINE_CONSIGNMENT_IDS')
    only_goods = _truthy(os.environ.get('SYNC_PIPELINE_ONLY_GOODS'))
    if scope_ens_ids:
        print(f"Scope: ENS header staging_ids = {scope_ens_ids}")
    if scope_cons_ids:
        print(f"Scope: consignment staging_ids = {scope_cons_ids}")
    if only_goods:
        print("Mode: ONLY_GOODS - skipping ENS, SFD and SDI phases")

    changes = 0
    polled = 0

    # ══════════════════════════════════════════════════════════
    #  0. ENS HEADERS — sync TSS status back (TSS is master)
    # ══════════════════════════════════════════════════════════
    if only_goods:
        print("\nENS Headers: skipped (SYNC_PIPELINE_ONLY_GOODS)")
    else:
        try:
            ens_sql = f"""
                SELECT staging_id, ens_reference, tss_status, staging_declaration_id, error_message
                FROM {S}.StagingEnsHeaders
                WHERE ens_reference IS NOT NULL
                  AND status NOT IN ('CANCELLED')
                  AND UPPER(REPLACE(COALESCE(CAST(tss_status AS NVARCHAR(100)), ''), '_', ' ')) NOT IN ('ARRIVED', 'CANCELLED', 'CANCELED')
            """
            if scope_ens_ids:
                placeholders = ', '.join(['?'] * len(scope_ens_ids))
                ens_sql += f" AND staging_id IN ({placeholders})"
                ens_sql += " ORDER BY staging_id"
                cur.execute(ens_sql, list(scope_ens_ids))
            else:
                ens_sql += " ORDER BY staging_id"
                cur.execute(ens_sql)
            ens_rows = cur.fetchall()
            print(f"\nENS Headers: {len(ens_rows)} to poll")

            for sid, ref, prev_tss, staging_declaration_id, prev_error in ens_rows:
                params = {'reference': ref, 'fields': 'status,error_message,arrival_date_time'}
                url = f"{api_url}/headers"
                result = api_get(session, url, params)
                log_call(cur, conn, sid, 'READ_HEADER_STATUS', url, params, result)
                polled += 1
                prev_tss = prev_tss or ''
                prev_error = prev_error or ''
                live_error = result.get('error') or ''
                declaration_synced = False

                if result['success']:
                    declaration_synced = sync_header_status_to_declaration(
                        cur,
                        conn,
                        staging_declaration_id,
                        ref,
                        result['status'],
                        result.get('error') or '',
                    )

                if result['success'] and (result['status'] != prev_tss or live_error != prev_error):
                    arrival_dt = coerce_tss_datetime(
                        (result.get('item') or {}).get('arrival_date_time')
                    )
                    cur.execute(f"""UPDATE {S}.StagingEnsHeaders
                        SET tss_status=?, error_message=?,
                            arrival_date_time = COALESCE(?, arrival_date_time),
                            updated_at=SYSUTCDATETIME()
                        WHERE staging_id=?""",
                        [result['status'], live_error or None, arrival_dt, sid])
                    conn.commit()
                    changes += 1
                    flag = ' *** DO NOT LOAD ***' if result['status'] == 'Do Not Load' else ''
                    error_note = ' error cleared' if prev_error and not live_error else ''
                    print(f"  #{sid} {ref}: {prev_tss} -> {result['status']} ({result['duration_ms']}ms){flag}{error_note}")
                elif result['success'] and declaration_synced:
                    changes += 1
                    print(f"  #{sid} {ref}: declaration UI status synced ({result['status']}) ({result['duration_ms']}ms)")
                elif result['success']:
                    print(f"  #{sid} {ref}: no change ({result['status']}) ({result['duration_ms']}ms)")
                else:
                    print(f"  #{sid} {ref}: ERROR {result['error'][:60]}")
        except Exception as e:
            print(f"\nENS Headers: skipped ({e})")

    # ══════════════════════════════════════════════════════════
    #  1. CONSIGNMENTS with DEC references
    # ══════════════════════════════════════════════════════════
    cons_sql = f"""
        SELECT staging_id, dec_reference, status, tss_status, staging_ens_id
        FROM {S}.StagingConsignments
        WHERE dec_reference IS NOT NULL
          AND status NOT IN ('CANCELLED')
          AND UPPER(REPLACE(COALESCE(CAST(tss_status AS NVARCHAR(100)), ''), '_', ' ')) NOT IN ('ARRIVED', 'CANCELLED', 'CANCELED')
    """
    if scope_cons_ids:
        placeholders = ', '.join(['?'] * len(scope_cons_ids))
        cons_sql += f" AND staging_id IN ({placeholders})"
        cons_sql += " ORDER BY staging_id"
        cur.execute(cons_sql, list(scope_cons_ids))
    else:
        cons_sql += " ORDER BY staging_id"
        cur.execute(cons_sql)
    cons = cur.fetchall()
    print(f"\nConsignments: {len(cons)} to poll")

    # Read every documented v2.9.5 consignment field so sync brings full payload,
    # not just status. UPDATE is dynamic: only columns existing locally are written,
    # and COALESCE(NULLIF(?, ''), col) keeps existing non-null values when TSS
    # returns blank for a field.
    consignment_read_fields = [
        'status', 'error_message', 'movement_reference_number',
        'declaration_number', 'goods_description', 'trader_reference',
        'transport_document_number', 'controlled_goods', 'goods_domestic_status',
        'destination_country', 'no_sfd_reason', 'container_indicator',
        'consignor_eori', 'consignor_name', 'consignor_street_number',
        'consignor_city', 'consignor_postcode', 'consignor_country',
        'consignee_eori', 'consignee_name', 'consignee_street_number',
        'consignee_city', 'consignee_postcode', 'consignee_country',
        'importer_eori', 'importer_name', 'importer_street_number',
        'importer_city', 'importer_postcode', 'importer_country',
        'exporter_eori', 'exporter_name', 'exporter_street_number',
        'exporter_city', 'exporter_postcode', 'exporter_country',
        'buyer_same_as_importer', 'seller_same_as_exporter',
        'total_packages', 'gross_mass_kg', 'control_status',
        'eori_for_eidr', 'ducr', 'declaration_choice',
        'use_importer_sde', 'align_ukims', 'supervising_customs_office',
        'customs_warehouse_identifier',
    ]
    cons_cols = table_columns(cur, 'StagingConsignments')
    cons_max_lens = column_max_lengths(cur, 'StagingConsignments')

    for sid, ref, local_status, prev_status, local_ens_id in cons:
        params = {
            'reference': ref,
            'fields': ','.join(consignment_read_fields),
        }
        url = f"{api_url}/consignments"
        result = api_get(session, url, params)
        log_call(cur, conn, sid, 'READ_CONSIGNMENT_STATUS', url, params, result)
        polled += 1
        prev_status = prev_status or ''

        if result['success']:
            item = result.get('item') or {}
            # Always update tss_status / mrn / error
            base_set = []
            base_vals = []
            if 'tss_status' in cons_cols:
                base_set.append('tss_status = ?')
                base_vals.append(clip_to_column('tss_status', result['status'], cons_max_lens))
            if 'status' in cons_cols:
                base_set.append("""status = CASE
                    WHEN UPPER(REPLACE(COALESCE(CAST(? AS NVARCHAR(100)), ''), '_', ' ')) IN (
                        'AUTHORISED FOR MOVEMENT',
                        'AUTHORIZED FOR MOVEMENT',
                        'ARRIVED'
                    )
                    AND UPPER(REPLACE(COALESCE(CAST(status AS NVARCHAR(100)), ''), '_', ' ')) NOT IN ('IMPORTED', 'INGESTED')
                        THEN 'SUBMITTED'
                    ELSE status
                END""")
                base_vals.append(result['status'])
            if 'movement_reference_number' in cons_cols:
                base_set.append('movement_reference_number = COALESCE(NULLIF(?, \'\'), movement_reference_number)')
                base_vals.append(clip_to_column('movement_reference_number', result.get('mrn') or '', cons_max_lens))
            if 'error_message' in cons_cols:
                base_set.append('error_message = ?')
                base_vals.append(clip_to_column('error_message', result.get('error') or None, cons_max_lens))

            # Also enrich the row with any other readable field present in TSS response
            # (skip status/mrn/error_message — already handled above)
            extra_set = []
            extra_vals = []
            skip_keys = {'status', 'movement_reference_number', 'error_message',
                         'declaration_number', 'reference', 'consignment_number'}
            for key, value in item.items():
                if key in skip_keys or key not in cons_cols:
                    continue
                if value in (None, ''):
                    continue  # do not overwrite local with empty
                extra_set.append(f'{key} = COALESCE(NULLIF(?, \'\'), {key})')
                extra_vals.append(clip_to_column(key, value, cons_max_lens))

            sets = base_set + extra_set
            if sets:
                if 'updated_at' in cons_cols:
                    sets.append('updated_at = SYSUTCDATETIME()')
                cur.execute(
                    f"UPDATE {S}.StagingConsignments SET {', '.join(sets)} WHERE staging_id = ?",
                    [*base_vals, *extra_vals, sid],
                )
                if cur.rowcount and result['status'] != prev_status:
                    changes += 1
                conn.commit()

            if result['status'] != prev_status:
                flag = ' *** DO NOT LOAD ***' if result['status'] == 'Do Not Load' else ''
                print(f"  #{sid} {ref}: {prev_status} → {result['status']} ({result['duration_ms']}ms){flag}")
            else:
                enriched = len(extra_set)
                tail = f' (+{enriched} fields enriched)' if enriched else ''
                print(f"  #{sid} {ref}: no status change ({result['status']}) ({result['duration_ms']}ms){tail}")
        else:
            print(f"  #{sid} {ref}: ERROR {result['error'][:60]}")

        # Climb up to parent ENS only when this consignment has no local parent yet
        parent_ens_ref = ''
        if result.get('success') and not local_ens_id:
            item = result.get('item') or {}
            parent_ens_ref = (item.get('declaration_number') or '').strip()
        if parent_ens_ref:
            ens_chain = ensure_parent_ens(cur, conn, session, api_url, sid, parent_ens_ref)
            if ens_chain:
                changes += 1

        # Pull goods items if this consignment has none locally yet
        if result.get('success') and result['status'] not in ('Cancelled', 'CANCELLED', ''):
            reconciled = reconcile_goods_local_status_for_parent(
                cur,
                conn,
                sid,
                parent_local_status=local_status,
                parent_tss_status=result['status'],
            )
            if reconciled:
                changes += reconciled
                print(f"    + {reconciled} goods local status(es) set to CREATED")
            pulled = sync_goods_for_consignment(cur, conn, session, api_url, sid, ref)
            if pulled:
                changes += pulled

    # ══════════════════════════════════════════════════════════
    #  2. CHASE SFDs — lookup for submitted consignments
    # ══════════════════════════════════════════════════════════
    if only_goods:
        print("\nSFD chase: skipped (SYNC_PIPELINE_ONLY_GOODS)")
        sfd_rows = []
    else:
        cur.execute(f"""
            SELECT staging_id, dec_reference, tss_status
            FROM {S}.StagingConsignments
            WHERE dec_reference IS NOT NULL
              AND UPPER(COALESCE(tss_status,'')) IN ('SUBMITTED','AUTHORISED FOR MOVEMENT','AUTHORISED_FOR_MOVEMENT','ARRIVED')
              AND (
                    COALESCE(sfd_reference, '') = ''
                 OR COALESCE(sfd_mrn, '') = ''
                 OR UPPER(REPLACE(COALESCE(CAST(sfd_status AS NVARCHAR(100)), ''), '_', ' ')) NOT IN (
                        'ARRIVED',
                        'CANCELLED',
                        'CANCELED'
                    )
              )
            ORDER BY staging_id
        """)
        sfd_rows = cur.fetchall()
    for sid, ref, _ in sfd_rows:
        sfd_url = f"{api_url}/simplified_frontier_declarations"
        params = {'consignment_number': ref}
        result = api_get(session, sfd_url, params)
        log_call(cur, conn, sid, 'READ_SFD_STATUS', sfd_url, params, result)
        polled += 1
        if not result['success']:
            continue

        sfd_ref = (result.get('reference') or '').strip()
        sfd_status = (result.get('status') or '').strip()
        sfd_mrn = (result.get('mrn') or '').strip()
        update_needed = False

        existing_row = cur.execute(f"""
            SELECT sfd_reference, sfd_mrn, sfd_status
            FROM {S}.StagingConsignments
            WHERE staging_id = ?
        """, [sid]).fetchone()
        prev_sfd_ref = (existing_row[0] or '') if existing_row else ''
        prev_sfd_mrn = (existing_row[1] or '') if existing_row else ''
        prev_sfd_status = (existing_row[2] or '') if existing_row else ''

        if sfd_ref and sfd_ref != prev_sfd_ref:
            update_needed = True
        if sfd_mrn and sfd_mrn != prev_sfd_mrn:
            update_needed = True
        if sfd_status and sfd_status != prev_sfd_status:
            update_needed = True

        if update_needed:
            cur.execute(f"""UPDATE {S}.StagingConsignments
                SET sfd_reference = COALESCE(NULLIF(?, ''), sfd_reference),
                    sfd_mrn = COALESCE(NULLIF(?, ''), sfd_mrn),
                    sfd_status = COALESCE(NULLIF(?, ''), sfd_status),
                    updated_at = SYSUTCDATETIME()
                WHERE staging_id = ?""", [sfd_ref, sfd_mrn, sfd_status, sid])
            conn.commit()
            changes += 1
            print(f"  SFD synced for {ref}: {sfd_ref or prev_sfd_ref or 'pending'} ({sfd_status or prev_sfd_status or 'unknown'})")
        elif sfd_status:
            print(f"  SFD no change for {ref}: {sfd_ref or prev_sfd_ref or 'pending'} ({sfd_status})")

    # ══════════════════════════════════════════════════════════
    #  3. SUP DECS with SUP references
    # ══════════════════════════════════════════════════════════
    if only_goods:
        print("\nSup Decs: skipped (SYNC_PIPELINE_ONLY_GOODS)")
    else:
        try:
            header_id_col = first_existing(cur, 'StagingSupDecHeaders', 'staging_id', 'id')
            if not header_id_col:
                raise RuntimeError("Missing SDI header id column")
            cur.execute(f"""
                SELECT {header_id_col} AS staging_id, sup_dec_number, tss_status
                FROM {S}.StagingSupDecHeaders
                WHERE sup_dec_number IS NOT NULL AND status NOT IN ('CANCELLED','CLEARED')
                ORDER BY {header_id_col}
            """)
            sds = cur.fetchall()
            print(f"\nSup Decs: {len(sds)} to poll")

            for sid, ref, prev_status in sds:
                result = api_get(session, f"{api_url}/supplementary_declarations",
                                 {'reference': ref, 'fields': 'status,error_message,movement_reference_number,clear_date_time'})
                polled += 1
                prev_status = prev_status or ''

                if result['success'] and result['status'] != prev_status:
                    cur.execute(f"""UPDATE {S}.StagingSupDecHeaders
                        SET tss_status=?, movement_reference_number=?, error_message=?,
                            updated_at=SYSUTCDATETIME()
                        WHERE {header_id_col}=?""",
                        [result['status'], result.get('mrn','') or None,
                         result.get('error','') or None, sid])
                    conn.commit()
                    changes += 1
                    print(f"  #{sid} {ref}: {prev_status} -> {result['status']} ({result['duration_ms']}ms)")
                elif result['success']:
                    print(f"  #{sid} {ref}: no change ({result['status']}) ({result['duration_ms']}ms)")
        except Exception as e:
            print(f"\nSup Decs: skipped ({e})")

    print(f"\n{'=' * 55}")
    print(f"Polled: {polled}  |  Status changes: {changes}")
    conn.close()
    sys.exit(0)  # sync-only: API poll errors are non-fatal

if __name__ == '__main__':
    main()
