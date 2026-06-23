"""
TSS API Client — Fusion Flow V2
Handles Basic Auth, GET reads, POST creates/updates/submits to the
TSS Declaration API v2.9.4/v2.9.5.
"""
import base64
import json
import os
import time
import logging
import hashlib
import itertools
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse
import requests
from flask import current_app

logger = logging.getLogger(__name__)

RATE_LIMIT = 0.25  # seconds between calls
TIMEOUT = 30
API_PATH = '/x_fhmrc_tss_api/v1/tss_api'
_DEMO_COUNTER = itertools.count(1)


def _truthy(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on', 'enabled'}


def build_tss_api_url(base_url):
    """Return the canonical TSS API URL without duplicating API_PATH."""
    resolved_base = (base_url or '').strip().rstrip('/')
    if not resolved_base:
        return ''
    if resolved_base.endswith(API_PATH):
        return resolved_base
    return f"{resolved_base}{API_PATH}"


def _json_safe(value):
    """Return a JSON-serialisable copy of API payload values."""
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.strftime('%d/%m/%Y %H:%M:%S')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _extract_reference(response):
    if not isinstance(response, dict):
        return ''
    for key in (
        'reference',
        'declaration_number',
        'sup_dec_number',
        'supplementary_declaration_number',
        'sfd_number',
        'goods_id',
        'gmr_lrn',
        'glr_number',
        'movement_reference_number',
        'number',
    ):
        value = response.get(key)
        if value not in (None, ''):
            return str(value)
    return ''


def _extract_error_text(response):
    if not isinstance(response, dict):
        return ''
    error = response.get('error')
    if isinstance(error, dict):
        return (
            error.get('detail')
            or error.get('message')
            or error.get('error_description')
            or ''
        )
    if isinstance(error, str):
        return error
    return ''


class TssApiClient:
    """TSS Declaration API v2.9.4/v2.9.5 client."""

    def __init__(self, base_url=None, username=None, password=None, default_act_as=None):
        resolved_base = (base_url or current_app.config['TSS_API_BASE_URL']).strip().rstrip('/')
        self.api_url = build_tss_api_url(resolved_base)
        self.base_url = self.api_url[:-len(API_PATH)] if self.api_url.endswith(API_PATH) else resolved_base

        u = username or current_app.config['TSS_API_USERNAME']
        p = password or current_app.config['TSS_API_PASSWORD']
        self.username = u
        b64 = base64.b64encode(f"{u}:{p}".encode()).decode()

        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Basic {b64}',
        })
        self.default_act_as = (default_act_as or '').strip()

    def _act_as_params(self, act_as=None):
        if act_as is False:
            return {}
        value = (act_as or self.default_act_as or '').strip()
        if not value:
            return {}
        return {'actAs': value}

    def _request(self, method, resource, params=None, payload=None):
        """Execute an API request with rate limiting and error capture."""
        url = f"{self.api_url}/{resource}"
        t0 = time.time()
        safe_payload = _json_safe(payload)

        try:
            if method == 'GET':
                r = self.session.get(url, params=params, timeout=TIMEOUT)
            else:
                r = self.session.post(url, params=params, json=safe_payload, timeout=TIMEOUT)

            duration_ms = int((time.time() - t0) * 1000)

            result = {
                'http_status': r.status_code,
                'duration_ms': duration_ms,
                'url': url,
                'method': method,
                'request_params': params,
                'request_payload': safe_payload,
            }

            try:
                body = r.json()
                result['response'] = body.get('result', body)
                result['raw_response'] = json.dumps(body)[:4000]
            except ValueError:
                result['response'] = {'_raw': r.text[:2000]}
                result['raw_response'] = r.text[:4000]

            result['data'] = result['response']
            result['reference'] = _extract_reference(result['response'])

            if r.status_code == 200:
                response = result['response']
                if not isinstance(response, dict):
                    result['success'] = True
                    result['status'] = 'ok'
                    result['message'] = ''
                    result['process_message'] = ''
                    result['error_message'] = ''
                    return result

                process_message = response.get('process_message', '')
                error_message = response.get('error_message', '')
                error_details = response.get('error_details', '')
                structured_error = _extract_error_text(response)
                response_status = str(response.get('status', '') or '').strip().lower()
                api_error = (
                    (isinstance(process_message, str) and process_message.strip().upper().startswith('ERROR'))
                    or response_status in {'error', 'failure', 'failed'}
                    or bool(structured_error)
                )

                result['success'] = not api_error
                result['status'] = response.get('status', 'ok' if not api_error else 'error')
                result['message'] = (
                    process_message
                    or error_message
                    or error_details
                    or structured_error
                    or ''
                )
                result['process_message'] = process_message
                result['error_message'] = error_message or error_details or structured_error
            else:
                result['success'] = False
                result['status'] = 'error'
                result['message'] = r.text[:500]
                result['process_message'] = ''
                result['error_message'] = r.text[:500]

            return result

        except Exception as e:
            duration_ms = int((time.time() - t0) * 1000)
            logger.error(f"TSS API {method} {url} failed: {e}")
            return {
                'http_status': 0,
                'duration_ms': duration_ms,
                'success': False,
                'status': 'error',
                'message': str(e)[:500],
                'response': {},
                'raw_response': str(e)[:2000],
                'url': url,
                'method': method,
            }
        finally:
            time.sleep(RATE_LIMIT)

    @staticmethod
    def as_items(result):
        """Normalise TSS lookup payloads to a list."""
        if result is None:
            return []
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ('items', 'results', 'data', 'goods'):
                value = result.get(key)
                if isinstance(value, list):
                    return value
            return [result]
        return []

    @staticmethod
    def as_sdi_lookup_items(result):
        """Normalise GET /supplementary_declarations?sfd_number=... responses.

        TSS v2.9.5 documents this lookup as returning a result object with
        sup_dec_number set to either one SUP reference or multiple comma-
        separated SUP references. Filter calls can also return a list of
        objects using number. Keep callers working with a list of SDI records.
        """
        raw_items = TssApiClient.as_items(result)
        normalised = []
        for item in raw_items:
            if isinstance(item, str):
                refs = item.split(',')
                base = {}
            elif isinstance(item, dict):
                ref_value = (
                    item.get('sup_dec_number')
                    or item.get('reference')
                    or item.get('supplementary_declaration_number')
                    or item.get('number')
                    or ''
                )
                refs = str(ref_value).split(',') if ref_value else ['']
                base = item
            else:
                continue

            for ref in refs:
                cleaned = str(ref or '').strip()
                if not cleaned:
                    continue
                clone = dict(base)
                clone['sup_dec_number'] = cleaned
                clone.setdefault('reference', cleaned)
                normalised.append(clone)

        return normalised

    # ── Declaration Headers ──
    def read_header(self, ens_ref, fields):
        """GET /headers?reference=ENS...&fields=..."""
        return self._request('GET', 'headers', params={
            'reference': ens_ref,
            'fields': ','.join(fields) if isinstance(fields, list) else fields,
        })

    def create_header(self, payload):
        """POST /headers — create a new declaration header."""
        body = dict(payload or {})
        body['op_type'] = 'create'
        body['declaration_number'] = ''
        return self._request('POST', 'headers', payload=body)

    def update_header(self, ens_ref, payload):
        """POST /headers — update an existing header."""
        body = dict(payload or {})
        body['op_type'] = 'update'
        body['declaration_number'] = ens_ref
        return self._request('POST', 'headers', payload=body)

    def cancel_header(self, ens_ref):
        """POST /headers — cancel an existing header."""
        return self._request('POST', 'headers', payload={
            'op_type': 'cancel',
            'declaration_number': ens_ref,
        })

    # ── Consignments ──
    def create_consignment(self, ens_ref, payload):
        """POST /consignments — create a consignment on a header."""
        body = dict(payload or {})
        body['op_type'] = 'create'
        body['declaration_number'] = ens_ref
        body['consignment_number'] = ''
        return self._request('POST', 'consignments', payload=body)

    def update_consignment(self, dec_ref, payload):
        """POST /consignments — update an existing consignment."""
        body = dict(payload or {})
        body['op_type'] = 'update'
        body['consignment_number'] = dec_ref
        return self._request('POST', 'consignments', payload=body)

    def read_consignment(self, dec_ref, fields):
        """GET /consignments?reference=DEC...&fields=..."""
        return self._request('GET', 'consignments', params={
            'reference': dec_ref,
            'fields': ','.join(fields) if isinstance(fields, list) else fields,
        })

    def lookup_consignments_for_header(self, ens_ref, parent_type='declaration_number'):
        """GET /consignments?<parent_type>=ENS... to discover DEC refs for a header."""
        return self._request('GET', 'consignments', params={parent_type: ens_ref})

    def submit_consignment(self, dec_ref):
        """POST /consignments — submit a consignment."""
        return self._request('POST', 'consignments', payload={
            'op_type': 'submit',
            'consignment_number': dec_ref,
        })

    def cancel_consignment(self, dec_ref):
        """POST /consignments - cancel an existing consignment."""
        return self._request('POST', 'consignments', payload={
            'op_type': 'cancel',
            'consignment_number': dec_ref,
        })

    # ── Goods Items ──
    def create_goods(self, consignment_ref, payload):
        """POST /goods — add goods to a consignment."""
        body = dict(payload or {})
        body['op_type'] = 'create'
        body['consignment_number'] = consignment_ref
        body['goods_id'] = ''
        return self._request('POST', 'goods', payload=body)

    def lookup_goods(self, parent_ref, parent_type='ens_number'):
        """GET /goods?ens_number=DEC... (or sfd_number, sup_dec_number)."""
        return self._request('GET', 'goods', params={parent_type: parent_ref})

    def lookup_ens_goods(self, consignment_ref):
        """GET /goods?ens_number=DEC... for ENS-context goods ids."""
        return self.lookup_goods(consignment_ref, parent_type='ens_number')

    def read_goods(self, goods_id, fields):
        """GET /goods?reference=hex...&fields=..."""
        return self._request('GET', 'goods', params={
            'reference': goods_id,
            'fields': ','.join(fields) if isinstance(fields, list) else fields,
        })

    def delete_goods(self, goods_id):
        """POST /goods - delete a goods item by remote id."""
        return self._request('POST', 'goods', payload={
            'op_type': 'delete',
            'goods_id': goods_id,
        })

    # ── SFD ──
    def lookup_sfd(self, consignment_number):
        """GET /simplified_frontier_declarations?consignment_number=DEC..."""
        return self._request('GET', 'simplified_frontier_declarations',
                             params={'consignment_number': consignment_number})

    def lookup_sfd_items(self, consignment_number):
        """Return zero-or-more SFD records for a consignment."""
        result = self.lookup_sfd(consignment_number)
        return self.as_items(result.get('response'))

    def read_sfd(self, sfd_ref, fields):
        """GET /simplified_frontier_declarations?reference=DEC...&fields=..."""
        return self._request('GET', 'simplified_frontier_declarations', params={
            'reference': sfd_ref,
            'fields': ','.join(fields) if isinstance(fields, list) else fields,
        })

    def lookup_sfd_goods(self, sfd_number):
        """GET /goods?sfd_number=DEC... and return a list."""
        result = self.lookup_goods(sfd_number, parent_type='sfd_number')
        return self.as_items(result.get('response'))

    def create_sfd(self, payload, act_as=None):
        """POST /simplified_frontier_declarations op_type=create - standalone SFD."""
        body = dict(payload or {})
        body['op_type'] = 'create'
        body.setdefault('sfd_number', '')
        return self._request(
            'POST',
            'simplified_frontier_declarations',
            params=self._act_as_params(act_as),
            payload=body,
        )

    def update_sfd(self, sfd_number, payload, act_as=None):
        """POST /simplified_frontier_declarations op_type=update."""
        body = dict(payload or {})
        body['op_type'] = 'update'
        body['sfd_number'] = sfd_number
        return self._request(
            'POST',
            'simplified_frontier_declarations',
            params=self._act_as_params(act_as),
            payload=body,
        )

    def submit_sfd(self, sfd_number, act_as=None):
        """POST /simplified_frontier_declarations op_type=submit."""
        return self._request(
            'POST',
            'simplified_frontier_declarations',
            params=self._act_as_params(act_as),
            payload={
                'op_type': 'submit',
                'sfd_number': sfd_number,
            },
        )

    def filter_sfds(self, filter_text, act_as=None):
        """GET /simplified_frontier_declarations?filter=... - list SFDs by status filter."""
        params = {'filter': filter_text}
        params.update(self._act_as_params(act_as))
        return self._request('GET', 'simplified_frontier_declarations', params=params)

    # ── SFD Header (Standalone) ──
    def create_sfd_header(self, payload, act_as=None):
        """POST /sfd_headers op_type=create - originate a standalone SFD header."""
        body = dict(payload or {})
        body['op_type'] = 'create'
        body.setdefault('sfd_number', '')
        return self._request(
            'POST',
            'sfd_headers',
            params=self._act_as_params(act_as),
            payload=body,
        )

    def update_sfd_header(self, sfd_number, payload, act_as=None):
        """POST /sfd_headers op_type=update."""
        body = dict(payload or {})
        body['op_type'] = 'update'
        body['sfd_number'] = sfd_number
        return self._request(
            'POST',
            'sfd_headers',
            params=self._act_as_params(act_as),
            payload=body,
        )

    def cancel_sfd_header(self, sfd_number, act_as=None):
        """POST /sfd_headers op_type=cancel."""
        return self._request(
            'POST',
            'sfd_headers',
            params=self._act_as_params(act_as),
            payload={
                'op_type': 'cancel',
                'sfd_number': sfd_number,
            },
        )

    def read_sfd_header(self, reference, fields=None, act_as=None):
        """GET /sfd_headers?reference=...&fields=... - read a standalone SFD header."""
        params = {'reference': reference}
        if fields:
            params['fields'] = ','.join(fields) if isinstance(fields, list) else fields
        params.update(self._act_as_params(act_as))
        return self._request('GET', 'sfd_headers', params=params)

    # ── Supplementary Declarations ──
    def lookup_sdi(self, sfd_number, act_as=None):
        """GET /supplementary_declarations?sfd_number=DEC..."""
        params = {'sfd_number': sfd_number}
        params.update(self._act_as_params(act_as))
        return self._request('GET', 'supplementary_declarations', params=params)

    def lookup_sdi_items(self, sfd_number, act_as=None):
        """Return zero-or-more SDI records for an SFD reference."""
        result = self.lookup_sdi(sfd_number, act_as=act_as)
        return self.as_sdi_lookup_items(result.get('response'))

    def filter_sdis(self, filter_text, act_as=None):
        """GET /supplementary_declarations?filter=... - list SDs by status filter."""
        params = {'filter': filter_text}
        params.update(self._act_as_params(act_as))
        return self._request('GET', 'supplementary_declarations', params=params)

    def filter_sdi_items(self, filter_text, act_as=None):
        """Return zero-or-more SDI records from a documented SD filter call."""
        result = self.filter_sdis(filter_text, act_as=act_as)
        return self.as_sdi_lookup_items(result.get('response'))

    def read_sdi(self, sup_dec_number, fields=None, act_as=None):
        """GET /supplementary_declarations?reference=SUP..."""
        params = {'reference': sup_dec_number}
        if fields:
            params['fields'] = ','.join(fields) if isinstance(fields, list) else fields
        params.update(self._act_as_params(act_as))
        return self._request('GET', 'supplementary_declarations', params=params)

    def create_sdi(self, payload, act_as=None):
        """POST /supplementary_declarations op_type=create - originate a new SDI."""
        body = dict(payload or {})
        body['op_type'] = 'create'
        body.setdefault('sup_dec_number', '')
        return self._request(
            'POST',
            'supplementary_declarations',
            params=self._act_as_params(act_as),
            payload=body,
        )

    def update_sdi(self, sup_dec_number, payload, act_as=None):
        """POST /supplementary_declarations op_type=update."""
        body = dict(payload or {})
        body['op_type'] = 'update'
        body['sup_dec_number'] = sup_dec_number
        return self._request(
            'POST',
            'supplementary_declarations',
            params=self._act_as_params(act_as),
            payload=body,
        )

    def submit_sdi(self, sup_dec_number, act_as=None):
        """POST /supplementary_declarations op_type=submit."""
        return self._request(
            'POST',
            'supplementary_declarations',
            params=self._act_as_params(act_as),
            payload={
                'op_type': 'submit',
                'sup_dec_number': sup_dec_number,
            },
        )

    def recall_sdi(self, sup_dec_number, act_as=None):
        """POST /supplementary_declarations op_type=recall."""
        return self._request(
            'POST',
            'supplementary_declarations',
            params=self._act_as_params(act_as),
            payload={
                'op_type': 'recall',
                'sup_dec_number': sup_dec_number,
            },
        )

    def cancel_sdi(self, sup_dec_number, act_as=None):
        """POST /supplementary_declarations op_type=cancel."""
        return self._request(
            'POST',
            'supplementary_declarations',
            params=self._act_as_params(act_as),
            payload={
                'op_type': 'cancel',
                'sup_dec_number': sup_dec_number,
            },
        )

    def reclassify_sdi_to_immi(
        self,
        sup_dec_number,
        reclassification_type='h1_to_h8',
        internal_market_movement_complies='yes',
    ):
        """POST /supplementary_declarations op_type=reclassify."""
        return self._request('POST', 'supplementary_declarations', payload={
            'op_type': 'reclassify',
            'sup_dec_number': sup_dec_number,
            'reclassification_type': reclassification_type,
            'internal_market_movement_complies': internal_market_movement_complies,
        })

    def lookup_sdi_goods(self, sup_dec_number, act_as=None):
        """GET /goods?sup_dec_number=SUP... and return a list."""
        params = {'sup_dec_number': sup_dec_number}
        params.update(self._act_as_params(act_as))
        result = self._request('GET', 'goods', params=params)
        return self.as_items(result.get('response'))

    def update_sdi_goods(self, sup_dec_number, goods_id, payload, act_as=None):
        """POST /goods op_type=update for SDI-context goods."""
        body = dict(payload or {})
        body['op_type'] = 'update'
        body['goods_id'] = goods_id
        return self._request('POST', 'goods', params=self._act_as_params(act_as), payload=body)

    def get_agent_relationships(self):
        """GET /agent_relationships."""
        return self._request('GET', 'agent_relationships')

    # ── Choice Values ──
    def get_choice_values(self, field_name):
        """GET /choice_values/<field_name>"""
        url = f"{self.base_url}/x_fhmrc_tss_api/v1/choice_values/{field_name}"
        t0 = time.time()
        try:
            r = self.session.get(url, timeout=TIMEOUT)
            duration_ms = int((time.time() - t0) * 1000)
            result = {
                'http_status': r.status_code,
                'duration_ms': duration_ms,
                'url': url,
                'method': 'GET',
                'request_params': None,
                'request_payload': None,
            }
            try:
                body = r.json()
                result['response'] = body.get('result', body)
                result['raw_response'] = json.dumps(body)[:4000]
            except ValueError:
                result['response'] = {'_raw': r.text[:2000]}
                result['raw_response'] = r.text[:4000]

            result['data'] = result['response']
            result['reference'] = ''
            result['success'] = r.status_code == 200
            result['status'] = 'ok' if result['success'] else 'error'
            result['message'] = '' if result['success'] else r.text[:500]
            result['process_message'] = ''
            result['error_message'] = result['message']
            return result
        except Exception as e:
            duration_ms = int((time.time() - t0) * 1000)
            logger.error(f"TSS API GET {url} failed: {e}")
            return {
                'http_status': 0,
                'duration_ms': duration_ms,
                'success': False,
                'status': 'error',
                'message': str(e)[:500],
                'response': {},
                'raw_response': str(e)[:2000],
                'url': url,
                'method': 'GET',
            }
        finally:
            time.sleep(RATE_LIMIT)

    # ── GMR ──
    def create_gmr(self, ens_ref, payload):
        """POST /gvms_gmr — create a GMR."""
        body = dict(payload or {})
        body['op_type'] = 'create'
        body['ens_lrn'] = ens_ref
        return self._request('POST', 'gvms_gmr', payload=body)

    def submit_gmr(self, gmr_lrn):
        """POST /gvms_gmr - submit an existing GMR to GVMS."""
        return self._request('POST', 'gvms_gmr', payload={
            'op_type': 'submit',
            'gmr_lrn': gmr_lrn,
        })

    def update_gmr(self, gmr_lrn, payload):
        """POST /gvms_gmr - update an existing GMR."""
        body = dict(payload or {})
        body['op_type'] = 'update'
        body['gmr_lrn'] = gmr_lrn
        return self._request('POST', 'gvms_gmr', payload=body)

    def cancel_gmr(self, gmr_lrn):
        """POST /gvms_gmr - cancel an existing GMR."""
        return self._request('POST', 'gvms_gmr', payload={
            'op_type': 'cancel',
            'gmr_lrn': gmr_lrn,
        })

    def read_gmr(self, reference, fields=None):
        """GET /gvms_gmr?reference=...&fields=... - read a GMR by gmr_id or ens reference."""
        params = {'reference': reference}
        if fields:
            params['fields'] = ','.join(fields) if isinstance(fields, list) else fields
        return self._request('GET', 'gvms_gmr', params=params)


    def test_connection(self):
        """
        Probe TSS API connectivity.
        GET /consignments?reference=__probe__ — TSS returns structured JSON
        even for unknown references, so any non-HTML response confirms
        BASE_URL and credentials are correct.
        Raises ValueError if an HTML login page is returned (bad auth/URL).
        """
        result = self._request('GET', 'consignments', params={'reference': '__probe__'})
        raw = result.get('raw_response', '') or ''
        if '<html' in raw.lower() or '<!doctype' in raw.lower():
            raise ValueError(
                "HTML login page returned — check BASE_URL and credentials."
            )
        return result


def _next_demo_reference(prefix):
    return f"{prefix}{900000000000000 + next(_DEMO_COUNTER):015d}"


def _stable_demo_reference(prefix, seed):
    digest = hashlib.sha1(str(seed or prefix).encode('utf-8')).hexdigest()
    number = 100000000000000 + (int(digest[:12], 16) % 800000000000000)
    return f"{prefix}{number:015d}"


def _demo_goods_id(seed=None):
    if isinstance(seed, dict):
        # Hash full payload (minus op_type/goods_id) so each goods item gets a
        # unique but stable id — different items get different ids, same item
        # submitted twice gets the same id (idempotent).
        canonical = json.dumps(
            {k: v for k, v in sorted(seed.items()) if k not in ('op_type', 'goods_id')},
            sort_keys=True, default=str,
        )
        token = canonical
    else:
        token = seed or f"goods-{next(_DEMO_COUNTER)}"
    return hashlib.md5(str(token).encode('utf-8')).hexdigest()


class _DemoResponse:
    def __init__(self, body, status_code=200):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


class _DemoTssSession:
    """Small requests.Session-compatible adapter for tenant Demo Mode."""

    def __init__(self):
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        return _DemoResponse({'result': self._get_result(url, params or {})})

    def post(self, url, params=None, json=None, timeout=None):
        return _DemoResponse({'result': self._post_result(url, json or {}, params or {})})

    def _resource(self, url):
        parsed = urlparse(url)
        path = (parsed.path or url).rstrip('/')
        if '/choice_values/' in path:
            return 'choice_values'
        return path.rsplit('/', 1)[-1]

    def _post_result(self, url, payload, params):
        resource = self._resource(url)
        op_type = str(payload.get('op_type') or 'create').strip().lower()

        if resource == 'headers':
            # Stable seed: movement_type + arrival_date_time + identity_no_of_transport
            seed = '-'.join(str(payload.get(f) or '') for f in (
                'movement_type', 'arrival_date_time', 'identity_no_of_transport',
                'arrival_port', 'carrier_eori',
            ))
            ref = payload.get('declaration_number') or _stable_demo_reference('ENS', seed or 'ens-default')
            status = {'create': 'created', 'update': 'updated', 'cancel': 'cancelled'}.get(op_type, op_type)
            return self._success(status, ref, declaration_number=ref)

        if resource == 'consignments':
            # Stable seed: parent ENS ref + transport doc number + goods description
            # This makes the same consignment always produce the same DEC ref even
            # across multiple pipeline runs, preventing unique-index collisions.
            seed = '-'.join(str(payload.get(f) or '') for f in (
                'declaration_number', 'transport_document_number', 'goods_description',
                'importer_eori', 'consignor_eori',
            ))
            ref = payload.get('consignment_number') or _stable_demo_reference('DEC', seed or 'dec-default')
            status = {
                'create': 'created',
                'update': 'updated',
                'submit': 'submitted',
                'cancel': 'cancelled',
            }.get(op_type, op_type)
            return self._success(status, ref, consignment_number=ref, reference=ref)

        if resource == 'goods':
            ref = payload.get('goods_id') or _demo_goods_id(payload)
            status = {'create': 'created', 'update': 'updated'}.get(op_type, op_type)
            return self._success(status, ref, goods_id=ref, reference=ref)

        if resource == 'supplementary_declarations':
            seed = '-'.join(str(payload.get(f) or '') for f in (
                'sfd_number', 'declaration_number', 'importer_eori',
            ))
            ref = payload.get('sup_dec_number') or _stable_demo_reference('SUP', seed or 'sup-default')
            status = {
                'create': 'created',
                'update': 'updated',
                'submit': 'submitted',
                'recall': 'recalled',
                'cancel': 'cancelled',
            }.get(op_type, op_type)
            return self._success(status, ref, sup_dec_number=ref, reference=ref)

        if resource == 'simplified_frontier_declarations':
            ref = payload.get('sfd_number') or payload.get('reference') or _stable_demo_reference('DEC', payload.get('consignment_number') or payload.get('sfd_parent') or 'sfd-default')
            status = {'update': 'updated', 'submit': 'submitted', 'create': 'created'}.get(op_type, op_type)
            return self._success(status, ref, sfd_number=ref, reference=ref)

        if resource == 'sfd_headers':
            seed = '-'.join(str(payload.get(f) or '') for f in (
                'identity_no_of_transport', 'arrival_port', 'arrival_date_time',
            ))
            ref = payload.get('sfd_number') or _stable_demo_reference('DEC', seed or 'sfd-header-default')
            status = {'create': 'created', 'update': 'updated', 'cancel': 'cancelled'}.get(op_type, op_type)
            return self._success(status, ref, sfd_number=ref, reference=ref)

        if resource == 'gvms_gmr':
            seed = '-'.join(str(payload.get(f) or '') for f in ('inspection_required', 'haulier_eori', 'vehicle_reg_no'))
            ref = payload.get('gmr_id') or _stable_demo_reference('GMR', seed or 'gmr-default')
            status = 'Open' if op_type == 'submit' else 'created'
            return self._success(status, ref, gmr_id=ref, reference=ref, gvms_status='Open')

        return self._success('ok', _stable_demo_reference('DMO', str(payload)), reference=_stable_demo_reference('DMO', str(payload)))

    def _get_result(self, url, params):
        resource = self._resource(url)

        if resource == 'choice_values':
            return {'items': self._choice_values(url)}

        if resource == 'agent_relationships':
            return {'items': self._agent_relationships()}

        if resource == 'headers':
            ref = params.get('reference') or params.get('declaration_number') or _stable_demo_reference('ENS', params)
            return self._header_item(ref)

        if resource == 'consignments':
            if params.get('declaration_number'):
                return {'items': []}
            ref = params.get('reference') or params.get('consignment_number') or _stable_demo_reference('DEC', params)
            return self._consignment_item(ref, ens_ref=params.get('declaration_number'))

        if resource == 'goods':
            parent = (
                params.get('ens_number')
                or params.get('consignment_number')
                or params.get('sfd_number')
                or params.get('sup_dec_number')
                or params.get('reference')
            )
            if params.get('reference'):
                return self._goods_item(params.get('reference'), parent)
            return {'items': [self._goods_item(_demo_goods_id(parent), parent)] if parent else []}

        if resource == 'simplified_frontier_declarations':
            dec_ref = params.get('consignment_number') or params.get('reference') or ''
            if params.get('filter'):
                return {'items': [self._sfd_item(dec_ref or 'sfd-default')], 'filter': params.get('filter')}
            return {'items': [self._sfd_item(dec_ref)] if dec_ref else []}

        if resource == 'sfd_headers':
            ref = params.get('reference') or _stable_demo_reference('SFD', 'sfd-header-default')
            return self._sfd_item(ref)

        if resource == 'supplementary_declarations':
            if params.get('filter'):
                parent = _stable_demo_reference('DEC', params.get('filter') or 'sdi-filter-default')
                return {'items': [self._sdi_item(parent)], 'filter': params.get('filter')}
            parent = params.get('sfd_number') or params.get('reference') or ''
            return {'items': [self._sdi_item(parent)] if parent else []}

        if resource == 'gvms_gmr':
            ref = params.get('gmr_id') or params.get('reference') or _stable_demo_reference('GMR', 'gmr-default')
            arrived = self._has_submitted_gmr(gmr_ref=ref)
            gmr_status = 'Arrived' if arrived else 'Open'
            return {
                'gmr_id': ref,
                'reference': ref,
                'status': gmr_status,
                'gvms_status': gmr_status,
                'process_message': 'SUCCESS',
                'demo_mode': True,
            }

        return {'items': []}

    def _success(self, status, ref, **extra):
        data = {
            'status': status,
            'process_message': 'SUCCESS',
            'reference': ref,
            'demo_mode': True,
            'demo_message': 'Simulated TSS response. No live TSS API call was made.',
        }
        data.update(extra)
        return data

    def _has_submitted_gmr(self, ens_ref=None, gmr_ref=None):
        """Return True if a submitted/active GMR exists for the given ENS or GMR reference.
        Falls back to False silently when called outside a Flask app context (e.g. scripts)."""
        try:
            from app.db import query_one
            from app.tenant import get_tenant
            schema = get_tenant()['schema']
            if gmr_ref:
                row = query_one(
                    f"SELECT TOP 1 staging_id FROM {schema}.StagingGmrs"
                    " WHERE gmr_id = ? AND status IN ('SUBMITTED','ACTIVE','CLOSED')",
                    [gmr_ref],
                )
            elif ens_ref:
                # Match by direct ens_reference OR via staging_ens_id → StagingEnsHeaders join
                # (covers seeded GMRs where ens_reference is NULL but staging_ens_id is set)
                row = query_one(
                    f"""SELECT TOP 1 g.staging_id
                        FROM {schema}.StagingGmrs g
                        LEFT JOIN {schema}.StagingEnsHeaders h ON h.staging_id = g.staging_ens_id
                        WHERE (g.ens_reference = ? OR h.ens_reference = ?)
                          AND g.status IN ('SUBMITTED','ACTIVE','CLOSED')""",
                    [ens_ref, ens_ref],
                )
            else:
                return False
            return bool(row)
        except Exception:
            return False

    def _all_cons_auth_for_movement(self, ens_ref):
        """True if the ENS has ≥1 consignment and ALL are Authorised for Movement."""
        if not ens_ref:
            return False
        try:
            from app.db import query_one
            from app.tenant import get_tenant
            schema = get_tenant()['schema']
            row = query_one(
                f"""SELECT COUNT(*) AS total,
                           SUM(CASE WHEN UPPER(REPLACE(COALESCE(tss_status, ''), '_', ' ')) IN (
                               'AUTHORISED FOR MOVEMENT',
                               'AUTHORIZED FOR MOVEMENT',
                               'ARRIVED'
                           ) THEN 1 ELSE 0 END) AS auth_count
                    FROM {schema}.StagingConsignments c
                    JOIN {schema}.StagingEnsHeaders h ON h.staging_id = c.staging_ens_id
                    WHERE h.ens_reference = ?""",
                [ens_ref],
            )
            if not row:
                return False
            total = row['total'] or 0
            return total > 0 and (row['auth_count'] or 0) >= total
        except Exception:
            return False

    def _all_cons_arrived(self, ens_ref):
        """True if every local consignment under the ENS is already Arrived."""
        if not ens_ref:
            return False
        try:
            from app.db import query_one
            from app.tenant import get_tenant
            schema = get_tenant()['schema']
            row = query_one(
                f"""SELECT COUNT(*) AS total,
                           SUM(CASE WHEN UPPER(REPLACE(COALESCE(tss_status, ''), '_', ' ')) = 'ARRIVED'
                                    THEN 1 ELSE 0 END) AS arrived_count
                    FROM {schema}.StagingConsignments c
                    JOIN {schema}.StagingEnsHeaders h ON h.staging_id = c.staging_ens_id
                    WHERE h.ens_reference = ?""",
                [ens_ref],
            )
            if not row:
                return False
            total = row['total'] or 0
            return total > 0 and (row['arrived_count'] or 0) >= total
        except Exception:
            return False

    def _header_item(self, ref):
        arrived = self._has_submitted_gmr(ens_ref=ref) or self._all_cons_arrived(ref)
        if arrived:
            status = 'Arrived'
        elif self._all_cons_auth_for_movement(ref):
            status = 'Authorised for Movement'
        else:
            status = 'Processing'
        return {
            'reference': ref,
            'declaration_number': ref,
            'status': status,
            'error_message': '',
            'movement_type': '3a',
            'arrival_date_time': datetime.utcnow().strftime('%d/%m/%Y %H:%M:%S'),
            'arrival_port': 'GBAUBELBELBEL',
            'carrier_eori': 'XI000000000000',
            'carrier_name': 'Demo Carrier Ltd',
            'identity_no_of_transport': 'DEMO-TRUCK',
            'nationality_of_transport': 'GB',
            'route': 'Route A',
            'demo_mode': True,
        }

    def _consignment_item(self, ref, ens_ref=None):
        ens_ref = ens_ref or _stable_demo_reference('ENS', ref)
        arrived = self._has_submitted_gmr(ens_ref=ens_ref)
        return {
            'reference': ref,
            'declaration_number': ens_ref,
            'status': 'Arrived' if arrived else 'Authorised for Movement',
            'error_message': '',
            'movement_reference_number': _stable_demo_reference('26XI', ref),
            'goods_description': 'Demo goods shipment',
            'transport_document_number': 'DEMO-CMR-001',
            'controlled_goods': 'no',
            'goods_domestic_status': 'D',
            'destination_country': 'GB',
            'container_indicator': '0',
            'consignor_eori': 'XI000000000000',
            'consignor_name': 'Demo Consignor Ltd',
            'consignor_street_number': '1 Demo Street',
            'consignor_city': 'Belfast',
            'consignor_postcode': 'BT1 1AA',
            'consignor_country': 'GB',
            'consignee_name': 'Demo Consignee Ltd',
            'consignee_street_number': '2 Demo Road',
            'consignee_city': 'Belfast',
            'consignee_postcode': 'BT2 2BB',
            'consignee_country': 'GB',
            'importer_eori': 'XI000000000000',
            'importer_name': 'Demo Importer Ltd',
            'importer_street_number': '3 Demo Avenue',
            'importer_city': 'Belfast',
            'importer_postcode': 'BT3 3CC',
            'importer_country': 'GB',
            'exporter_eori': 'XI000000000000',
            'exporter_name': 'Demo Exporter Ltd',
            'exporter_street_number': '4 Demo Lane',
            'exporter_city': 'Belfast',
            'exporter_postcode': 'BT4 4DD',
            'exporter_country': 'GB',
            'buyer_same_as_importer': 'Yes',
            'seller_same_as_exporter': 'Yes',
            'total_packages': '1',
            'gross_mass_kg': '100.000',
            'demo_mode': True,
        }

    def _goods_item(self, goods_id, parent):
        return {
            'reference': goods_id,
            'goods_id': goods_id,
            'consignment_number': parent or '',
            'goods_description': 'Demo goods item',
            'commodity_code': '4418295000',
            'type_of_packages': 'pallets',
            'number_of_packages': '1',
            'gross_mass_kg': '100.000',
            'net_mass_kg': '95.000',
            'country_of_origin': 'GB',
            'item_invoice_amount': '100.00',
            'item_invoice_currency': 'GBP',
            'status': 'created',
            'demo_mode': True,
        }

    def _sfd_item(self, dec_ref):
        sfd_ref = _stable_demo_reference('DEC', dec_ref)
        return {
            'reference': sfd_ref,
            'sfd_number': sfd_ref,
            'consignment_number': dec_ref,
            'status': 'Authorised for Movement',
            'goods_description': 'Demo goods shipment',
            'transport_document_number': 'DEMO-CMR-001',
            'controlled_goods': 'no',
            'importer_eori': 'XI000000000000',
            'demo_mode': True,
        }

    def _sdi_item(self, parent):
        sup_ref = parent if str(parent).startswith('SUP') else _stable_demo_reference('SUP', parent)
        return {
            'reference': sup_ref,
            'sup_dec_number': sup_ref,
            'sfd_number': parent if str(parent).startswith(('DEC', 'SFD')) else '',
            'status': 'Draft',
            'demo_mode': True,
        }

    def _agent_relationships(self):
        return [
            {
                'agent_account': 'Demo Agent Ltd',
                'customer_account': 'Demo Customer Ltd',
                'customer_account_sys_id': 'demo_customer_account_sys_id',
            },
            {
                'agent_account': 'Demo Agent Ltd',
                'customer_account': 'Demo Importer Ltd',
                'customer_account_sys_id': 'demo_importer_account_sys_id',
            },
        ]

    def _choice_values(self, url):
        field_name = urlparse(url).path.rstrip('/').rsplit('/', 1)[-1]
        if field_name == 'preference':
            return [
                {'value': '100', 'name': 'No preference'},
                {'value': '300', 'name': 'Preference claimed'},
            ]
        return [{'value': 'demo', 'name': f'Demo {field_name}'}]


class DemoTssApiClient(TssApiClient):
    """TSS API client facade that never makes network calls."""

    def __init__(self, default_act_as=None):
        self.api_url = 'demo://tss-api'
        self.base_url = 'demo://tss'
        self.username = 'demo'
        self.session = _DemoTssSession()
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-Fusion-Demo-Mode': 'true',
        })
        self.default_act_as = (default_act_as or '').strip()
        self.demo_mode = True


def build_env_client():
    """Construct a TSS client outside Flask app context using environment variables."""
    if (os.environ.get('TSS_API_ENVIRONMENT', '') or '').strip().lower() == 'demo' or _truthy(os.environ.get('DEMO_ENABLED')):
        return DemoTssApiClient(default_act_as=os.environ.get('TSS_API_ACT_AS', ''))
    return TssApiClient(
        base_url=os.environ.get('TSS_API_BASE_URL', ''),
        username=os.environ.get('TSS_API_USERNAME', ''),
        password=os.environ.get('TSS_API_PASSWORD', ''),
        default_act_as=os.environ.get('TSS_API_ACT_AS', ''),
    )


def resolve_tss_settings():
    """
    Resolve TSS settings outside Flask request context.

    Priority:
    1. Active tenant AppConfiguration
    2. Environment variables
    3. CFG.Credentials + CFG.Environments for the active tenant code
    """
    from app import config_store

    env_base_url = (os.environ.get('TSS_API_BASE_URL', '') or '').strip()
    env_environment = (os.environ.get('TSS_API_ENVIRONMENT', '') or '').strip().lower()
    env_username = (os.environ.get('TSS_API_USERNAME', '') or '').strip()
    env_password = (os.environ.get('TSS_API_PASSWORD', '') or '').strip()
    env_act_as = (os.environ.get('TSS_API_ACT_AS', '') or '').strip()
    env_demo_enabled = (os.environ.get('DEMO_ENABLED', '') or '').strip()

    app_demo_enabled = config_store.get_db_value("DEMO", "ENABLED")
    app_mode = (config_store.get_db_value("TSS_API", "ENVIRONMENT") or '').strip().lower()
    app_test_url = (config_store.get_db_value("TSS_API", "TEST_URL") or '').strip()
    app_base_url = (config_store.get_db_value("TSS_API", "BASE_URL") or '').strip()
    app_username = (config_store.get_db_value("TSS_API", "USERNAME") or '').strip()
    app_password = (config_store.get_db_value("TSS_API", "PASSWORD") or '').strip()
    app_act_as = (config_store.get_db_value("TSS_API", "ACT_AS") or '').strip()

    cfg_base_url = ''
    cfg_username = ''
    cfg_password = ''
    cfg_env_code = ''
    try:
        from app.db import get_standalone_connection
        from app.tenant import get_tenant

        tenant_code = get_tenant()["code"]
        conn = get_standalone_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT TOP 1 c.tss_username, c.tss_password, e.base_url, c.env_code
            FROM CFG.Credentials c
            JOIN CFG.Environments e ON e.env_code = c.env_code AND e.active = 1
            WHERE c.client_code = ? AND c.active = 1
            ORDER BY c.credential_id DESC
        """, [tenant_code])
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            cfg_username = (row[0] or '').strip()
            cfg_password = (row[1] or '').strip()
            cfg_base_url = (row[2] or '').strip()
            cfg_env_code = (row[3] or '').strip().lower()
    except Exception:
        pass

    selected_mode = app_mode or env_environment
    # DEMO.ENABLED=true always wins regardless of which environment is set
    demo_enabled = (
        _truthy(app_demo_enabled)
        or _truthy(env_demo_enabled)
        or selected_mode == 'demo'
    )
    preferred_app_url = app_test_url if selected_mode == 'test' and app_test_url else app_base_url
    environment = 'demo' if demo_enabled else (selected_mode or cfg_env_code or ('env' if (env_base_url or env_username or env_password) else 'env'))
    base_url = 'demo://tss-api' if demo_enabled else (preferred_app_url or env_base_url or cfg_base_url)
    username = 'demo' if demo_enabled else (app_username or env_username or cfg_username)
    password = 'demo' if demo_enabled else (app_password or env_password or cfg_password)
    act_as = app_act_as or env_act_as

    source_bits = [
        'Demo Mode' if demo_enabled else ('AppConfiguration URL' if preferred_app_url else ('environment URL' if env_base_url else ('CFG URL' if cfg_base_url else 'missing URL'))),
        'simulated credentials' if demo_enabled else ('AppConfiguration username' if app_username else ('environment username' if env_username else ('CFG username' if cfg_username else 'missing username'))),
        'simulated password' if demo_enabled else ('AppConfiguration password' if app_password else ('environment password' if env_password else ('CFG password' if cfg_password else 'missing password'))),
        'AppConfiguration actAs' if app_act_as else ('environment actAs' if env_act_as else 'no actAs'),
    ]

    return {
        'base_url': base_url,
        'username': username,
        'password': password,
        'act_as': act_as,
        'environment': environment,
        'demo_enabled': demo_enabled,
        'using_fallback': False if demo_enabled else not (preferred_app_url and app_username and app_password),
        'source_label': ', '.join(source_bits),
    }


def build_cfg_client():
    """
    Construct a TSS client using the shared TSS resolution path.
    """
    resolved = resolve_tss_settings()
    if resolved.get('demo_enabled'):
        return DemoTssApiClient(default_act_as=resolved.get('act_as'))
    return TssApiClient(
        base_url=resolved['base_url'],
        username=resolved['username'],
        password=resolved['password'],
        default_act_as=resolved.get('act_as'),
    )
