"""
NOT FOR PRD: reads/writes legacy BKD.Staging* tables removed by migration 078. Do not run against Fusion_TSS_Automation_PRD.

Synovia Flow — Pipeline Validator (Consignments + Goods + Sup Decs)
Validates PENDING records across all staging tables.
Updates to VALIDATED or FAILED with error messages.

Usage:
    python scripts/validate_pipeline.py
"""
import os, sys, re
from datetime import datetime, timezone
import pyodbc
try:
    from _console_output import configure_console_output
except ModuleNotFoundError:
    from scripts._console_output import configure_console_output

configure_console_output()

# EORI format: 2-letter country code + 6-15 digits (GB/XI/IE/FR etc.)
_EORI_RE = re.compile(r'^[A-Z]{2}\d{6,15}$')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.db_connection import build_connection_string
from app.tenant import get_tenant, tenant_aware_cursor
from app.pipeline_validation import (
    build_validation_choice_sets,
    strict_masterdata_validation_enabled,
    validate_consignment as shared_validate_consignment,
    validate_goods as shared_validate_goods,
    validate_supdec as shared_validate_supdec,
)

def get_connection():
    from dotenv import load_dotenv
    load_dotenv()
    return pyodbc.connect(build_connection_string(timeout=30), autocommit=False)

def load_cv(cursor, table, col='value'):
    try:
        cursor.execute(f"SELECT [{col}] FROM TSS.[{table}]")
        return set(r[0] for r in cursor.fetchall())
    except:
        return set()

S = get_tenant()["schema"]


def csv_int_ids(env_name):
    raw = (os.environ.get(env_name) or '').strip()
    if not raw:
        return []
    ids = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


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

# ══════════════════════════════════════════════════════════════
#  CONSIGNMENT VALIDATION
# ══════════════════════════════════════════════════════════════

def validate_consignment(row, cv):
    errors = []
    def chk(field, label):
        if not (row.get(field) or '').strip():
            errors.append(f"REQUIRED: {label}")

    # Truly required by TSS API
    chk('goods_description', 'Goods Description')
    chk('transport_document_number', 'Transport Document Number')
    chk('importer_eori', 'Importer EORI')
    chk('container_indicator', 'Container Indicator (0=Uncontainerised, 1=Containerised)')

    # EORI format checks
    for field, label in [
        ('importer_eori', 'Importer EORI'),
        ('exporter_eori', 'Exporter EORI'),
        ('consignor_eori', 'Consignor EORI'),
        ('consignee_eori', 'Consignee EORI'),
    ]:
        val = (row.get(field) or '').strip()
        if val and not _EORI_RE.match(val):
            errors.append(f"FORMAT: {label} '{val}' is not a valid EORI (expected e.g. GB123456789000)")

    # Must link to an ENS header
    if not row.get('staging_ens_id'):
        errors.append("REQUIRED: Must link to an ENS Header (staging_ens_id)")

    # Controlled goods → needs domestic status
    if (row.get('controlled_goods') or '').strip() == 'yes':
        if not (row.get('goods_domestic_status') or '').strip():
            errors.append("REQUIRED: Goods Domestic Status required when controlled_goods=yes")

    # Length checks
    desc = (row.get('goods_description') or '').strip()
    if desc and len(desc) > 254:
        errors.append(f"LENGTH: Goods Description exceeds 254 chars ({len(desc)})")

    tdoc = (row.get('transport_document_number') or '').strip()
    if tdoc and len(tdoc) > 35:
        errors.append(f"LENGTH: Transport Doc exceeds 35 chars ({len(tdoc)})")

    container_indicator = (row.get('container_indicator') or '').strip()
    if container_indicator and container_indicator not in {'0', '1'}:
        errors.append("INVALID: Container Indicator must be 0 or 1")

    return errors


# ══════════════════════════════════════════════════════════════
#  GOODS ITEM VALIDATION
# ══════════════════════════════════════════════════════════════

def validate_goods(row, cv):
    errors = []
    def chk(field, label):
        if not (row.get(field) or '').strip():
            errors.append(f"REQUIRED: {label}")

    chk('goods_description', 'Goods Description')
    chk('type_of_packages', 'Type of Packages')
    chk('package_marks', 'Package Marks')
    chk('controlled_goods', 'Controlled Goods (yes/no)')

    if not row.get('staging_cons_id'):
        errors.append("REQUIRED: Must link to a Consignment (staging_cons_id)")

    # Numeric checks
    try:
        gross = float(row.get('gross_mass_kg') or 0)
        if gross <= 0:
            errors.append("REQUIRED: Gross Mass KG must be > 0")
    except ValueError:
        errors.append("FORMAT: Gross Mass KG must be numeric")

    net = row.get('net_mass_kg')
    if net:
        try:
            net_val = float(net)
            if net_val > gross:
                errors.append("INVALID: Net mass cannot exceed gross mass")
        except ValueError:
            errors.append("FORMAT: Net Mass KG must be numeric")

    pkgs = row.get('number_of_packages')
    if not pkgs or int(pkgs or 0) < 1:
        errors.append("REQUIRED: Number of Packages must be >= 1")

    # Package type CV check — only enforce when the CV set is populated
    tp = (row.get('type_of_packages') or '').strip()
    pkg_cv = cv.get('type_of_package', set())
    if tp and pkg_cv and tp not in pkg_cv:
        errors.append(f"INVALID: Type of Packages '{tp}' not in allowed values")

    # Commodity code format
    cc = (row.get('commodity_code') or '').strip()
    if cc and len(cc) < 8:
        errors.append(f"FORMAT: Commodity Code must be at least 8 digits (got {len(cc)})")

    return errors


# ══════════════════════════════════════════════════════════════
#  SUPPLEMENTARY DECLARATION VALIDATION
# ══════════════════════════════════════════════════════════════

def validate_supdec(row, cv):
    errors = []
    def chk(field, label):
        if not (row.get(field) or '').strip():
            errors.append(f"REQUIRED: {label}")

    chk('declaration_choice', 'Declaration Choice (H1/H3/H4)')
    chk('incoterm', 'Incoterm')
    chk('delivery_location_country', 'Delivery Location Country')
    chk('delivery_location_town', 'Delivery Location Town')

    # CV checks
    dc = (row.get('declaration_choice') or '').strip()
    if dc and dc not in cv.get('sd_declaration_choice', set()):
        errors.append(f"INVALID: Declaration Choice '{dc}' not in allowed values")

    inc = (row.get('incoterm') or '').strip()
    if inc and inc not in cv.get('incoterm', set()):
        errors.append(f"INVALID: Incoterm '{inc}' not in allowed values")

    dlc = (row.get('delivery_location_country') or '').strip()
    if dlc and dlc not in cv.get('country', set()):
        errors.append(f"INVALID: Delivery Country '{dlc}' not in country list")

    return errors


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    print("Synovia Flow — Pipeline Validator")
    print("=" * 55)

    conn = get_connection()
    cur = tenant_aware_cursor(conn.cursor())

    # Load choice values
    print("Loading choice values...")
    cv = build_validation_choice_sets(cur)
    strict_party_checks = strict_masterdata_validation_enabled()
    print(f"Strict party/masterdata validation: {'ON' if strict_party_checks else 'OFF'}")

    total_ok = 0
    total_fail = 0
    filter_consignment_ids = csv_int_ids('VALIDATE_PIPELINE_CONSIGNMENT_IDS')
    filter_goods_ids = csv_int_ids('VALIDATE_PIPELINE_GOODS_IDS')
    filter_supdec_ids = csv_int_ids('VALIDATE_PIPELINE_SUPDEC_IDS')
    scoped_pipeline = bool(filter_consignment_ids or filter_goods_ids or filter_supdec_ids)
    if scoped_pipeline:
        print(
            "Scope: "
            f"consignments={','.join(str(i) for i in filter_consignment_ids) or '-'}; "
            f"goods={','.join(str(i) for i in filter_goods_ids) or '-'}; "
            f"supdecs={','.join(str(i) for i in filter_supdec_ids) or '-'}"
        )

    # ── Consignments ──
    cons_where = ["status IN ('PENDING', 'PENDING_REVIEW', 'PENDING REVIEW')"]
    cons_params = []
    if scoped_pipeline:
        if filter_consignment_ids:
            placeholders = ','.join('?' for _ in filter_consignment_ids)
            cons_where.append(f"staging_id IN ({placeholders})")
            cons_params.extend(filter_consignment_ids)
        else:
            cons_where.append("1 = 0")
    cur.execute(f"SELECT * FROM {S}.StagingConsignments WHERE {' AND '.join(cons_where)}", cons_params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"\nConsignments: {len(rows)} PENDING/PENDING_REVIEW")

    for row in rows:
        errs = shared_validate_consignment(row, cv, cur, strict_party_checks=strict_party_checks)
        sid = row['staging_id']
        if errs:
            cur.execute(f"UPDATE {S}.StagingConsignments SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME() WHERE staging_id=?",
                        [' | '.join(errs)[:4000], sid])
            conn.commit()
            total_fail += 1
            print(f"  #{sid}: FAILED ({len(errs)} errors)")
        else:
            cur.execute(f"UPDATE {S}.StagingConsignments SET status='VALIDATED', error_message=NULL, updated_at=SYSUTCDATETIME() WHERE staging_id=?", [sid])
            conn.commit()
            total_ok += 1
            print(f"  #{sid}: VALIDATED")

    # ── Goods Items ──
    goods_where = ["status IN ('PENDING', 'PENDING_REVIEW', 'PENDING REVIEW')"]
    goods_params = []
    if scoped_pipeline:
        scope_parts = []
        if filter_goods_ids:
            placeholders = ','.join('?' for _ in filter_goods_ids)
            scope_parts.append(f"staging_id IN ({placeholders})")
            goods_params.extend(filter_goods_ids)
        if filter_consignment_ids:
            placeholders = ','.join('?' for _ in filter_consignment_ids)
            scope_parts.append(f"staging_cons_id IN ({placeholders})")
            goods_params.extend(filter_consignment_ids)
        goods_where.append('(' + ' OR '.join(scope_parts) + ')' if scope_parts else '1 = 0')
    cur.execute(f"SELECT * FROM {S}.StagingGoodsItems WHERE {' AND '.join(goods_where)}", goods_params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"\nGoods Items: {len(rows)} PENDING/PENDING_REVIEW")

    for row in rows:
        errs = shared_validate_goods(row, cv)
        sid = row['staging_id']
        if errs:
            cur.execute(f"UPDATE {S}.StagingGoodsItems SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME() WHERE staging_id=?",
                        [' | '.join(errs)[:4000], sid])
            conn.commit()
            total_fail += 1
            print(f"  #{sid}: FAILED ({len(errs)} errors)")
        else:
            cur.execute(f"UPDATE {S}.StagingGoodsItems SET status='VALIDATED', error_message=NULL, updated_at=SYSUTCDATETIME() WHERE staging_id=?", [sid])
            conn.commit()
            total_ok += 1
            print(f"  #{sid}: VALIDATED")

    # ── Sup Decs ──
    try:
        header_id_col = first_existing(cur, 'StagingSupDecHeaders', 'staging_id', 'id')
        if not header_id_col:
            raise RuntimeError("Missing SDI header id column")
        supdec_where = ["status IN ('PENDING', 'PENDING_REVIEW', 'PENDING REVIEW')"]
        supdec_params = []
        if scoped_pipeline:
            if filter_supdec_ids:
                placeholders = ','.join('?' for _ in filter_supdec_ids)
                supdec_where.append(f"{header_id_col} IN ({placeholders})")
                supdec_params.extend(filter_supdec_ids)
            else:
                supdec_where.append("1 = 0")
        cur.execute(f"SELECT * FROM {S}.StagingSupDecHeaders WHERE {' AND '.join(supdec_where)}", supdec_params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        print(f"\nSup Decs: {len(rows)} PENDING/PENDING_REVIEW")

        for row in rows:
            errs = shared_validate_supdec(row, cv)
            sid = row[header_id_col]
            if errs:
                cur.execute(f"UPDATE {S}.StagingSupDecHeaders SET status='FAILED', error_message=?, updated_at=SYSUTCDATETIME() WHERE {header_id_col}=?",
                            [' | '.join(errs)[:4000], sid])
                conn.commit()
                total_fail += 1
                print(f"  #{sid}: FAILED ({len(errs)} errors)")
            else:
                cur.execute(f"UPDATE {S}.StagingSupDecHeaders SET status='VALIDATED', error_message=NULL, updated_at=SYSUTCDATETIME() WHERE {header_id_col}=?", [sid])
                conn.commit()
                total_ok += 1
                print(f"  #{sid}: VALIDATED")
    except Exception as e:
        print(f"\nSup Decs: skipped ({e})")

    print(f"\n{'=' * 55}")
    print(f"Validated: {total_ok}  |  Failed: {total_fail}  |  Total: {total_ok + total_fail}")
    conn.close()
    sys.exit(1 if total_fail > 0 else 0)

if __name__ == '__main__':
    main()
