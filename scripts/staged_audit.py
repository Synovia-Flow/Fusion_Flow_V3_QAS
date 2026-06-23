"""
NOT FOR PRD: reads BKD.Staging* tables removed by migration 078.
             Use STG.BKD_* or ING.BKD_* for new pipeline work.

Synovia Flow — Staged Records Comparison Audit (Offline)
=========================================================
Compares every BKD.StagingEnsHeaders and BKD.StagingConsignments record
that has a TSS reference against the live TSS API.

For each record:
  - FOUND     → shows field-by-field comparison (local vs TSS)
  - NOT FOUND → flags as orphan and offers interactive Y/N delete

Cascade delete:
  ENS Header  → deletes linked StagingConsignments first
  Consignment → deletes linked StagingGoodsItems first

Usage:
    python scripts/staged_audit.py
    python scripts/staged_audit.py --dry-run          # report only, no deletes
    python scripts/staged_audit.py --yes              # auto-confirm all deletes
    python scripts/staged_audit.py --section ens      # ENS headers only
    python scripts/staged_audit.py --section cons     # consignments only
    python scripts/staged_audit.py --config path.ini  # explicit config file
    python scripts/staged_audit.py --db-section PRD_Database
"""
import os, sys, time, base64, configparser, argparse
from datetime import datetime, timezone
import pyodbc, requests
try:
    from _console_output import configure_console_output
except ModuleNotFoundError:
    from scripts._console_output import configure_console_output

configure_console_output()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── ANSI colours ─────────────────────────────────────────────────────────────
GREEN  = '\033[92m'
RED    = '\033[91m'
YELLOW = '\033[93m'
CYAN   = '\033[96m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
RESET  = '\033[0m'

def ok(s):   return f'{GREEN}{s}{RESET}'
def err(s):  return f'{RED}{s}{RESET}'
def warn(s): return f'{YELLOW}{s}{RESET}'
def info(s): return f'{CYAN}{s}{RESET}'
def bold(s): return f'{BOLD}{s}{RESET}'
def dim(s):  return f'{DIM}{s}{RESET}'

# ── Config auto-discovery ─────────────────────────────────────────────────────
_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_AUTO_SEARCH  = [
    os.path.join(_PROJECT_ROOT, 'config.ini'),
    os.path.join(_SCRIPT_DIR,   'config.ini'),
    os.path.join(os.getcwd(),   'config.ini'),
]

def _find_config(explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for p in _AUTO_SEARCH:
        if os.path.isfile(p):
            return p
    return None


# ── Database connection ───────────────────────────────────────────────────────
def get_connection(config_path=None, db_section='QAS_Database'):
    cfg_path = _find_config(config_path)
    if cfg_path:
        cfg = configparser.ConfigParser()
        cfg.read(cfg_path)
        if db_section in cfg:
            s = cfg[db_section]
            drv = '{ODBC Driver 17 for SQL Server}' if os.name == 'nt' else '{ODBC Driver 18 for SQL Server}'
            return pyodbc.connect(
                f"DRIVER={drv};"
                f"SERVER={s['server']};"
                f"DATABASE={s.get('database', 'Fusion_TSS')};"
                f"UID={s['username']};"
                f"PWD={s['password']};"
                "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;",
                autocommit=False
            )
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    drv = '{ODBC Driver 17 for SQL Server}' if os.name == 'nt' else '{ODBC Driver 18 for SQL Server}'
    return pyodbc.connect(
        f"DRIVER={drv};"
        f"SERVER={os.environ['AZURE_SQL_SERVER']};"
        f"DATABASE={os.environ.get('AZURE_SQL_DATABASE', 'Fusion_TSS')};"
        f"UID={os.environ['AZURE_SQL_USERNAME']};"
        f"PWD={os.environ['AZURE_SQL_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;",
        autocommit=False
    )


# ── TSS API session ───────────────────────────────────────────────────────────
def get_api_session(config_path=None):
    cfg_path = _find_config(config_path)
    if cfg_path:
        cfg = configparser.ConfigParser()
        cfg.read(cfg_path)
        if 'tss_api' in cfg:
            s = cfg['tss_api']
            base_url = s['base_url'].rstrip('/')
            username = s['username']
            password = s['password']
        else:
            raise ValueError(f"[tss_api] section not found in {cfg_path}")
    else:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        base_url = os.environ.get('TSS_API_BASE_URL', '').rstrip('/')
        username = os.environ.get('TSS_API_USERNAME', '')
        password = os.environ.get('TSS_API_PASSWORD', '')

    api_url = f"{base_url}/x_fhmrc_tss_api/v1/tss_api"
    b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
    session = requests.Session()
    session.headers.update({'Accept': 'application/json',
                             'Authorization': f'Basic {b64}'})
    return session, api_url


RATE_LIMIT = 0.25
TIMEOUT    = 30


def api_get(session, url, params):
    t0 = time.time()
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        time.sleep(RATE_LIMIT)

        if r.status_code == 200:
            body   = r.json()
            result = body.get('result', body)
            if not result:                          # None, {}, []
                return {'found': False, 'code': 200, 'data': None, 'ms': ms}
            # API may return a list for some endpoints
            data = result[0] if isinstance(result, list) else result
            return {'found': True,  'code': 200, 'data': data,  'ms': ms}

        if r.status_code in (404, 400):
            return {'found': False, 'code': r.status_code, 'data': None, 'ms': ms}

        return {'found': None, 'code': r.status_code, 'data': None,
                'ms': ms, 'error': r.text[:300]}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {'found': None, 'code': 0, 'data': None,
                'ms': ms, 'error': str(e)[:300]}


# ── Field comparison maps  (local_col, tss_col, display_label) ───────────────
ENS_FIELDS = 'status,movement_type,arrival_date_time,arrival_port,' \
             'carrier_name,carrier_eori,vehicle_registration,trailer_registration'

ENS_COMPARE = [
    ('tss_status',           'status',               'TSS Status'),
    ('movement_type',        'movement_type',         'Movement Type'),
    ('arrival_port',         'arrival_port',          'Arrival Port'),
    ('carrier_name',         'carrier_name',          'Carrier Name'),
    ('carrier_eori',         'carrier_eori',          'Carrier EORI'),
    ('vehicle_registration', 'vehicle_registration',  'Vehicle Reg'),
    ('trailer_registration', 'trailer_registration',  'Trailer Reg'),
]

CONS_FIELDS = 'status,goods_description,importer_eori,country_of_destination,error_message'

CONS_COMPARE = [
    ('tss_status',        'status',                 'TSS Status'),
    ('goods_description', 'goods_description',      'Goods Description'),
    ('importer_eori',     'importer_eori',           'Importer EORI'),
    ('destination_country','country_of_destination', 'Destination Country'),
]


def _norm(v):
    return '' if v is None else str(v).strip()


def show_comparison(local_row, tss_data, field_map):
    """Print field-by-field diff. Returns number of mismatches."""
    diffs = 0
    W = 24  # label column width
    for local_col, tss_col, label in field_map:
        lv = _norm(local_row.get(local_col))
        tv = _norm(tss_data.get(tss_col) if tss_data else '')
        match = lv.lower() == tv.lower()
        icon  = ok('✓') if match else warn('≠')
        lv_s  = dim(lv or '—')
        tv_s  = (ok(tv) if match else warn(tv)) if tv else dim('—')
        print(f"      {icon}  {label:<{W}}  local={lv_s:<30}  tss={tv_s}")
        if not match and (lv or tv):
            diffs += 1
    return diffs


def ask_delete(prompt, dry_run, auto_yes):
    if dry_run:
        print(f"    {warn('[DRY-RUN]')} would delete {prompt}")
        return False
    if auto_yes:
        print(f"    {warn('→ Auto-confirming delete:')} {prompt}")
        return True
    try:
        ans = input(f"    {YELLOW}→ Delete {prompt} from local DB? [y/N]: {RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ('y', 'yes')


# ── Section 1: ENS Headers ────────────────────────────────────────────────────
def audit_ens_headers(conn, session, api_url, dry_run, auto_yes):
    cur = conn.cursor()

    # Records with a TSS reference — these can be verified
    cur.execute("""
        SELECT staging_id, label, ens_reference, status, tss_status,
               movement_type, arrival_port,
               carrier_name, carrier_eori,
               vehicle_registration, trailer_registration,
               created_at
        FROM BKD.StagingEnsHeaders
        WHERE ens_reference IS NOT NULL
        ORDER BY staging_id
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Records with no TSS reference (never submitted)
    no_ref = (cur.execute(
        "SELECT COUNT(*) FROM BKD.StagingEnsHeaders WHERE ens_reference IS NULL"
    ).fetchone() or [0])[0]

    if no_ref:
        print(f"  {dim(f'{no_ref} record(s) have no ENS reference — not submitted to TSS, skipped')}")

    if not rows:
        print(info("  No staged ENS headers with TSS references found."))
        cur.close()
        return 0, 0

    print(f"  {len(rows)} record(s) to check against TSS\n")

    orphans = 0
    deleted = 0

    for i, row in enumerate(rows, 1):
        sid    = row['staging_id']
        ref    = row['ens_reference']
        status = row.get('status', '')
        label  = row.get('label') or ''

        hdr = f"[{i}/{len(rows)}]  {bold(ref)}  #{sid}"
        if label:
            hdr += f"  ({label})"
        hdr += f"  portal_status={info(status)}"
        print(f"  {hdr}")

        resp = api_get(session, f"{api_url}/headers",
                       {'reference': ref, 'fields': ENS_FIELDS})

        # ── API error ─────────────────────────────────────────────────────────
        if resp['found'] is None:
            print(f"    {warn('? API ERROR')} HTTP {resp['code']}  {resp.get('error','')[:80]}")
            print()
            continue

        # ── NOT FOUND IN TSS ──────────────────────────────────────────────────
        if not resp['found']:
            print(f"    {err('✗  NOT FOUND IN TSS')}  (HTTP {resp['code']})")
            orphans += 1

            n_cons = (cur.execute(
                "SELECT COUNT(*) FROM BKD.StagingConsignments WHERE staging_ens_id=?",
                [sid]).fetchone() or [0])[0]
            if n_cons:
                print(f"    {warn(f'   └─ {n_cons} linked consignment(s) will also be removed')}")

            if ask_delete(f"ENS {ref} (staging_id={sid})", dry_run, auto_yes):
                try:
                    # Delete consignments (and their goods) first
                    cons_ids = [r[0] for r in cur.execute(
                        "SELECT staging_id FROM BKD.StagingConsignments WHERE staging_ens_id=?",
                        [sid]).fetchall()]
                    for cid in cons_ids:
                        cur.execute("DELETE FROM BKD.StagingGoodsItems WHERE staging_cons_id=?", [cid])
                    cur.execute("DELETE FROM BKD.StagingConsignments WHERE staging_ens_id=?", [sid])
                    cur.execute("DELETE FROM BKD.StagingEnsHeaders WHERE staging_id=?", [sid])
                    conn.commit()
                    print(f"    {ok('✓  Deleted')} ENS {ref}, {n_cons} consignment(s) and their goods items")
                    deleted += 1
                except Exception as e:
                    conn.rollback()
                    print(f"    {err(f'Delete failed: {e}')}")
            print()
            continue

        # ── FOUND — show comparison ───────────────────────────────────────────
        tss_data   = resp['data']
        tss_status = tss_data.get('status', '—')
        print(f"    {ok('✓  FOUND IN TSS')}  tss_status={ok(tss_status)}  ({resp['ms']}ms)")
        diffs = show_comparison(row, tss_data, ENS_COMPARE)
        if diffs:
            print(f"    {warn(f'   {diffs} field(s) differ — run sync_pipeline.py to update local record')}")
        print()

    cur.close()
    return orphans, deleted


# ── Section 2: Consignments ───────────────────────────────────────────────────
def audit_consignments(conn, session, api_url, dry_run, auto_yes):
    cur = conn.cursor()

    cur.execute("""
        SELECT c.staging_id, c.staging_ens_id, c.dec_reference,
               c.status, c.tss_status,
               c.goods_description, c.importer_eori,
               c.destination_country,
               c.created_at,
               e.ens_reference AS parent_ens
        FROM BKD.StagingConsignments c
        LEFT JOIN BKD.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
        WHERE c.dec_reference IS NOT NULL
        ORDER BY c.staging_id
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    no_ref = (cur.execute(
        "SELECT COUNT(*) FROM BKD.StagingConsignments WHERE dec_reference IS NULL"
    ).fetchone() or [0])[0]

    if no_ref:
        print(f"  {dim(f'{no_ref} record(s) have no DEC reference — not submitted to TSS, skipped')}")

    if not rows:
        print(info("  No staged consignments with TSS references found."))
        cur.close()
        return 0, 0

    print(f"  {len(rows)} record(s) to check against TSS\n")

    orphans = 0
    deleted = 0

    for i, row in enumerate(rows, 1):
        sid    = row['staging_id']
        ref    = row['dec_reference']
        parent = row.get('parent_ens') or f"ENS#{row.get('staging_ens_id','?')}"
        status = row.get('status', '')

        print(f"  [{i}/{len(rows)}]  {bold(ref)}  #{sid}  "
              f"parent={info(parent)}  portal_status={info(status)}")

        resp = api_get(session, f"{api_url}/consignments",
                       {'reference': ref, 'fields': CONS_FIELDS})

        # ── API error ─────────────────────────────────────────────────────────
        if resp['found'] is None:
            print(f"    {warn('? API ERROR')} HTTP {resp['code']}  {resp.get('error','')[:80]}")
            print()
            continue

        # ── NOT FOUND IN TSS ──────────────────────────────────────────────────
        if not resp['found']:
            print(f"    {err('✗  NOT FOUND IN TSS')}  (HTTP {resp['code']})")
            orphans += 1

            n_goods = (cur.execute(
                "SELECT COUNT(*) FROM BKD.StagingGoodsItems WHERE staging_cons_id=?",
                [sid]).fetchone() or [0])[0]
            if n_goods:
                print(f"    {warn(f'   └─ {n_goods} goods item(s) will also be removed')}")

            if ask_delete(f"Consignment {ref} (staging_id={sid})", dry_run, auto_yes):
                try:
                    cur.execute("DELETE FROM BKD.StagingGoodsItems WHERE staging_cons_id=?", [sid])
                    cur.execute("DELETE FROM BKD.StagingConsignments WHERE staging_id=?", [sid])
                    conn.commit()
                    print(f"    {ok('✓  Deleted')} {ref} and {n_goods} goods item(s)")
                    deleted += 1
                except Exception as e:
                    conn.rollback()
                    print(f"    {err(f'Delete failed: {e}')}")
            print()
            continue

        # ── FOUND — show comparison ───────────────────────────────────────────
        tss_data   = resp['data']
        tss_status = tss_data.get('status', '—')
        print(f"    {ok('✓  FOUND IN TSS')}  tss_status={ok(tss_status)}  ({resp['ms']}ms)")
        diffs = show_comparison(row, tss_data, CONS_COMPARE)
        if diffs:
            print(f"    {warn(f'   {diffs} field(s) differ — run sync_pipeline.py to update local record')}")
        if tss_data.get('error_message'):
            print(f"    {warn('   TSS error:')} {tss_data['error_message'][:120]}")
        print()

    cur.close()
    return orphans, deleted


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='Compare staged ENS/Consignment records against live TSS API')
    parser.add_argument('--dry-run',    action='store_true',
                        help='Report only — do not delete anything')
    parser.add_argument('--yes',        action='store_true',
                        help='Auto-confirm all deletes without prompting')
    parser.add_argument('--section',    choices=['ens', 'cons', 'all'], default='all',
                        help='Which staged table to audit (default: all)')
    parser.add_argument('--config',     default=None, metavar='FILE',
                        help='Path to config.ini (auto-discovered if omitted)')
    parser.add_argument('--db-section', default='QAS_Database', dest='db_section',
                        metavar='SECTION',
                        help='INI section for DB credentials (default: QAS_Database)')
    args = parser.parse_args()

    print()
    print(bold('=' * 68))
    print(bold(f"  STAGED RECORDS AUDIT  —  "
               f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"))
    if args.dry_run:
        print(warn("  MODE: DRY-RUN (no changes will be made)"))
    elif args.yes:
        print(warn("  MODE: AUTO-YES (all orphans will be deleted without prompting)"))
    else:
        print(info("  MODE: INTERACTIVE (will prompt Y/N for each orphan)"))
    print(bold('=' * 68))

    cfg = _find_config(args.config)
    print(info(f"  Config  : {cfg or 'env vars'}"))

    # Connect DB
    try:
        conn = get_connection(args.config, args.db_section)
        print(ok("  DB      : connected"))
    except Exception as e:
        print(err(f"  DB      : FAILED — {e}"))
        sys.exit(1)

    # Connect API
    try:
        session, api_url = get_api_session(args.config)
        print(ok(f"  API     : {api_url}"))
    except Exception as e:
        print(err(f"  API     : FAILED — {e}"))
        conn.close()
        sys.exit(1)

    ens_orphans = ens_deleted = cons_orphans = cons_deleted = 0

    # ── ENS Headers ───────────────────────────────────────────────────────────
    if args.section in ('ens', 'all'):
        print(f"\n{bold('─' * 68)}")
        print(bold("  SECTION 1 — ENS HEADERS  (BKD.StagingEnsHeaders)"))
        print(bold('─' * 68))
        ens_orphans, ens_deleted = audit_ens_headers(
            conn, session, api_url, args.dry_run, args.yes)

    # ── Consignments ──────────────────────────────────────────────────────────
    if args.section in ('cons', 'all'):
        print(f"\n{bold('─' * 68)}")
        print(bold("  SECTION 2 — CONSIGNMENTS  (BKD.StagingConsignments)"))
        print(bold('─' * 68))
        cons_orphans, cons_deleted = audit_consignments(
            conn, session, api_url, args.dry_run, args.yes)

    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(bold('=' * 68))
    print(bold("  SUMMARY"))
    print(f"  ENS Headers   : checked, {warn(str(ens_orphans))} not in TSS"
          + (f", {ok(str(ens_deleted))} deleted" if ens_deleted else ""))
    print(f"  Consignments  : checked, {warn(str(cons_orphans))} not in TSS"
          + (f", {ok(str(cons_deleted))} deleted" if cons_deleted else ""))
    if args.dry_run:
        print(warn("  (dry-run — nothing was changed)"))
    print(bold('=' * 68))
    print()


if __name__ == '__main__':
    main()
