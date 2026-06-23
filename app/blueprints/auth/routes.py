"""
Auth Blueprint - Session-based login for Fusion Flow V2 BKD Portal

Built-in tenants (temporary bridge - replace with M365 when ready):
  birkdale     / admin  ->  tenant BKD (Birkdale)
  countrywide  / admin  ->  tenant CWF (Countrywide)
  claritycargo / admin  ->  tenant CLR (Clarity Cargo)
  primeline    / admin  ->  tenant PLE (Primeline Express)
  synovia      / admin  ->  tenant SYD (Synovia Digital — demo)

Tenant context stored in session: tenant_code, tenant_name, username.
Resolved centrally via app.tenant - no if/else scattered elsewhere.
"""
from flask import Blueprint, render_template, request, redirect, url_for, session

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('dashboard.index'))

    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        from app.tenant import get_tenant_by_credentials
        tenant = get_tenant_by_credentials(username, password)

        if tenant:
            session.permanent = True
            session['logged_in']   = True
            session['username']    = username
            session['tenant_code'] = tenant['code']
            session['tenant_name'] = tenant['name']
            next_url = request.args.get('next') or url_for('dashboard.index')
            return redirect(next_url)

        error = 'Invalid username or password.'

    return render_template('auth/login.html', error=error)


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))
