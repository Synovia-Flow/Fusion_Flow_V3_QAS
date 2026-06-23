"""
Legacy ENS queue worker.

Picks up BKD.StagingDeclarations in Queued status, calls the TSS API to create
the declaration, and writes the result back to the staging row.
"""
import os
import sys
import json
import time
import base64
import logging
import pyodbc
import requests
from dotenv import load_dotenv

load_dotenv()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from config.db_connection import build_connection_string
from app.tenant import tenant_aware_cursor

from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger('process_queue')

WORKER_ID = f"cron-{datetime.now(timezone.utc).strftime('%H%M%S')}"
BATCH_SIZE = 10
RATE_LIMIT = 0.3


def get_connection():
    return pyodbc.connect(build_connection_string(timeout=30), autocommit=False)


def get_api_session():
    base_url = os.environ['TSS_API_BASE_URL'].rstrip('/')
    api_url = f"{base_url}/x_fhmrc_tss_api/v1/tss_api"
    username = os.environ['TSS_API_USERNAME']
    password = os.environ['TSS_API_PASSWORD']
    b64 = base64.b64encode(f"{username}:{password}".encode()).decode()

    session = requests.Session()
    session.headers.update({
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Basic {b64}',
    })
    return session, api_url


def claim_batch(conn):
    """Atomically claim a batch of Queued records for processing."""
    cursor = tenant_aware_cursor(conn.cursor())
    cursor.execute("""
        UPDATE TOP (?) BKD.StagingDeclarations
        SET status = 'Processing',
            worker_id = ?,
            claimed_at = GETUTCDATE(),
            version = version + 1
        OUTPUT INSERTED.id, INSERTED.declaration_type, INSERTED.payload_json
        WHERE status = 'Queued'
          AND retry_count < max_retries
    """, [BATCH_SIZE, WORKER_ID])
    rows = cursor.fetchall()
    conn.commit()
    return rows


def process_declaration(session, api_url, dec_id, dec_type, payload_json, conn):
    """Call TSS API to create the declaration and log the result."""
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as e:
        mark_failed(conn, dec_id, f"Invalid JSON payload: {e}")
        return

    # Determine which API resource to call
    if dec_type == 'ENS_HEADER':
        resource = 'headers'
        payload['op_type'] = 'create'
        payload['declaration_number'] = ''
    else:
        mark_failed(conn, dec_id, f"Unknown declaration type: {dec_type}")
        return

    # Call TSS API
    url = f"{api_url}/{resource}"
    t0 = time.time()
    try:
        r = session.post(url, json=payload, timeout=30)
        duration = int((time.time() - t0) * 1000)

        body = r.json() if r.status_code == 200 else {}
        result = body.get('result', {})
        success = result.get('process_message', '') == 'SUCCESS'
        ext_ref = result.get('reference', '')
        status_msg = result.get('status', '')

        # Log the API call
        log_api_call(conn, dec_id, 'CREATE_HEADER', 'POST', url,
                     json.dumps(payload)[:4000], r.status_code, status_msg,
                     result.get('process_message', ''), r.text[:4000], duration,
                     None if success else r.text[:2000])

        if success and ext_ref:
            # Mark as Success + store ENS reference
            cursor = tenant_aware_cursor(conn.cursor())
            cursor.execute("""
                UPDATE BKD.StagingDeclarations
                SET status = 'Success',
                    external_ref = ?,
                    api_response_json = ?,
                    completed_at = GETUTCDATE(),
                    updated_at = GETUTCDATE()
                WHERE id = ?
            """, [ext_ref, r.text[:4000], dec_id])
            conn.commit()

            # Add to polling tracker
            cursor.execute("""
                INSERT INTO BKD.PollingTracker
                    (staging_id, external_ref, resource_type, fields_to_poll, target_status)
                VALUES (?, ?, 'headers', 'status,error_message', 'Authorised for Movement')
            """, [dec_id, ext_ref])
            conn.commit()

            logger.info(f"SUCCESS: #{dec_id} -> {ext_ref} ({duration}ms)")
        else:
            error_msg = result.get('process_message', r.text[:500])
            mark_failed(conn, dec_id, error_msg, r.text[:4000])
            logger.warning(f"FAILED: #{dec_id} -> {error_msg}")

    except Exception as e:
        duration = int((time.time() - t0) * 1000)
        mark_failed(conn, dec_id, str(e)[:500])
        log_api_call(conn, dec_id, 'CREATE_HEADER', 'POST', url,
                     json.dumps(payload)[:4000], 0, 'error', str(e)[:500],
                     '', duration, str(e)[:2000])
        logger.error(f"ERROR: #{dec_id} -> {e}")

    time.sleep(RATE_LIMIT)


def mark_failed(conn, dec_id, error_msg, response_json=None):
    cursor = tenant_aware_cursor(conn.cursor())
    cursor.execute("""
        UPDATE BKD.StagingDeclarations
        SET status = 'Failed',
            error_message = ?,
            api_response_json = ?,
            retry_count = retry_count + 1,
            updated_at = GETUTCDATE()
        WHERE id = ?
    """, [error_msg[:2000], response_json, dec_id])
    conn.commit()


def log_api_call(conn, staging_id, call_type, method, url, request_payload,
                 http_status, response_status, response_message, response_json,
                 duration_ms, error_detail):
    cursor = tenant_aware_cursor(conn.cursor())
    cursor.execute("""
        INSERT INTO BKD.ApiCallLog
            (staging_id, call_type, http_method, url, request_payload,
             http_status, response_status, response_message, response_json,
             duration_ms, error_detail)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, [staging_id, call_type, method, url[:500], request_payload,
          http_status, response_status[:50] if response_status else None,
          response_message[:500] if response_message else None,
          response_json[:4000] if response_json else None,
          duration_ms, error_detail])
    conn.commit()


def main():
    logger.info(f"Process Queue started (worker={WORKER_ID})")

    conn = get_connection()
    session, api_url = get_api_session()

    # Claim batch
    batch = claim_batch(conn)
    if not batch:
        logger.info("No queued declarations; clean exit.")
        conn.close()
        sys.exit(0)

    logger.info(f"Claimed {len(batch)} declarations for processing")

    for row in batch:
        dec_id, dec_type, payload_json = row
        process_declaration(session, api_url, dec_id, dec_type, payload_json, conn)

    conn.close()
    logger.info("Process Queue complete.")
    sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logger.exception("Fatal error in process_queue")
        sys.exit(1)
