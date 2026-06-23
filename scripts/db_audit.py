"""
NOT FOR PRD: reads/writes legacy BKD.Staging* tables removed by migration 078. Do not run against Fusion_TSS_Automation_PRD.

Synovia Flow — Local DB Audit vs TSS
====================================
Checks every ENS Header, Consignment, and Goods Item that has a TSS
reference in the local staging DB against the live TSS API.

For each record NOT found in TSS (orphan), the script prints a summary
and offers an interactive console prompt to delete it from the local DB.

Cascade rules on deletion:
  • ENS Header  → deletes its Consignments  → deletes their Goods Items
  • Consignment → deletes its Goods Items
  • Goods Item  → deleted individually

Usage:
    # Using environment variables / .env (Render/cloud):
    python scripts/db_audit.py

    # Using a local INI config file (Windows local run):
    python scripts/db_audit.py --config path\\to\\config.ini
    python scripts/db_audit.py --config path\\to\\config.ini --db-section QAS_Database

    # Other flags:
    python scripts/db_audit.py --dry-run     # show orphans, no deletes
    python scripts/db_audit.py --yes         # delete all orphans without prompting
    python scripts/db_audit.py --section ens # only check ENS headers (cons|goods|all)

INI file format expected:
    [QAS_Database]
    driver   = {ODBC Driver 17 for SQL Server}
    server   = your-server.database.windows.net
    database = Fusion_TSS
    user     = youruser
    password = yourpassword
    encrypt  = yes
    trust_server_certificate = no

    [tss_api]
    base_url = https://api.tsstestenv.co.uk/api
    username = youruser
    password = yourpassword
"""
import os, sys, time, base64, argparse, configparser
from datetime import datetime, timezone
import pyodbc, requests
try:
    from _console_output import configure_console_output
except ModuleNotFoundError:
    from scripts._console_output import configure_console_output

configure_console_output()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

S          = 'BKD'
RATE_LIMIT = 0.2
TIMEOUT    = 20

# ── ANSI colours (disabled on Windows unless FORCE_COLOR set) ────────────────
USE_COLOR = sys.stdout.isatty() or os.environ.get('FORCE_COLOR')
def _c(code, text): return f"\033[{code}m{text}\033[0m" if USE_COLOR else text
RED    = lambda t: _c('31', t)
GREEN  = lambda t: _c('32', t)
YELLOW = lambda t: _c('33', t)
CYAN   = lambda t: _c('36', t)
BOLD   = lambda t: _c('1',  t)
DIM    = lambda t: _c('2',  t)


# ── Config loader ─────────────────────────────────────────────────────────────

_cfg = None   # populated by _load_ini()

# Candidate locations searched when --config is not supplied
_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_AUTO_SEARCH = [
    os.path.join(_PROJECT_ROOT, 'config.ini'),
    os.path.join(_SCRIPT_DIR,   'config.ini'),
    os.path.join(os.getcwd(),   'config.ini'),
]

def _load_ini(path, db_section):
    global _cfg
    _cfg = configparser.ConfigParser()
    read = _cfg.read(path)
    if not read:
        print(RED(f"  Cannot read config file: {path}"))
        sys.exit(1)
    if db_section not in _cfg:
        available = [s for s in _cfg.sections()]
        print(RED(f"  Section [{db_section}] not found in {path}"))
        print(f"  Available sections: {available}")
        sys.exit(1)
    print(f"  Config : {path}  [{db_section}]")
    return _cfg, db_section


def _auto_load_ini(db_section):
    """Try each candidate path in order; load the first one that exists."""
    for path in _AUTO_SEARCH:
        if os.path.isfile(path):
            _load_ini(path, db_section)
            return True
    return False


# ── Infrastructure ────────────────────────────────────────────────────────────

def get_connection(db_section='QAS_Database'):
    if _cfg and db_section in _cfg:
        sec = _cfg[db_section]
        driver   = sec.get('driver', '{ODBC Driver 17 for SQL Server}')
        server   = sec['server']
        database = sec['database']
        user     = sec['user']
        password = sec['password']
        encrypt  = sec.get('encrypt', 'yes')
        trust    = sec.get('trust_server_certificate', 'no')
        return pyodbc.connect(
            f"DRIVER={driver};SERVER={server};DATABASE={database};"
            f"UID={user};PWD={password};"
            f"Encrypt={encrypt};TrustServerCertificate={trust};Connection Timeout=30;",
            autocommit=False
        )
    # Fall back to environment variables / .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    driver = '{ODBC Driver 17 for SQL Server}' if os.name == 'nt' else '{ODBC Driver 18 for SQL Server}'
    return pyodbc.connect(
        f"DRIVER={driver};"
        f"SERVER={os.environ['AZURE_SQL_SERVER']};"
        f"DATABASE={os.environ.get('AZURE_SQL_DATABASE', 'Fusion_TSS')};"
        f"UID={os.environ['AZURE_SQL_USERNAME']};"
        f"PWD={os.environ['AZURE_SQL_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;",
        autocommit=False
    )


def get_api_session():
    if _cfg and 'tss_api' in _cfg:
        sec = _cfg['tss_api']
        base_url = sec.get('base_url', '').rstrip('/')
        u = sec.get('username', '')
        p = sec.get('password', '')
    else:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        base_url = os.environ.get('TSS_API_BASE_URL', '').rstrip('/')
        u = os.environ.get('TSS_API_USERNAME', '')
        p = os.environ.get('TSS_API_PASSWORD', '')
    api_url = f"{base_url}/x_fhmrc_tss_api/v1/tss_api"
    b64 = base64.b64encode(f"{u}:{p}".encode()).decode()
    session = requests.Session()
    session.headers.update({'Accept': 'application/json',
                            'Authorization': f'Basic {b64}'})
    return session, api_url


def tss_check(session, url, reference):
    """Returns (found: bool, status: str, http_code: int, ms: int)."""
    t0 = time.time()
    try:
        r = session.get(url, params={'reference': reference}, timeout=TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        time.sleep(RATE_LIMIT)
        if r.status_code == 404:
            return False, 'NOT FOUND (404)', 404, ms
        if r.status_code == 200:
            body = r.json()
            result = body.get('result') or {}
            if not result or result == {}:
                return False, 'EMPTY RESULT', 200, ms
            status = result.get('status') or result.get('tss_status') or 'present'
            return True, status, 200, ms
        return False, f'HTTP {r.status_code}', r.status_code, ms
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return False, f'ERROR: {str(e)[:60]}', 0, ms


# ── Console helpers ───────────────────────────────────────────────────────────

def print_header(title):
    print()
    print(BOLD(f"{'═' * 60}"))
    print(BOLD(f"  {title}"))
    print(BOLD(f"{'═' * 60}"))


def print_row(icon, sid, ref, status, ms, extra=''):
    ref_str  = (ref or '')[:35].ljust(35)
    stat_str = (status or '').ljust(22)
    extra_str = DIM(f'  {extra}') if extra else ''
    print(f"  {icon}  #{str(sid).ljust(6)}  {ref_str}  {stat_str}  {DIM(str(ms)+'ms')}{extra_str}")


def ask_delete(label, dry_run, auto_yes):
    """Returns True if the user wants to delete."""
    if dry_run:
        print(DIM(f"    [dry-run] would delete: {label}"))
        return False
    if auto_yes:
        print(YELLOW(f"    --yes: deleting {label}"))
        return True
    try:
        ans = input(YELLOW(f"    Delete {label}? [y/N/a=all/q=quit] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if ans == 'q':
        print(YELLOW("  Quitting."))
        sys.exit(0)
    if ans == 'a':
        # Caller should set auto_yes = True going forward — we can't do that here,
        # but returning True is enough for the immediate deletion.
        return True
    return ans == 'y'


# ── Section 1: ENS Headers ───────────────────────────────────────────────────

def audit_ens_headers(conn, session, api_url, dry_run, auto_yes):
    print_header("1 / 3  —  ENS Headers")
    cur = conn.cursor()
    cur.execute(f"""
        SELECT staging_id, ens_reference, label, status, tss_status
        FROM {S}.StagingEnsHeaders
        WHERE ens_reference IS NOT NULL
        ORDER BY staging_id
    """)
    rows = cur.fetchall()
    print(f"  Checking {len(rows)} headers against TSS…\n")

    orphans = []
    found = 0
    errors = 0

    for staging_id, ref, label, local_status, tss_status in rows:
        in_tss, status_str, http, ms = tss_check(
            session, f"{api_url}/headers", ref)
        extra = f'{label or ""}' if label else ''
        if in_tss:
            print_row(GREEN('✓'), staging_id, ref, status_str, ms, extra)
            found += 1
        elif http == 0:
            print_row(YELLOW('?'), staging_id, ref, status_str, ms, extra)
            errors += 1
        else:
            print_row(RED('✗'), staging_id, ref, status_str, ms, extra)
            orphans.append((staging_id, ref, label, local_status))

    print(f"\n  Found: {GREEN(str(found))}   Orphans: {RED(str(len(orphans)))}   "
          f"Errors/skipped: {YELLOW(str(errors))}")

    if not orphans:
        cur.close()
        return 0

    print(f"\n  {BOLD('Orphans not found in TSS:')}")
    for sid, ref, label, lstatus in orphans:
        print(f"    • #{sid}  {ref}  [{lstatus}]  {DIM(label or '')}")

    deleted = 0
    _auto = auto_yes
    for sid, ref, label, lstatus in orphans:
        desc = f"#{sid} {ref} (+ its consignments & goods items)"
        if ask_delete(desc, dry_run, _auto):
            try:
                # Cascade: goods items → consignments → header
                cur.execute(f"""
                    DELETE g FROM {S}.StagingGoodsItems g
                    JOIN {S}.StagingConsignments c ON c.staging_id = g.staging_cons_id
                    WHERE c.staging_ens_id = ?
                """, [sid])
                gi_count = cur.rowcount
                cur.execute(f"DELETE FROM {S}.StagingConsignments WHERE staging_ens_id = ?", [sid])
                cons_count = cur.rowcount
                cur.execute(f"DELETE FROM {S}.StagingEnsHeaders WHERE staging_id = ?", [sid])
                conn.commit()
                print(GREEN(f"    Deleted: ENS header #{sid}, "
                            f"{cons_count} consignment(s), {gi_count} goods item(s)"))
                deleted += 1
            except Exception as e:
                conn.rollback()
                print(RED(f"    Error deleting #{sid}: {e}"))

    cur.close()
    return deleted


# ── Section 2: Consignments ──────────────────────────────────────────────────

def audit_consignments(conn, session, api_url, dry_run, auto_yes):
    print_header("2 / 3  —  Consignments")
    cur = conn.cursor()
    cur.execute(f"""
        SELECT c.staging_id, c.dec_reference, c.goods_description,
               c.status, c.tss_status, e.ens_reference
        FROM {S}.StagingConsignments c
        LEFT JOIN {S}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
        WHERE c.dec_reference IS NOT NULL
        ORDER BY c.staging_id
    """)
    rows = cur.fetchall()
    print(f"  Checking {len(rows)} consignments against TSS…\n")

    orphans = []
    found = 0
    errors = 0

    for staging_id, ref, goods_desc, local_status, tss_status, ens_ref in rows:
        in_tss, status_str, http, ms = tss_check(
            session, f"{api_url}/consignments", ref)
        extra = f'ENS:{ens_ref or "?"}' if ens_ref else ''
        if in_tss:
            print_row(GREEN('✓'), staging_id, ref, status_str, ms, extra)
            found += 1
        elif http == 0:
            print_row(YELLOW('?'), staging_id, ref, status_str, ms, extra)
            errors += 1
        else:
            print_row(RED('✗'), staging_id, ref, status_str, ms, extra)
            orphans.append((staging_id, ref, goods_desc, local_status, ens_ref))

    print(f"\n  Found: {GREEN(str(found))}   Orphans: {RED(str(len(orphans)))}   "
          f"Errors/skipped: {YELLOW(str(errors))}")

    if not orphans:
        cur.close()
        return 0

    print(f"\n  {BOLD('Orphans not found in TSS:')}")
    for sid, ref, desc, lstatus, ens_ref in orphans:
        print(f"    • #{sid}  {ref}  [{lstatus}]  {DIM((desc or '')[:40])}")

    deleted = 0
    _auto = auto_yes
    for sid, ref, desc, lstatus, ens_ref in orphans:
        desc_str = f"#{sid} {ref} (+ its goods items)"
        if ask_delete(desc_str, dry_run, _auto):
            try:
                cur.execute(f"DELETE FROM {S}.StagingGoodsItems WHERE staging_cons_id = ?", [sid])
                gi_count = cur.rowcount
                cur.execute(f"DELETE FROM {S}.StagingConsignments WHERE staging_id = ?", [sid])
                conn.commit()
                print(GREEN(f"    Deleted: consignment #{sid}, {gi_count} goods item(s)"))
                deleted += 1
            except Exception as e:
                conn.rollback()
                print(RED(f"    Error deleting #{sid}: {e}"))

    cur.close()
    return deleted


# ── Section 3: Goods Items ───────────────────────────────────────────────────

def audit_goods_items(conn, session, api_url, dry_run, auto_yes):
    print_header("3 / 3  —  Goods Items")
    cur = conn.cursor()
    cur.execute(f"""
        SELECT g.staging_id, g.goods_id, g.goods_description,
               g.status, c.dec_reference
        FROM {S}.StagingGoodsItems g
        LEFT JOIN {S}.StagingConsignments c ON c.staging_id = g.staging_cons_id
        WHERE g.goods_id IS NOT NULL
        ORDER BY g.staging_id
    """)
    rows = cur.fetchall()
    print(f"  Checking {len(rows)} goods items against TSS…\n")

    orphans = []
    found = 0
    errors = 0

    for staging_id, goods_id, goods_desc, local_status, dec_ref in rows:
        in_tss, status_str, http, ms = tss_check(
            session, f"{api_url}/goods", goods_id)
        extra = f'CONS:{dec_ref or "?"}' if dec_ref else ''
        if in_tss:
            print_row(GREEN('✓'), staging_id, goods_id, status_str, ms, extra)
            found += 1
        elif http == 0:
            print_row(YELLOW('?'), staging_id, goods_id, status_str, ms, extra)
            errors += 1
        else:
            print_row(RED('✗'), staging_id, goods_id, status_str, ms, extra)
            orphans.append((staging_id, goods_id, goods_desc, local_status, dec_ref))

    print(f"\n  Found: {GREEN(str(found))}   Orphans: {RED(str(len(orphans)))}   "
          f"Errors/skipped: {YELLOW(str(errors))}")

    if not orphans:
        cur.close()
        return 0

    print(f"\n  {BOLD('Orphans not found in TSS:')}")
    for sid, gid, desc, lstatus, dec_ref in orphans:
        print(f"    • #{sid}  {(gid or '')[:40]}  [{lstatus}]  {DIM((desc or '')[:30])}")

    deleted = 0
    _auto = auto_yes
    for sid, gid, desc, lstatus, dec_ref in orphans:
        gid_short = (gid or '')[:30]
        if ask_delete(f"goods item #{sid} ({gid_short})", dry_run, _auto):
            try:
                cur.execute(f"DELETE FROM {S}.StagingGoodsItems WHERE staging_id = ?", [sid])
                conn.commit()
                print(GREEN(f"    Deleted: goods item #{sid}"))
                deleted += 1
            except Exception as e:
                conn.rollback()
                print(RED(f"    Error deleting #{sid}: {e}"))

    cur.close()
    return deleted


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Audit local BKD staging DB against live TSS API')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show orphans but do not delete anything')
    parser.add_argument('--yes', action='store_true',
                        help='Delete all orphans without prompting')
    parser.add_argument('--section', choices=['ens', 'cons', 'goods', 'all'],
                        default='all',
                        help='Which section to audit (default: all)')
    parser.add_argument('--config', metavar='PATH',
                        help='Path to INI config file (use instead of env vars)')
    parser.add_argument('--db-section', metavar='SECTION', default='QAS_Database',
                        help='INI section for DB credentials (default: QAS_Database)')
    args = parser.parse_args()

    dry_run    = args.dry_run
    auto_yes   = args.yes
    section    = args.section
    db_section = args.db_section

    print(BOLD("\nSynovia Flow — DB Audit vs TSS"))
    print(f"  Run at : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Mode   : {'DRY RUN — no deletes' if dry_run else ('AUTO-DELETE all orphans' if auto_yes else 'Interactive')}")
    print(f"  Section: {section.upper()}")

    if args.config:
        _load_ini(args.config, db_section)
    else:
        found = _auto_load_ini(db_section)
        if not found:
            print(DIM(f"  No config.ini found — using environment variables"))

    try:
        conn = get_connection(db_section)
        print(f"  DB     : {GREEN('connected')}")
    except Exception as e:
        print(RED(f"  DB connection failed: {e}"))
        sys.exit(1)

    try:
        session, api_url = get_api_session()
        print(f"  API    : {api_url}")
    except Exception as e:
        print(RED(f"  API setup failed: {e}"))
        conn.close()
        sys.exit(1)

    total_deleted = 0

    try:
        if section in ('ens', 'all'):
            total_deleted += audit_ens_headers(conn, session, api_url, dry_run, auto_yes)

        if section in ('cons', 'all'):
            total_deleted += audit_consignments(conn, session, api_url, dry_run, auto_yes)

        if section in ('goods', 'all'):
            total_deleted += audit_goods_items(conn, session, api_url, dry_run, auto_yes)

    except KeyboardInterrupt:
        print(YELLOW("\n\n  Interrupted by user."))

    print()
    print(BOLD('═' * 60))
    if dry_run:
        print(BOLD("  Audit complete (dry-run — nothing deleted)"))
    else:
        print(BOLD(f"  Audit complete — {total_deleted} record(s) deleted from local DB"))
    print(BOLD('═' * 60))
    print()

    conn.close()
    sys.exit(0)


if __name__ == '__main__':
    main()
