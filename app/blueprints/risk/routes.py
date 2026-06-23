"""Risk blueprint — stub in automation PRD.

Legacy risk dashboard queried portal-managed staging tables that do not exist in
Fusion_TSS_Automation_PRD. Data quality checks are performed during the
automated email-to-TSS pipeline.
"""
from flask import Blueprint, render_template

risk_bp = Blueprint('risk', __name__, template_folder='../../templates/risk')


@risk_bp.route('/')
def index():
    return render_template('risk/index.html',
        creds={}, all_issues=[], filtered=[], errors=[], warnings=[],
        cats={}, filter_cat='', filter_entity='', entities=[])
