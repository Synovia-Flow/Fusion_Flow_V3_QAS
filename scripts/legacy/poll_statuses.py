"""
Legacy PollingTracker status worker.

Reads the TSS API for status updates and writes the changes back to the
dashboard-facing staging tables.
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
logger = logging.getLogger('poll_statuses')

RATE_LIMIT = 0.25

# Resource types that map to SFD records (supplementary fiscal declarations)
SFD_RESOURCE_TYPES = {'supplementary_fiscal_declaration', 'sfd', 'sfds'}

# TSS resource type for SDI (supplementary declaration items / import declarations)
SDI_RESOURCE_TYPE = 'supplementary_declaration'


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
        'Authorization': f'Basic {b64}',
    })
    return session, api_url


def handle_sfd_authorised_for_movement(cursor, conn, staging_id, ext_ref, result, api_url):
    """
    Called when an SFD poll returns AUTHORISED_FOR_MOVEMENT.

    Responsibilities:
      1. Extract the SDI reference number from the API response
         (TSS typically returns it as 'supplementary_declaration_reference' or 'sd_reference'
          or inside a nested 'supplementary_declaration' object)
      2. Write the reference back to BKD.StagingSupDecHeaders.sup_dec_number
      3. Register the SDI in BKD.PollingTracker so polling can begin
    """
    # TSS versions have returned the SDI reference under several keys; keep the
    # fallback chain explicit so status polling survives minor response changes.
    sdi_ref = (
        result.get('supplementary_declaration_reference')
        or result.get('sd_reference')
        or result.get('supplementary_declaration', {}).get('reference')
        or result.get('supplementary_declaration', {}).get('sd_number')
        or result.get('reference')  # fallback: use the same ref
    )

    if not sdi_ref:
        logger.warning(
            'SFD %s AUTHORISED_FOR_MOVEMENT but no SDI reference found in response. '
            'Raw keys: %s', ext_ref, list(result.keys())
        )
        # Still mark SFD as authorised in staging table
        cursor.execute("""
            UPDATE BKD.StagingSupDecHeaders
            SET tss_status = 'AUTHORISED_FOR_MOVEMENT',
                updated_at = GETUTCDATE()
            WHERE staging_id = ?
        """, [staging_id])
        conn.commit()
        return

    logger.info(
        'SFD %s AUTHORISED_FOR_MOVEMENT; SDI reference: %s; writing back to StagingSupDecHeaders',
        ext_ref, sdi_ref
    )

    # 1. Write SDI reference back to StagingSupDecHeaders
    cursor.execute("""
        UPDATE BKD.StagingSupDecHeaders
        SET sup_dec_number = ?,
            tss_status = 'AUTHORISED_FOR_MOVEMENT',
            status = 'SUBMITTED',
            updated_at = GETUTCDATE()
        WHERE staging_id = ?
    """, [sdi_ref, staging_id])
    conn.commit()

    # 2. Check if SDI is already being polled (avoid duplicates)
    existing = cursor.execute("""
        SELECT id FROM BKD.PollingTracker
        WHERE external_ref = ? AND resource_type = ? AND active = 1
    """, [sdi_ref, SDI_RESOURCE_TYPE]).fetchone()

    if existing:
        logger.info('SDI %s already has an active poll; skipping registration', sdi_ref)
        return

    # 3. Register SDI in PollingTracker so polling begins on next cron run
    cursor.execute("""
        INSERT INTO BKD.PollingTracker
            (staging_id, external_ref, resource_type, fields_to_poll,
             target_status, last_status, active, poll_count, max_polls,
             created_at, last_polled_at)
        VALUES
            (?, ?, ?, 'status,error_message',
             'ACCEPTED', 'AUTHORISED_FOR_MOVEMENT', 1, 0, 200,
             GETUTCDATE(), NULL)
    """, [staging_id, sdi_ref, SDI_RESOURCE_TYPE])
    conn.commit()

    logger.info(
        'SDI %s registered in PollingTracker (staging_id=%s, resource_type=%s)',
        sdi_ref, staging_id, SDI_RESOURCE_TYPE
    )


def update_staging_table(cursor, conn, resource_type, staging_id, new_status, error_msg):
    """Update the correct staging table based on resource_type."""
    if resource_type.lower() in SFD_RESOURCE_TYPES:
        cursor.execute("""
            UPDATE BKD.StagingSupDecHeaders
            SET tss_status = ?,
                error_message = CASE WHEN ? != '' THEN ? ELSE error_message END,
                updated_at = GETUTCDATE()
            WHERE staging_id = ?
        """, [new_status, error_msg, error_msg, staging_id])
    elif resource_type.lower() == SDI_RESOURCE_TYPE:
        cursor.execute("""
            UPDATE BKD.StagingSupDecHeaders
            SET tss_status = ?,
                status = CASE
                    WHEN ? IN ('ACCEPTED','CLEARED') THEN 'ACCEPTED'
                    WHEN ? IN ('REJECTED','FAILED') THEN 'FAILED'
                    ELSE status
                END,
                error_message = CASE WHEN ? != '' THEN ? ELSE error_message END,
                updated_at = GETUTCDATE()
            WHERE staging_id = ?
        """, [new_status, new_status, new_status, error_msg, error_msg, staging_id])
    else:
        # Default: StagingDeclarations (ENS)
        cursor.execute("""
            UPDATE BKD.StagingDeclarations
            SET external_status = ?,
                error_message = CASE WHEN ? != '' THEN ? ELSE error_message END,
                updated_at = GETUTCDATE()
            WHERE id = ?
        """, [new_status, error_msg, error_msg, staging_id])
    conn.commit()


def main():
    logger.info("Status Poller started")

    conn = get_connection()
    session, api_url = get_api_session()
    cursor = tenant_aware_cursor(conn.cursor())

    # Get all active polling targets
    cursor.execute("""
        SELECT id, staging_id, external_ref, resource_type,
               fields_to_poll, target_status, last_status, poll_count, max_polls
        FROM BKD.PollingTracker
        WHERE active = 1 AND poll_count < max_polls
        ORDER BY last_polled_at ASC
    """)
    targets = cursor.fetchall()

    if not targets:
        logger.info("No active polling targets; clean exit.")
        conn.close()
        sys.exit(0)

    logger.info(f"Polling {len(targets)} active declarations")

    for row in targets:
        poll_id, staging_id, ext_ref, resource_type, fields, target_status, last_status, poll_count, max_polls = row

        url = f"{api_url}/{resource_type}"
        params = {'reference': ext_ref, 'fields': fields}

        t0 = time.time()
        try:
            r = session.get(url, params=params, timeout=30)
            duration = int((time.time() - t0) * 1000)

            if r.status_code == 200:
                result = r.json().get('result', {})
                new_status = result.get('status', '')
                error_msg = result.get('error_message', '') or ''

                status_changed = new_status != last_status

                # Update polling tracker
                cursor.execute("""
                    UPDATE BKD.PollingTracker
                    SET last_status = ?,
                        poll_count = poll_count + 1,
                        last_polled_at = GETUTCDATE()
                    WHERE id = ?
                """, [new_status, poll_id])

                # Update the appropriate staging table
                update_staging_table(cursor, conn, resource_type, staging_id, new_status, error_msg)

                # Critical SFD Authorised for Movement callback.
                # When an SFD is authorised for movement, TSS returns the SDI
                # reference number. We must write this back to the staging record
                # before SDI polling can begin.
                if (resource_type.lower() in SFD_RESOURCE_TYPES
                        and new_status == 'AUTHORISED_FOR_MOVEMENT'):
                    logger.info('SFD AUTHORISED_FOR_MOVEMENT callback triggered for %s', ext_ref)
                    handle_sfd_authorised_for_movement(
                        cursor, conn, staging_id, ext_ref, result, api_url
                    )
                    # Deactivate the SFD poll once the related SDI tracking is registered.
                    cursor.execute("UPDATE BKD.PollingTracker SET active = 0 WHERE id = ?", [poll_id])
                    conn.commit()
                    logger.info('SFD poll %d deactivated after AUTHORISED_FOR_MOVEMENT', poll_id)

                # Deactivate polling when a non-SFD target reaches its desired status.
                elif new_status == target_status:
                    cursor.execute("UPDATE BKD.PollingTracker SET active = 0 WHERE id = ?", [poll_id])
                    conn.commit()
                    logger.info(f"TARGET REACHED: {ext_ref} -> {new_status}")

                # Check for terminal error states
                elif new_status in ('Cancelled', 'Do Not Load', 'REJECTED', 'FAILED'):
                    cursor.execute("UPDATE BKD.PollingTracker SET active = 0 WHERE id = ?", [poll_id])
                    conn.commit()
                    if new_status == 'Do Not Load':
                        logger.critical(f"DO NOT LOAD: {ext_ref}; IMMEDIATE ATTENTION REQUIRED")
                    else:
                        logger.warning(f"TERMINAL: {ext_ref} -> {new_status}")

                elif status_changed:
                    logger.info(f"STATUS CHANGE: {ext_ref}: {last_status} -> {new_status} ({duration}ms)")
                else:
                    logger.debug(f"No change: {ext_ref} still {new_status} ({duration}ms)")

                # Log API call
                cursor.execute("""
                    INSERT INTO BKD.ApiCallLog
                        (staging_id, call_type, http_method, url, http_status,
                         response_status, response_json, duration_ms)
                    VALUES (?, 'POLL_STATUS', 'GET', ?, ?, ?, ?, ?)
                """, [staging_id, f"{url}?reference={ext_ref}", r.status_code,
                      new_status, r.text[:4000], duration])
                conn.commit()

            else:
                logger.warning(f"Poll failed: {ext_ref} -> HTTP {r.status_code}")
                cursor.execute("""
                    UPDATE BKD.PollingTracker
                    SET poll_count = poll_count + 1, last_polled_at = GETUTCDATE()
                    WHERE id = ?
                """, [poll_id])
                conn.commit()

        except Exception as e:
            logger.error(f"Poll error: {ext_ref} -> {e}")
            cursor.execute("""
                UPDATE BKD.PollingTracker
                SET poll_count = poll_count + 1, last_polled_at = GETUTCDATE()
                WHERE id = ?
            """, [poll_id])
            conn.commit()

        time.sleep(RATE_LIMIT)

    conn.close()
    logger.info("Status Poller complete.")
    sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logger.exception("Fatal error in poll_statuses")
        sys.exit(1)
