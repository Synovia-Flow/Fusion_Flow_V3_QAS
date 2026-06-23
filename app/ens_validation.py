"""Shared ENS header validation helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re


def _payload_text(value):
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.strftime('%d/%m/%Y %H:%M:%S')
    return str(value).strip()


def _parse_arrival_datetime(value):
    if isinstance(value, datetime):
        dt = value
    else:
        text = _payload_text(value)
        if not text:
            return None
        normalized = text.replace('Z', '+00:00')
        formats = [
            '%d/%m/%Y %H:%M:%S',
            '%d/%m/%Y %H:%M',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
        ]
        if 'T' in normalized:
            dt = datetime.fromisoformat(normalized)
        else:
            dt = None
            for fmt in formats:
                try:
                    dt = datetime.strptime(normalized.split('.')[0], fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                raise ValueError(f"Cannot parse arrival date '{text}'")

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_choice_values(cursor):
    cv = {}
    tables = {
        'movement_type': 'CV_movement_type',
        'port': 'CV_port',
        'country': 'CV_country',
        'transport_charge': 'CV_transport_charge',
        'passive_transport': 'CV_passive_transport_types',
    }
    for key, table in tables.items():
        try:
            if key == 'port':
                cursor.execute(f"SELECT location_code FROM TSS.[{table}]")
            else:
                cursor.execute(f"SELECT value FROM TSS.[{table}]")
            cv[key] = set(r[0] for r in cursor.fetchall())
        except Exception:
            cv[key] = set()
    return cv


def validate_ens_payload(payload, cv):
    errors = []

    required = [
        ('movement_type', 'Movement Type'),
        ('identity_no_of_transport', 'Identity of Transport'),
        ('nationality_of_transport', 'Nationality of Transport'),
        ('arrival_date_time', 'Arrival Date/Time'),
        ('arrival_port', 'Arrival Port'),
        ('place_of_loading', 'Place of Loading'),
        ('place_of_unloading', 'Place of Unloading'),
        ('transport_charges', 'Transport Charges'),
        ('carrier_eori', 'Carrier EORI'),
    ]
    for field, label in required:
        val = _payload_text(payload.get(field))
        if not val:
            errors.append(f"REQUIRED: {label} is mandatory")

    mt = _payload_text(payload.get('movement_type'))
    if mt and mt not in cv.get('movement_type', set()):
        errors.append(f"INVALID: Movement Type '{mt}' not in allowed values")

    port = _payload_text(payload.get('arrival_port'))
    if port and port not in cv.get('port', set()):
        errors.append(f"INVALID: Arrival Port '{port}' not in allowed ports")

    nat = _payload_text(payload.get('nationality_of_transport'))
    if nat and nat not in cv.get('country', set()):
        errors.append(f"INVALID: Nationality '{nat}' not in country list")

    tc = _payload_text(payload.get('transport_charges'))
    if tc and tc not in cv.get('transport_charge', set()):
        errors.append(f"INVALID: Transport Charges '{tc}' not in allowed values")

    cc = _payload_text(payload.get('carrier_country'))
    if cc and cc not in cv.get('country', set()):
        errors.append(f"INVALID: Carrier Country '{cc}' not in country list")

    ident = _payload_text(payload.get('identity_no_of_transport'))
    if ident:
        if mt in ('1',):
            if not re.match(r'^IMO\d{7}$', ident):
                errors.append(f"FORMAT: Maritime identity must be IMO + 7 digits (got '{ident}')")
        elif mt in ('1a', '3', '3a'):
            if not re.match(r'^IMO\d{7}#.{4,16}$', ident):
                errors.append(f"FORMAT: RoRo identity must be IMO + 7 digits + # + 4-16 chars (got '{ident}')")

    eori = _payload_text(payload.get('carrier_eori'))
    if eori:
        if len(eori) < 3:
            errors.append(f"FORMAT: Carrier EORI too short ('{eori}')")
        elif eori[:2].upper() == 'GB':
            errors.append(f"INVALID: Carrier EORI must be XI or EU prefix, not GB (got '{eori}')")

    adt = _payload_text(payload.get('arrival_date_time'))
    if adt:
        try:
            arrival_dt = _parse_arrival_datetime(payload.get('arrival_date_time'))
        except ValueError:
            errors.append(f"FORMAT: Cannot parse arrival date '{adt}'")
        else:
            now = _utcnow_naive()
            if arrival_dt < now - timedelta(minutes=1):
                errors.append("INVALID: Arrival Date/Time cannot be in the past for ENS headers")
            elif arrival_dt > now + timedelta(days=14):
                errors.append("INVALID: Arrival Date/Time cannot be more than 14 days in the future for ENS headers")

    length_checks = [
        ('identity_no_of_transport', 27, 'Identity of Transport'),
        ('conveyance_ref', 35, 'Conveyance Ref'),
        ('seal_number', 20, 'Seal Number'),
        ('place_of_loading', 33, 'Place of Loading'),
        ('place_of_unloading', 33, 'Place of Unloading'),
        ('carrier_name', 35, 'Carrier Name'),
        ('carrier_street_number', 35, 'Carrier Street'),
        ('carrier_city', 35, 'Carrier City'),
        ('carrier_postcode', 9, 'Carrier Postcode'),
    ]
    for field, maxlen, label in length_checks:
        val = _payload_text(payload.get(field))
        if val and len(val) > maxlen:
            errors.append(f"LENGTH: {label} exceeds max {maxlen} chars (got {len(val)})")

    if mt == '3a':
        pt = _payload_text(payload.get('type_of_passive_transport'))
        if not pt:
            errors.append("REQUIRED: Passive Transport Type is mandatory for RoRo Accompanied (3a)")
        elif pt not in cv.get('passive_transport', set()):
            errors.append(f"INVALID: Passive Transport Type '{pt}' not in allowed values")

        acceptance_same = _payload_text(payload.get('place_of_acceptance_same_as_loading')).lower()
        delivery_same = _payload_text(payload.get('place_of_delivery_same_as_unloading')).lower()
        if acceptance_same not in {'yes', 'no'}:
            errors.append("REQUIRED: Accept=Load? must be set to yes or no for RoRo Accompanied (3a)")
        elif acceptance_same == 'no' and not _payload_text(payload.get('place_of_acceptance')):
            errors.append("REQUIRED: Place of Acceptance is mandatory when Accept=Load? is no for RoRo Accompanied (3a)")

        if delivery_same not in {'yes', 'no'}:
            errors.append("REQUIRED: Deliv=Unload? must be set to yes or no for RoRo Accompanied (3a)")
        elif delivery_same == 'no' and not _payload_text(payload.get('place_of_delivery')):
            errors.append("REQUIRED: Place of Delivery is mandatory when Deliv=Unload? is no for RoRo Accompanied (3a)")

    if mt == '4':
        cr = _payload_text(payload.get('conveyance_ref'))
        if not cr:
            errors.append("REQUIRED: Conveyance Ref is mandatory for Air movements")
        elif len(cr) > 8:
            errors.append(f"LENGTH: Conveyance Ref max 8 chars for Air (got {len(cr)})")

    if mt in ('1', '1a', '3', '3a'):
        carrier_requirements = [
            ('carrier_name', 'Carrier Name'),
            ('carrier_street_number', 'Carrier Street / Number'),
            ('carrier_city', 'Carrier City'),
            ('carrier_postcode', 'Carrier Postcode'),
            ('carrier_country', 'Carrier Country'),
        ]
        for field, label in carrier_requirements:
            if not _payload_text(payload.get(field)):
                errors.append(f"REQUIRED: {label} is mandatory for Maritime/RoRo movements")

    return errors


def auto_validate_declaration_record(*_args, **_kwargs):
    """Compatibility shim for legacy import paths removed from automation PRD.

    Production email automation validates normalized STG records directly. A few
    old modules still import this name during Flask startup, so keep the symbol
    available without reintroducing old staging-table queries.
    """
    return {'ok': True, 'status': 'Validated', 'errors': []}
