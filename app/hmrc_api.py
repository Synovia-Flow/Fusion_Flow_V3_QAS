"""
Fusion Flow - HMRC and EU customs reference lookups.

Provides:
- EORI validation with route-aware fallback:
  - GB -> HMRC single-check endpoint
  - XI / EU -> EU Commission SOAP endpoint
- Commodity code lookups via HMRC Trade Tariff
"""
import logging
import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime

logger = logging.getLogger(__name__)

_HMRC_EORI_URL = 'https://trader.services.eori.hmrc.gov.uk/check-eori-number/{eori}'
_TARIFF_URL = 'https://www.trade-tariff.service.gov.uk/api/v2/commodities/{code}'
_EU_SOAP_URL = 'https://ec.europa.eu/taxation_customs/dds2/eos/validation/services/validation'
_SOAP_NS = 'http://eori.ws.eos.dds.s/'
_TIMEOUT = 10

_HTTP_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/125.0.0.0 Safari/537.36'
)

_EU_PREFIXES = {
    'XI', 'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR',
    'DE', 'GR', 'HU', 'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL',
    'PT', 'RO', 'SK', 'SI', 'ES', 'SE',
}
_GB_PATTERN = re.compile(r'^GB\d{12,15}$')


def _now_iso():
    return datetime.utcnow().isoformat()


def _base_eori_result(eori, route='UNKNOWN'):
    eori = (eori or '').strip().upper()
    return {
        'eori': eori,
        'valid': False,
        'trader_name': None,
        'checked_at': _now_iso(),
        'error': None,
        'route': route,
        'status': None,
        'processing_date': None,
    }


def _route_eori(eori):
    eori = (eori or '').strip().upper()
    if not eori:
        return 'UNKNOWN'
    prefix = eori[:2]
    if prefix == 'GB' and _GB_PATTERN.match(eori):
        return 'HMRC_UK'
    if prefix in _EU_PREFIXES:
        return 'EU_SOAP'
    return 'UNKNOWN'


def _hmrc_single(eori):
    result = _base_eori_result(eori, route='HMRC_UK')
    if not result['eori']:
        result['error'] = 'Empty EORI'
        return result

    try:
        resp = requests.get(
            _HMRC_EORI_URL.format(eori=result['eori']),
            timeout=_TIMEOUT,
            headers={'Accept': 'application/json', 'User-Agent': _HTTP_UA},
        )
        if resp.status_code == 200:
            data = resp.json()
            result['valid'] = bool(data.get('valid', False))
            result['trader_name'] = data.get('traderName') or data.get('name')
            result['status'] = 'Valid' if result['valid'] else 'Invalid'
            result['processing_date'] = data.get('processingDate')
        elif resp.status_code == 404:
            result['status'] = 'Invalid'
        else:
            result['error'] = f'HTTP {resp.status_code}'
            result['status'] = 'Error'
    except requests.Timeout:
        result['error'] = 'Timeout'
        result['status'] = 'Error'
        logger.warning('EORI HMRC check timed out for %s', result['eori'])
    except Exception as exc:
        result['error'] = str(exc)
        result['status'] = 'Error'
        logger.error('EORI HMRC check failed for %s: %s', result['eori'], exc)
    return result


def _build_eu_envelope(eoris):
    eori_xml = ''.join(f'<eori xmlns="{_SOAP_NS}">{e}</eori>' for e in eoris)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:s="{_SOAP_NS}">'
        '<soapenv:Body>'
        f'<s:validateEORI>{eori_xml}</s:validateEORI>'
        '</soapenv:Body>'
        '</soapenv:Envelope>'
    )


def _child_text(parent, *tag_names):
    for tag in tag_names:
        el = parent.find(tag)
        if el is not None and el.text:
            return el.text.strip()
        el = parent.find(f'{{{_SOAP_NS}}}{tag}')
        if el is not None and el.text:
            return el.text.strip()
    return ''


def _parse_eu_response(xml_text, eoris):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {
            e: {
                **_base_eori_result(e, route='EU_SOAP'),
                'error': f'XML parse error: {exc}',
                'status': 'Error',
            } for e in eoris
        }

    return_el = None
    for el in root.iter():
        if el.tag == 'return' or el.tag.endswith('}return'):
            return_el = el
            break

    if return_el is None:
        return {
            e: {
                **_base_eori_result(e, route='EU_SOAP'),
                'error': 'No <return> element in response',
                'status': 'Error',
            } for e in eoris
        }

    request_date = _child_text(return_el, 'requestDate')
    result_els = (
        return_el.findall('r')
        or return_el.findall('result')
        or return_el.findall(f'{{{_SOAP_NS}}}result')
    )

    results = {}
    for result_el in result_els:
        eori_value = _child_text(result_el, 'eori').upper()
        status_code = _child_text(result_el, 'status')
        valid = (status_code == '0')
        results[eori_value] = {
            **_base_eori_result(eori_value, route='EU_SOAP'),
            'valid': valid,
            'trader_name': _child_text(result_el, 'n', 'name') or None,
            'status': _child_text(result_el, 'statusDescr') or ('Valid' if valid else 'Invalid'),
            'processing_date': request_date or None,
            'error': _child_text(result_el, 'errorReason') or None,
        }

    for eori in eoris:
        if eori not in results:
            results[eori] = {
                **_base_eori_result(eori, route='EU_SOAP'),
                'error': 'Not returned in EU SOAP response',
                'status': 'Error',
            }

    return results


def _eu_batch(eoris):
    eoris = [(e or '').strip().upper() for e in eoris if (e or '').strip()]
    if not eoris:
        return {}

    try:
        resp = requests.post(
            _EU_SOAP_URL,
            data=_build_eu_envelope(eoris),
            timeout=_TIMEOUT,
            headers={
                'Content-Type': 'text/xml; charset=utf-8',
                'Accept': 'text/xml',
                'User-Agent': _HTTP_UA,
                'SOAPAction': '',
            },
        )
        if resp.status_code == 200:
            return _parse_eu_response(resp.text, eoris)
        return {
            e: {
                **_base_eori_result(e, route='EU_SOAP'),
                'error': f'HTTP {resp.status_code}',
                'status': 'Error',
            } for e in eoris
        }
    except requests.Timeout:
        return {
            e: {
                **_base_eori_result(e, route='EU_SOAP'),
                'error': 'Timeout',
                'status': 'Error',
            } for e in eoris
        }
    except Exception as exc:
        logger.error('EU SOAP EORI batch failed: %s', exc)
        return {
            e: {
                **_base_eori_result(e, route='EU_SOAP'),
                'error': str(exc),
                'status': 'Error',
            } for e in eoris
        }


def check_eori(eori: str) -> dict:
    """
    Validate an EORI number.

    Returns:
        {
          'eori': str,
          'valid': bool,
          'trader_name': str | None,
          'checked_at': ISO timestamp,
          'error': str | None,
          'route': 'HMRC_UK' | 'EU_SOAP' | 'UNKNOWN',
          'status': str | None,
          'processing_date': str | None,
        }
    """
    eori = (eori or '').strip().upper()
    route = _route_eori(eori)
    if route == 'HMRC_UK':
        return _hmrc_single(eori)
    if route == 'EU_SOAP':
        return _eu_batch([eori]).get(eori, _base_eori_result(eori, route='EU_SOAP'))

    result = _base_eori_result(eori, route='UNKNOWN')
    result['error'] = 'Unsupported or unrecognised EORI prefix'
    result['status'] = 'Invalid'
    return result


def check_eori_batch(eori_list: list) -> dict:
    """
    Validate a list of EORIs.
    Uses EU SOAP batching for XI/EU prefixes and per-record HMRC checks for GB.
    """
    results = {}
    hmrc_eoris = []
    eu_eoris = []

    for raw in eori_list:
        eori = (raw or '').strip().upper()
        if not eori or eori in results:
            continue
        route = _route_eori(eori)
        if route == 'HMRC_UK':
            hmrc_eoris.append(eori)
        elif route == 'EU_SOAP':
            eu_eoris.append(eori)
        else:
            result = _base_eori_result(eori, route='UNKNOWN')
            result['error'] = 'Unsupported or unrecognised EORI prefix'
            result['status'] = 'Invalid'
            results[eori] = result

    for eori in hmrc_eoris:
        results[eori] = _hmrc_single(eori)

    if eu_eoris:
        results.update(_eu_batch(eu_eoris))

    return results


def _normalise_commodity(code: str) -> str:
    """Normalise to a 10-digit commodity code by right-padding with zeros."""
    code = (code or '').strip().replace(' ', '')
    return code.ljust(10, '0') if len(code) <= 10 else code[:10]


def check_commodity(code: str) -> dict:
    """
    Validate a commodity code via the HMRC Trade Tariff API.
    Right-pads to 10 digits with trailing zeros.
    """
    code_clean = _normalise_commodity(code)
    result = {
        'code': code_clean,
        'valid': False,
        'description': None,
        'chapter': None,
        'vat_rates': [],
        'checked_at': _now_iso(),
        'error': None,
    }

    raw_len = len((code or '').strip().replace(' ', ''))
    if raw_len < 6:
        result['error'] = f'Code too short (need >=6 digits): {code!r}'
        return result

    try:
        resp = requests.get(
            _TARIFF_URL.format(code=code_clean),
            timeout=_TIMEOUT,
            headers={'Accept': 'application/json', 'User-Agent': _HTTP_UA},
        )
        if resp.status_code == 200:
            payload = resp.json()
            data = payload.get('data', {})
            attrs = data.get('attributes', {})
            result['valid'] = True
            result['description'] = attrs.get('description') or attrs.get('formatted_description')
            result['chapter'] = code_clean[:2]
            included = payload.get('included', [])
            vat = [
                item.get('attributes', {}).get('duty_expression', {}).get('base', '')
                for item in included
                if item.get('type') == 'measure'
                and item.get('attributes', {}).get('measure_type_id', '') in ('305', '306')
            ]
            result['vat_rates'] = [value for value in vat if value]
        elif resp.status_code == 404:
            result['error'] = 'Not found in UK Tariff'
        else:
            result['error'] = f'HTTP {resp.status_code}'
    except requests.Timeout:
        result['error'] = 'Timeout'
        logger.warning('Commodity check timed out for %s', code_clean)
    except Exception as exc:
        result['error'] = str(exc)
        logger.error('Commodity check failed for %s: %s', code_clean, exc)
    return result


def check_commodity_batch(code_list: list) -> dict:
    """Validate a list of commodity codes."""
    results = {}
    seen = set()
    for code in code_list:
        code_clean = _normalise_commodity(code)
        if not code_clean or code_clean == '0000000000' or code_clean in seen:
            continue
        seen.add(code_clean)
        results[code_clean] = check_commodity(code_clean)
    return results
