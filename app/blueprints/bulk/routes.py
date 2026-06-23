"""Bulk template/upload blueprint — stub in automation PRD.

Legacy bulk upload required portal-managed staging tables that do not exist in
Fusion_TSS_Automation_PRD. Data enters the pipeline via the email ingestion path
rather than manual CSV upload.
"""
from flask import Blueprint, flash, redirect, url_for

templates_bp = Blueprint('templates', __name__,
    template_folder='../../templates/bulk',
    url_prefix='/bulk')

_STUB_MSG = 'Bulk upload is not available in automation PRD.'
_REDIR = 'orchestrator.index'


@templates_bp.route('/')
def index():
    flash(_STUB_MSG, 'info')
    return redirect(url_for(_REDIR))


@templates_bp.route('/download/<entity_type>')
def download_template(entity_type):
    flash(_STUB_MSG, 'info')
    return redirect(url_for(_REDIR))


@templates_bp.route('/download-cv/<cv_table>')
def download_cv(cv_table):
    flash(_STUB_MSG, 'info')
    return redirect(url_for(_REDIR))


@templates_bp.route('/upload/<entity_type>', methods=['GET', 'POST'])
def upload(entity_type):
    flash(_STUB_MSG, 'info')
    return redirect(url_for(_REDIR))
