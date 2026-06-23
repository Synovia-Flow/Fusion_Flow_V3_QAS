"""Jobs blueprint — stub in automation PRD.

Legacy job history queried ApiCallLog and StagingDeclarations tables that do not
exist in Fusion_TSS_Automation_PRD. Job execution history is available in
Technical > Jobs backed by TSS.BKD_API_Exchanges.
"""
from flask import Blueprint, flash, redirect, url_for

jobs_bp = Blueprint('jobs', __name__, template_folder='../../templates/jobs')

_STUB_MSG = 'Job history is available in Technical logs.'
_REDIR = 'technical.index'


@jobs_bp.route('/')
def history():
    flash(_STUB_MSG, 'info')
    return redirect(url_for(_REDIR, tab='jobs'))


@jobs_bp.route('/partial')
def history_partial():
    return redirect(url_for(_REDIR, tab='jobs'))


@jobs_bp.route('/retry/<int:dec_id>', methods=['POST'])
def retry(dec_id):
    flash('Manual retry is not available in automation PRD.', 'warning')
    return redirect(url_for(_REDIR, tab='jobs'))
