"""Shared user-facing guidance for TSS statuses and errors."""
import json
import re

from app.status_utils import normalize_status_key


REFERENCE_RE = re.compile(r"\b(?:ENS|DEC|SUP|GMR|SFD)[A-Z0-9]{6,}\b", re.IGNORECASE)

EORI_TIN_POINTER_LABELS = {
    'consignee/identificationnumber': ('Consignee EORI/TIN', 'consignee_eori'),
    'consignor/identificationnumber': ('Consignor EORI/TIN', 'consignor_eori'),
    'goodsshipment/seller/identificationnumber': ('Seller EORI/TIN', 'seller_eori'),
    'goodsshipment/buyer/identificationnumber': ('Buyer EORI/TIN', 'buyer_eori'),
    'importer/identificationnumber': ('Importer EORI/TIN', 'importer_eori'),
    'exporter/identificationnumber': ('Exporter EORI/TIN', 'exporter_eori'),
    'carrier/identificationnumber': ('Carrier EORI/TIN on ENS Header', 'carrier_eori'),
}


def clean_tss_message(*values):
    """Return the most useful readable message from raw API/log fields."""
    candidates = []
    for order, value in enumerate(values):
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if text.startswith('{'):
            try:
                payload = json.loads(text)
            except Exception:
                candidates.append((text, order))
                continue
            result = payload.get('result') if isinstance(payload, dict) else None
            if isinstance(result, dict):
                for key in ('process_message', 'message', 'error_message', 'status_message'):
                    message = result.get(key)
                    if message:
                        candidates.append((str(message).strip(), order))
            for key in ('process_message', 'message', 'error_message', 'error', 'status_message'):
                message = payload.get(key) if isinstance(payload, dict) else None
                if message:
                    candidates.append((str(message).strip(), order))
            for nested_key in ('data', 'item'):
                nested = payload.get(nested_key) if isinstance(payload, dict) else None
                if isinstance(nested, dict):
                    for key in ('process_message', 'message', 'error_message', 'error', 'status_message'):
                        message = nested.get(key)
                        if message:
                            candidates.append((str(message).strip(), order))
            continue
        candidates.append((text, order))

    if not candidates:
        return ''

    def score(candidate):
        text, order = candidate
        lower = text.lower()
        pointer_count = len(re.findall(r'PointerNames:', text, flags=re.IGNORECASE))
        validation_count = len(re.findall(r'Validation Code:', text, flags=re.IGNORECASE))
        quality = min(len(text), 1000)
        quality += pointer_count * 300
        quality += validation_count * 100
        if 'valid eori/tin number shall be declared' in lower:
            quality += 1000
        if 'invalid op_type' in lower:
            quality += 1000
        if 'mandatory field' in lower or 'required:' in lower:
            quality += 800
        if 'trader input required' in lower:
            quality += 500
        return (quality, -order)

    return max(candidates, key=score)[0]


def _first_reference(text):
    match = REFERENCE_RE.search(text or '')
    return match.group(0) if match else None


def _invalid_op_type_guidance(text, *, local_status=None, tss_status=None):
    lower = text.lower()
    op_match = re.search(r"op_type[^:]*:\s*'?([a-z_]+)'?", lower)
    if not op_match:
        op_match = re.search(r":\s*([a-z_]+)\s*$", lower)
    op_type = (op_match.group(1) if op_match else '').upper() or 'THIS ACTION'
    ref = _first_reference(text) or 'this record'

    if op_type == 'SUBMIT':
        title = 'TSS rejected the submit action'
        summary = (
            f'This does not mean {ref} is Submitted. It means Fusion tried to run '
            'op_type=submit and TSS refused that operation for the current TSS state.'
        )
        next_step = (
            'Run Sync Cargo Statuses to read the latest TSS state. If TSS still says Draft, '
            'treat this as a rejected action, not a successful submission.'
        )
    elif op_type == 'UPDATE':
        title = 'TSS rejected the update action'
        summary = (
            f'TSS already has {ref}, but it did not accept op_type=update for the current '
            'resource/state combination.'
        )
        next_step = (
            'Sync the record first. If TSS still rejects update, create a new record only when '
            'the workflow really needs a new TSS reference; otherwise correct the local action.'
        )
    elif op_type == 'CREATE':
        title = 'TSS rejected the create action'
        summary = (
            f'TSS did not accept op_type=create for {ref}. This usually means the record already '
            'exists in TSS or the endpoint expects a different operation.'
        )
        next_step = 'Sync the TSS reference/status first, then retry only the operation TSS expects.'
    else:
        title = 'TSS rejected the requested operation'
        summary = (
            f'TSS rejected op_type={op_type} for {ref}. The local status only shows what Fusion '
            'attempted or stored; the TSS status is the source of truth for the remote state.'
        )
        next_step = 'Sync the record, then rerun the next valid workflow action for the TSS state.'

    local = normalize_status_key(local_status)
    remote = normalize_status_key(tss_status)
    detail_parts = []
    if local:
        detail_parts.append(f'Local status: {local}.')
    if remote:
        detail_parts.append(f'TSS status: {remote}.')
    detail_parts.append('The word in the error is the attempted API operation, not proof that the operation succeeded.')

    return {
        'tone': 'warning',
        'title': title,
        'summary': summary,
        'detail': ' '.join(detail_parts),
        'next_step': next_step,
        'technical': text,
        'raw': text,
    }


def _extract_pointer_names(text):
    pointers = []
    for match in re.finditer(r"PointerNames:([^;]+)", text or '', flags=re.IGNORECASE):
        pointer = match.group(1).strip()
        if pointer:
            pointers.append(pointer)
    if pointers:
        return pointers
    for part in (text or '').split(';'):
        part = part.strip()
        if '/' in part and 'PointerNames:' not in part and 'Validation Code' not in part and 'Code Description' not in part:
            pointers.append(part)
    return pointers


def _field_from_pointer(pointer):
    compact = (pointer or '').replace('\\', '/').lower()
    compact = re.sub(r'\s+', '', compact)
    for needle, field_info in EORI_TIN_POINTER_LABELS.items():
        if needle in compact:
            return field_info
    return ('Party EORI/TIN', None)


def _eori_tin_guidance(text, *, entity_label=None):
    seen = set()
    fields = []
    for pointer in _extract_pointer_names(text):
        label, field = _field_from_pointer(pointer)
        key = field or label
        if key in seen:
            continue
        seen.add(key)
        fields.append({'label': label, 'field': field, 'pointer': pointer})

    if not fields:
        fields = [{'label': 'Party EORI/TIN', 'field': None, 'pointer': None}]

    field_names = ', '.join(
        f"{item['label']} ({item['field']})" if item.get('field') else item['label']
        for item in fields
    )
    subject = entity_label or 'this record'
    suggestions = [
        'Check for missing values, spaces, hyphens, copied company names, or old account numbers.',
        'If TSS cannot auto-populate the party from the EORI, provide the matching party name and full address from Master Data.',
        'Use the correct actor: consignee, consignor, buyer and seller can be different parties.',
        'If buyer/seller should follow importer/exporter, confirm the same-as flags and source EORI values are populated.',
    ]
    if any(item.get('field') == 'carrier_eori' for item in fields):
        suggestions.insert(0, 'Carrier EORI/TIN belongs to the linked ENS Header, not the consignment. Open the ENS Header and fill Carrier EORI before resubmitting cargo.')
    if any(item.get('field') == 'consignee_eori' for item in fields):
        suggestions.append('If TSS needs consignee address details, set Consignee Address Required / EORI Unknown to true and provide the full name/address; leave the EORI blank only when it is genuinely unknown.')

    return {
        'tone': 'danger',
        'title': 'Invalid EORI/TIN numbers',
        'summary': f'TSS rejected {subject} because one or more party EORI/TIN values are invalid.',
        'detail': f'Review these fields: {field_names}. Values must be real EORI/TIN numbers, not company names, blanks, placeholders, or values with spaces/separators. If TSS cannot auto-populate a party from the EORI, send the matching name and full address as well.',
        'next_step': 'Open Edit, correct the highlighted party EORI/TIN fields, save, then run Send Cargo Pipeline to update the existing DEC in TSS, followed by Sync Cargo Statuses.',
        'suggestions': suggestions,
        'fields': fields,
        'technical': text,
        'raw': text,
    }


def _no_sfd_reason_guidance(text, *, entity_label=None):
    subject = entity_label or 'this consignment'
    return {
        'tone': 'danger',
        'title': 'No SFD reason is required',
        'summary': f'TSS cannot continue creating the SFD path for {subject} without a no_sfd_reason value.',
        'detail': (
            'TSS requires no_sfd_reason when the importer EORI is not registered on TSS or cannot be used '
            'for SFD auto-creation. This is an opt-out reason for SFD generation, not a normal mandatory '
            'field for every consignment.'
        ),
        'next_step': (
            'Open Edit, either replace the Importer EORI with one registered on TSS, or select a No SFD '
            'Reason for this consignment. Save, then rerun Send Cargo Pipeline and Sync Cargo Statuses.'
        ),
        'suggestions': [
            'If an SFD is expected, fix the importer EORI/masterdata instead of selecting a No SFD reason.',
            'If this is intentionally ENS-only or TSS cannot create an SFD for the importer, choose the correct TSS no_sfd_reason choice.',
            'Confirm the importer name and full address match the EORI before resubmitting.',
        ],
        'fields': [
            {
                'label': 'No SFD Reason',
                'field': 'no_sfd_reason',
                'pointer': 'no_sfd_reason',
            },
            {
                'label': 'Importer EORI/TIN',
                'field': 'importer_eori',
                'pointer': 'importer_eori',
            },
        ],
        'technical': text,
        'raw': text,
    }


def explain_tss_error(*values, local_status=None, tss_status=None, http_status=None, entity_label=None):
    """Map common technical failures to concise user-facing guidance."""
    text = clean_tss_message(*values)
    if not text:
        return None

    lower = text.lower()
    http = str(http_status or '').strip()
    subject = entity_label or 'this record'

    if 'invalid op_type' in lower:
        return _invalid_op_type_guidance(text, local_status=local_status, tss_status=tss_status)

    if 'no_sfd_reason' in lower and ('mandatory field' in lower or 'must also be' in lower or 'not supplied' in lower):
        return _no_sfd_reason_guidance(text, entity_label=subject)

    if (
        'valid eori/tin number shall be declared' in lower
        or ('eori/tin' in lower and 'identificationnumber' in lower)
        or ('eori number shall be declared' in lower and 'identificationnumber' in lower)
    ):
        return _eori_tin_guidance(text, entity_label=subject)

    if 'eori is not registered on tss' in lower or 'cannot assist with auto population' in lower:
        match = re.search(r'\b([A-Z]{2}[A-Z0-9]{6,17})\b', text, flags=re.IGNORECASE)
        eori = match.group(1).upper() if match else 'that EORI'
        return {
            'tone': 'danger',
            'title': 'EORI is not registered on TSS',
            'summary': f'TSS does not recognise {eori} for auto-populating the party details.',
            'detail': 'A valid-looking EORI can still fail if TSS does not know it. In that case the payload must include the complete matching name and address, or use the correct EORI from master data.',
            'next_step': 'Open Edit, use Master Data Quick-fill for the affected party, confirm the name/EORI match, save, then rerun Validate Pipeline and Send Cargo Pipeline.',
            'suggestions': [
                'Do not mix a customer name with another customer EORI.',
                'If the EORI is genuinely not registered on TSS, provide name, street, city, postcode and country.',
                'Check BKD.Partners for the CustomerNo/name before resubmitting.',
            ],
            'technical': text,
            'raw': text,
        }

    if 'unable to access target record' in lower:
        ref = _first_reference(text) or 'that TSS reference'
        return {
            'tone': 'warning',
            'title': 'TSS record is not accessible',
            'summary': f'TSS says the current API account cannot access {ref}.',
            'detail': 'The reference may exist in the TSS portal, but it is normally owned by another auth relationship, agent role, customer, or environment.',
            'next_step': 'Confirm the selected TSS environment and API user in Admin Settings. If the record belongs to another account, import with the account that owns it or keep a local review note.',
            'suggestions': [
                'Check whether the reference belongs to test or production before retrying.',
                'Confirm the API user has access to the trader/customer that created the record.',
                'Use the workbench import log to keep the failed reference visible for follow-up.',
            ],
            'technical': text,
            'raw': text,
        }

    if 'customs agent role required' in lower:
        return {
            'tone': 'warning',
            'title': 'TSS returned Customs Agent Role Required',
            'summary': 'Fusion reached the TSS API and TSS returned this message for the current SUP/goods endpoint. This is not a local validation blocker.',
            'detail': 'Keep the technical exchange log, then compare whether the final submit endpoint also returns this message or whether the SUP can proceed using its existing TSS data.',
            'next_step': 'Retry the manual submit after refresh. If TSS keeps returning the same message on the final submit call, confirm the SUP owner/account relationship with TSS.',
            'technical': text,
            'raw': text,
        }

    if 'no supported' in lower and ('attachment' in lower or 'pdf' in lower or 'xlsx' in lower or 'csv' in lower):
        return {
            'tone': 'warning',
            'title': 'No supported ingestion attachment was found',
            'summary': 'Fusion received the email/document event, but none of the attachments matched a supported import path.',
            'detail': 'For this flow the email must include a supported Excel, PDF, CSV or ZIP attachment. Scanned PDFs may require OCR or manual review.',
            'next_step': 'Open the ingestion record, confirm the attachment names and retry after adding a supported Excel/PDF file.',
            'suggestions': [
                'For BKD sales orders, use the Sales Orders Excel path.',
                'For goods-only import, attach to an existing consignment before retrying.',
                'If the file is a scanned PDF, route it to review/OCR instead of auto-create.',
            ],
            'technical': text,
            'raw': text,
        }

    if 'product master' in lower or ('sku' in lower and ('commodity_code' in lower or 'commodity code' in lower)):
        return {
            'tone': 'warning',
            'title': 'Product master data is missing',
            'summary': 'Fusion could not resolve the SKU/product into the customs fields needed for goods creation.',
            'detail': 'Goods can be staged for review, but TSS creation will fail until commodity code, origin, weights and valuation defaults are available.',
            'next_step': 'Add or fix the product in Master Data, then retry the ingestion or edit the goods item manually.',
            'suggestions': [
                'Check SKU/product code spelling from the source Excel or PDF.',
                'Populate commodity_code, country_of_origin and weight fields in product master.',
                'Leave the record in review if the SKU ownership is uncertain.',
            ],
            'technical': text,
            'raw': text,
        }

    if 'missing weight' in lower or 'missing weights' in lower or 'gross_mass_kg' in lower or 'gross mass' in lower:
        return {
            'tone': 'warning',
            'title': 'Goods weight is missing',
            'summary': 'The goods item does not have enough weight data for local validation or TSS payload creation.',
            'detail': 'Gross mass is required for goods. Product master weights can fill this automatically for Excel/PDF ingestion.',
            'next_step': 'Add weights to product master or edit the goods item with gross mass before running Validate Pipeline.',
            'technical': text,
            'raw': text,
        }

    if http == '401' or 'http 401' in lower or 'unauthorized' in lower:
        return {
            'tone': 'danger',
            'title': 'TSS credentials were rejected',
            'summary': 'Fusion reached the TSS API, but TSS returned HTTP 401 before processing the request.',
            'detail': 'This is a configuration/authentication problem, not a data validation problem on the record.',
            'next_step': 'Check the selected TSS URL, username and password in Admin configuration, then run the health check again.',
            'technical': text,
            'raw': text,
        }

    if 'unicodeencodeerror' in lower or "charmap codec can't encode" in lower:
        return {
            'tone': 'danger',
            'title': 'The script failed while printing output',
            'summary': 'The TSS operation may have completed, but Python crashed when Windows could not print a special character.',
            'detail': 'This is a console encoding failure in the local script output, not a TSS validation error.',
            'next_step': 'Rerun Sync Cargo Statuses to confirm the real TSS state, then retry the job after the script output fix is deployed.',
            'technical': text,
            'raw': text,
        }

    if 'required:' in lower or 'mandatory field' in lower:
        return {
            'tone': 'danger',
            'title': 'Required data is missing',
            'summary': f'{subject} is missing data required before the workflow can continue.',
            'detail': 'The field-level form hints should show the likely blocker in red on the edit screen.',
            'next_step': 'Open Edit, fix the highlighted field, save, then rerun Validate Pipeline.',
            'technical': text,
            'raw': text,
        }

    if 'invalid format' in lower or lower.startswith('format:') or lower.startswith('invalid:'):
        return {
            'tone': 'danger',
            'title': 'A field has an invalid format',
            'summary': f'{subject} has a value that does not match the expected TSS format.',
            'detail': 'This is usually fixable in the edit form; the API will keep rejecting it until the format matches.',
            'next_step': 'Open Edit, correct the highlighted value, save, then rerun Validate Pipeline.',
            'technical': text,
            'raw': text,
        }

    if 'trader input required' in lower:
        return {
            'tone': 'warning',
            'title': 'TSS needs trader input',
            'summary': 'TSS has accepted the record far enough to flag a required correction.',
            'detail': 'Edit the consignment in Fusion, correct the flagged fields, save, then run Send Cargo Pipeline to push the update to TSS.',
            'next_step': 'Edit → fix fields → Save → Send Cargo Pipeline → Sync Cargo Statuses.',
            'technical': text,
            'raw': text,
        }

    return None


def format_error_explanation(explanation):
    """Build plain text suitable for technical modal bodies."""
    if not explanation:
        return ''
    lines = [
        explanation.get('title') or 'What this means',
        explanation.get('summary') or '',
        explanation.get('detail') or '',
        'Next: ' + (explanation.get('next_step') or ''),
    ]
    return '\n'.join(line for line in lines if line).strip()


LOCAL_STATUS_HELP = {
    'PENDING': 'Waiting for validation or retry.',
    'INSERTED': 'Record inserted into Fusion staging — not yet validated or sent to TSS.',
    'VALIDATED': 'Local checks passed. Ready for the next pipeline step.',
    'QUEUED': 'Batch submitted and waiting for the pipeline to process it.',
    'CREATED': 'Fusion created the TSS record and stored its reference. Not the same as TSS acceptance.',
    'IMPORTED': 'Record imported from TSS as a stub. Refresh or sync to populate full data, or edit and submit.',
    'SUBMITTED': 'Fusion submitted the record to TSS. Sync to get the latest TSS status.',
    'SYNCED': 'Fusion has recently pulled the current status from TSS.',
    'FAILED': 'Fusion stopped because validation, configuration or TSS returned an error. Check the error message and retry.',
    'REJECTED': 'TSS or the workflow rejected the last operation.',
    'CANCELLED': 'Record cancelled locally — no further pipeline actions will run.',
    'CLOSED': 'Record closed.',
}

TSS_STATUS_HELP = {
    'DRAFT': 'TSS has the record but it has not been submitted yet.',
    'SUBMITTED': 'Submitted to TSS. CDS is validating — sync to see the outcome.',
    'PROCESSING': 'TSS is actively processing the declaration through CDS. No action needed yet.',
    'ACCEPTED': 'TSS accepted the record.',
    'AUTHORISED FOR MOVEMENT': 'TSS has authorised this consignment for Route A movement. All consignments on the same ENS must reach this state before a GMR can be created.',
    'ARRIVED': 'Goods have arrived in Northern Ireland. TSS will auto-draft an SDI (supplementary declaration). Submit the SDI by the 10th of the following month.',
    'CLEARED': 'Customs duty cleared. Terminal state.',
    'CLOSED': 'SDI or declaration closed by TSS. Terminal state — duty satisfied.',
    'TRADER INPUT REQUIRED': 'TSS needs a correction before the record can continue. Edit the fields in Fusion, save, then run Send Cargo Pipeline and Sync Cargo Statuses.',
    'AMENDMENT REQUIRED': 'TSS requires a formal amendment to the declaration. Correct the data and resubmit.',
    'DO NOT LOAD': 'TSS has blocked loading. Immediate operational attention needed — contact HMRC/TSS support.',
    'REJECTED': 'TSS rejected the record or operation. Review the error message and resubmit.',
    'CANCELLED': 'Declaration cancelled in TSS. Movement is blocked and cannot be reversed.',
    'UNDER CONTROLS': 'Goods are under customs control pending examination or duty assessment.',
    'PENDING PAYMENT': 'TSS is waiting for duty payment before clearing the declaration.',
    'INVALIDATED': 'SDI expired after 30 days without payment. A new supplementary declaration will be needed.',
}


def explain_status(value, source='local'):
    key = normalize_status_key(value)
    if not key:
        return 'No status has been recorded yet.'
    if source == 'tss':
        return TSS_STATUS_HELP.get(key, 'Latest status reported by TSS.')
    return LOCAL_STATUS_HELP.get(key, 'Local workflow status stored by Fusion.')
