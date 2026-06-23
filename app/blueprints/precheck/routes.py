"""Pre-check blueprint — stub in automation PRD.

Legacy pre-check dashboard queried portal-managed staging tables that do not
exist in Fusion_TSS_Automation_PRD. EORI and commodity validation is performed
inline during the automated pipeline.
"""
from flask import Blueprint, flash, jsonify, redirect, url_for
from app.hmrc_api import check_eori, check_commodity, _normalise_commodity

precheck_bp = Blueprint(
    'precheck', __name__,
    template_folder='../../templates/precheck',
    url_prefix='/precheck',
)

_STUB_MSG = 'Pre-check dashboard is not available in automation PRD.'
_REDIR = 'orchestrator.index'


@precheck_bp.route('/')
def dashboard():
    flash(_STUB_MSG, 'info')
    return redirect(url_for(_REDIR))


@precheck_bp.route('/check/eori', methods=['POST'])
def check_single_eori():
    flash(_STUB_MSG, 'info')
    return redirect(url_for(_REDIR))


@precheck_bp.route('/check/commodity', methods=['POST'])
def check_single_commodity():
    flash(_STUB_MSG, 'info')
    return redirect(url_for(_REDIR))


@precheck_bp.route('/run', methods=['POST'])
def run_all():
    flash(_STUB_MSG, 'info')
    return redirect(url_for(_REDIR))


@precheck_bp.route('/api/eori/<path:eori>')
def api_eori(eori):
    """JSON API — live HMRC EORI check (no staging dependency)."""
    try:
        result = check_eori(eori)
        return jsonify(result)
    except Exception as exc:
        return jsonify({'valid': False, 'error': str(exc)}), 200


@precheck_bp.route('/api/commodity/<path:code>')
def api_commodity(code):
    """JSON API — live HMRC commodity check (no staging dependency)."""
    try:
        normalised = _normalise_commodity(code)
        result = check_commodity(normalised)
        return jsonify(result)
    except Exception as exc:
        return jsonify({'valid': False, 'error': str(exc)}), 200
