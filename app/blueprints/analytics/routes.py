"""Analytics Blueprint — automation PRD stub.

Legacy analytics routes queried ApiCallLog and StagingDeclarations tables,
neither of which exist in Fusion_TSS_Automation_PRD.

API exchange analytics will be rebuilt against TSS.BKD_API_Exchanges.
Until then, all routes return a stub response.
"""
from flask import Blueprint, jsonify, redirect, url_for

analytics_bp = Blueprint('analytics', __name__, template_folder='../../templates/analytics')

_STUB_JSON = {'status': 'not_available', 'detail': 'Analytics not available in automation PRD yet.'}


@analytics_bp.route('/')
def index():
    return redirect(url_for('dashboard.index'))


@analytics_bp.route('/api/volume')
def api_volume():
    return jsonify(_STUB_JSON), 503


@analytics_bp.route('/api/latency')
def api_latency():
    return jsonify(_STUB_JSON), 503


@analytics_bp.route('/api/throughput')
def api_throughput():
    return jsonify(_STUB_JSON), 503


@analytics_bp.route('/api/errors')
def api_errors():
    return jsonify(_STUB_JSON), 503


@analytics_bp.route('/export', endpoint='export')
def export_csv():
    return redirect(url_for('dashboard.index'))


@analytics_bp.route('/api/hourly')
def api_hourly():
    return jsonify(_STUB_JSON), 503


@analytics_bp.route('/api/daily')
def api_daily():
    return jsonify(_STUB_JSON), 503
