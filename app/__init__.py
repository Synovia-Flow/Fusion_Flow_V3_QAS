"""
Fusion Flow V2 - Flask Application Factory
Synovia Digital Ltd - Birkdale TSS Portal
"""
import os
import time
import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from flask import Flask, jsonify, redirect, url_for, g, request, session
from whitenoise import WhiteNoise

from .db import init_db, query_one
from .status_utils import badge_class_for_status, fixed_consignment_local_status, status_display
from .tss_guidance import explain_status, explain_tss_error, format_error_explanation
from .workflow_refs import (
    consignment_detail_url,
    ens_detail_url,
    gmr_detail_url,
    goods_detail_url,
    supdec_detail_url,
)

# Paths that don't require session authentication. Ingest webhook routes do
# their own API-key check before accepting payloads from mailbox/local workers.
_PUBLIC_PATHS = {
    '/',
    '/auth/login',
    '/auth/logout',
    '/health',
    '/static',
    '/ingest/receive',
    '/ingest/receive-batch',
    '/ingest/receive-email',
    '/ingest/receive-sales-orders',
    '/ingest/receive-sales-orders-details',
}

logger = logging.getLogger(__name__)


def create_app(config_name=None):
    """Create and configure the Flask application."""
    app = Flask(__name__)

    # Load configuration
    config_name = config_name or os.environ.get('FLASK_ENV', 'production')
    from config.settings import config_map
    app.config.from_object(config_map.get(config_name, config_map['production']))

    # Initialise database connection lifecycle
    init_db(app)

    # Auth guard — redirect unauthenticated requests to login
    @app.before_request
    def _require_login():
        path = request.path
        if any(path == p or path.startswith(p + '/') for p in _PUBLIC_PATHS):
            return None
        if not session.get('logged_in'):
            return redirect(url_for('auth.login', next=request.path))

    # Request timing middleware — adds X-Response-Time header, warns on slow requests
    @app.before_request
    def _start_timer():
        g.request_start = time.monotonic()

    @app.after_request
    def _record_timing(response):
        duration_ms = int((time.monotonic() - g.get('request_start', time.monotonic())) * 1000)
        response.headers['X-Response-Time'] = f'{duration_ms}ms'
        if duration_ms > 2000 and not request.path.startswith('/static'):
            logger.warning('Slow request: %s %s took %dms', request.method, request.path, duration_ms)
        return response

    # Static files via WhiteNoise (no Nginx needed)
    app.wsgi_app = WhiteNoise(
        app.wsgi_app,
        root=os.path.join(app.root_path, 'static'),
        prefix='static/'
    )

    # Register blueprints
    from .blueprints.auth.routes import auth_bp
    app.register_blueprint(auth_bp)

    from .blueprints.dashboard.routes import dashboard_bp
    from .blueprints.master_data.routes import master_data_bp
    from .blueprints.declarations.routes import declarations_bp
    from .blueprints.jobs.routes import jobs_bp

    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
    app.register_blueprint(master_data_bp, url_prefix='/master-data')
    app.register_blueprint(declarations_bp, url_prefix='/ens')
    app.register_blueprint(jobs_bp, url_prefix='/jobs')

    # Help Centre (TSS Declaration Guide)
    from .blueprints.help.routes import help_bp
    app.register_blueprint(help_bp, url_prefix='/help')

    # Consignment + Goods CRUD (deployed 20260408_113256)
    from app.blueprints.consignments.routes import consignments_bp
    from app.blueprints.goods.routes import goods_bp
    app.register_blueprint(consignments_bp)    # /consignments
    app.register_blueprint(goods_bp)           # /goods

    # SFD — read-only view of consignments that have an SFD reference from TSS
    from app.blueprints.sfd.routes import sfd_bp
    app.register_blueprint(sfd_bp)             # /sfd

    # Supplementary Declarations (SDI) CRUD (deployed 20260408_134029)
    from app.blueprints.supdec.routes import supdec_bp
    app.register_blueprint(supdec_bp)           # /supdec

    # GMR — Goods Movement Reference (Route A Step 5) — deployed 20260409
    from app.blueprints.gmr.routes import gmr_bp
    app.register_blueprint(gmr_bp)              # /gmr

    # Templates & Bulk Upload (deployed 20260408_135133)
    from app.blueprints.bulk.routes import templates_bp
    app.register_blueprint(templates_bp)        # /bulk

    # Analytics — performance metrics, latency trends, declaration funnel
    from app.blueprints.analytics.routes import analytics_bp
    app.register_blueprint(analytics_bp, url_prefix='/analytics')

    # Tenant Analytics — cross-tenant usage, billing estimate (SYD owner only).
    # Lives under /analytics/tenants so it appears as a tab inside the Analytics
    # module; the blueprint's before_request 404s every non-SYD tenant.
    from app.blueprints.tenant_analytics.routes import tenant_analytics_bp
    app.register_blueprint(tenant_analytics_bp, url_prefix='/analytics/tenants')

    # Orchestrator — web-triggered validate/submit/sync jobs with full DB logging
    from app.blueprints.orchestrator.routes import orchestrator_bp
    app.register_blueprint(orchestrator_bp, url_prefix='/orchestrate')

    # Operations — cancel, delete (cascade), sync individual declarations
    from app.blueprints.operations.routes import operations_bp
    app.register_blueprint(operations_bp, url_prefix='/operations')

    # Pre-Check — EORI/XORI + commodity code validation via HMRC APIs
    from app.blueprints.precheck.routes import precheck_bp
    app.register_blueprint(precheck_bp)   # /precheck

    # Technical Logs — job execution history, API call audit, latency
    from app.blueprints.technical.routes import technical_bp
    app.register_blueprint(technical_bp, url_prefix='/technical')

    # Test Case Creator — scenario-based end-to-end test automation
    from app.blueprints.testcases.routes import testcases_bp
    app.register_blueprint(testcases_bp, url_prefix='/testcases')

    # Risk & Validation Module — EORI/format checks, credential status, orphan detection
    from app.blueprints.risk.routes import risk_bp
    app.register_blueprint(risk_bp, url_prefix='/risk')

    # Ingest — local document receive webhook + queue dashboard
    from app.blueprints.ingest.routes import ingest_bp
    app.register_blueprint(ingest_bp)   # /ingest

    # Admin — runtime configuration via BKD.AppConfiguration (protected by _require_login)
    from app.blueprints.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix='/admin')

    # Stitch public/static surfaces + protected live-data API.
    from app.blueprints.stitch.routes import stitch_bp, stitch_pages_bp
    app.register_blueprint(stitch_pages_bp)
    app.register_blueprint(stitch_bp)

    # Birkdale-style business-reference aliases.
    from .blueprints.consignments.routes import detail_by_ref as _consignment_detail_by_ref, list_view as _consignment_list
    from .blueprints.master_data.routes import (
        company as _master_data_company,
        company_edit as _master_data_company_edit,
        cv_table_detail as _master_data_cv_table_detail,
        cv_tables as _master_data_cv_tables,
        eori_checker as _master_data_eori_checker,
        index as _master_data_index,
        partner_create as _master_data_partner_create,
        partner_edit as _master_data_partner_edit,
        partners as _master_data_partners,
        product_create as _master_data_product_create,
        product_edit as _master_data_product_edit,
        products as _master_data_products,
    )
    from .blueprints.supdec.routes import detail_by_ref as _supdec_detail_by_ref, list_view as _supdec_list

    @app.route('/declarations/')
    def declarations_list_compat():
        from flask import redirect
        return redirect('/ens/', 301)

    @app.route('/declarations/<path:rest>')
    def declarations_detail_compat(rest):
        from flask import redirect
        return redirect(f'/ens/{rest}', 301)

    @app.route('/flow/declaration-workbench')
    def declaration_workbench_public_alias():
        from app.blueprints.operations.routes import declaration_workbench
        return declaration_workbench()

    @app.route('/flow/declaration-workbench/<path:ens_key>')
    def declaration_workbench_detail_public_alias(ens_key):
        from app.blueprints.operations.routes import declaration_workbench_detail
        return declaration_workbench_detail(ens_key)

    @app.route('/sdi/')
    def sdi_list_alias():
        return _supdec_list()

    @app.route('/sdi/<string:sup_ref>/detail')
    def sdi_detail_alias(sup_ref):
        return _supdec_detail_by_ref(sup_ref)

    @app.route('/masterdata/')
    def masterdata_index_alias():
        return _master_data_index()

    @app.route('/masterdata/company')
    def masterdata_company_alias():
        return _master_data_company()

    @app.route('/masterdata/company/edit', methods=['GET', 'POST'])
    def masterdata_company_edit_alias():
        return _master_data_company_edit()

    @app.route('/masterdata/partners', methods=['GET'])
    def masterdata_partners_alias():
        return _master_data_partners()

    @app.route('/masterdata/partners/new', methods=['GET', 'POST'])
    def masterdata_partner_create_alias():
        return _master_data_partner_create()

    @app.route('/masterdata/partners/<int:partner_id>/edit', methods=['GET', 'POST'])
    def masterdata_partner_edit_alias(partner_id):
        return _master_data_partner_edit(partner_id)

    @app.route('/masterdata/products', methods=['GET'])
    def masterdata_products_alias():
        return _master_data_products()

    @app.route('/masterdata/products/new', methods=['GET', 'POST'])
    def masterdata_product_create_alias():
        return _master_data_product_create()

    @app.route('/masterdata/products/<int:product_id>/edit', methods=['GET', 'POST'])
    def masterdata_product_edit_alias(product_id):
        return _master_data_product_edit(product_id)

    @app.route('/masterdata/cv-tables')
    def masterdata_cv_tables_alias():
        return _master_data_cv_tables()

    @app.route('/masterdata/cv-tables/<string:table_name>')
    def masterdata_cv_table_detail_alias(table_name):
        return _master_data_cv_table_detail(table_name)

    @app.route('/masterdata/eori-checker', methods=['GET', 'POST'])
    def masterdata_eori_checker_alias():
        return _master_data_eori_checker()

    # Root redirect
    @app.route('/')
    def index():
        if session.get('logged_in'):
            return redirect(url_for('dashboard.index'))
        return redirect(url_for('auth.login'))

    # Health check (Render uses this) — enhanced with DB ping + queue depth + error rate
    @app.route('/health')
    def health():
        db_ok = False
        queue_depth = 0
        error_rate_1h = 0.0

        try:
            query_one('SELECT 1 AS ok')
            db_ok = True

            qd = query_one(
                "SELECT COUNT(*) AS c FROM [STG].[BKD_ENS_Headers]"
                " WHERE sub_status IN ('PENDING','STAGED')"
            )
            queue_depth = (qd or {}).get('c', 0)

            er = query_one("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN HttpStatus != 200 OR HttpStatus IS NULL
                             THEN 1 ELSE 0 END) AS failed
                FROM [TSS].[BKD_API_Exchanges]
                WHERE CalledAt > DATEADD(hour, -1, SYSUTCDATETIME())
            """) or {}
            if er.get('total'):
                error_rate_1h = round((er.get('failed') or 0) / er['total'] * 100, 1)
        except Exception as exc:
            logger.error('Health check DB query failed: %s', exc)

        status = 'healthy' if db_ok else 'degraded'
        # Always return 200 — Render uses this to confirm the process is alive.
        # A 503 here causes Render to fail the deploy and roll back, even when
        # the app is running fine and the DB is temporarily unreachable.
        return jsonify({
            'status': status,
            'service': 'Fusion Flow V2 \u2013 Birkdale TSS Portal',
            'client': app.config.get('CLIENT_CODE', 'BKD'),
            'checks': {
                'database': 'ok' if db_ok else 'error',
                'queue_depth': queue_depth,
                'error_rate_1h_pct': error_rate_1h,
            },
        }), 200

    # Template context processors
    @app.context_processor
    def inject_globals():
        from flask import session
        # Active tenant drives client_name/client_code so all existing templates
        # automatically show the right tenant branding without modification.
        tenant_code = session.get('tenant_code', app.config.get('CLIENT_CODE', 'BKD'))
        tenant_name = session.get('tenant_name', app.config.get('CLIENT_NAME', 'Birkdale Sales Ltd'))
        try:
            from app.config_store import cfg
            raw_tss_environment = (
                cfg.get('TSS_API', 'ENVIRONMENT', tenant_code=tenant_code) or ''
            ).strip().lower()
            raw_demo_flag = str(
                cfg.get('DEMO', 'ENABLED', tenant_code=tenant_code) or ''
            ).strip().lower()
            if raw_tss_environment:
                demo_mode_enabled = raw_tss_environment == 'demo'
            else:
                demo_mode_enabled = raw_demo_flag in {'1', 'true', 'yes', 'y', 'on', 'enabled'}
        except Exception:
            raw_tss_environment = ''
            demo_mode_enabled = False

        if demo_mode_enabled:
            tss_environment = 'demo'
            tss_environment_label = 'DEMO'
        elif raw_tss_environment in {'production', 'prod', 'prd'}:
            tss_environment = 'production'
            tss_environment_label = 'PRODUCTION'
        elif raw_tss_environment in {'test', 'testing', 'tst'}:
            tss_environment = 'test'
            tss_environment_label = 'TEST'
        else:
            fallback_url = app.config.get('TSS_API_BASE_URL', '')
            if 'test' in fallback_url.lower():
                tss_environment = 'test'
                tss_environment_label = 'TEST'
            else:
                tss_environment = 'unknown'
                tss_environment_label = 'UNSET'

        return {
            'client_name':  tenant_name,
            'client_code':  tenant_code,
            'tenant_code':  tenant_code,
            'tenant_name':  tenant_name,
            'tenant_logo_filename': {
                'BKD': 'img/birkdale.png',
                'CWF': 'img/countrywide.png',
                'CLR': 'img/claritycargologo.png',
                'PLE': 'img/primeline-express.png',
                'SYD': 'img/synovia_logo.jpg',
            }.get(str(tenant_code or '').upper()),
            'tss_environment': tss_environment,
            'tss_environment_label': tss_environment_label,
            'demo_mode_enabled': demo_mode_enabled,
            'username':     session.get('username', ''),
            'ingest_url':   os.environ.get('INGEST_SERVICE_URL', '').rstrip('/'),
            'ens_detail_url': ens_detail_url,
            'consignment_detail_url': consignment_detail_url,
            'goods_detail_url': goods_detail_url,
            'supdec_detail_url': supdec_detail_url,
            'gmr_detail_url': gmr_detail_url,
            'status_badge_class': badge_class_for_status,
            'fixed_consignment_local_status': fixed_consignment_local_status,
            'explain_status': explain_status,
            'explain_tss_error': explain_tss_error,
            'format_error_explanation': format_error_explanation,
        }

    # Jinja2 filter: safely format datetime objects OR strings
    # Prevents crashes when a value is unexpectedly a string instead of datetime.
    import datetime as _dt
    from zoneinfo import ZoneInfo

    def _parse_datetime_value(value):
        if value is None:
            return None
        if isinstance(value, _dt.datetime):
            return value
        if isinstance(value, _dt.date):
            return _dt.datetime.combine(value, _dt.time.min)
        raw = str(value).strip()
        if not raw:
            return None
        normalized = raw.replace('Z', '+00:00')
        if '.' in normalized:
            head, tail = normalized.split('.', 1)
            for marker in ('+', '-'):
                if marker in tail:
                    frac, tz = tail.split(marker, 1)
                    tail = f"{frac[:6]}{marker}{tz}"
                    break
            else:
                tail = tail[:6]
            normalized = f"{head}.{tail}"
        try:
            return _dt.datetime.fromisoformat(normalized)
        except Exception:
            for parse_fmt in (
                '%d/%m/%Y %H:%M:%S',
                '%d/%m/%Y %H:%M',
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M',
                '%Y-%m-%d',
            ):
                try:
                    return _dt.datetime.strptime(raw.split('.')[0], parse_fmt)
                except Exception:
                    continue
        return None

    def _last_sunday_utc(year, month, hour=1):
        if month == 12:
            next_month = _dt.datetime(year + 1, 1, 1, tzinfo=_dt.timezone.utc)
        else:
            next_month = _dt.datetime(year, month + 1, 1, tzinfo=_dt.timezone.utc)
        last_day = next_month - _dt.timedelta(days=1)
        days_since_sunday = (last_day.weekday() + 1) % 7
        last_sunday = last_day - _dt.timedelta(days=days_since_sunday)
        return last_sunday.replace(hour=hour, minute=0, second=0, microsecond=0)

    def _fallback_dublin_timezone(utc_value):
        start = _last_sunday_utc(utc_value.year, 3)
        end = _last_sunday_utc(utc_value.year, 10)
        offset_hours = 1 if start <= utc_value < end else 0
        return _dt.timezone(_dt.timedelta(hours=offset_hours))

    def _datefmt(value, fmt='%d/%m/%Y %H:%M'):
        if value is None:
            return '—'
        if isinstance(value, (_dt.datetime, _dt.date)):
            return value.strftime(fmt)
        raw = str(value).strip()
        if not raw:
            return '---'
        normalized = raw.replace('Z', '+00:00')
        if '.' in normalized:
            head, tail = normalized.split('.', 1)
            for marker in ('+', '-'):
                if marker in tail:
                    frac, tz = tail.split(marker, 1)
                    tail = f"{frac[:6]}{marker}{tz}"
                    break
            else:
                tail = tail[:6]
            normalized = f"{head}.{tail}"
        try:
            return _dt.datetime.fromisoformat(normalized).strftime(fmt)
        except Exception:
            for parse_fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                try:
                    return _dt.datetime.strptime(raw.split('.')[0], parse_fmt).strftime(fmt)
                except Exception:
                    continue
            return raw

    def _localdt(value, fmt='%d/%m/%Y %H:%M', tz_name=None):
        parsed = _parse_datetime_value(value)
        if parsed is None:
            if value is None:
                return '---'
            return str(value).strip() or '---'
        display_tz_name = (
            tz_name
            or os.environ.get('PORTAL_TIMEZONE')
            or os.environ.get('DISPLAY_TIMEZONE')
            or 'Europe/Dublin'
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        try:
            display_tz = ZoneInfo(display_tz_name)
        except Exception:
            if display_tz_name in {'Europe/Dublin', 'Europe/London'}:
                display_tz = _fallback_dublin_timezone(parsed.astimezone(_dt.timezone.utc))
            else:
                display_tz = _dt.timezone.utc
        return parsed.astimezone(display_tz).strftime(fmt)

    app.jinja_env.filters['datefmt'] = _datefmt
    app.jinja_env.filters['localdt'] = _localdt
    app.jinja_env.filters['status_display'] = status_display
    app.jinja_env.filters['status_badge_class'] = badge_class_for_status
    app.jinja_env.filters['fixed_consignment_local_status'] = fixed_consignment_local_status

    def _decimal_scale(value, scale=2):
        if value in (None, ''):
            return ''
        text = str(value).strip()
        if not text:
            return ''
        try:
            dec = Decimal(text).quantize(Decimal('1').scaleb(-int(scale)), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            return text
        normalized = format(dec.normalize(), 'f')
        if '.' in normalized:
            normalized = normalized.rstrip('0').rstrip('.')
        return normalized or '0'

    app.jinja_env.filters['decimal_scale'] = _decimal_scale

    return app
