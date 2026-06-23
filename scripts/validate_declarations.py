"""
NOT FOR PRD: reads/writes BKD.StagingDeclarations removed by migration 078.
             Use STG.BKD_* or ING.BKD_* for new pipeline work.

Synovia Flow -- ENS Header Validation Job
Validates all local draft/review records in BKD.StagingDeclarations.
Updates status to 'Validated' or 'Validation_Error' with clear error messages.

Usage:
    python scripts/validate_declarations.py
"""
import os, sys, json
import pyodbc

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string
from app.tenant import tenant_aware_cursor
from app.ens_validation import load_choice_values, validate_ens_payload


def validate_record(payload, cv):
    """Backward-compatible alias used by older ENS validation callers."""
    return validate_ens_payload(payload, cv)


def get_connection():
    from dotenv import load_dotenv
    load_dotenv()
    return pyodbc.connect(build_connection_string(timeout=30), autocommit=False)

def main():
    print("Synovia Flow -- ENS Header Validation Job")
    print("=" * 50)

    conn = get_connection()
    cursor = tenant_aware_cursor(conn.cursor())

    # Load choice values for validation
    print("Loading choice values...")
    cv = load_choice_values(cursor)
    for k, v in cv.items():
        print(f"  {k}: {len(v)} values")

    date_filter = os.environ.get('ENS_DECLARATIONS_DATE', 'today')
    if date_filter == 'all':
        date_clause = ''
        date_params = []
        print("Scope: all dates")
    else:
        date_clause = "AND CAST(created_at AS DATE) = CAST(GETUTCDATE() AS DATE)"
        date_params = []
        print("Scope: today only (set ENS_DECLARATIONS_DATE=all to override)")

    # Get local draft/review records
    cursor.execute(f"""
        SELECT id, payload_json
        FROM BKD.StagingDeclarations
        WHERE status IN ('Inserted', 'PENDING_REVIEW', 'PENDING REVIEW', 'Validation_Error')
          {date_clause}
        ORDER BY created_at
    """, date_params)
    records = cursor.fetchall()

    if not records:
        print("\nNo records to validate.")
        conn.close()
        return

    print(f"\nValidating {len(records)} records...")

    validated = 0
    failed = 0

    for row in records:
        dec_id, payload_json = row

        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            cursor.execute("""
                UPDATE BKD.StagingDeclarations
                SET status = 'Validation_Error',
                    error_message = 'Invalid JSON payload',
                    updated_at = GETUTCDATE()
                WHERE id = ?
            """, [dec_id])
            conn.commit()
            failed += 1
            print(f"  #{dec_id}: INVALID JSON")
            continue

        errors = validate_ens_payload(payload, cv)

        if errors:
            error_text = ' | '.join(errors)
            cursor.execute("""
                UPDATE BKD.StagingDeclarations
                SET status = 'Validation_Error',
                    error_message = ?,
                    updated_at = GETUTCDATE()
                WHERE id = ?
            """, [error_text[:4000], dec_id])
            conn.commit()
            failed += 1
            print(f"  #{dec_id}: FAILED ({len(errors)} errors)")
            for e in errors:
                print(f"    - {e}")
        else:
            cursor.execute("""
                UPDATE BKD.StagingDeclarations
                SET status = 'Validated',
                    error_message = NULL,
                    updated_at = GETUTCDATE()
                WHERE id = ?
            """, [dec_id])
            conn.commit()
            validated += 1
            print(f"  #{dec_id}: PASSED")

    print(f"\nDone: {validated} validated, {failed} failed out of {len(records)}")
    conn.close()
    sys.exit(1 if failed > 0 else 0)


if __name__ == '__main__':
    main()
