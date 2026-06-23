"""Operations Blueprint — automation PRD stub.

All legacy workbench routes (declaration chain visualisation, TSS sync/cancel,
staging-table deletes) are disabled in automation PRD because they target
legacy Staging* tables that do not exist in this environment.

Automation flow is driven by:
  ING.BKD_EmailMessage / ING.BKD_EmailAttachment  → email intake
  STG.BKD_ENS_*  / STG.BKD_*                      → normalised drafts
  TSS.BKD_*                                        → remote TSS state
  AppConfiguration / CompanyMaster                 → config / masterdata

Portal views for the automation flow live in:
  /dashboard  — activity summary
  /ens        — STG ENS headers
  /consignments — STG consignments
  /sfd        — TSS SFD tracking
  /supdec     — STG SDI headers
  /ingest     — ING email / document queue
"""
from flask import Blueprint, flash, redirect, url_for
from app.status_utils import consignment_should_discover_sdi
from app.tenant import get_tenant

operations_bp = Blueprint(
    'operations', __name__,
    template_folder='../../templates/operations'
)

_STUB = 'Not available in automation PRD yet.'
_REDIR = 'dashboard.index'

_GOODS_READY_STATUSES = {'CREATED', 'IMPORTED', 'SYNCED', 'SUBMITTED', 'ACCEPTED', 'ACTIVE'}


def _workbench_consignment_requires_sdi(row):
    return consignment_should_discover_sdi(row)


def _workbench_goods_ready(goods):
    return bool(goods) and all(
        (g.get('status') or '').upper() in _GOODS_READY_STATUSES for g in goods
    )


def _tenant_schema():
    return get_tenant()['schema']


@operations_bp.route('/')
def index():
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/action-required')
def action_required():
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/flow')
def flow():
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/flow/declaration-workbench')
def declaration_workbench():
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/flow/declaration-workbench/<path:ens_key>')
def declaration_workbench_detail(ens_key):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/flow/declaration-workbench/import-tss', methods=['POST'])
def declaration_workbench_import_tss():
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/flow/declaration-workbench/<path:ens_key>/sync-tss', methods=['POST'])
def declaration_workbench_sync_tss(ens_key):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/flow/declaration-workbench/<path:ens_key>/submit-ens', methods=['POST'])
def declaration_workbench_submit_ens(ens_key):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/cancel-tss/<int:dec_id>', methods=['POST'])
def cancel_tss(dec_id):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/delete/<int:dec_id>', methods=['POST'])
def delete_dec(dec_id):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/delete-pipeline/<int:ens_id>', methods=['POST'])
def delete_pipeline(ens_id):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/delete-orphans', methods=['POST'])
def delete_orphans():
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))


@operations_bp.route('/sync-tss/<int:dec_id>', methods=['POST'])
def sync_single(dec_id):
    flash(_STUB, 'warning')
    return redirect(url_for(_REDIR))
