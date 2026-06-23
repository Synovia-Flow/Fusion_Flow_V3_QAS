"""
NOT FOR PRD: reads/writes BKD.Staging* tables removed by migration 078.
             Use STG.BKD_* or ING.BKD_* for new pipeline work.

Synovia Flow -- ENS Header Status Sync
Polls TSS API for status updates on all Submitted declarations.
Logs every status change. Captures full API response.

JSON Capture: Set CAPTURE_JSON = True to save all exchanges
              to D:\\Birkdale_Build\\Json\\status_sync\\

Usage:
    python scripts/sync_statuses.py
"""
import os, sys, json, time
from datetime import datetime, timezone
import pyodbc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string
from app.tenant import tenant_aware_cursor
from app.tss_api import build_cfg_client

# ===================================
#  JSON CAPTURE SWITCH
# ===================================
CAPTURE_JSON = os.environ.get('CAPTURE_JSON', '').lower() in ('1', 'true', 'yes')
_DEFAULT_JSON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'json_captures')
JSON_DIR = os.environ.get('JSON_DIR', _DEFAULT_JSON_DIR)
# ===================================

RATE_LIMIT = 0.25
TIMEOUT = 30

# Fields to read back from TSS on every poll
POLL_FIELDS = ['status', 'error_message', 'route']


def _needs_local_submitted_repair(local_status, ens_ref):
    """Header exists in TSS, so keep local workflow status as Submitted."""
    normalized = (local_status or '').strip().replace('_', ' ').upper()
    return bool(
        (ens_ref or '').startswith('ENS')
        and normalized in {'DRAFT', 'CREATED', 'UPDATED', 'SUBMIT ERROR'}
    )


def save_json(filename, data):
    if not CAPTURE_JSON:
        return
    os.makedirs(JSON_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(JSON_DIR, f'{ts}_{filename}')
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    print(f'    JSON: {path}')


def get_connection():
    from dotenv import load_dotenv
    load_dotenv()
    return pyodbc.connect(build_connection_string(timeout=30), autocommit=False)


def get_api_session():
    from dotenv import load_dotenv
    load_dotenv()
    client = build_cfg_client()
    return client.session, client.api_url


def poll_header(session, api_url, ens_ref):
    """GET /headers?reference=ENS...&fields=status,error_message,route"""
    url = f"{api_url}/headers"
    fields_str = ','.join(POLL_FIELDS)
    params = {'reference': ens_ref, 'fields': fields_str}

    t0 = time.time()
    exchange = {
        'ens_reference': ens_ref,
        'request_url': f"{url}?reference={ens_ref}&fields={fields_str}",
        'request_method': 'GET',
        'request_timestamp': datetime.now(timezone.utc).isoformat(),
    }

    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        duration = int((time.time() - t0) * 1000)

        exchange['http_status'] = r.status_code
        exchange['duration_ms'] = duration
        exchange['response_raw'] = r.text[:4000]

        if r.status_code == 200:
            body = r.json()
            result = body.get('result', {})
            exchange['success'] = True
            exchange['tss_status'] = result.get('status', '')
            exchange['tss_error_message'] = result.get('error_message', '')
            exchange['tss_route'] = result.get('route', '')
            exchange['response_parsed'] = result
        else:
            exchange['success'] = False
            exchange['tss_status'] = ''
            exchange['error'] = r.text[:500]

    except Exception as e:
        exchange['http_status'] = 0
        exchange['duration_ms'] = int((time.time() - t0) * 1000)
        exchange['success'] = False
        exchange['error'] = str(e)[:500]

    exchange['response_timestamp'] = datetime.now(timezone.utc).isoformat()
    time.sleep(RATE_LIMIT)
    return exchange


def main():
    print("Synovia Flow -- ENS Header Status Sync")
    print(f"JSON Capture: {'ENABLED -> ' + JSON_DIR if CAPTURE_JSON else 'DISABLED'}")
    print("=" * 55)

    conn = get_connection()
    cursor = tenant_aware_cursor(conn.cursor())
    session, api_url = get_api_session()
    print(f"API: {api_url}")

    # Get all declarations with an ENS reference that aren't terminal
    cursor.execute("""
        SELECT id, external_ref, status, external_status, error_message
        FROM BKD.StagingDeclarations
        WHERE external_ref IS NOT NULL
          AND external_ref LIKE 'ENS%'
          AND status NOT IN ('Cancelled')
        ORDER BY created_at
    """)
    records = cursor.fetchall()

    if not records:
        print("\nNo ENS declarations to poll.")
        conn.close()
        return

    print(f"\nPolling {len(records)} ENS declarations...")

    changes = []
    unchanged = 0
    errors = 0
    local_repairs = 0
    all_exchanges = []

    for row in records:
        dec_id, ens_ref, local_status, prev_tss_status, prev_error = row
        prev_tss_status = prev_tss_status or ''
        prev_error = prev_error or ''

        print(f"  #{dec_id} {ens_ref} (local={local_status}, tss={prev_tss_status or 'unknown'})...", end=' ')

        exchange = poll_header(session, api_url, ens_ref)
        exchange['staging_id'] = dec_id
        exchange['previous_tss_status'] = prev_tss_status
        all_exchanges.append(exchange)

        if not exchange.get('success'):
            errors += 1
            print(f"ERROR: {exchange.get('error', 'Unknown')[:80]}")

            # Log failed poll
            try:
                cursor.execute("""
                    INSERT INTO BKD.ApiCallLog
                        (staging_id, call_type, http_method, url,
                         http_status, response_status, response_json,
                         duration_ms, error_detail)
                    VALUES (?, 'STATUS_POLL', 'GET', ?, ?, 'error', ?, ?, ?)
                """, [
                    dec_id, exchange.get('request_url', '')[:500],
                    exchange.get('http_status', 0),
                    exchange.get('response_raw', '')[:4000],
                    exchange.get('duration_ms', 0),
                    exchange.get('error', '')[:2000],
                ])
                conn.commit()
            except Exception:
                pass

            save_json(f'{ens_ref}_poll_error.json', exchange)
            continue

        new_tss_status = exchange.get('tss_status', '')
        new_error = exchange.get('tss_error_message', '')
        new_route = exchange.get('tss_route', '')
        duration = exchange.get('duration_ms', 0)

        # Detect changes
        status_changed = new_tss_status != prev_tss_status
        error_changed = new_error != prev_error
        repair_local_status = _needs_local_submitted_repair(local_status, ens_ref)

        if status_changed or error_changed or repair_local_status:
            change_record = {
                'staging_id': dec_id,
                'ens_reference': ens_ref,
                'previous_status': prev_tss_status,
                'new_status': new_tss_status,
                'previous_error': prev_error,
                'new_error': new_error,
                'route': new_route,
                'local_status_repaired': repair_local_status,
                'detected_at': datetime.now(timezone.utc).isoformat(),
            }
            if status_changed or error_changed:
                changes.append(change_record)
            if repair_local_status:
                local_repairs += 1

            # Update staging record
            cursor.execute("""
                    UPDATE BKD.StagingDeclarations
                    SET status = CASE
                            WHEN external_ref IS NOT NULL
                             AND UPPER(ISNULL(status, '')) IN ('DRAFT', 'CREATED', 'UPDATED', 'SUBMIT_ERROR', 'SUBMIT ERROR')
                                THEN 'Submitted'
                            ELSE status
                        END,
                    external_status = ?,
                    external_route = ?,
                    error_message = CASE WHEN ? != '' THEN ? ELSE error_message END,
                    external_error_message = ?,
                    updated_at = GETUTCDATE()
                WHERE id = ?
            """, [
                new_tss_status, new_route,
                new_error, new_error, new_error,
                dec_id,
            ])
            conn.commit()

            # Log the change
            response_message = (
                f"Local status repaired: {local_status} -> Submitted; TSS {new_tss_status}"
                if repair_local_status and not (status_changed or error_changed)
                else f"Status changed: {prev_tss_status} -> {new_tss_status}"
            )
            cursor.execute("""
                INSERT INTO BKD.ApiCallLog
                    (staging_id, call_type, http_method, url,
                     http_status, response_status, response_message,
                     response_json, duration_ms, error_detail)
                VALUES (?, 'STATUS_CHANGE', 'GET', ?, ?, ?, ?, ?, ?, ?)
            """, [
                dec_id, exchange.get('request_url', '')[:500],
                exchange.get('http_status', 0),
                new_tss_status,
                response_message,
                exchange.get('response_raw', '')[:4000],
                duration,
                new_error[:2000] if new_error else None,
            ])
            conn.commit()

            # Alert on critical statuses
            if repair_local_status and not (status_changed or error_changed):
                print(f"LOCAL STATUS REPAIRED: {local_status} -> Submitted; TSS {new_tss_status} ({duration}ms)")
            elif new_tss_status == 'Do Not Load':
                print(f"*** DO NOT LOAD *** {prev_tss_status} -> {new_tss_status} ({duration}ms)")
                save_json(f'{ens_ref}_DO_NOT_LOAD.json', exchange)
            elif new_tss_status in ('Trader Input Required', 'Amendment Required'):
                print(f"ACTION NEEDED: {prev_tss_status} -> {new_tss_status} ({duration}ms)")
                save_json(f'{ens_ref}_ACTION_NEEDED.json', exchange)
            else:
                print(f"CHANGED: {prev_tss_status} -> {new_tss_status} ({duration}ms)")

            save_json(f'{ens_ref}_status_change.json', change_record)
        else:
            unchanged += 1
            print(f"no change ({new_tss_status}) ({duration}ms)")

            # Log the poll even if no change
            cursor.execute("""
                INSERT INTO BKD.ApiCallLog
                    (staging_id, call_type, http_method, url,
                     http_status, response_status, response_json,
                     duration_ms)
                VALUES (?, 'STATUS_POLL', 'GET', ?, ?, ?, ?, ?)
            """, [
                dec_id, exchange.get('request_url', '')[:500],
                exchange.get('http_status', 0),
                new_tss_status,
                exchange.get('response_raw', '')[:4000],
                duration,
            ])
            conn.commit()

    # Summary
    summary = {
        'job': 'sync_statuses',
        'run_at': datetime.now(timezone.utc).isoformat(),
        'api_url': api_url,
        'capture_json': CAPTURE_JSON,
        'total_polled': len(records),
        'status_changes': len(changes),
        'local_status_repairs': local_repairs,
        'unchanged': unchanged,
        'errors': errors,
        'changes': changes,
        'all_exchanges': all_exchanges,
    }
    save_json('batch_summary.json', summary)

    print(f"\n{'=' * 55}")
    print(f"Polled:    {len(records)}")
    print(f"Changed:   {len(changes)}")
    print(f"Repaired:  {local_repairs}")
    print(f"Unchanged: {unchanged}")
    print(f"Errors:    {errors}")

    if changes:
        print(f"\nStatus Changes:")
        for c in changes:
            flag = ''
            if c['new_status'] == 'Do Not Load':
                flag = ' *** CRITICAL ***'
            elif c['new_status'] in ('Trader Input Required', 'Amendment Required'):
                flag = ' [ACTION NEEDED]'
            elif c['new_status'] in ('Authorised for Movement', 'Arrived'):
                flag = ' [OK]'
            print(f"  {c['ens_reference']}: {c['previous_status']} -> {c['new_status']}{flag}")
            if c['new_error']:
                print(f"    Error: {c['new_error'][:100]}")

    conn.close()
    print("\nDone.")
    sys.exit(1 if errors > 0 else 0)


if __name__ == '__main__':
    main()
