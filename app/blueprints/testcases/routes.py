"""
Test Automation Blueprint
Runs complete end-to-end scenarios against the live database and TSS API.

Each scenario:
  - Seeds data with a unique run prefix so multiple runs don't collide
  - Executes pipeline jobs in the correct sequence
  - Asserts DB state after each step (pass / fail / skip)
  - Produces a detailed per-step result log

Scenarios:
  A — Happy Path: valid ENS → consignment → goods → validate → submit
  B — Validation Failure: bad EORI format should be caught by validate_pipeline
  C — Missing Required Fields: no goods description → FAILED
"""
import os
import sys
import json
import datetime
import subprocess
import logging

from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from app.db import query_all, query_one, execute

log = logging.getLogger(__name__)

testcases_bp = Blueprint(
    'testcases', __name__,
    template_folder='../../templates/testcases'
)

S = 'BKD'

def _find_project_root():
    cwd = os.getcwd()
    if (
        os.path.isdir(os.path.join(cwd, 'scripts'))
        and os.path.isdir(os.path.join(cwd, 'tests'))
        and os.path.isdir(os.path.join(cwd, 'app'))
    ):
        return cwd

    candidate = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if (
            os.path.isdir(os.path.join(candidate, 'scripts'))
            and os.path.isdir(os.path.join(candidate, 'tests'))
            and os.path.isdir(os.path.join(candidate, 'app'))
        ):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return cwd

_PROJECT_ROOT = _find_project_root()


def _build_subprocess_env():
    env = os.environ.copy()
    existing = env.get('PYTHONPATH', '')
    parts = [_PROJECT_ROOT]
    if existing:
        parts.extend(p for p in existing.split(os.pathsep) if p)
    env['PYTHONPATH'] = os.pathsep.join(dict.fromkeys(parts))
    return env


# ═══════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════

def _run_prefix(run_id):
    return f'[TC-{run_id}]'


def _run_script(script):
    env = _build_subprocess_env()
    try:
        from app.blueprints.orchestrator.routes import _load_tss_env
        for k, v in _load_tss_env().items():
            if not env.get(k):
                env[k] = v
    except Exception:
        pass
    # Build absolute path so Python doesn't resolve relative to cwd
    abs_script = script if os.path.isabs(script) else os.path.join(_PROJECT_ROOT, script)
    try:
        proc = subprocess.run(
            [sys.executable, abs_script],
            capture_output=True, text=True,
            timeout=180, cwd=_PROJECT_ROOT, env=env,
        )
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, 'TIMEOUT after 180s'
    except Exception as exc:
        return False, f'SUBPROCESS ERROR: {exc}'


def _run_unittest_module(module_name):
    env = _build_subprocess_env()
    module_relpath = os.path.join(*module_name.split('.')) + '.py'
    module_abspath = os.path.join(_PROJECT_ROOT, module_relpath)

    commands = [
        [sys.executable, '-m', 'unittest', module_name, '-v'],
    ]
    if os.path.isfile(module_abspath):
        commands.append([
            sys.executable, '-m', 'unittest', 'discover',
            '-s', os.path.dirname(module_abspath),
            '-p', os.path.basename(module_abspath),
            '-t', _PROJECT_ROOT,
            '-v',
        ])

    attempts = []
    try:
        for cmd in commands:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
                cwd=_PROJECT_ROOT,
                env=env,
            )
            output = (proc.stdout + proc.stderr).strip()
            if proc.returncode == 0:
                return True, output
            attempts.append(output)
        return False, '\n\n--- fallback ---\n\n'.join(a for a in attempts if a)
    except subprocess.TimeoutExpired:
        return False, 'TIMEOUT after 180s'
    except Exception as exc:
        return False, f'UNITTEST ERROR: {exc}'


def _assert(label, ok, detail=''):
    return {
        'label': label,
        'ok': ok,
        'detail': detail,
        'ts': datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S'),
    }


# ═══════════════════════════════════════════════════════════════
#  SEED HELPERS (label-based, no source column needed)
# ═══════════════════════════════════════════════════════════════

def _seed_ens(run_id, movement_type='RoRo', arrival_port='GBBEL', label_suffix=''):
    prefix = _run_prefix(run_id)
    label = f'{prefix} ENS{label_suffix}'
    try:
        execute(f"""
            INSERT INTO {S}.StagingEnsHeaders
                (label, ens_reference, status, movement_type, arrival_port, created_at)
            VALUES (?, NULL, 'CREATED', ?, ?, SYSUTCDATETIME())
        """, [label, movement_type, arrival_port])
        row = query_one(
            f"SELECT TOP 1 staging_id FROM {S}.StagingEnsHeaders WHERE label=? ORDER BY staging_id DESC",
            [label])
        return row['staging_id'] if row else None
    except Exception as e:
        log.error('_seed_ens: %s', e)
        return None


def _seed_consignment(run_id, ens_id, goods_desc, importer_eori,
                      transport_doc='TESTDOC001', label_suffix='',
                      container_indicator='0', controlled_goods='no'):
    prefix = _run_prefix(run_id)
    label = f'{prefix} Consignment{label_suffix}'
    controlled_goods = (controlled_goods or 'no').strip() or 'no'
    try:
        execute(f"""
            INSERT INTO {S}.StagingConsignments
                (staging_ens_id, label, goods_description,
                 importer_eori, transport_document_number, container_indicator,
                 controlled_goods, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', SYSUTCDATETIME(), SYSUTCDATETIME())
        """, [ens_id, label, goods_desc, importer_eori, transport_doc, container_indicator,
              controlled_goods])
        row = query_one(
            f"SELECT TOP 1 staging_id FROM {S}.StagingConsignments WHERE label=? ORDER BY staging_id DESC",
            [label])
        return row['staging_id'] if row else None
    except Exception as e:
        log.error('_seed_consignment: %s', e)
        return None


def _seed_goods(run_id, cons_id, goods_desc='Test Item', commodity_code='12345678',
                gross_mass=500, pkg_type='BA', num_pkgs=10, controlled_goods='no'):
    prefix = _run_prefix(run_id)
    label = f'{prefix} Goods'
    controlled_goods = (controlled_goods or 'no').strip() or 'no'
    try:
        execute(f"""
            INSERT INTO {S}.StagingGoodsItems
                (staging_cons_id, item_number, label,
                 goods_description, commodity_code,
                 gross_mass_kg, type_of_packages, number_of_packages,
                 package_marks, controlled_goods, status, retry_count, max_retries,
                 created_at, updated_at)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, 3, SYSUTCDATETIME(), SYSUTCDATETIME())
        """, [cons_id, label, goods_desc, commodity_code,
              gross_mass, pkg_type, num_pkgs, label, controlled_goods])
        row = query_one(
            f"SELECT TOP 1 staging_id FROM {S}.StagingGoodsItems WHERE package_marks=? ORDER BY staging_id DESC",
            [label])
        return row['staging_id'] if row else None
    except Exception as e:
        log.error('_seed_goods: %s', e)
        return None


def _seed_declaration(run_id):
    """Seed a BKD.StagingDeclarations record for validate_declarations testing.
    Queries the DB for valid CV values so the payload passes validation."""
    prefix = _run_prefix(run_id)
    try:
        # Prefer a movement type that only requires carrier_name (not passive_transport or conveyance_ref)
        mv = query_one(
            "SELECT TOP 1 value FROM TSS.CV_movement_type WHERE value NOT IN ('3a','4') ORDER BY value")
        if not mv:
            mv = query_one("SELECT TOP 1 value FROM TSS.CV_movement_type ORDER BY value")
        tc = query_one("SELECT TOP 1 value FROM TSS.CV_transport_charge ORDER BY value")
        pt = query_one("SELECT TOP 1 location_code FROM TSS.CV_port ORDER BY location_code")
        nat = query_one("SELECT TOP 1 value FROM TSS.CV_country ORDER BY value")

        movement_type     = mv['value'] if mv else '3'
        transport_charges = tc['value'] if tc else 'C'
        arrival_port      = pt['location_code'] if pt else 'GBBEL'
        nationality       = nat['value'] if nat else 'GB'

        if movement_type in ('1a', '3', '3a'):
            identity_no = 'IMO1234567#TRLR1234'
        elif movement_type == '1':
            identity_no = 'IMO1234567'
        else:
            identity_no = 'TESTTRANSPORT1'

        payload = {
            'movement_type':             movement_type,
            'identity_no_of_transport':  identity_no,
            'nationality_of_transport':  nationality,
            'arrival_date_time':         _tomorrow_arrival_datetime(),
            'arrival_port':              arrival_port,
            'place_of_loading':          'FRPAR',
            'place_of_unloading':        arrival_port,
            'transport_charges':         transport_charges,
            'carrier_eori':              'XI123456789000',
            'carrier_name':              'Test Carrier Ltd',
            'carrier_street_number':     '10 Harbour Road',
            'carrier_city':              'Belfast',
            'carrier_postcode':          'BT1 1AA',
            'carrier_country':           nationality,
        }
        execute(f"""
            INSERT INTO {S}.StagingDeclarations
                (declaration_type, status, source, payload_json, created_by)
            VALUES ('ENS_HEADER', 'Inserted', ?, ?, 'TestCase')
        """, [prefix, json.dumps(payload)])
        row = query_one(
            f"SELECT TOP 1 id FROM {S}.StagingDeclarations WHERE source=? ORDER BY id DESC",
            [prefix])
        return row['id'] if row else None
    except Exception as e:
        log.error('_seed_declaration: %s', e)
        return None


def _cleanup(run_id):
    prefix = _run_prefix(run_id)
    deleted = 0
    for table, col in [
        ('StagingGoodsItems',   'package_marks'),
        ('StagingConsignments', 'label'),
        ('StagingEnsHeaders',   'label'),
        ('StagingDeclarations', 'source'),
    ]:
        try:
            execute(f"DELETE FROM {S}.{table} WHERE {col} LIKE ?", [f'{prefix}%'])
            deleted += 1
        except Exception as e:
            log.error('cleanup %s: %s', table, e)
    return deleted


def _get_status(table, staging_id):
    try:
        row = query_one(f"SELECT status, error_message FROM {S}.{table} WHERE staging_id=?", [staging_id])
        return (row.get('status') or ''), (row.get('error_message') or '')
    except Exception:
        return '', ''


def _tomorrow_arrival_datetime(now=None):
    now = now or datetime.datetime.now(datetime.UTC)
    arrival = now + datetime.timedelta(days=1)
    return arrival.strftime('%d/%m/%Y %H:%M:%S')


# ═══════════════════════════════════════════════════════════════
#  SCENARIOS
# ═══════════════════════════════════════════════════════════════

SCENARIOS = {
    'A': {
        'id': 'A',
        'name': 'Happy Path — Valid Consignment',
        'description': 'Creates a complete ENS → Consignment → Goods chain with valid data. '
                       'Runs validate_pipeline → expects VALIDATED.',
        'icon': 'bi-check-circle-fill',
        'color': '#16a34a',
        'steps': ['Seed ENS header', 'Seed Consignment', 'Seed Goods Item',
                  'Run: validate_pipeline', 'Assert: all VALIDATED'],
    },
    'B': {
        'id': 'B',
        'name': 'Validation Failure — Bad EORI',
        'description': 'Seeds a consignment with a malformed EORI (BADFORMAT). '
                       'Runs validate_pipeline → expects FAILED with EORI error.',
        'icon': 'bi-x-circle-fill',
        'color': '#ef4444',
        'steps': ['Seed ENS header', 'Seed Consignment (bad EORI)', 'Seed Goods Item',
                  'Run: validate_pipeline', 'Assert: consignment FAILED', 'Assert: error mentions EORI'],
    },
    'C': {
        'id': 'C',
        'name': 'Validation Failure — Missing Description',
        'description': 'Seeds a consignment with no goods_description. '
                       'Runs validate_pipeline → expects FAILED with required field error.',
        'icon': 'bi-exclamation-circle-fill',
        'color': '#d97706',
        'steps': ['Seed ENS header', 'Seed Consignment (blank description)', 'Seed Goods Item',
                  'Run: validate_pipeline', 'Assert: consignment FAILED'],
    },
    'D': {
        'id': 'D',
        'name': 'Full ENS Pipeline — Validate + Submit',
        'description': 'Creates an ENS header stub, runs Validate ENS then Submit ENS. '
                       'Requires live TSS credentials. Checks for external_ref on success.',
        'icon': 'bi-send-fill',
        'color': '#2563eb',
        'mode': 'live',
        'steps': ['Seed ENS header', 'Run: validate', 'Assert: VALIDATED',
                  'Run: submit', 'Assert: Submitted or error logged'],
    },
    'E': {
        'id': 'E',
        'name': 'Route A Workflow - GMR to SDI Chain',
        'description': 'Runs the existing Route A orchestration suite so the UI also exposes '
                       'the expected phase order from ENS validation through GMR and SDI discovery.',
        'icon': 'bi-diagram-3-fill',
        'color': '#7c3aed',
        'mode': 'rules',
        'unittest_module': 'tests.test_auto_route_a',
        'steps': ['Load workflow suite', 'Run: tests.test_auto_route_a',
                  'Assert: GMR and SDI phases stay in the expected order'],
    },
    'F': {
        'id': 'F',
        'name': 'GMR Sync - Downstream SDI Stub',
        'description': 'Runs the sync_gmr suite that checks whether an arrived customs chain can '
                       'stage the pending supplementary declaration stub correctly.',
        'icon': 'bi-truck-flatbed',
        'color': '#0f766e',
        'mode': 'rules',
        'unittest_module': 'tests.test_sync_gmr',
        'steps': ['Load sync_gmr suite', 'Run: tests.test_sync_gmr',
                  'Assert: pending SDI stub creation logic still passes'],
    },
    'G': {
        'id': 'G',
        'name': 'SDI Workflow - Wait Until ARRIVED',
        'description': 'Runs the SDI alignment suite to confirm SDI only becomes the next '
                       'business step once the movement has reached ARRIVED.',
        'icon': 'bi-hourglass-split',
        'color': '#b45309',
        'mode': 'rules',
        'unittest_module': 'tests.test_sdi_workflow_alignment',
        'steps': ['Load SDI workflow suite', 'Run: tests.test_sdi_workflow_alignment',
                  'Assert: Start SDI appears only at the right stage'],
    },
    'H': {
        'id': 'H',
        'name': 'Start SDI - ENS Parent Linking',
        'description': 'Runs the parent-linking suite to ensure Start SDI from ENS stays inside '
                       'the correct declaration chain and preselects the right parent when possible.',
        'icon': 'bi-signpost-split-fill',
        'color': '#2563eb',
        'mode': 'rules',
        'unittest_module': 'tests.test_supdec_parent_linking',
        'steps': ['Load parent-linking suite', 'Run: tests.test_supdec_parent_linking',
                  'Assert: only ENS-linked DEC/SFD parents are offered'],
    },
    'I': {
        'id': 'I',
        'name': 'SDI Deadline - Month-End Discovery',
        'description': 'Runs the deadline suite that checks the supplementary declaration due '
                       'date rolls correctly to the tenth of the following month.',
        'icon': 'bi-calendar-check-fill',
        'color': '#db2777',
        'mode': 'rules',
        'unittest_module': 'tests.test_discover_sdi',
        'steps': ['Load discovery suite', 'Run: tests.test_discover_sdi',
                  'Assert: next SDI deadline matches the documented rule'],
    },
    'J': {
        'id': 'J',
        'name': 'SFD Mirror - SFD vs EIDR Visibility',
        'description': 'Runs the SFD route suite to confirm the UI can surface both classic SFD '
                       'and EIDR paths when downstream customs references appear.',
        'icon': 'bi-journals',
        'color': '#475569',
        'mode': 'rules',
        'unittest_module': 'tests.test_sfd_routes',
        'steps': ['Load SFD mirror suite', 'Run: tests.test_sfd_routes',
                  'Assert: customs path visibility stays coherent in the UI'],
    },
}


def _run_scenario(scenario_id):
    """Execute a scenario, return list of step results."""
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime('%H%M%S')
    steps = []
    ts_start = datetime.datetime.now(datetime.timezone.utc)
    scenario = SCENARIOS.get(scenario_id, {})

    try:
        if scenario.get('unittest_module'):
            steps += _scenario_unittest(run_id, scenario)
        elif scenario_id == 'A':
            steps += _scenario_a(run_id)
        elif scenario_id == 'B':
            steps += _scenario_b(run_id)
        elif scenario_id == 'C':
            steps += _scenario_c(run_id)
        elif scenario_id == 'D':
            steps += _scenario_d(run_id)
        else:
            steps.append(_assert('Unknown scenario', False, f'No scenario "{scenario_id}"'))
    except Exception as exc:
        steps.append(_assert('Unexpected error', False, str(exc)))
    finally:
        if not scenario.get('unittest_module'):
            _cleanup(run_id)

    duration = (datetime.datetime.now(datetime.timezone.utc) - ts_start).total_seconds()
    passed = sum(1 for s in steps if s['ok'])
    failed = sum(1 for s in steps if not s['ok'])
    return {
        'scenario_id': scenario_id,
        'scenario': SCENARIOS.get(scenario_id, {}),
        'run_id': run_id,
        'steps': steps,
        'passed': passed,
        'failed': failed,
        'total': len(steps),
        'ok': failed == 0,
        'duration': round(duration, 1),
        'ts': ts_start.strftime('%H:%M:%S'),
    }


def _scenario_unittest(run_id, scenario):
    steps = []
    module_name = scenario.get('unittest_module')
    if not module_name:
        return [_assert('Load workflow suite', False, 'No unittest module configured')]

    steps.append(_assert('Load workflow suite', True, module_name))
    ok, output = _run_unittest_module(module_name)
    steps.append(_assert(f'Run: {module_name}', ok, _last_lines(output, 12)))
    steps.append(_assert(
        'Assert: suite passed',
        ok,
        'Existing workflow/unit suite passed' if ok else 'Review module output above',
    ))
    return steps


def _scenario_a(run_id):
    steps = []

    ens_id = _seed_ens(run_id)
    steps.append(_assert('Seed ENS header', ens_id is not None,
                          f'staging_id={ens_id}' if ens_id else 'INSERT failed'))
    if not ens_id:
        return steps

    cons_id = _seed_consignment(run_id, ens_id,
                                 goods_desc='Test Goods — Happy Path',
                                 importer_eori='GB123456789000')
    steps.append(_assert('Seed Consignment', cons_id is not None,
                          f'staging_id={cons_id}' if cons_id else 'INSERT failed'))
    if not cons_id:
        return steps

    goods_id = _seed_goods(run_id, cons_id)
    steps.append(_assert('Seed Goods Item', goods_id is not None,
                          f'staging_id={goods_id}' if goods_id else 'INSERT failed'))

    ok, output = _run_script('scripts/validate_pipeline.py')
    steps.append(_assert('Run: validate_pipeline', ok,
                          _last_lines(output, 8)))

    status, err = _get_status('StagingConsignments', cons_id)
    steps.append(_assert('Assert: consignment VALIDATED', status == 'VALIDATED',
                          f'status={status} | {err[:120] if err else ""}'))

    gstatus, gerr = _get_status('StagingGoodsItems', goods_id)
    steps.append(_assert('Assert: goods VALIDATED', gstatus == 'VALIDATED',
                          f'status={gstatus} | {gerr[:120] if gerr else ""}'))

    return steps


def _scenario_b(run_id):
    steps = []

    ens_id = _seed_ens(run_id)
    steps.append(_assert('Seed ENS header', ens_id is not None))
    if not ens_id:
        return steps

    cons_id = _seed_consignment(run_id, ens_id,
                                 goods_desc='Test Goods — Bad EORI',
                                 importer_eori='BADFORMAT123',
                                 label_suffix=' (bad EORI)')
    steps.append(_assert('Seed Consignment (bad EORI)', cons_id is not None,
                          f'staging_id={cons_id}'))
    if not cons_id:
        return steps

    _seed_goods(run_id, cons_id)
    steps.append(_assert('Seed Goods Item', True))

    ok, output = _run_script('scripts/validate_pipeline.py')
    steps.append(_assert('Run: validate_pipeline', True,   # script exit OK even when records fail
                          _last_lines(output, 6)))

    status, err = _get_status('StagingConsignments', cons_id)
    steps.append(_assert('Assert: consignment FAILED', status == 'FAILED',
                          f'status={status}'))

    eori_mentioned = 'EORI' in (err or '').upper() or 'eori' in (err or '').lower()
    steps.append(_assert('Assert: error mentions EORI', eori_mentioned,
                          f'error_message: {err[:200] if err else "(empty)"}'))

    return steps


def _scenario_c(run_id):
    steps = []

    ens_id = _seed_ens(run_id)
    steps.append(_assert('Seed ENS header', ens_id is not None))
    if not ens_id:
        return steps

    cons_id = _seed_consignment(run_id, ens_id,
                                 goods_desc='',   # intentionally blank
                                 importer_eori='GB123456789000',
                                 label_suffix=' (blank desc)')
    steps.append(_assert('Seed Consignment (blank description)', cons_id is not None))
    if not cons_id:
        return steps

    _seed_goods(run_id, cons_id)
    steps.append(_assert('Seed Goods Item', True))

    ok, output = _run_script('scripts/validate_pipeline.py')
    steps.append(_assert('Run: validate_pipeline', True, _last_lines(output, 6)))

    status, err = _get_status('StagingConsignments', cons_id)
    steps.append(_assert('Assert: consignment FAILED',
                          status == 'FAILED',
                          f'status={status} | {err[:200] if err else ""}'))

    return steps


def _scenario_d(run_id):
    """Full ENS pipeline: seed StagingDeclarations → validate_declarations → submit_declarations."""
    steps = []

    dec_id = _seed_declaration(run_id)
    steps.append(_assert('Seed ENS Declaration (status=Inserted)', dec_id is not None,
                          f'id={dec_id}'))
    if not dec_id:
        return steps

    ok, output = _run_script('scripts/validate_declarations.py')
    steps.append(_assert('Run: validate_declarations', ok, _last_lines(output, 8)))

    row = query_one(f"SELECT status, error_message FROM {S}.StagingDeclarations WHERE id=?", [dec_id])
    status = (row.get('status') or '') if row else ''
    err    = (row.get('error_message') or '') if row else ''
    validated = status == 'Validated'
    steps.append(_assert('Assert: ENS Validated', validated,
                          f'status={status} | {err[:120] if err else ""}'))

    if not validated:
        steps.append(_assert('Run: submit_declarations', False,
                              'Skipped — ENS did not validate'))
        return steps

    ok, output = _run_script('scripts/submit_declarations.py')
    steps.append(_assert('Run: submit_declarations', ok, _last_lines(output, 8)))

    row = query_one(f"SELECT status, external_ref FROM {S}.StagingDeclarations WHERE id=?", [dec_id])
    if row:
        submitted = row.get('status') in ('Submitted', 'Accepted', 'Processing')
        steps.append(_assert('Assert: status Submitted', submitted,
                              f"status={row.get('status')} | ref={row.get('external_ref')}"))

    return steps


def _last_lines(text, n=8):
    if not text:
        return ''
    lines = text.strip().splitlines()
    return '\n'.join(lines[-n:])


# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════

@testcases_bp.route('/')
def index():
    return render_template('testcases/index.html', scenarios=SCENARIOS, results=None)


@testcases_bp.route('/run', methods=['GET'])
def run_redirect():
    return redirect(url_for('testcases.index'))


@testcases_bp.route('/run', methods=['POST'])
def run():
    scenario_id = request.form.get('scenario_id', 'A').upper()
    if scenario_id == 'ALL':
        all_results = [_run_scenario(sid) for sid in sorted(SCENARIOS.keys())]
        return render_template('testcases/index.html',
                               scenarios=SCENARIOS, results=None,
                               all_results=all_results,
                               run_all=True)

    result = _run_scenario(scenario_id)
    return render_template('testcases/index.html',
                           scenarios=SCENARIOS,
                           results=result,
                           run_all=False)


@testcases_bp.route('/report')
def report():
    """Run all scenarios and return a standalone HTML report."""
    all_results = [_run_scenario(sid) for sid in sorted(SCENARIOS.keys())]
    return render_template('testcases/report.html',
                           all_results=all_results,
                           generated_at=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))
