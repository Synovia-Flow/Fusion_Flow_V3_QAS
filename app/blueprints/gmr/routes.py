"""GMR Blueprint — automation PRD stub.

GMR / Route A routes are disabled in automation PRD because they depend on
legacy staging tables (StagingGmrs, StagingEnsHeaders, StagingConsignments,
StagingGoodsItems) that do not exist in the Fusion_TSS_Automation_PRD database.

Route A / GVMS automation will be re-implemented against STG.BKD_GMR_Movements
and TSS.BKD_GMR_Movements when the STG pipeline is wired up.
"""
from flask import Blueprint, flash, redirect, url_for

gmr_bp = Blueprint('gmr', __name__,
    template_folder='../../templates/gmr',
    url_prefix='/gmr')

_STUB = 'Not available in automation PRD yet.'
_REDIR = 'dashboard.index'

_TSS_GMR_BLOCKED_STATUSES = {'ARRIVED', 'CLOSED', 'COMPLETED'}


def _can_cancel_gmr_in_tss(row):
    row = row or {}
    if not row.get('gmr_id'):
        return False
    gvms = (row.get('gvms_status') or '').strip().lower()
    if gvms in ('arrived', 'closed', 'completed'):
        return False
    return row.get('status') in ('SUBMITTED', 'ACTIVE')


def _consignment_is_test_fallback_ready(row):
    row = row or {}
    if not row.get('dec_reference'):
        return False
    tss = (row.get('tss_status') or '').upper()
    if 'TRADER INPUT' in tss or tss in ('REJECTED', 'CANCELLED', 'ABANDONED'):
        return False
    goods = row.get('goods_count') or 0
    ready = row.get('goods_ready_count') or 0
    return goods > 0 and goods == ready


@gmr_bp.route('/')
def list_view():
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@gmr_bp.route('/create', methods=['GET', 'POST'])
def create():
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@gmr_bp.route('/<string:gmr_ref>/detail')
def detail_by_ref(gmr_ref):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@gmr_bp.route('/<int:sid>')
def detail(sid):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@gmr_bp.route('/<int:sid>/edit', methods=['GET', 'POST'])
def edit(sid):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@gmr_bp.route('/<int:sid>/retry', methods=['POST'])
def retry(sid):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@gmr_bp.route('/<int:sid>/delete', methods=['POST'])
def delete(sid):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@gmr_bp.route('/<int:sid>/cancel-tss', methods=['POST'])
def cancel_tss(sid):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@gmr_bp.route('/<int:sid>/mark-active', methods=['POST'])
def mark_active(sid):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))
