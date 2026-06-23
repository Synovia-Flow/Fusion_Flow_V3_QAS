"""
Synovia Flow — GMR Submitter
Takes PENDING GMRs, calls TSS API to create + submit to GVMS.
TSS Rule: create MUST be immediately followed by submit in same run.
Updates status to SUBMITTED or FAILED.

Usage:
    python scripts/submit_gmr.py
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

from app.tss_api import build_tss_api_url, resolve_tss_settings
from app.tenant import get_tenant, tenant_aware_cursor
from config.db_connection import build_connection_string

RATE_LIMIT = 0.3
TIMEOUT = 30
S = get_tenant()["schema"]
AUTHORISED_STATUSES = ('AUTHORISED_FOR_MOVEMENT', 'Authorised for Movement')


def get_connection():
    from dotenv import load_dotenv
    load_dotenv()
    return pyodbc.connect(build_connection_string(timeout=30), autocommit=False)


def get_api_session():
    resolved = resolve_tss_settings()
    if resolved.get('demo_enabled'):
        from app.tss_api import build_cfg_client
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
    session.headers.update({
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Basic {b64}',
    })
    return session, api_url


def api_post(session, url, payload):
    t0 = time.time()
    try:
        r = session.post(url, json=payload, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        result = body.get('result', {})
        success = r.status_code == 200 and result.get('process_message') == 'SUCCESS'
        return {
            'success': success,
            'http_status': r.status_code,
            'gmr_id': result.get('gmr_id') or result.get('reference', ''),
            'gvms_status': result.get('status', ''),
            'message': result.get('process_message') or result.get('error_message') or r.text[:200],
            'duration_ms': ms,
            'raw': r.text[:4000],
        }
    except Exception as e:
        return {
            'success': False, 'http_status': 0, 'gmr_id': '',
            'gvms_status': '', 'message': str(e)[:500],
            'duration_ms': int((time.time() - t0) * 1000), 'raw': '',
        }
    finally:
        time.sleep(RATE_LIMIT)


def _count_authorised_consignments(consignments):
    rows = list(consignments or [])
    auth_count = sum(1 for row in rows if row[2] in AUTHORISED_STATUSES)
    return len(rows), auth_count


def gmr_submit_readiness_from_consignment_rows(consignments):
    cons_count, auth_count = _count_authorised_consignments(consignments)

    if cons_count < 1:
        return False, "no consignments linked to ENS header"
    if auth_count == cons_count:
        return True, f"{auth_count}/{cons_count} consignments authorised for movement"
    return False, f"{auth_count}/{cons_count} consignments authorised for movement"


def resolve_staging_ens_id(cur, staging_ens_id, ens_ref):
    if staging_ens_id:
        return staging_ens_id
    row = cur.execute(f"""
        SELECT TOP 1 staging_id
        FROM {S}.StagingEnsHeaders
        WHERE ens_reference = ?
        ORDER BY staging_id DESC
    """, [ens_ref]).fetchone()
    return row[0] if row else None


def fetch_gmr_consignment_rows(cur, staging_ens_id):
    if not staging_ens_id:
        return []
    return cur.execute(f"""
        SELECT c.staging_id, c.dec_reference, c.tss_status, c.status,
               COUNT(g.staging_id) AS goods_count,
               SUM(CASE WHEN g.status = 'CREATED' THEN 1 ELSE 0 END) AS goods_ready_count
        FROM {S}.StagingConsignments c
        LEFT JOIN {S}.StagingGoodsItems g ON g.staging_cons_id = c.staging_id
        WHERE c.staging_ens_id = ?
        GROUP BY c.staging_id, c.dec_reference, c.tss_status, c.status
    """, [staging_ens_id]).fetchall()


def main():
    print("Synovia Flow — GMR Submitter")
    print("=" * 55)

    conn = get_connection()
    cur = tenant_aware_cursor(conn.cursor())
    session, api_url = get_api_session()
    gmr_url = f"{api_url}/gvms_gmr"

    cur.execute(f"""
        SELECT staging_id, staging_ens_id, ens_reference, vehicle_registration, trailer_registration, label
        FROM {S}.StagingGmrs
        WHERE status = 'PENDING' AND retry_count < max_retries
        ORDER BY staging_id
    """)
    gmrs = cur.fetchall()
    print(f"GMRs to submit: {len(gmrs)}")

    submitted = 0
    failed = 0
    skipped = 0

    for sid, staging_ens_id, ens_ref, vehicle_reg, trailer_reg, label in gmrs:
        print(f"\n  #{sid} {label or ens_ref}")

        if not ens_ref and staging_ens_id:
            row = cur.execute(
                f"SELECT ens_reference FROM {S}.StagingEnsHeaders WHERE staging_id=?",
                [staging_ens_id]
            ).fetchone()
            if row:
                ens_ref = row[0]
            if ens_ref:
                cur.execute(
                    f"UPDATE {S}.StagingGmrs SET ens_reference=? WHERE staging_id=?",
                    [ens_ref, sid])
                conn.commit()
                print(f"    Resolved ens_reference from staging_ens_id: {ens_ref}")

        if not ens_ref:
            print(f"    SKIP — no ENS reference")
            cur.execute(
                f"UPDATE {S}.StagingGmrs SET status='FAILED', error_message=? WHERE staging_id=?",
                ["No ENS reference set on GMR", sid])
            conn.commit()
            failed += 1
            continue

        # Do not call GVMS until every linked DEC is authorised in TSS.
        resolved_ens_id = resolve_staging_ens_id(cur, staging_ens_id, ens_ref)
        consignment_rows = fetch_gmr_consignment_rows(cur, resolved_ens_id)
        ready, readiness_message = gmr_submit_readiness_from_consignment_rows(consignment_rows)
        if not ready:
            print(f"    SKIP - awaiting Route A authorisation ({readiness_message})")
            cur.execute(f"""
                UPDATE {S}.StagingGmrs SET
                    error_message = NULL,
                    updated_at = SYSUTCDATETIME()
                WHERE staging_id = ?
            """, [sid])
            conn.commit()
            skipped += 1
            continue

        # Step 1: CREATE
        create_payload = {
            "op_type": "create",
            "declaration_header_number": ens_ref,
        }
        if vehicle_reg:
            create_payload["vehicle_registration"] = vehicle_reg
        if trailer_reg:
            create_payload["trailer_registration"] = trailer_reg

        print(f"    POST create → {ens_ref}")
        create_result = api_post(session, gmr_url, create_payload)
        print(f"    HTTP {create_result['http_status']} — {create_result['message'][:80]}")

        if not create_result['success']:
            cur.execute(f"""
                UPDATE {S}.StagingGmrs SET
                    status = 'FAILED',
                    error_message = ?,
                    retry_count = retry_count + 1,
                    updated_at = SYSUTCDATETIME()
                WHERE staging_id = ?
            """, [f"CREATE failed: {create_result['message'][:500]}", sid])
            conn.commit()
            failed += 1
            continue

        gmr_id = create_result.get('gmr_id') or ''

        # ── Step 2: SUBMIT (mandatory immediately after create) ──
        submit_payload = {
            "op_type": "submit",
            "declaration_header_number": ens_ref,
        }

        print(f"    POST submit → {ens_ref}")
        submit_result = api_post(session, gmr_url, submit_payload)
        print(f"    HTTP {submit_result['http_status']} — {submit_result['message'][:80]}")

        if submit_result['success']:
            final_gmr_id = submit_result.get('gmr_id') or gmr_id
            final_status = submit_result.get('gvms_status') or 'Open'
            cur.execute(f"""
                UPDATE {S}.StagingGmrs SET
                    status = 'SUBMITTED',
                    gmr_id = ?,
                    gvms_status = ?,
                    error_message = NULL,
                    submitted_at = SYSUTCDATETIME(),
                    updated_at = SYSUTCDATETIME()
                WHERE staging_id = ?
            """, [final_gmr_id or None, final_status, sid])
            conn.commit()
            submitted += 1
            print(f"    OK — GMR ID: {final_gmr_id}  GVMS: {final_status}")
        else:
            # Create succeeded but submit failed — still capture the gmr_id
            cur.execute(f"""
                UPDATE {S}.StagingGmrs SET
                    status = 'FAILED',
                    gmr_id = ?,
                    error_message = ?,
                    retry_count = retry_count + 1,
                    updated_at = SYSUTCDATETIME()
                WHERE staging_id = ?
            """, [gmr_id or None,
                  f"SUBMIT failed: {submit_result['message'][:500]}", sid])
            conn.commit()
            failed += 1

    print(f"\n{'=' * 55}")
    print(f"Submitted: {submitted}  |  Failed: {failed}  |  Skipped: {skipped}")
    conn.close()
    sys.exit(1 if failed > 0 else 0)


if __name__ == '__main__':
    main()
