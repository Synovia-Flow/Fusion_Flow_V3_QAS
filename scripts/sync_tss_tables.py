"""
NOT FOR PRD: sources ENS references from BKD.StagingEnsHeaders / BKD.StagingConsignments
             (removed by migration 078). Adapt to query STG.BKD_ENS_Headers / STG.BKD_ENS_Consignments
             before running on Fusion_TSS_Automation_PRD.

Synovia Flow — TSS Inbound Sync
Pulls live data from the TSS API and populates the BKD synced mirror tables:
  BKD.EnsHeaders       ← GET /headers
  BKD.EnsConsignments  ← GET /consignments (by ENS reference)
  BKD.Sfds             ← GET /simplified_frontier_declarations (by consignment)

Sources (what we know to ask about):
  - BKD.StagingEnsHeaders  WHERE ens_reference IS NOT NULL
  - BKD.StagingConsignments WHERE dec_reference IS NOT NULL

Usage:
    python scripts/sync_tss_tables.py
"""
import os, sys, json, time, base64
from datetime import datetime, timezone
import pyodbc, requests
try:
    from _console_output import configure_console_output
except ModuleNotFoundError:
    from scripts._console_output import configure_console_output

configure_console_output()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tenant import get_tenant, tenant_aware_cursor
from app.tss_api import build_cfg_client, build_tss_api_url, resolve_tss_settings

S = get_tenant()["schema"]
RATE_LIMIT = 0.25   # seconds between TSS API calls
TIMEOUT    = 30
_TABLE_COLUMN_CACHE = {}
SCRIPT_VERSION = "2026-05-07-sfd-number-alias-v1"

ENS_FIELDS  = 'status,movement_type,arrival_date_time,arrival_port,' \
              'carrier_name,carrier_eori,vehicle_registration,trailer_registration'
CONS_FIELDS = 'status,goods_description,importer_eori,country_of_destination,' \
              'ens_lrn,ens_number,ens_header_reference,declaration_number,error_message'
SFD_FIELDS  = 'sfd_number,status,mrn,ens_number,consignment_number,error_message'


# ── DB connection ────────────────────────────────────────────────
def get_connection():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    from config.db_connection import build_connection_string
    return pyodbc.connect(build_connection_string(), autocommit=False)


# ── API session ──────────────────────────────────────────────────
def get_api_session():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    resolved = resolve_tss_settings()
    if resolved.get('demo_enabled'):
        client = build_cfg_client()
        return client.session, client.api_url
    api_url = build_tss_api_url(resolved.get('base_url') or '')
    username = resolved.get('username') or ''
    password = resolved.get('password') or ''
    if not api_url:
        raise RuntimeError('TSS API base URL is not configured.')
    if not username or not password:
        raise RuntimeError('TSS API credentials are not configured.')
    b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
    session = requests.Session()
    session.headers.update({
        'Accept': 'application/json',
        'Authorization': f'Basic {b64}',
    })
    return session, api_url


# ── Generic GET ──────────────────────────────────────────────────
def api_get(session, url, params):
    t0 = time.time()
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        time.sleep(RATE_LIMIT)
        if r.status_code == 200:
            body = r.json()
            result = body.get('result', body)
            return {'success': True, 'status_code': r.status_code,
                    'result': result, 'raw': r.text, 'ms': ms}
        return {'success': False, 'status_code': r.status_code,
                'result': None, 'raw': r.text, 'ms': ms}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {'success': False, 'status_code': 0,
                'result': None, 'raw': str(e), 'ms': ms}


def result_items(result):
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ('items', 'results', 'data'):
            value = result.get(key)
            if isinstance(value, list):
                return value
        return [result]
    return []


def log_api_call(cur, call_type, url, status_code, ms, raw, error=None):
    cur.execute(f"""
        INSERT INTO {S}.ApiCallLog
            (call_type, http_method, url, http_status,
             response_json, duration_ms, error_detail, called_at)
        VALUES (?, 'GET', ?, ?, ?, ?, ?, SYSUTCDATETIME())
    """, [call_type, url, status_code, raw[:4000] if raw else None, ms,
          str(error)[:2000] if error else None])


def table_columns(cur, table_name):
    cache_key = (S, table_name)
    if cache_key in _TABLE_COLUMN_CACHE:
        return _TABLE_COLUMN_CACHE[cache_key]

    cur.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
    """, [S, table_name])
    columns = {row[0].lower() for row in cur.fetchall()}

    # Some deployed databases have catch-up columns created outside the
    # original migrations. sys.columns is the most reliable fallback for those.
    cur.execute("""
        SELECT c.name
        FROM sys.columns c
        JOIN sys.tables t ON t.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
    """, [S, table_name])
    columns.update(row[0].lower() for row in cur.fetchall())

    _TABLE_COLUMN_CACHE[cache_key] = columns
    return columns


def first_existing(cur, table_name, *candidates):
    columns = table_columns(cur, table_name)
    for candidate in candidates:
        if candidate and candidate.lower() in columns:
            return candidate
    return None


def add_existing_payload_values(cur, table_name, payload, value, *candidates):
    columns = table_columns(cur, table_name)
    for candidate in candidates:
        if candidate and candidate.lower() in columns:
            payload[candidate] = value


def first_non_blank(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def add_existing_payload_text(cur, table_name, payload, value, *candidates):
    text = first_non_blank(value)
    if text is not None:
        add_existing_payload_values(cur, table_name, payload, text, *candidates)


def build_sfd_payload(cur, sfd, dec_ref, cons_id):
    sfd_ref = first_non_blank(
        sfd.get('sfd_number'),
        sfd.get('reference'),
        sfd.get('declaration_number'),
        sfd.get('number'),
    )
    if not sfd_ref:
        return None, {}

    payload = {
        'raw_json': json.dumps(sfd, default=str),
    }
    if cons_id is not None:
        add_existing_payload_values(cur, 'Sfds', payload, cons_id, 'consignment_id')
    add_existing_payload_text(
        cur,
        'Sfds',
        payload,
        sfd_ref,
        'sfd_number',
        'sfd_reference',
        'reference',
    )
    add_existing_payload_text(
        cur,
        'Sfds',
        payload,
        first_non_blank(sfd.get('mrn'), sfd.get('movement_reference_number')),
        'mrn',
        'movement_reference_number',
        'sfd_mrn',
    )
    add_existing_payload_text(
        cur,
        'Sfds',
        payload,
        first_non_blank(sfd.get('status'), sfd.get('tss_status')),
        'tss_status',
        'status',
    )
    add_existing_payload_text(
        cur,
        'Sfds',
        payload,
        first_non_blank(sfd.get('error_message'), sfd.get('error_detail')),
        'error_message',
        'error_detail',
        'last_error',
    )
    add_existing_payload_text(
        cur,
        'Sfds',
        payload,
        first_non_blank(
            sfd.get('ens_number'),
            sfd.get('ens_lrn'),
            sfd.get('ens_reference'),
        ),
        'ens_reference',
        'ens_number',
        'ens_lrn',
        'ens_header_reference',
    )
    add_existing_payload_text(
        cur,
        'Sfds',
        payload,
        dec_ref,
        'declaration_number',
        'consignment_number',
        'ens_consignment_reference',
        'ens_consignment_ref',
    )
    return sfd_ref, payload


def sync_staging_consignment_sfd(cur, dec_ref, sfd):
    """Keep the local consignment SFD fields aligned with the live TSS SFD row."""
    if not dec_ref:
        return 0

    columns = table_columns(cur, 'StagingConsignments')
    sfd_ref = first_non_blank(
        sfd.get('sfd_number'),
        sfd.get('reference'),
        sfd.get('declaration_number'),
        sfd.get('number'),
    )
    sfd_mrn = first_non_blank(sfd.get('mrn'), sfd.get('movement_reference_number'))
    sfd_status = first_non_blank(sfd.get('status'), sfd.get('tss_status'))

    assignments = []
    params = []
    for column, value in (
        ('sfd_reference', sfd_ref),
        ('sfd_mrn', sfd_mrn),
        ('sfd_status', sfd_status),
    ):
        if column in columns:
            assignments.append(f"{column} = COALESCE(NULLIF(?, ''), {column})")
            params.append(value or '')

    if not assignments:
        return 0
    if 'updated_at' in columns:
        assignments.append('updated_at = SYSUTCDATETIME()')

    cur.execute(
        f"UPDATE {S}.StagingConsignments SET {', '.join(assignments)} WHERE dec_reference = ?",
        [*params, dec_ref],
    )
    return cur.rowcount or 0


def upsert_row(cur, table_name, key_candidates, key_value, payload):
    key_col = first_existing(cur, table_name, *key_candidates)
    if not key_col:
        raise RuntimeError(f"{table_name} is missing all expected key columns: {', '.join(key_candidates)}")

    columns = table_columns(cur, table_name)
    filtered = {
        column: value
        for column, value in payload.items()
        if column and column.lower() in columns and column.lower() != key_col.lower()
    }

    assignments = [f"{column} = ?" for column in filtered]
    params = list(filtered.values())
    if 'synced_at' in columns:
        assignments.append("synced_at = SYSUTCDATETIME()")
    if 'updated_at' in columns:
        assignments.append("updated_at = SYSUTCDATETIME()")

    if not assignments:
        raise RuntimeError(f"{table_name} has no writable columns available for sync")

    cur.execute(
        f"UPDATE {S}.{table_name} SET {', '.join(assignments)} WHERE {key_col} = ?",
        params + [key_value],
    )
    if cur.rowcount:
        return False, key_col

    insert_columns = []
    insert_values = []
    insert_params = []
    for column, value in filtered.items():
        insert_columns.append(column)
        insert_values.append('?')
        insert_params.append(value)

    if key_col.lower() in columns:
        insert_columns.append(key_col)
        insert_values.append('?')
        insert_params.append(key_value)
    if 'synced_at' in columns:
        insert_columns.append('synced_at')
        insert_values.append('SYSUTCDATETIME()')
    if 'updated_at' in columns:
        insert_columns.append('updated_at')
        insert_values.append('SYSUTCDATETIME()')

    cur.execute(
        f"INSERT INTO {S}.{table_name} ({', '.join(insert_columns)}) VALUES ({', '.join(insert_values)})",
        insert_params,
    )
    return True, key_col


# ── ENS HEADERS SYNC ────────────────────────────────────────────
def sync_ens_headers(conn, session, api_url):
    cur = tenant_aware_cursor(conn.cursor())
    cur.execute(f"""
        SELECT DISTINCT ens_reference
        FROM {S}.StagingEnsHeaders
        WHERE ens_reference IS NOT NULL
          AND UPPER(REPLACE(COALESCE(CAST(tss_status AS NVARCHAR(100)), ''), '_', ' ')) NOT IN ('ARRIVED', 'CANCELLED', 'CANCELED')
        ORDER BY ens_reference
    """)
    refs = [r[0] for r in cur.fetchall()]
    print(f"  ENS Headers: {len(refs)} to sync")

    synced = updated = failed = 0
    for ref in refs:
        resp = api_get(session, f"{api_url}/headers",
                       {'reference': ref, 'fields': ENS_FIELDS})
        log_api_call(cur, 'SYNC_ENS_HEADER', f"{api_url}/headers",
                     resp['status_code'], resp['ms'], resp['raw'])

        if not resp['success'] or not resp['result']:
            print(f"    SKIP {ref} — HTTP {resp['status_code']}")
            failed += 1
            continue

        r = resp['result']
        raw_json = json.dumps(r, default=str)

        # Upsert: update if exists, insert if not
        cur.execute(f"""
            UPDATE {S}.EnsHeaders SET
                tss_status           = ?,
                movement_type        = ?,
                arrival_date_time    = ?,
                arrival_port         = ?,
                carrier_name         = ?,
                carrier_eori         = ?,
                vehicle_registration = ?,
                trailer_registration = ?,
                raw_json             = ?,
                synced_at            = SYSUTCDATETIME(),
                updated_at           = SYSUTCDATETIME()
            WHERE declaration_number = ?
        """, [
            r.get('status'), r.get('movement_type'),
            r.get('arrival_date_time'), r.get('arrival_port'),
            r.get('carrier_name'), r.get('carrier_eori'),
            r.get('vehicle_registration'), r.get('trailer_registration'),
            raw_json, ref
        ])

        if cur.rowcount == 0:
            cur.execute(f"""
                INSERT INTO {S}.EnsHeaders
                    (declaration_number, tss_status, movement_type,
                     arrival_date_time, arrival_port,
                     carrier_name, carrier_eori,
                     vehicle_registration, trailer_registration,
                     raw_json, synced_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME(), SYSUTCDATETIME())
            """, [
                ref, r.get('status'), r.get('movement_type'),
                r.get('arrival_date_time'), r.get('arrival_port'),
                r.get('carrier_name'), r.get('carrier_eori'),
                r.get('vehicle_registration'), r.get('trailer_registration'),
                raw_json
            ])
            synced += 1
            print(f"    NEW  {ref}  [{r.get('status','')}]")
        else:
            updated += 1
            print(f"    UPD  {ref}  [{r.get('status','')}]")

    conn.commit()
    print(f"  ENS Headers done — {synced} new, {updated} updated, {failed} failed")
    cur.close()
    return synced + updated


# ── ENS CONSIGNMENTS SYNC ───────────────────────────────────────
def sync_ens_consignments(conn, session, api_url):
    cur = tenant_aware_cursor(conn.cursor())
    # All submitted consignments with a TSS reference
    cur.execute(f"""
        SELECT DISTINCT dec_reference, staging_ens_id
        FROM {S}.StagingConsignments
        WHERE dec_reference IS NOT NULL
          AND UPPER(REPLACE(COALESCE(CAST(tss_status AS NVARCHAR(100)), ''), '_', ' ')) NOT IN ('ARRIVED', 'CANCELLED', 'CANCELED')
        ORDER BY dec_reference
    """)
    rows = cur.fetchall()
    print(f"  ENS Consignments: {len(rows)} to sync")
    mirror_key_col = first_existing(cur, 'EnsConsignments', 'declaration_number', 'consignment_number')
    mirror_header_fk_col = first_existing(cur, 'EnsConsignments', 'ens_header_id', 'header_id')
    mirror_parent_ref_col = first_existing(
        cur,
        'EnsConsignments',
        'ens_declaration_number',
        'ens_reference',
        'ens_lrn',
        'ens_number',
        'ens_header_reference',
    )
    if mirror_key_col:
        print(f"    Using {S}.EnsConsignments key column: {mirror_key_col}")
    if mirror_parent_ref_col:
        print(f"    Using {S}.EnsConsignments ENS parent column: {mirror_parent_ref_col}")
    if not mirror_header_fk_col:
        print(f"    {S}.EnsConsignments has no ENS header FK column; sync will fall back to ens_reference only")

    synced = updated = failed = 0
    for dec_ref, ens_id in rows:
        resp = api_get(session, f"{api_url}/consignments",
                       {'reference': dec_ref, 'fields': CONS_FIELDS})
        log_api_call(cur, 'SYNC_ENS_CONSIGNMENT', f"{api_url}/consignments",
                     resp['status_code'], resp['ms'], resp['raw'])

        if not resp['success'] or not resp['result']:
            print(f"    SKIP {dec_ref} — HTTP {resp['status_code']}")
            failed += 1
            continue

        r = resp['result']
        raw_json = json.dumps(r, default=str)

        # Resolve ens_reference — prefer API response, fall back to staging FK
        ens_ref = (
            r.get('ens_lrn')
            or r.get('ens_number')
            or r.get('ens_header_reference')
            or r.get('declaration_number')
        )
        if not ens_ref and ens_id:
            row = cur.execute(
                f"SELECT ens_reference FROM {S}.StagingEnsHeaders WHERE staging_id = ?",
                [ens_id]).fetchone()
            if row:
                ens_ref = row[0]

        if mirror_parent_ref_col and not ens_ref:
            print(f"    SKIP {dec_ref} - missing ENS parent reference for {mirror_parent_ref_col}")
            failed += 1
            continue

        # Resolve ens_header_id FK from BKD.EnsHeaders
        ens_header_id = None
        if ens_ref:
            row = cur.execute(
                f"SELECT id FROM {S}.EnsHeaders WHERE declaration_number = ?",
                [ens_ref]).fetchone()
            if row:
                ens_header_id = row[0]

        payload = {
            mirror_header_fk_col: ens_header_id,
            'tss_status': r.get('status'),
            'goods_description': r.get('goods_description'),
            'importer_eori': r.get('importer_eori'),
            'country_of_destination': r.get('country_of_destination'),
            'raw_json': raw_json,
        }
        add_existing_payload_values(
            cur,
            'EnsConsignments',
            payload,
            ens_ref,
            'ens_reference',
            'ens_declaration_number',
            'ens_lrn',
            'ens_number',
            'ens_header_reference',
        )

        inserted, _ = upsert_row(
            cur,
            'EnsConsignments',
            ('declaration_number', 'consignment_number'),
            dec_ref,
            payload,
        )

        if inserted:
            synced += 1
            print(f"    NEW  {dec_ref}  [{r.get('status','')}]")
        else:
            updated += 1
            print(f"    UPD  {dec_ref}  [{r.get('status','')}]")

    conn.commit()
    print(f"  Consignments done — {synced} new, {updated} updated, {failed} failed")
    cur.close()
    return synced + updated


# ── SFDs SYNC ────────────────────────────────────────────────────
def sync_sfds(conn, session, api_url):
    cur = tenant_aware_cursor(conn.cursor())
    # Consignments that have reached a state where SFD would exist
    staging_cons_cols = table_columns(cur, 'StagingConsignments')
    sfd_pending_filter = ""
    if {'sfd_reference', 'sfd_mrn', 'sfd_status'}.issubset(staging_cons_cols):
        sfd_pending_filter = """
          AND (
                COALESCE(sfd_reference, '') = ''
             OR COALESCE(sfd_mrn, '') = ''
             OR UPPER(REPLACE(COALESCE(CAST(sfd_status AS NVARCHAR(100)), ''), '_', ' ')) NOT IN (
                    'ARRIVED',
                    'CANCELLED',
                    'CANCELED',
                    'CLEARED'
                )
          )
        """

    cur.execute(f"""
        SELECT DISTINCT dec_reference
        FROM {S}.StagingConsignments
        WHERE dec_reference IS NOT NULL
          AND tss_status IN ('Submitted','SUBMITTED','Authorised for Movement','AUTHORISED_FOR_MOVEMENT','Arrived','ARRIVED','Cleared','CLEARED')
          {sfd_pending_filter}
        ORDER BY dec_reference
    """)
    refs = [r[0] for r in cur.fetchall()]
    print(f"  SFDs: checking {len(refs)} consignments")
    sfd_cons_ref_col = first_existing(cur, 'Sfds', 'declaration_number', 'consignment_number')
    cons_lookup_ref_col = first_existing(cur, 'EnsConsignments', 'declaration_number', 'consignment_number')
    cons_lookup_id_col = first_existing(cur, 'EnsConsignments', 'consignment_id', 'id')
    if sfd_cons_ref_col:
        print(f"    Using {S}.Sfds consignment reference column: {sfd_cons_ref_col}")

    synced = updated = skipped = 0
    for dec_ref in refs:
        resp = api_get(session, f"{api_url}/simplified_frontier_declarations",
                       {'consignment_number': dec_ref, 'fields': SFD_FIELDS})
        log_api_call(cur, 'SYNC_SFD', f"{api_url}/simplified_frontier_declarations",
                     resp['status_code'], resp['ms'], resp['raw'])

        if not resp['success'] or not resp['result']:
            skipped += 1
            continue

        sfds = result_items(resp['result'])

        for sfd in sfds:
            # CLR and BKD schemas have drifted on the SFD reference column name.
            # Populate every existing alias so NOT NULL variants stay insertable.
            sfd_ref, payload = build_sfd_payload(cur, sfd, dec_ref, None)
            if not sfd_ref:
                skipped += 1
                continue

            # Resolve consignment_id FK
            cons_id = None
            if cons_lookup_ref_col and cons_lookup_id_col:
                row = cur.execute(
                    f"SELECT {cons_lookup_id_col} FROM {S}.EnsConsignments WHERE {cons_lookup_ref_col} = ?",
                    [dec_ref]).fetchone()
                if row:
                    cons_id = row[0]

            if cons_id is not None:
                add_existing_payload_values(cur, 'Sfds', payload, cons_id, 'consignment_id')

            inserted, _ = upsert_row(
                cur,
                'Sfds',
                ('sfd_number', 'sfd_reference', 'reference'),
                sfd_ref,
                payload,
            )
            staged = sync_staging_consignment_sfd(cur, dec_ref, sfd)

            if inserted:
                synced += 1
                mrn = first_non_blank(
                    sfd.get('mrn'),
                    sfd.get('movement_reference_number'),
                )
                staged_note = f" staged:{staged}" if staged else ""
                print(f"    NEW SFD {sfd_ref}  MRN:{mrn}  [{sfd.get('status','')}]{staged_note}")
            else:
                updated += 1
                staged_note = f" staged:{staged}" if staged else ""
                print(f"    UPD SFD {sfd_ref}  [{sfd.get('status','')}]{staged_note}")

    conn.commit()
    print(f"  SFDs done — {synced} new, {updated} updated, {skipped} no SFD yet")
    cur.close()
    return synced + updated


# ── MAIN ────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"  TSS TABLE SYNC  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  Script version: {SCRIPT_VERSION}")
    print("=" * 60)

    try:
        conn = get_connection()
        print("  DB connected")
    except Exception as e:
        print(f"  DB connection failed: {e}")
        sys.exit(1)

    try:
        session, api_url = get_api_session()
        print(f"  API: {api_url}")
    except Exception as e:
        print(f"  API setup failed: {e}")
        conn.close()
        sys.exit(1)

    total = 0
    success = True
    try:
        print("\n[ 1/3 ] ENS Headers")
        total += sync_ens_headers(conn, session, api_url)

        print("\n[ 2/3 ] ENS Consignments")
        total += sync_ens_consignments(conn, session, api_url)

        print("\n[ 3/3 ] SFDs")
        total += sync_sfds(conn, session, api_url)

    except Exception as e:
        print(f"\nERROR: {e}")
        conn.rollback()
        success = False
    finally:
        conn.close()

    print(f"\n{'=' * 60}")
    print(f"  Sync complete — {total} records upserted")
    print("=" * 60)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
