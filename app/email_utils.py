"""
Email Utility — Fusion Flow V2
Sends transactional emails via Office 365 SMTP AUTH (STARTTLS on port 587).

Usage:
    from app.email_utils import send_notification
    ok, err = send_notification('customer@example.com', 'cons_validated', cons_record)

All sends are logged to TSS.BKD_API_Exchanges with call_type='EMAIL_SENT'.
Config is read from Admin Settings (SMTP.SERVER, SMTP.PORT, SMTP.SENDER_EMAIL,
SMTP.SENDER_PASSWORD, SMTP.ENABLED), with legacy Flask config/env fallbacks.
"""
import smtplib
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

log = logging.getLogger(__name__)

# ── Notification type registry ─────────────────────────────────────────────

NOTIFICATION_TYPES = {
    'doc_received': {
        'label': 'Document Received — Information Required',
        'subject': 'Action Required: Your import document has been received',
    },
    'cons_validated': {
        'label': 'Consignment Validated',
        'subject': 'Your consignment has been validated and submitted to HMRC',
    },
    'cons_failed': {
        'label': 'Consignment — Action Required',
        'subject': 'Action Required: Issue with your consignment declaration',
    },
    'arrival_confirmed': {
        'label': 'Arrival Confirmed — Complete Supplementary Declaration',
        'subject': 'Action Required: Your goods have arrived — Supplementary Declaration needed',
    },
    'sdi_reminder': {
        'label': 'Supplementary Declaration Reminder',
        'subject': 'Reminder: Supplementary Declaration due by 10th of this month',
    },
    'sdi_overdue': {
        'label': 'Supplementary Declaration OVERDUE',
        'subject': 'OVERDUE: Supplementary Declaration must be submitted immediately',
    },
    'custom': {
        'label': 'Custom Message',
        'subject': 'Message from Synovia Integration',
    },
}


# ── Core send function ─────────────────────────────────────────────────────

def _truthy_config(value, default=True):
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'y', 'on', 'enabled'}:
        return True
    if text in {'0', 'false', 'no', 'n', 'off', 'disabled'}:
        return False
    return default


def _first_config_value(*values, default=''):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _smtp_db_value(key):
    try:
        from app import config_store
        return config_store.get_db_value('SMTP', key)
    except Exception:
        return None


def resolve_smtp_config(app=None):
    """Resolve SMTP config from Admin Settings, legacy Flask config and env.

    Admin Settings stores values as SMTP.SERVER, SMTP.SENDER_EMAIL and
    SMTP.SENDER_PASSWORD. Older code/config used SMTP_SERVER, SMTP_USERNAME and
    SMTP_PASSWORD, so this resolver accepts both shapes.
    """
    from flask import current_app

    cfg = (app or current_app).config

    server = _first_config_value(
        _smtp_db_value('SERVER'),
        cfg.get('SMTP_SERVER'),
        os.environ.get('SMTP_SERVER'),
        default='smtp.office365.com',
    )
    port = _first_config_value(
        _smtp_db_value('PORT'),
        cfg.get('SMTP_PORT'),
        os.environ.get('SMTP_PORT'),
        default='587',
    )
    sender = _first_config_value(
        _smtp_db_value('SENDER_EMAIL'),
        cfg.get('SMTP_SENDER'),
        cfg.get('SMTP_USERNAME'),
        os.environ.get('SMTP_SENDER'),
        os.environ.get('SMTP_USERNAME'),
    )
    username = _first_config_value(
        _smtp_db_value('USERNAME'),
        sender,
        cfg.get('SMTP_USERNAME'),
        os.environ.get('SMTP_USERNAME'),
    )
    password = _first_config_value(
        _smtp_db_value('SENDER_PASSWORD'),
        _smtp_db_value('PASSWORD'),
        cfg.get('SMTP_PASSWORD'),
        os.environ.get('SMTP_PASSWORD'),
    )
    enabled_raw = _first_config_value(
        _smtp_db_value('ENABLED'),
        cfg.get('SMTP_ENABLED'),
        os.environ.get('SMTP_ENABLED'),
        default='true',
    )

    try:
        port_int = int(port)
    except (TypeError, ValueError):
        port_int = 587

    return {
        'server': server,
        'port': port_int,
        'username': username,
        'password': password,
        'sender': sender or username,
        'enabled': _truthy_config(enabled_raw, default=True),
    }


def _normalise_address_list(value):
    """Return a clean list of email addresses from a string or iterable.
    Accepts comma, semicolon or whitespace separators, drops blanks and
    deduplicates while preserving order."""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        candidates = []
        for item in value:
            candidates.extend(_normalise_address_list(item))
        seen = set()
        out = []
        for addr in candidates:
            key = addr.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(addr)
        return out
    text = str(value).strip()
    if not text:
        return []
    parts = [chunk.strip() for chunk in text.replace(';', ',').split(',')]
    seen = set()
    out = []
    for chunk in parts:
        if not chunk:
            continue
        # Allow whitespace-separated entries within a single chunk too.
        for token in chunk.split():
            token = token.strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(token)
    return out


def send_email(to_address, subject, body_html, body_text=None, app=None,
               cc_addresses=None, inline_images=None):
    """
    Send a single email. Returns (ok: bool, error: str|None).
    Requires Flask app context (reads config from current_app or passed app).

    to_address may be a single string, a comma/semicolon/space-separated
    string, or a list/tuple of addresses. cc_addresses follows the same
    rules and may be omitted.

    inline_images is an optional iterable of (cid, file_path, mime_subtype)
    tuples. When provided, the message is wrapped in multipart/related and
    each image is attached inline with Content-ID: <cid>, so the HTML body
    can reference them with <img src="cid:cid">. This is the canonical way
    to embed logos in HTML email - Gmail and Outlook both strip data: URIs
    in <img src> for security, so the previous base64 approach silently
    rendered as a broken image in many clients.
    """
    cfg = resolve_smtp_config(app)

    to_list = _normalise_address_list(to_address)
    cc_list = _normalise_address_list(cc_addresses)
    if not to_list:
        return False, 'No recipient (to) address provided'

    # Drop any CC entries that already appear in To to avoid duplicate delivery.
    to_keys = {a.lower() for a in to_list}
    cc_list = [a for a in cc_list if a.lower() not in to_keys]
    all_recipients = to_list + cc_list

    if not cfg['enabled']:
        log.info('SMTP disabled — skipping send to %s', ', '.join(all_recipients))
        return False, 'SMTP_ENABLED=false'

    smtp_server = cfg['server']
    smtp_port = cfg['port']
    smtp_user = cfg['username']
    smtp_pass = cfg['password']
    sender = cfg['sender']

    if not smtp_user or not smtp_pass:
        return False, 'SMTP credentials not configured (SMTP.SENDER_EMAIL / SMTP.SENDER_PASSWORD)'

    inline_images = list(inline_images or [])
    if inline_images:
        # multipart/related wraps an alternative body + image attachments so
        # the HTML can reference each image as cid:<id>.
        msg = MIMEMultipart('related')
        msg['Subject'] = subject
        msg['From']    = f'Synovia Integration <{sender}>'
        msg['To']      = ', '.join(to_list)
        if cc_list:
            msg['Cc'] = ', '.join(cc_list)
        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        if body_text:
            alt.attach(MIMEText(body_text, 'plain', 'utf-8'))
        alt.attach(MIMEText(body_html, 'html', 'utf-8'))
        for cid, image_path, subtype in inline_images:
            try:
                with open(image_path, 'rb') as fh:
                    image_part = MIMEImage(fh.read(), _subtype=(subtype or 'jpeg'))
                image_part.add_header('Content-ID', f'<{cid}>')
                image_part.add_header(
                    'Content-Disposition', 'inline', filename=os.path.basename(image_path),
                )
                msg.attach(image_part)
            except Exception as exc:
                log.warning('Failed to attach inline image %s (%s): %s', cid, image_path, exc)
    else:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'Synovia Integration <{sender}>'
        msg['To']      = ', '.join(to_list)
        if cc_list:
            msg['Cc'] = ', '.join(cc_list)
        if body_text:
            msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(sender, all_recipients, msg.as_string())
        log.info('Email sent: %s → %s%s',
                 subject[:60],
                 ', '.join(to_list),
                 f' (cc {", ".join(cc_list)})' if cc_list else '')
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        err = f'SMTP auth failed: {e}'
        log.error(err)
        return False, err
    except smtplib.SMTPException as e:
        err = f'SMTP error: {e}'
        log.error(err)
        return False, err
    except Exception as e:
        err = f'Email send error: {e}'
        log.error(err)
        return False, err


def _log_email(staging_id, entity_type, to_address, subject, ok, error=None, call_type='EMAIL_SENT'):
    """Log email send attempt to TSS.BKD_API_Exchanges.

    call_type defaults to the generic EMAIL_SENT for backward compatibility.
    Specific flows pass their own call_type so technical logs can filter by
    purpose (e.g. ENS_MOVEMENT_PACK_EMAIL for the ENS pack)."""
    try:
        from app.db import insert_api_call_log
        insert_api_call_log(
            'BKD',
            (call_type or 'EMAIL_SENT'),
            staging_id=staging_id,
            http_method='SMTP',
            url=to_address or '',
            request_payload=subject,
            http_status=200 if ok else 500,
            response_status='SENT' if ok else 'FAILED',
            response_message=error,
        )
    except Exception as e:
        log.warning('Failed to log email: %s', e)


# ── Notification dispatcher ────────────────────────────────────────────────

def send_notification(to_address, notif_type, record, custom_body=None,
                      staging_id=None, entity_type='CONS'):
    """
    Send a pre-built notification email.

    Args:
        to_address:   Recipient email address
        notif_type:   One of NOTIFICATION_TYPES keys, or 'custom'
        record:       Dict-like DB record (cons, sd, etc.)
        custom_body:  For type='custom', plain-text body to embed
        staging_id:   For logging to TSS.BKD_API_Exchanges
        entity_type:  'CONS', 'SDI', 'ENS' etc.

    Returns: (ok: bool, error: str|None)
    """
    ntype = NOTIFICATION_TYPES.get(notif_type, NOTIFICATION_TYPES['custom'])
    subject = ntype['subject']
    html    = _render_email(notif_type, record, custom_body)
    text    = _render_plain(notif_type, record, custom_body)

    ok, err = send_email(to_address, subject, html, text)
    _log_email(staging_id, entity_type, to_address, subject, ok, err)
    return ok, err


# ── HTML template renderer ─────────────────────────────────────────────────

_BRAND_COLOR  = '#0b1d3a'
_ACCENT_COLOR = '#2563eb'
_AMBER_COLOR  = '#d97706'
_SUCCESS      = '#16a34a'
_DANGER       = '#dc2626'

_BASE_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{subject}</title>
<style>
  body {{ margin:0; padding:0; background:#f3f4f6; font-family:'Segoe UI',Arial,sans-serif; font-size:14px; color:#1f2937; }}
  .wrap {{ max-width:620px; margin:32px auto; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,.12); }}
  .header {{ background:{brand}; padding:24px 32px; }}
  .header img {{ height:32px; }}
  .header h1 {{ color:#fff; font-size:18px; margin:8px 0 0; font-weight:600; }}
  .header .sub {{ color:#93c5fd; font-size:12px; margin:2px 0 0; }}
  .body {{ padding:28px 32px; }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:12px; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.4px; }}
  .badge-blue {{ background:#dbeafe; color:#1d4ed8; }}
  .badge-green {{ background:#dcfce7; color:#15803d; }}
  .badge-amber {{ background:#fef3c7; color:#92400e; }}
  .badge-red {{ background:#fee2e2; color:#991b1b; }}
  .field-table {{ width:100%; border-collapse:collapse; margin:16px 0; font-size:13px; }}
  .field-table td {{ padding:6px 10px; border-bottom:1px solid #f3f4f6; }}
  .field-table td:first-child {{ color:#6b7280; font-weight:600; width:40%; }}
  code {{ font-family:monospace; background:#f3f4f6; padding:1px 5px; border-radius:3px; font-size:12px; }}
  .cta {{ display:block; text-align:center; margin:24px 0; }}
  .cta a {{ background:{accent}; color:#fff !important; padding:12px 28px; border-radius:6px; text-decoration:none; font-weight:600; font-size:14px; display:inline-block; }}
  .message-box {{ background:#eff6ff; border-left:4px solid {accent}; padding:12px 16px; border-radius:0 6px 6px 0; margin:16px 0; font-size:13px; }}
  .message-box.amber {{ background:#fffbeb; border-left-color:{amber}; }}
  .message-box.red {{ background:#fef2f2; border-left-color:{danger}; }}
  .footer {{ background:#f9fafb; padding:16px 32px; border-top:1px solid #e5e7eb; font-size:11px; color:#9ca3af; }}
  .footer a {{ color:#6b7280; text-decoration:none; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div style="font-size:11px;color:#93c5fd;text-transform:uppercase;letter-spacing:1px;font-weight:700;">SYNOVIA INTEGRATION</div>
    <h1>{title}</h1>
    <div class="sub">Fusion Flow — Declaration Management Portal</div>
  </div>
  <div class="body">
    {body}
  </div>
  <div class="footer">
    <p>This email was sent by Synovia Integration on behalf of Birkdale Sales Ltd.</p>
    <p>If you have questions please contact your Synovia account manager.</p>
  </div>
</div>
</body>
</html>
""".format(
    subject='{subject}', title='{title}', body='{body}',
    brand=_BRAND_COLOR, accent=_ACCENT_COLOR,
    amber=_AMBER_COLOR, danger=_DANGER
)


def _field_row(label, value):
    if not value:
        return ''
    return f'<tr><td>{label}</td><td>{value}</td></tr>'


def _cons_fields(rec):
    rows = ''.join([
        _field_row('Goods Description', rec.get('goods_description') or rec.get('label') or ''),
        _field_row('Transport Document', rec.get('transport_document_number') or ''),
        _field_row('Importer EORI', f'<code>{rec.get("importer_eori") or "—"}</code>'),
        _field_row('DEC Reference', f'<code>{rec.get("dec_reference") or "Pending"}</code>'),
        _field_row('ENS Reference', f'<code>{rec.get("ens_reference") or "—"}</code>'),
    ])
    return f'<table class="field-table">{rows}</table>' if rows else ''


def _render_email(notif_type, rec, custom_body=None):
    rec = rec or {}
    ref = rec.get('dec_reference') or rec.get('ens_reference') or f"#{rec.get('staging_id','')}"

    if notif_type == 'doc_received':
        title = 'Document Received'
        body = f"""
        <p>We have received your import document and are processing it now.</p>
        <p>To complete your declaration we may need additional information from you.
           A member of the Birkdale team will be in touch shortly.</p>
        {_cons_fields(rec)}
        <div class="message-box">
            <strong>What happens next?</strong><br>
            Our team will review your document and contact you if any information is missing or requires clarification.
        </div>
        """

    elif notif_type == 'cons_validated':
        title = 'Consignment Validated &amp; Submitted'
        body = f"""
        <p>Your consignment <strong>{ref}</strong> has been successfully validated and submitted to HMRC via TSS.</p>
        {_cons_fields(rec)}
        <div class="message-box">
            <strong>Next step:</strong> Your haulier will need a <strong>GMR (Goods Movement Reference)</strong>
            to present at the port. This will be provided once TSS confirms authorisation.
        </div>
        """

    elif notif_type == 'cons_failed':
        err = rec.get('error_message') or 'Please contact your Synovia account manager for details.'
        title = 'Action Required — Consignment Issue'
        body = f"""
        <p>There is an issue with your consignment <strong>{ref}</strong> that requires your attention.</p>
        {_cons_fields(rec)}
        <div class="message-box red">
            <strong>Issue:</strong><br>{err}
        </div>
        <p>Please review the details and contact the Birkdale team to resolve this before your shipment date.</p>
        """

    elif notif_type == 'arrival_confirmed':
        due = rec.get('submission_due_date') or rec.get('deadline') or 'the 10th of next month'
        title = 'Goods Arrived — Supplementary Declaration Required'
        body = f"""
        <p>Your goods have been confirmed as arrived at the UK border.</p>
        <p>You are now required to submit a <strong>Supplementary Import Declaration (SDI)</strong>
           to HMRC by <strong>{due}</strong>.</p>
        {_cons_fields(rec)}
        <div class="message-box amber">
            <strong>Deadline:</strong> Supplementary Declarations must be submitted by the 10th of the
            month following arrival. Failure to submit on time may result in penalties.
        </div>
        <p>Your Synovia account manager will contact you shortly with the declaration details,
           or you can log in to the portal to review and approve.</p>
        """

    elif notif_type == 'sdi_reminder':
        due = rec.get('submission_due_date') or '10th of this month'
        ref = rec.get('ens_consignment_ref') or f"SDI #{rec.get('staging_id','')}"
        title = 'Reminder: Supplementary Declaration Due'
        body = f"""
        <p>This is a reminder that your Supplementary Declaration for <strong>{ref}</strong>
           is due by <strong>{due}</strong>.</p>
        <div class="message-box amber">
            <strong>Action required:</strong> Please ensure your declaration is submitted on time
            to avoid HMRC penalties. Contact your Synovia account manager if you need assistance.
        </div>
        """

    elif notif_type == 'sdi_overdue':
        ref = rec.get('ens_consignment_ref') or f"SDI #{rec.get('staging_id','')}"
        title = 'OVERDUE: Supplementary Declaration Must Be Submitted Now'
        body = f"""
        <p>Your Supplementary Declaration for <strong>{ref}</strong> is <strong style="color:{_DANGER}">OVERDUE</strong>.</p>
        <div class="message-box red">
            <strong>Immediate action required.</strong><br>
            This declaration has passed its submission deadline. Please contact your Synovia
            account manager immediately to submit and minimise any potential HMRC penalties.
        </div>
        """

    else:  # custom
        title = 'Message from Synovia Integration'
        body = f"""
        <div class="message-box">
            {custom_body or 'No message provided.'}
        </div>
        """

    ntype = NOTIFICATION_TYPES.get(notif_type, NOTIFICATION_TYPES['custom'])
    return _BASE_HTML.replace('{subject}', ntype['subject']).replace('{title}', title).replace('{body}', body)


def _render_plain(notif_type, rec, custom_body=None):
    """Minimal plain-text fallback."""
    rec = rec or {}
    ntype = NOTIFICATION_TYPES.get(notif_type, NOTIFICATION_TYPES['custom'])
    ref = rec.get('dec_reference') or rec.get('ens_reference') or f"#{rec.get('staging_id','')}"
    lines = [
        'SYNOVIA INTEGRATION — Fusion Flow',
        '=' * 50,
        ntype['subject'],
        '',
        f'Reference: {ref}',
    ]
    if custom_body:
        lines += ['', custom_body]
    lines += [
        '',
        'This email was sent by Synovia Integration on behalf of Birkdale Sales Ltd.',
        'Contact your Synovia account manager with any queries.',
    ]
    return '\n'.join(lines)
