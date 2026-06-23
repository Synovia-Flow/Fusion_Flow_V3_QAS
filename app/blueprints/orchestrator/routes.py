"""
Web-triggered job runner with database logging.

Runs validate, submit and sync scripts from the portal and streams the results
back to the operator via HTMX.
"""
import os
import sys
import json
import datetime
import subprocess
import re
from urllib.parse import unquote, urlparse

from flask import Blueprint, render_template, request, redirect, url_for, flash
from app.db import query_all, execute, insert_api_call_log
from app.tenant import get_tenant

orchestrator_bp = Blueprint(
    'orchestrator', __name__,
    template_folder='../../templates/orchestrator'
)

# Locate project root by walking upward from this file until scripts/ is found.
# Counting fixed levels (../..) fails on Render if nesting differs from dev.
def _find_project_root():
    candidate = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isdir(os.path.join(candidate, 'scripts')):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return '/app'  # Render WORKDIR fallback

_PROJECT_ROOT = _find_project_root()

JOBS = {
    # ENS header pipeline (legacy / standalone).
    'validate': {
        'label': 'Validate ENS Headers',
        'script': 'scripts/validate_declarations.py',
        'icon': 'bi-check2-circle',
        'color': 'info',
        'description': "Checks all 'Inserted' ENS headers against business rules and TSS choice values.",
    },
    'submit': {
        'label': 'Submit ENS Headers to TSS',
        'script': 'scripts/submit_declarations.py',
        'icon': 'bi-send',
        'color': 'success',
        'description': "Submits all 'Validated' ENS headers to the TSS API and captures references.",
    },
    'sync': {
        'label': 'Sync ENS Statuses',
        'script': 'scripts/sync_statuses.py',
        'icon': 'bi-arrow-repeat',
        'color': 'primary',
        'description': 'Polls TSS for all submitted ENS declarations and tracks status changes.',
    },
    'sync_all': {
        'label': 'Sync All TSS Data',
        'script': 'scripts/run_tenant_syncs.py',
        'icon': 'bi-arrow-repeat',
        'color': 'primary',
        'description': 'Runs the current tenant full sync: ENS/cargo pipeline, TSS mirror tables, GMR arrivals and SDI discovery.',
        'timeout_seconds': 900,
    },
    # Full cargo pipeline: consignments, goods and supplementary declarations.
    'validate_pipeline': {
        'label': 'Validate Cargo Pipeline',
        'script': 'scripts/validate_pipeline.py',
        'icon': 'bi-check2-all',
        'color': 'info',
        'description': 'Validates PENDING consignments, goods items and sup decs: EORI prefixes, CV lookups and required fields.',
    },
    'submit_pipeline': {
        'label': 'Send Cargo Pipeline to TSS',
        'script': 'scripts/submit_pipeline.py',
        'icon': 'bi-send-fill',
        'color': 'success',
        'description': 'Creates VALIDATED consignments and goods in TSS, and submits supplementary declarations where applicable. ENS consignment progression is read back by sync jobs.',
    },
    'sync_pipeline': {
        'label': 'Sync Cargo Statuses',
        'script': 'scripts/sync_pipeline.py',
        'icon': 'bi-arrow-repeat',
        'color': 'primary',
        'description': 'Polls TSS for status updates on all CREATED/SUBMITTED consignments, goods and sup decs.',
    },
    # GMR / GVMS jobs.
    'submit_gmr': {
        'label': 'Submit GMRs to GVMS',
        'script': 'scripts/submit_gmr.py',
        'icon': 'bi-truck',
        'color': 'success',
        'description': 'Creates and submits PENDING GMRs to GVMS. TSS Rule: create + submit run together.',
    },
    'sync_gmr': {
        'label': 'Sync GMR Statuses',
        'script': 'scripts/sync_gmr.py',
        'icon': 'bi-truck',
        'color': 'primary',
        'description': 'Polls GVMS for GMR status updates. On Arrived/Closed, flags linked consignments and chains SDI stub/discovery.',
    },
    'discover_sdi': {
        'label': 'Discover SDIs from TSS',
        'script': 'scripts/sdi_autosubmit.py',
        'icon': 'bi-search',
        'color': 'info',
        'description': 'Queries TSS for supplementary declarations linked to SFDs and stages them in PRD STG/TSS SDI tables.',
    },
    'auto_route_a': {
        'label': 'Run Route A Automation',
        'script': 'scripts/auto_route_a.py',
        'icon': 'bi-magic',
        'color': 'warning',
        'description': 'One automation pass for ENS -> consignments -> goods -> SFD -> GMR -> SDI discovery.',
    },
    # Utility jobs.
    'queue': {
        'label': 'Process Queue',
        'script': 'scripts/legacy/process_queue.py',
        'icon': 'bi-play-circle',
        'color': 'warning',
        'description': "Processes up to 10 'Queued' records atomically using the old cron job logic.",
    },
    'poll': {
        'label': 'Poll Statuses',
        'script': 'scripts/legacy/poll_statuses.py',
        'icon': 'bi-radar',
        'color': 'secondary',
        'description': 'Lightweight status poll for active PollingTracker entries.',
    },
    'sync_tss': {
        'label': 'Sync TSS Tables',
        'script': 'scripts/sync_tss_tables.py',
        'icon': 'bi-cloud-download',
        'color': 'info',
        'description': 'Pulls live ENS headers, consignments and SFDs from TSS API into local mirror tables.',
    },
}

PIPELINE_PHASES = ['validate_pipeline', 'submit_pipeline', 'sync_pipeline']


def _recent_runs(n=30):
    try:
        return query_all(f"""
            SELECT TOP {n}
                ApiExchangeId AS id,
                CallType AS call_type,
                HttpStatus AS http_status,
                ResponseMessage AS result,
                CAST(DurationMs AS FLOAT) / 1000.0 AS duration_sec,
                ResponseJson AS output_snippet,
                ErrorDetail AS error_detail,
                CalledAt AS called_at
            FROM TSS.BKD_API_Exchanges
            WHERE CallType LIKE 'JOB_%'
            ORDER BY CalledAt DESC
        """)
    except Exception:
        return []


def _load_tss_env():
    """Read TSS API settings using the shared resolver."""
    try:
        from app.tss_api import resolve_tss_settings
        resolved = resolve_tss_settings()
        if resolved.get('base_url') or resolved.get('username') or resolved.get('password'):
            return {
                'TSS_API_BASE_URL': (resolved.get('base_url') or '').rstrip('/'),
                'TSS_API_USERNAME': resolved.get('username') or '',
                'TSS_API_PASSWORD': resolved.get('password') or '',
            }
    except Exception:
        pass
    return {}


def _run_script(phase, script_path, extra_env=None):
    """Run a script via subprocess and return (ok, output, duration_ms)."""
    t0 = datetime.datetime.utcnow()
    timeout_seconds = int((JOBS.get(phase) or {}).get('timeout_seconds', 180))

    run_env = os.environ.copy()
    tenant = get_tenant()
    run_env['TENANT_CODE'] = tenant['code']
    run_env['TENANT_SCHEMA'] = tenant['schema']
    if phase == 'sync_all':
        run_env['FUSION_AUTO_SYNC_TENANTS'] = tenant['code']
        run_env['FUSION_AUTO_SYNC_STEPS'] = 'all'
        run_env.setdefault('FUSION_AUTO_SYNC_TIMEOUT_SECONDS', '600')
    run_env.setdefault('PYTHONIOENCODING', 'utf-8')
    for k, v in _load_tss_env().items():
        if not run_env.get(k):
            run_env[k] = v
    for k, v in (extra_env or {}).items():
        if v not in (None, ''):
            run_env[k] = str(v)

    abs_script = (script_path if os.path.isabs(script_path)
                  else os.path.join(_PROJECT_ROOT, script_path))
    try:
        proc = subprocess.run(
            [sys.executable, abs_script],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout_seconds,
            cwd=_PROJECT_ROOT,
            env=run_env,
        )
        duration_ms = int((datetime.datetime.utcnow() - t0).total_seconds() * 1000)
        output = (proc.stdout + proc.stderr).strip()
        ok = proc.returncode == 0
    except subprocess.TimeoutExpired:
        duration_ms = timeout_seconds * 1000
        output = f'TIMEOUT: job exceeded {timeout_seconds} seconds'
        ok = False
    except Exception as exc:
        duration_ms = int((datetime.datetime.utcnow() - t0).total_seconds() * 1000)
        output = f'SUBPROCESS ERROR: {exc}'
        ok = False

    try:
        insert_api_call_log(
            tenant['schema'],
            f'JOB_{phase.upper()}',
            http_method='EXEC',
            url=script_path,
            http_status=0 if ok else 1,
            response_status='OK' if ok else 'FAILED',
            response_message=output[-4000:],
            duration_ms=duration_ms,
        )
    except Exception:
        pass

    return ok, output, duration_ms


def _background_log_path(phase):
    """Return a fresh timestamped log path for a background phase run."""
    from datetime import datetime as _dt
    log_dir = os.path.join(_PROJECT_ROOT, 'logs')
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        return None
    stamp = _dt.now().strftime('%Y%m%d_%H%M%S')
    return os.path.join(log_dir, f'{phase}_{stamp}.log')


def _run_script_background(phase, script_path, extra_env=None):
    """Launch script detached — return immediately, redirect stdout/stderr to a log file."""
    run_env = os.environ.copy()
    tenant = get_tenant()
    run_env['TENANT_CODE'] = tenant['code']
    run_env['TENANT_SCHEMA'] = tenant['schema']
    if phase == 'sync_all':
        run_env['FUSION_AUTO_SYNC_TENANTS'] = tenant['code']
        run_env['FUSION_AUTO_SYNC_STEPS'] = 'all'
    run_env.setdefault('PYTHONIOENCODING', 'utf-8')
    run_env['PYTHONUNBUFFERED'] = '1'
    for k, v in _load_tss_env().items():
        if not run_env.get(k):
            run_env[k] = v
    for k, v in (extra_env or {}).items():
        if v not in (None, ''):
            run_env[k] = str(v)

    abs_script = (script_path if os.path.isabs(script_path)
                  else os.path.join(_PROJECT_ROOT, script_path))
    log_path = _background_log_path(phase)
    log_handle = None
    if log_path:
        try:
            log_handle = open(log_path, 'w', encoding='utf-8')
        except Exception:
            log_handle = None
    stdout = log_handle if log_handle else subprocess.DEVNULL
    stderr = subprocess.STDOUT if log_handle else subprocess.DEVNULL
    kwargs = dict(env=run_env, cwd=_PROJECT_ROOT,
                  stdout=stdout, stderr=stderr,
                  bufsize=0)
    if os.name != 'nt':
        kwargs['start_new_session'] = True

    try:
        subprocess.Popen([sys.executable, '-u', abs_script], **kwargs)
        try:
            insert_api_call_log(
                tenant['schema'],
                f'JOB_{phase.upper()}',
                http_method='EXEC',
                url=script_path,
                http_status=0,
                response_status='STARTED',
                response_message=json.dumps({'background': True, 'log_path': log_path}) if log_path else '{"background":true}',
                duration_ms=0,
            )
        except Exception:
            pass
        return True
    except Exception:
        if log_handle:
            try:
                log_handle.close()
            except Exception:
                pass
        return False


def _pipeline_scope_env(form, phase):
    if phase not in {'validate_pipeline', 'submit_pipeline', 'sync_pipeline'}:
        return {}
    cons_ids = (form.get('scope_consignment_ids') or '').strip()
    goods_ids = (form.get('scope_goods_ids') or '').strip()
    supdec_ids = (form.get('scope_supdec_ids') or '').strip()
    truthy = lambda v: (v or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    if phase == 'validate_pipeline':
        return {
            'VALIDATE_PIPELINE_CONSIGNMENT_IDS': cons_ids,
            'VALIDATE_PIPELINE_GOODS_IDS': goods_ids,
            'VALIDATE_PIPELINE_SUPDEC_IDS': supdec_ids,
        }
    if phase == 'sync_pipeline':
        env = {}
        if cons_ids:
            env['SYNC_PIPELINE_CONSIGNMENT_IDS'] = cons_ids
        if truthy(form.get('only_goods')):
            env['SYNC_PIPELINE_ONLY_GOODS'] = '1'
        return env
    env = {
        'SUBMIT_PIPELINE_CONSIGNMENT_IDS': cons_ids,
        'SUBMIT_PIPELINE_GOODS_IDS': goods_ids,
        'SUBMIT_PIPELINE_SUPDEC_IDS': supdec_ids,
    }
    if truthy(form.get('skip_consignments')):
        env['SUBMIT_PIPELINE_SKIP_CONSIGNMENTS'] = '1'
    if truthy(form.get('skip_goods')):
        env['SUBMIT_PIPELINE_SKIP_GOODS'] = '1'
    if truthy(form.get('skip_supdecs')):
        env['SUBMIT_PIPELINE_SKIP_SUPDECS'] = '1'
    return env


def _scoped_consignment_ids(form):
    raw_ids = (form.get('scope_consignment_ids') or '').strip()
    consignment_ids = []
    for raw in raw_ids.split(','):
        raw = raw.strip()
        if not raw:
            continue
        try:
            consignment_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return consignment_ids


def _apply_scoped_generate_sd(form):
    """Stub — generate_SD flag update requires staging tables not present in PRD."""
    return 0


def _safe_next_url(value):
    candidate = (value or '').strip()
    if not candidate:
        return url_for('orchestrator.index')
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return url_for('orchestrator.index')
    if not candidate.startswith('/'):
        return url_for('orchestrator.index')
    return candidate


def _active_schema():
    tenant = get_tenant()
    return (tenant or {}).get('schema') or (tenant or {}).get('code') or 'BKD'


def _output_summary(output):
    lines = [line.strip() for line in (output or '').splitlines() if line.strip()]
    if not lines:
        return 'Completed.'
    for line in reversed(lines):
        if not all(c in '=-*[ ]' for c in line):
            return line[:240]
    return lines[-1][:240]


def _job_flash_category(ok, summary):
    if not ok:
        return 'danger'

    text = (summary or '').strip()
    status_change_match = re.search(r'Status changes:\s*0\b', text, re.IGNORECASE)
    polled_match = re.search(r'Polled:\s*(\d+)\b', text, re.IGNORECASE)
    if status_change_match and polled_match and int(polled_match.group(1)) > 0:
        return 'warning'

    if re.search(r'SDIs discovered=0\b', text, re.IGNORECASE):
        return 'warning'

    return 'success'


@orchestrator_bp.route('/')
def index():
    return render_template(
        'orchestrator/index.html',
        jobs=JOBS,
        queue_stats={},
        recent_runs=_recent_runs(),
        alerts=[],
        movement_stats={},
    )


@orchestrator_bp.route('/run', methods=['POST'])
def run_job():
    phase = request.form.get('phase', 'validate')
    started = datetime.datetime.utcnow()

    if phase == 'all':
        phases_to_run = PIPELINE_PHASES
    elif phase in JOBS:
        phases_to_run = [phase]
    else:
        flash(f'Unknown job phase: {phase}', 'danger')
        return redirect(url_for('orchestrator.index'))

    run_results = []
    overall_ok = True

    for p in phases_to_run:
        job = JOBS[p]
        script = job['script']
        if job.get('background'):
            fired = _run_script_background(p, script)
            run_results.append({
                'phase': p,
                'label': job['label'],
                'script': script,
                'ok': fired,
                'output': 'Started in background — results will appear in Job Logs.' if fired else 'Failed to launch background process.',
                'duration_ms': 0,
                'background': True,
            })
            if not fired:
                overall_ok = False
        else:
            ok, output, duration_ms = _run_script(p, script)
            if not ok:
                overall_ok = False
            run_results.append({
                'phase': p,
                'label': job['label'],
                'script': script,
                'ok': ok,
                'output': output,
                'duration_ms': duration_ms,
                'background': False,
            })
        if phase == 'all' and not run_results[-1]['ok']:
            break

    return render_template(
        'orchestrator/_run_result.html',
        run_results=run_results,
        overall_ok=overall_ok,
        started=started,
        queue_stats={},
    )


@orchestrator_bp.route('/run-and-return', methods=['POST'])
def run_and_return():
    phase = request.form.get('phase', '').strip()
    next_url = _safe_next_url(request.form.get('next_url') or request.referrer)

    if phase == 'all':
        phases_to_run = PIPELINE_PHASES
    elif phase in JOBS:
        phases_to_run = [phase]
    else:
        flash(f'Unknown job phase: {phase}', 'danger')
        return redirect(next_url)

    try:
        _apply_scoped_generate_sd(request.form)
    except Exception as exc:
        flash(f'Could not update Generate SDI option before running the pipeline: {exc}', 'warning')

    overall_ok = True
    last_summary = 'Completed.'
    for current_phase in phases_to_run:
        job = JOBS[current_phase]
        script = job['script']
        if job.get('background'):
            fired = _run_script_background(
                current_phase, script,
                extra_env=_pipeline_scope_env(request.form, current_phase),
            )
            if fired:
                last_summary = 'Started in background — check Job Logs for results.'
                flash(f'{job["label"]}: {last_summary}', 'info')
            else:
                overall_ok = False
                flash(f'{job["label"]}: failed to launch background process.', 'danger')
        else:
            ok, output, _duration_ms = _run_script(
                current_phase,
                script,
                extra_env=_pipeline_scope_env(request.form, current_phase),
            )
            overall_ok = overall_ok and ok
            last_summary = _output_summary(output)
            if not ok:
                flash(f'{job["label"]}: {last_summary}', 'danger')
                if phase == 'all':
                    break

    if phase != 'all' and not JOBS[phase].get('background'):
        label = JOBS[phase]['label']
        flash(f'{label}: {last_summary}', _job_flash_category(overall_ok, last_summary))
    elif phase == 'all' and overall_ok and not any(JOBS[p].get('background') for p in phases_to_run):
        flash(f'Pipeline: {last_summary}', _job_flash_category(overall_ok, last_summary))
    return redirect(next_url)


@orchestrator_bp.route('/status')
def status():
    """HTMX partial that refreshes recent run history."""
    return render_template(
        'orchestrator/_status.html',
        queue_stats={},
        movement_stats={},
        recent_runs=_recent_runs(20),
        alerts=[],
    )
