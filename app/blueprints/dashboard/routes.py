"""Dashboard Blueprint — Automation Activity: email ingestion, staging pipeline, TSS status."""
import re

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from app.db import query_all, query_one
from app.tenant import get_tenant

dashboard_bp = Blueprint('dashboard', __name__, template_folder='../../templates/dashboard')


_ENS_SEARCH_PREFIX_RE = re.compile(r'^(ENS\d*|ICR\d+|DRAFT\d+)', re.IGNORECASE)
_CONSIGNMENT_SEARCH_PREFIX_RE = re.compile(r'^(DEC\d*|S-?ORD\d+|ORD\d+)', re.IGNORECASE)
_SFD_SEARCH_PREFIX_RE = re.compile(r'^(SFD\d*|EIDR\d*)', re.IGNORECASE)
_SDI_SEARCH_PREFIX_RE = re.compile(r'^(SUP\d*|SDI#?\d*)', re.IGNORECASE)


@dashboard_bp.route('/')
def index():
    auto = get_automation_stats()
    if request.headers.get('HX-Request') == 'true':
        return render_template('partials/_dashboard_content.html', auto=auto)
    return render_template('dashboard/index.html', auto=auto)


@dashboard_bp.route('/partial')
def partial():
    auto = get_automation_stats()
    return render_template('partials/_dashboard_content.html', auto=auto)


def get_automation_stats():
    """Automation pipeline stats from ING, STG, and TSS schemas."""
    client_code = (get_tenant().get('code') or 'BKD').upper()
    a = {}

    try:
        row = query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN Skipped=0 THEN 1 ELSE 0 END) AS processed,"
            " SUM(CASE WHEN Skipped=1 THEN 1 ELSE 0 END) AS skipped"
            " FROM ING.BKD_EmailMessage WHERE ClientCode=?",
            [client_code],
        )
        a['emails_total'] = row['total'] or 0
        a['emails_processed'] = row['processed'] or 0
        a['emails_skipped'] = row['skipped'] or 0
    except Exception:
        a['emails_total'] = a['emails_processed'] = a['emails_skipped'] = 0

    try:
        row = query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN Status='Saved' THEN 1 ELSE 0 END) AS saved,"
            " SUM(CASE WHEN Status='Error' THEN 1 ELSE 0 END) AS errors,"
            " SUM(CASE WHEN Status='Skipped' THEN 1 ELSE 0 END) AS skipped"
            " FROM ING.BKD_EmailAttachment WHERE ClientCode=?",
            [client_code],
        )
        a['att_total'] = row['total'] or 0
        a['att_saved'] = row['saved'] or 0
        a['att_errors'] = row['errors'] or 0
        a['att_skipped'] = row['skipped'] or 0
    except Exception:
        a['att_total'] = a['att_saved'] = a['att_errors'] = a['att_skipped'] = 0

    try:
        rows = query_all(
            "SELECT COALESCE(NULLIF(TssStatus, ''), 'CREATED') AS st, COUNT(*) AS cnt"
            " FROM TSS.BKD_ENS_Headers WHERE ClientCode=?"
            " GROUP BY COALESCE(NULLIF(TssStatus, ''), 'CREATED')",
            [client_code],
        )
        a['ens_by_status'] = {r['st']: r['cnt'] for r in rows}
        a['ens_total'] = sum(a['ens_by_status'].values())
    except Exception:
        a['ens_by_status'] = {}
        a['ens_total'] = 0

    try:
        rows = query_all(
            "SELECT COALESCE(NULLIF(TssStatus, ''), 'CREATED') AS st, COUNT(*) AS cnt"
            " FROM TSS.BKD_ENS_Consignments WHERE ClientCode=?"
            " GROUP BY COALESCE(NULLIF(TssStatus, ''), 'CREATED')",
            [client_code],
        )
        a['cons_by_status'] = {r['st']: r['cnt'] for r in rows}
        a['cons_total'] = sum(a['cons_by_status'].values())
    except Exception:
        a['cons_by_status'] = {}
        a['cons_total'] = 0

    try:
        rows = query_all(
            "SELECT COALESCE(NULLIF(TssStatus, ''), 'CREATED') AS st, COUNT(*) AS cnt"
            " FROM TSS.BKD_GoodsItems WHERE ClientCode=?"
            " GROUP BY COALESCE(NULLIF(TssStatus, ''), 'CREATED')",
            [client_code],
        )
        a['goods_by_status'] = {r['st']: r['cnt'] for r in rows}
        a['goods_total'] = sum(a['goods_by_status'].values())
    except Exception:
        a['goods_by_status'] = {}
        a['goods_total'] = 0

    try:
        row = query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN ResponseStatus='SENT' THEN 1 ELSE 0 END) AS sent,"
            " SUM(CASE WHEN ResponseStatus='FAILED' THEN 1 ELSE 0 END) AS failed"
            " FROM TSS.BKD_API_Exchanges"
            " WHERE Flow='EMAIL_AUTOMATION' AND EntityKind='EMAIL' AND ClientCode=?",
            [client_code],
        )
        a['notify_total'] = row['total'] or 0
        a['notify_sent'] = row['sent'] or 0
        a['notify_failed'] = row['failed'] or 0
    except Exception:
        a['notify_total'] = a['notify_sent'] = a['notify_failed'] = 0

    try:
        row = query_one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN movement_notified_at IS NOT NULL THEN 1 ELSE 0 END) AS notified"
            " FROM STG.BKD_ENS_Headers"
            " WHERE ClientCode=?",
            [client_code],
        )
        a['movement_total'] = row['total'] or 0
        a['movement_notified'] = row['notified'] or 0
    except Exception:
        a['movement_total'] = a['movement_notified'] = 0

    try:
        rows = query_all(
            "SELECT COALESCE(NULLIF(t.TssStatus,''), NULLIF(h.tss_status,''), 'PENDING_SYNC') AS st,"
            " COUNT(DISTINCT h.stg_sdi_id) AS cnt"
            " FROM STG.BKD_SDI_Headers h"
            " LEFT JOIN TSS.BKD_SDI_Headers t"
            "   ON t.ClientCode = h.ClientCode"
            "  AND t.SupDecNumber = h.tss_sup_dec_number"
            " WHERE h.ClientCode=?"
            " GROUP BY COALESCE(NULLIF(t.TssStatus,''), NULLIF(h.tss_status,''), 'PENDING_SYNC')"
            " ORDER BY COALESCE(NULLIF(t.TssStatus,''), NULLIF(h.tss_status,''), 'PENDING_SYNC')",
            [client_code],
        )
        a['sdi_by_status'] = {r['st']: r['cnt'] for r in rows}
        a['sdi_total'] = sum(a['sdi_by_status'].values())
    except Exception:
        a['sdi_by_status'] = {}
        a['sdi_total'] = 0

    try:
        a['recent_emails'] = query_all(
            "SELECT TOP 20 EmailMessageId, SenderEmail, Subject, ReceivedAt,"
            " AttachmentCount, AllowedSender, Skipped, SkipReason"
            " FROM ING.BKD_EmailMessage WHERE ClientCode=?"
            " ORDER BY ReceivedAt DESC",
            [client_code],
        )
    except Exception:
        a['recent_emails'] = []

    try:
        a['blockers'] = query_all(
            "SELECT TOP 10 c.stg_consignment_id, c.sub_status, c.error_message,"
            " c.tss_consignment_ref, COALESCE(c.trader_reference, c.transport_document_number) AS document_no,"
            " h.tss_ens_header_ref, c.stg_header_id"
            " FROM STG.BKD_ENS_Consignments c"
            " LEFT JOIN STG.BKD_ENS_Headers h ON h.stg_header_id = c.stg_header_id"
            " LEFT JOIN TSS.BKD_ENS_Consignments tc"
            "   ON tc.ClientCode = c.ClientCode"
            "  AND tc.ConsignmentReference = c.tss_consignment_ref"
            " WHERE (UPPER(COALESCE(c.sub_status,''))='FAILED' OR c.error_message IS NOT NULL)"
            " AND c.ClientCode=?"
            " AND UPPER(REPLACE(COALESCE(tc.TssStatus, ''), ' ', '_'))"
            "     NOT IN ('AUTHORISED_FOR_MOVEMENT', 'AUTHORIZED_FOR_MOVEMENT', 'ARRIVED')"
            " ORDER BY c.stg_consignment_id DESC",
            [client_code],
        )
    except Exception:
        a['blockers'] = []

    return a


def get_recent_activity():
    """Removed — legacy staging tables not on automation PRD. Use get_automation_stats()."""
    return []


@dashboard_bp.route('/search')
def search():
    q = (request.args.get('q') or '').strip()
    target = _search_redirect_target(q)
    if target:
        endpoint, values = target
        return redirect(url_for(endpoint, **values))
    results = []
    if len(q) >= 2:
        results = _run_search(q)
    return render_template('dashboard/search.html', q=q, results=results)


def _search_redirect_target(q):
    """Route obvious operational references to the right worklist."""
    raw = (q or '').strip()
    if len(raw) < 2:
        return None
    token = _compact_search_token(raw)
    if _ENS_SEARCH_PREFIX_RE.match(token):
        return ('declarations.list_declarations', {'q': raw})
    if _CONSIGNMENT_SEARCH_PREFIX_RE.match(token):
        return ('consignments.list_view', {'q': raw})
    if _SFD_SEARCH_PREFIX_RE.match(token):
        return ('sfd.list_view', {'q': raw})
    if _SDI_SEARCH_PREFIX_RE.match(token):
        return ('supdec.list_view', {'q': raw})
    return None


def _compact_search_token(value):
    return re.sub(r'[^A-Z0-9]+', '', str(value or '').upper())


def _compact_sql(column):
    return (
        "UPPER(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE("
        f"{column}, ''), '-', ''), ' ', ''), '#', ''), '/', ''))"
    )


def _run_search(q):
    client_code = (get_tenant().get('code') or 'BKD').upper()
    compact = _compact_search_token(q)
    like = f'%{q}%'
    compact_like = f'%{compact}%'
    results = []

    try:
        ens_ref = _compact_sql("h.tss_ens_header_ref")
        conveyance_ref = _compact_sql("h.conveyance_ref")
        rows = query_all(
            "SELECT TOP 10 stg_header_id, tss_ens_header_ref, sub_status, source, label"
            " FROM STG.BKD_ENS_Headers h"
            " WHERE ClientCode = ?"
            "   AND (tss_ens_header_ref LIKE ? OR label LIKE ?"
            f"        OR {ens_ref} LIKE ? OR {conveyance_ref} LIKE ?"
            "        OR CAST(stg_header_id AS VARCHAR) = ?)"
            " ORDER BY stg_header_id DESC",
            [client_code, like, like, compact_like, compact_like, q],
        )
        for r in rows:
            results.append({
                'kind': 'ENS',
                'ref': r['tss_ens_header_ref'] or f'Draft #{r["stg_header_id"]}',
                'detail': r['label'] or r['source'] or '',
                'status': r['sub_status'],
                'url': url_for('declarations.header_detail', staging_id=r['stg_header_id']),
            })
    except Exception:
        pass

    try:
        cons_ref = _compact_sql("c.tss_consignment_ref")
        trader_ref = _compact_sql("c.trader_reference")
        transport_ref = _compact_sql("c.transport_document_number")
        ens_ref = _compact_sql("h.tss_ens_header_ref")
        conveyance_ref = _compact_sql("h.conveyance_ref")
        rows = query_all(
            "SELECT TOP 10 c.stg_consignment_id, c.tss_consignment_ref, c.sub_status,"
            " c.goods_description, c.trader_reference, c.transport_document_number, h.tss_ens_header_ref"
            " FROM STG.BKD_ENS_Consignments c"
            " LEFT JOIN STG.BKD_ENS_Headers h"
            "   ON h.ClientCode = c.ClientCode"
            "  AND h.stg_header_id = c.stg_header_id"
            " WHERE c.ClientCode = ?"
            "   AND (c.tss_consignment_ref LIKE ? OR c.trader_reference LIKE ?"
            "        OR c.transport_document_number LIKE ? OR c.goods_description LIKE ?"
            "        OR h.tss_ens_header_ref LIKE ? OR h.conveyance_ref LIKE ?"
            f"        OR {cons_ref} LIKE ? OR {trader_ref} LIKE ?"
            f"        OR {transport_ref} LIKE ? OR {ens_ref} LIKE ? OR {conveyance_ref} LIKE ?"
            "        OR CAST(c.stg_consignment_id AS VARCHAR) = ?)"
            " ORDER BY c.stg_consignment_id DESC",
            [
                client_code,
                like, like, like, like, like, like,
                compact_like, compact_like, compact_like, compact_like, compact_like,
                q,
            ],
        )
        for r in rows:
            results.append({
                'kind': 'CONS',
                'ref': r['tss_consignment_ref'] or f'Draft #{r["stg_consignment_id"]}',
                'detail': r['goods_description'] or r['trader_reference'] or r['transport_document_number'] or '',
                'status': r['sub_status'],
                'url': url_for('consignments.detail', sid=r['stg_consignment_id']),
            })
    except Exception:
        pass

    try:
        goods_id = _compact_sql("g.tss_hex_id")
        sku = _compact_sql("g.sku")
        cons_ref = _compact_sql("c.tss_consignment_ref")
        rows = query_all(
            "SELECT TOP 10 g.stg_item_id, g.goods_description, g.commodity_code, g.sku,"
            " g.sub_status, g.tss_hex_id, c.tss_consignment_ref, c.stg_consignment_id"
            " FROM STG.BKD_GoodsItems g"
            " LEFT JOIN STG.BKD_ENS_Consignments c"
            "   ON c.ClientCode = g.ClientCode"
            "  AND c.stg_consignment_id = g.stg_consignment_id"
            " WHERE g.ClientCode = ?"
            "   AND (g.goods_description LIKE ? OR g.commodity_code LIKE ? OR g.sku LIKE ?"
            "        OR g.tss_hex_id LIKE ? OR c.tss_consignment_ref LIKE ?"
            f"        OR {goods_id} LIKE ? OR {sku} LIKE ? OR {cons_ref} LIKE ?)"
            " ORDER BY g.stg_item_id DESC",
            [client_code, like, like, like, like, like, compact_like, compact_like, compact_like],
        )
        for r in rows:
            cons_id = r['stg_consignment_id']
            goods_url = (
                url_for('consignments.detail', sid=cons_id)
                if cons_id
                else url_for('consignments.list_view')
            )
            results.append({
                'kind': 'GOODS',
                'ref': r['sku'] or r['commodity_code'] or f'Goods #{r["stg_item_id"]}',
                'detail': r['goods_description'] or '',
                'status': r['sub_status'],
                'url': goods_url,
            })
    except Exception:
        pass

    try:
        sfd_ref = _compact_sql("s.SfdReference")
        dec_ref = _compact_sql("s.DeclarationNumber")
        movement_ref = _compact_sql("s.MovementReferenceNumber")
        rows = query_all(
            "SELECT TOP 10 s.SfdReference, s.DeclarationNumber, s.MovementReferenceNumber,"
            " s.TssStatus, s.UpdatedAt"
            " FROM TSS.BKD_SFD s"
            " WHERE s.ClientCode = ?"
            "   AND (s.SfdReference LIKE ? OR s.DeclarationNumber LIKE ?"
            "        OR s.MovementReferenceNumber LIKE ?"
            f"        OR {sfd_ref} LIKE ? OR {dec_ref} LIKE ? OR {movement_ref} LIKE ?)"
            " ORDER BY COALESCE(s.UpdatedAt, s.LastSyncedAt, s.CreatedAt) DESC",
            [client_code, like, like, like, compact_like, compact_like, compact_like],
        )
        for r in rows:
            ref = r['SfdReference'] or r['DeclarationNumber'] or ''
            results.append({
                'kind': 'SFD',
                'ref': ref,
                'detail': r['DeclarationNumber'] or r['MovementReferenceNumber'] or '',
                'status': r['TssStatus'],
                'url': url_for('sfd.detail', sfd_ref=ref) if ref else url_for('sfd.list_view', q=q),
            })
    except Exception:
        pass

    try:
        sup_ref = _compact_sql("h.tss_sup_dec_number")
        sfd_ref = _compact_sql("h.tss_sfd_consignment_ref")
        cons_ref = _compact_sql("h.tss_consignment_ref")
        rows = query_all(
            "SELECT TOP 10 h.stg_sdi_id, h.tss_sup_dec_number, h.tss_sfd_consignment_ref,"
            " h.tss_consignment_ref, h.sub_status, h.tss_status, h.importer_eori,"
            " h.trader_reference, h.transport_document_number, h.validation_errors_json,"
            " h.auto_submit_error"
            " FROM STG.BKD_SDI_Headers h"
            " WHERE h.ClientCode = ?"
            "   AND (h.tss_sup_dec_number LIKE ? OR h.tss_sfd_consignment_ref LIKE ?"
            "        OR h.tss_consignment_ref LIKE ? OR h.importer_eori LIKE ?"
            "        OR h.trader_reference LIKE ? OR h.transport_document_number LIKE ?"
            "        OR h.validation_errors_json LIKE ? OR h.auto_submit_error LIKE ?"
            f"        OR {sup_ref} LIKE ? OR {sfd_ref} LIKE ? OR {cons_ref} LIKE ?)"
            " ORDER BY COALESCE(h.updated_at, h.sdi_ready_at, h.submitted_at) DESC, h.stg_sdi_id DESC",
            [
                client_code,
                like, like, like, like, like, like, like, like,
                compact_like, compact_like, compact_like,
            ],
        )
        for r in rows:
            results.append({
                'kind': 'SDI',
                'ref': r['tss_sup_dec_number'] or f'SDI #{r["stg_sdi_id"]}',
                'detail': r['tss_sfd_consignment_ref'] or r['tss_consignment_ref'] or r['importer_eori'] or '',
                'status': r['sub_status'] or r['tss_status'],
                'url': url_for('supdec.detail', sid=r['stg_sdi_id']),
            })
    except Exception:
        pass

    try:
        rows = query_all(
            "SELECT TOP 10 EmailMessageId, SenderEmail, Subject, ReceivedAt, Skipped"
            " FROM ING.BKD_EmailMessage"
            " WHERE ClientCode = ?"
            "   AND (Subject LIKE ? OR SenderEmail LIKE ?)"
            " ORDER BY ReceivedAt DESC",
            [client_code, like, like],
        )
        for r in rows:
            results.append({
                'kind': 'EMAIL',
                'ref': r['SenderEmail'] or '',
                'detail': (r['Subject'] or '')[:80],
                'status': 'SKIPPED' if r['Skipped'] else 'OK',
                'url': url_for('ingest.queue'),
            })
    except Exception:
        pass

    try:
        attachment_name = _compact_sql("a.OriginalName")
        downloaded_name = _compact_sql("a.DownloadedName")
        rows = query_all(
            "SELECT TOP 10 a.EmailAttachmentId, a.OriginalName, a.DownloadedName,"
            " a.Status, a.SkipReason, a.ErrorText, m.Subject, m.SenderEmail"
            " FROM ING.BKD_EmailAttachment a"
            " LEFT JOIN ING.BKD_EmailMessage m"
            "   ON m.ClientCode = a.ClientCode"
            "  AND m.EmailMessageId = a.EmailMessageId"
            " WHERE a.ClientCode = ?"
            "   AND (a.OriginalName LIKE ? OR a.DownloadedName LIKE ?"
            "        OR a.SkipReason LIKE ? OR a.ErrorText LIKE ? OR m.Subject LIKE ?"
            f"        OR {attachment_name} LIKE ? OR {downloaded_name} LIKE ?)"
            " ORDER BY a.DownloadedAt DESC, a.EmailAttachmentId DESC",
            [client_code, like, like, like, like, like, compact_like, compact_like],
        )
        for r in rows:
            results.append({
                'kind': 'DOC',
                'ref': r['OriginalName'] or r['DownloadedName'] or f'Attachment #{r["EmailAttachmentId"]}',
                'detail': r['Subject'] or r['SkipReason'] or r['ErrorText'] or r['SenderEmail'] or '',
                'status': r['Status'],
                'url': url_for('ingest.queue'),
            })
    except Exception:
        pass

    return results
