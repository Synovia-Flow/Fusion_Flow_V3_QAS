"""Shared status normalization and badge helpers."""

STATUS_BADGE_LOOKUP = {
    'PENDING': 'badge-status-pending',
    'PENDING REVIEW': 'badge-status-pending',
    'INSERTED': 'badge-status-pending',
    'DRAFT': 'badge-status-pending',
    'PENDING SYNC': 'badge-status-warning',
    'API INACCESSIBLE': 'badge-status-warning',
    'QUEUED': 'badge-status-warning',
    'INGESTED': 'badge-status-warning',
    'VALIDATED': 'badge-status-draft',
    'SYNCED': 'badge-status-automation',
    'SUBMITTED': 'badge-status-success',
    'SUCCESS': 'badge-status-success',
    'PROCESSING': 'badge-status-draft',
    'ACTIVE': 'badge-status-success',
    'CREATED': 'badge-status-created',
    'FIXED': 'badge-status-warning',
    'IMPORTED': 'badge-status-draft',
    'ACCEPTED': 'badge-status-success',
    'CLEARED': 'badge-status-success',
    'ARRIVED': 'badge-status-success',
    'AUTHORISED FOR MOVEMENT': 'badge-status-success',
    'AUTHORISED': 'badge-status-success',
    'TRADER INPUT REQUIRED': 'badge-status-warning',
    'AMENDMENT REQUIRED': 'badge-status-warning',
    'PENDING PAYMENT': 'badge-status-automation',
    'INVALID': 'badge-status-warning',
    'REJECTED': 'badge-status-danger',
    'FAILED': 'badge-status-danger',
    'ERROR': 'badge-status-danger',
    'VALIDATION ERROR': 'badge-status-danger',
    'SUBMIT ERROR': 'badge-status-danger',
    'DO NOT LOAD': 'badge-status-danger',
    'OVERDUE': 'badge-status-danger',
    'CANCELLED': 'badge-status-cancelled',
    'CANCELED': 'badge-status-cancelled',
    'CLOSED': 'badge-status-success',
    'RESUBMIT': 'badge-status-warning',
}


def normalize_status_key(value):
    """Normalize any mixed-case status to shared lookup/display format."""
    if value is None:
        return ''
    text = str(value).strip()
    if not text:
        return ''
    return text.replace('_', ' ').upper()


LOCAL_STATUS_SYNC_EXEMPT = {
    'IMPORTED',
    'INGESTED',
}

TSS_STATUSES_FORCE_LOCAL_SUBMITTED = {
    'ARRIVED',
    'AUTHORISED FOR MOVEMENT',
    'AUTHORIZED FOR MOVEMENT',
}

LOCAL_CONSIGNMENT_FAILED_STATUSES = {
    'FAILED',
    'INVALID',
    'REJECTED',
    'TRADER INPUT REQUIRED',
    'TRADER_INPUT_REQUIRED',
}

PENDING_TSS_SYNC_STATUSES = {
    'PENDING SYNC',
    'SYNC PENDING',
    'IMPORTED',
}

LOCAL_DRAFT_FILTER_STATUSES = {
    '',
    'CREATED',
    'DRAFT',
    'INSERTED',
    'PENDING',
    'PENDING REVIEW',
    'VALIDATED',
}

TSS_FILTER_STATUS_TABS = [
    'ALL',
    'IMPORTED',
    'INGESTED',
    'PENDING_SYNC',
    'API_INACCESSIBLE',
    'DRAFT',
    'CREATED',
    'SUBMITTED',
    'PROCESSING',
    'AUTHORISED FOR MOVEMENT',
    'ARRIVED',
    'ACCEPTED',
    'CLEARED',
    'CLOSED',
    'TRADER INPUT REQUIRED',
    'AMENDMENT REQUIRED',
    'DO NOT LOAD',
    'REJECTED',
    'CANCELLED',
    'FAILED',
]


def canonical_filter_status(value):
    """Normalize query/tab status values to the same keys used by list counts."""
    normalized = normalize_status_key(value)
    if not normalized or normalized == 'ALL':
        return 'ALL'
    if normalized in {'PENDING SYNC', 'SYNC PENDING'}:
        return 'PENDING_SYNC'
    if normalized in {'API INACCESSIBLE', 'TSS API INACCESSIBLE'}:
        return 'API_INACCESSIBLE'
    if normalized in {'PENDING REVIEW'}:
        return 'PENDING_REVIEW'
    if normalized in {'VALIDATION ERROR'}:
        return 'VALIDATION_ERROR'
    if normalized in {'SUBMIT ERROR'}:
        return 'SUBMIT_ERROR'
    return normalized


def local_status_is_sync_exempt(local_status):
    """Imported/ingested local records are historical/source records; do not rewrite them."""
    return normalize_status_key(local_status) in LOCAL_STATUS_SYNC_EXEMPT


def tss_status_forces_local_submitted(tss_status):
    """Return True when a TSS status means local workflow status should be Submitted."""
    return normalize_status_key(tss_status) in TSS_STATUSES_FORCE_LOCAL_SUBMITTED


def _truthy_status_value(value):
    return str(value or '').strip().lower() in {'yes', 'y', 'true', '1', 'on'}


def consignment_should_discover_sdi(record):
    """Return True when Fusion should look for a TSS-generated SDI.

    generate_SD is useful as a local "we know this will need SDI" hint, but it
    is not the source of truth. Once TSS has produced an SFD, or the parent
    consignment has arrived, Fusion should discover SDI from TSS instead of
    deciding locally that none is required.
    """
    record = record or {}
    has_sfd = any(
        str(record.get(key) or '').strip()
        for key in ('sfd_reference', 'sfd_number', 'synced_sfd_reference')
    )
    no_sfd_reason = str(record.get('no_sfd_reason') or '').strip()
    if no_sfd_reason and not has_sfd:
        return False
    if _truthy_status_value(record.get('generate_SD')):
        return True
    if has_sfd:
        return True
    return normalize_status_key(record.get('tss_status') or record.get('cons_tss_status')) == 'ARRIVED'


def local_status_after_tss_sync(local_status, tss_status):
    """Apply the TSS-to-local status rule without changing import/ingest records."""
    if local_status_is_sync_exempt(local_status):
        return local_status
    if tss_status_forces_local_submitted(tss_status):
        return 'SUBMITTED'
    return local_status


def fixed_consignment_local_status(local_status, tss_status):
    """Display stale local failures as fixed when TSS has already authorised them."""
    local = normalize_status_key(local_status)
    remote = normalize_status_key(tss_status)
    if local in LOCAL_CONSIGNMENT_FAILED_STATUSES and remote in TSS_STATUSES_FORCE_LOCAL_SUBMITTED:
        return 'FIXED'
    return local_status or 'PENDING'


def local_goods_status_after_parent_sync(
    local_status,
    goods_tss_status=None,
    parent_local_status=None,
    parent_tss_status=None,
):
    """Keep goods local workflow aligned once their parent consignment is in TSS.

    Goods are created inside TSS as children of a consignment. If the parent is
    already submitted locally, or the parent/goods status has reached Arrived in
    TSS, a local Pending goods badge is stale; it should be Created unless the
    row is an imported/ingested historical mirror.
    """
    if local_status_is_sync_exempt(local_status):
        return local_status

    effective_parent_local = local_status_after_tss_sync(parent_local_status, parent_tss_status)
    if (
        normalize_status_key(goods_tss_status) == 'ARRIVED'
        or normalize_status_key(effective_parent_local) == 'SUBMITTED'
        or normalize_status_key(parent_tss_status) == 'ARRIVED'
    ):
        return 'CREATED'
    return local_status


def effective_tss_filter_status(local_status=None, tss_status=None, *, pending_sync=False, fallback='DRAFT'):
    """Return the list/filter status, preferring TSS state over local workflow state."""
    local = normalize_status_key(local_status)
    if local in LOCAL_STATUS_SYNC_EXEMPT:
        return local
    if pending_sync:
        return 'PENDING_SYNC'
    remote = normalize_status_key(tss_status)
    if remote in PENDING_TSS_SYNC_STATUSES:
        return 'PENDING_SYNC'
    if remote:
        return remote
    if local in LOCAL_DRAFT_FILTER_STATUSES:
        return fallback
    return canonical_filter_status(local) if local else fallback


def status_filter_tabs(counts, preferred, selected=None):
    """Build compact status tabs.

    ALL is always visible. Other tabs are shown only when records exist for
    that state, with the current selected tab preserved for shareable filtered
    URLs that happen to be empty.
    """
    counts = counts or {}
    selected = selected or 'ALL'
    tabs = []

    def add(status):
        if status not in tabs:
            tabs.append(status)

    for status in preferred:
        if not status:
            continue
        if status == 'ALL' or counts.get(status, 0) > 0 or status == selected:
            add(status)

    for status in sorted(status for status in counts if status):
        if status == 'ALL' or not counts.get(status):
            continue
        add(status)

    if selected and selected not in tabs:
        add(selected)

    return tabs


def status_display(value):
    """Display status text in consistent uppercase form."""
    text = normalize_status_key(value)
    return text or '-'


def badge_class_for_status(value, default='badge-status-pending'):
    """Return semantic badge class for mixed-case/local/TSS statuses."""
    return 'badge ' + STATUS_BADGE_LOOKUP.get(normalize_status_key(value), default)


TSS_DATA_LOCKED_STATUSES = {
    'PROCESSING',
    'SUBMITTED',
    'SUCCESS',
    'ACCEPTED',
    'CLEARED',
    'CLOSED',
    'CANCELLED',
    'CANCELED',
    'AUTHORISED FOR MOVEMENT',
    'AUTHORIZED FOR MOVEMENT',
    'ARRIVED',
}

LOCAL_DATA_LOCKED_STATUSES = {
    'PROCESSING',
    'SUBMITTED',
    'SUCCESS',
    'CANCELLED',
    'CANCELED',
}


def tss_allows_data_changes(tss_status=None, local_status=None):
    """Return whether local data-entry actions should stay visible/editable."""
    remote = normalize_status_key(tss_status)
    if remote:
        return remote not in TSS_DATA_LOCKED_STATUSES
    local = normalize_status_key(local_status)
    if local in LOCAL_DATA_LOCKED_STATUSES:
        return False
    return True


def tss_data_lock_reason(tss_status=None, local_status=None, entity_label='record'):
    """Human-readable reason for hiding edit/add actions on TSS-controlled records."""
    if tss_allows_data_changes(tss_status, local_status):
        return ''
    current = status_display(tss_status or local_status)
    return f'This {entity_label} is {current} in TSS, so local edits and new child records are locked.'
