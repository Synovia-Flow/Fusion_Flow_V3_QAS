"""
NOT FOR PRD: reads/writes BKD.Staging* tables removed by migration 078.
             Use STG.BKD_* or ING.BKD_* for new pipeline work.

Synovia Flow -- ENS Header API Submission Job
Takes 'Validated' records, calls TSS API (create or update), captures full response.
Updates status to 'Submitted' or 'Submit_Error' with ENS reference + response JSON.

Usage:
    python scripts/submit_declarations.py
"""
import os, sys, json, time
from datetime import datetime, timezone
import pyodbc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string
from app.tenant import tenant_aware_cursor
from app.tss_api import build_cfg_client

RATE_LIMIT = 0.3
TIMEOUT = 30


def get_connection():
    from dotenv import load_dotenv
    load_dotenv()
    return pyodbc.connect(build_connection_string(timeout=30), autocommit=False)


def get_api_session():
    from dotenv import load_dotenv
    load_dotenv()
    from app.tss_api import resolve_tss_settings
    resolved = resolve_tss_settings()
    client = build_cfg_client()
    return client.session, client.api_url, resolved


def table_columns(cursor, table_name):
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'BKD'
          AND TABLE_NAME = ?
        """,
        [table_name],
    )
    return {str(row[0]).lower() for row in cursor.fetchall()}


def capture_tss_create_identity(cursor, dec_id, tss_settings):
    """Persist the TSS user/base URL used for ENS create, if columns exist."""
    columns = table_columns(cursor, 'StagingDeclarations')
    assignments = []
    params = []
    username = (tss_settings or {}).get('username') or ''
    base_url = (tss_settings or {}).get('base_url') or ''
    if username and 'tss_created_username' in columns:
        assignments.append('tss_created_username = COALESCE(tss_created_username, ?)')
        params.append(username)
    if base_url and 'tss_created_base_url' in columns:
        assignments.append('tss_created_base_url = COALESCE(tss_created_base_url, ?)')
        params.append(base_url)
    if 'tss_created_at' in columns:
        assignments.append('tss_created_at = COALESCE(tss_created_at, GETUTCDATE())')
    if not assignments:
        return
    params.append(dec_id)
    cursor.execute(
        f"""
        UPDATE BKD.StagingDeclarations
        SET {', '.join(assignments)}
        WHERE id = ?
        """,
        params,
    )


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


def call_tss_api(session, api_url, payload, op_type='create', ens_ref=''):
    """
    Call TSS API to create or update a declaration header.
    Returns full result dict with every field captured.
    """
    url = f"{api_url}/headers"

    # Build API payload
    api_payload = dict(payload)
    api_payload['op_type'] = op_type
    api_payload['declaration_number'] = ens_ref if op_type == 'update' else ''

    # Remove empty values (TSS ignores them but cleaner)
    api_payload = {k: v for k, v in api_payload.items() if v not in (None, '')}

    t0 = time.time()
    result = {
        'op_type': op_type,
        'request_url': url,
        'request_json': json.dumps(api_payload)[:4000],
    }

    try:
        r = session.post(url, json=api_payload, timeout=TIMEOUT)
        duration = int((time.time() - t0) * 1000)

        result['http_status'] = r.status_code
        result['duration_ms'] = duration
        result['response_raw'] = r.text[:4000]

        try:
            body = r.json()
            api_result = body.get('result', {})
            result['response_status'] = api_result.get('status', '')
            result['process_message'] = api_result.get('process_message', '')
            result['reference'] = api_result.get('reference', '')
            result['error_details'] = api_result.get('error_details', '')
            result['response_json'] = json.dumps(body)[:4000]
        except ValueError:
            result['response_status'] = 'parse_error'
            result['process_message'] = 'Could not parse JSON response'
            result['response_json'] = r.text[:4000]

        # Determine success
        if r.status_code == 200 and result.get('process_message') == 'SUCCESS':
            result['success'] = True
        else:
            result['success'] = False
            if not result.get('process_message'):
                result['process_message'] = f"HTTP {r.status_code}: {r.text[:200]}"

    except Exception as e:
        result['http_status'] = 0
        result['duration_ms'] = int((time.time() - t0) * 1000)
        result['success'] = False
        result['process_message'] = str(e)[:500]
        result['response_raw'] = str(e)[:2000]

    time.sleep(RATE_LIMIT)
    return result


def log_api_call(cursor, conn, staging_id, call_type, result):
    """Log every API call to BKD.ApiCallLog."""
    try:
        cursor.execute("""
            INSERT INTO BKD.ApiCallLog
                (staging_id, call_type, http_method, url,
                 request_payload, http_status, response_status,
                 response_message, response_json, duration_ms, error_detail)
            VALUES (?, ?, 'POST', ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            staging_id, call_type,
            result.get('request_url', '')[:500],
            result.get('request_json', '')[:4000],
            result.get('http_status', 0),
            result.get('response_status', '')[:50],
            result.get('process_message', '')[:500],
            result.get('response_json', result.get('response_raw', ''))[:4000],
            result.get('duration_ms', 0),
            '' if result.get('success') else result.get('process_message', '')[:2000],
        ])
        conn.commit()
    except Exception as e:
        print(f"    Warning: Could not log API call: {e}")


def main():
    print("Synovia Flow -- ENS Header API Submission Job")
    print("=" * 50)

    conn = get_connection()
    cursor = tenant_aware_cursor(conn.cursor())
    session, api_url, tss_settings = get_api_session()

    print(f"API: {api_url}")

    # Get Validated records (new submissions)
    declaration_ids = csv_int_ids('SUBMIT_DECLARATION_IDS')
    date_filter = os.environ.get('ENS_DECLARATIONS_DATE', 'today')

    id_filter = ''
    date_clause = ''
    params = []

    if declaration_ids:
        placeholders = ','.join('?' for _ in declaration_ids)
        id_filter = f' AND id IN ({placeholders})'
        params.extend(declaration_ids)
        print(f"Scope: declaration IDs {','.join(str(i) for i in declaration_ids)}")
    elif date_filter == 'all':
        print("Scope: all dates")
    else:
        date_clause = "AND CAST(created_at AS DATE) = CAST(GETUTCDATE() AS DATE)"
        print("Scope: today only (set ENS_DECLARATIONS_DATE=all to override)")

    cursor.execute(f"""
        SELECT id, payload_json, external_ref
        FROM BKD.StagingDeclarations
        WHERE status IN ('Validated', 'Resubmit')
          {id_filter}
          {date_clause}
        ORDER BY created_at
    """, params)
    records = cursor.fetchall()

    if not records:
        print("\nNo records to submit.")
        conn.close()
        return

    print(f"\nSubmitting {len(records)} records...")

    submitted = 0
    failed = 0

    for row in records:
        dec_id, payload_json, existing_ref = row

        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            cursor.execute("""
                UPDATE BKD.StagingDeclarations
                SET status = 'Submit_Error',
                    error_message = 'Invalid JSON payload',
                    updated_at = GETUTCDATE()
                WHERE id = ?
            """, [dec_id])
            conn.commit()
            failed += 1
            continue

        # Determine if create or update
        if existing_ref and existing_ref.startswith('ENS'):
            op_type = 'update'
            print(f"  #{dec_id}: UPDATE {existing_ref}...", end=' ')
        else:
            op_type = 'create'
            print(f"  #{dec_id}: CREATE...", end=' ')

        result = call_tss_api(session, api_url, payload, op_type, existing_ref or '')

        # Log the API call
        log_api_call(cursor, conn, dec_id, f'{op_type.upper()}_HEADER', result)

        if result['success']:
            ens_ref = result.get('reference', existing_ref or '')
            cursor.execute("""
                UPDATE BKD.StagingDeclarations
                SET status = 'Submitted',
                    external_ref = ?,
                    external_status = ?,
                    api_http_status = ?,
                    api_response_status = ?,
                    api_process_message = ?,
                    api_response_json = ?,
                    api_request_json = ?,
                    api_duration_ms = ?,
                    api_called_at = GETUTCDATE(),
                    error_message = NULL,
                    api_error_message = NULL,
                    completed_at = GETUTCDATE(),
                    updated_at = GETUTCDATE()
                WHERE id = ?
            """, [
                ens_ref,
                result.get('response_status', ''),
                result.get('http_status', 0),
                result.get('response_status', ''),
                result.get('process_message', ''),
                result.get('response_json', '')[:4000],
                result.get('request_json', '')[:4000],
                result.get('duration_ms', 0),
                dec_id,
            ])
            if op_type == 'create':
                capture_tss_create_identity(cursor, dec_id, tss_settings)
            try:
                cursor.execute(
                    """
                    SELECT staging_id
                    FROM BKD.StagingEnsHeaders
                    WHERE staging_declaration_id = ?
                    """,
                    [dec_id],
                )
                header_row = cursor.fetchone()
                if header_row:
                    cursor.execute(
                        """
                        UPDATE BKD.StagingEnsHeaders
                        SET ens_reference = ?,
                            tss_status = ?,
                            status = 'SUBMITTED',
                            updated_at = GETUTCDATE()
                        WHERE staging_id = ?
                        """,
                        [ens_ref, result.get('response_status', '') or 'Draft', header_row[0]],
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE BKD.StagingEnsHeaders
                        SET staging_declaration_id = ?,
                            tss_status = ?,
                            status = 'SUBMITTED',
                            updated_at = GETUTCDATE()
                        WHERE ens_reference = ?
                        """,
                        [dec_id, result.get('response_status', '') or 'Draft', ens_ref],
                    )
            except Exception:
                pass
            conn.commit()
            submitted += 1
            print(f"OK -> {ens_ref} ({result.get('duration_ms', 0)}ms)")
        else:
            error_msg = result.get('process_message', 'Unknown error')
            cursor.execute("""
                UPDATE BKD.StagingDeclarations
                SET status = 'Submit_Error',
                    api_http_status = ?,
                    api_response_status = ?,
                    api_process_message = ?,
                    api_error_message = ?,
                    api_response_json = ?,
                    api_request_json = ?,
                    api_duration_ms = ?,
                    api_called_at = GETUTCDATE(),
                    error_message = ?,
                    updated_at = GETUTCDATE()
                WHERE id = ?
            """, [
                result.get('http_status', 0),
                result.get('response_status', ''),
                result.get('process_message', ''),
                error_msg[:2000],
                result.get('response_json', result.get('response_raw', ''))[:4000],
                result.get('request_json', '')[:4000],
                result.get('duration_ms', 0),
                error_msg[:2000],
                dec_id,
            ])
            conn.commit()
            failed += 1
            print(f"FAILED: {error_msg[:100]}")

    print(f"\nDone: {submitted} submitted, {failed} failed out of {len(records)}")
    conn.close()
    sys.exit(1 if failed > 0 else 0)


if __name__ == '__main__':
    main()
