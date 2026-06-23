"""
Shared local validation helpers for the Route A pipeline.

These functions mirror the rules used by scripts/validate_pipeline.py so
web edits can be revalidated immediately without waiting for a full batch run.
"""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app import config_store
from app.db import db_cursor
from app.tenant import get_tenant
from app.sdi_payloads import validate_taric_code
from app.tss_text import tss_unsafe_value_message


def _schema():
    return get_tenant()["schema"]

# EORI format: 2-letter country code + 6-15 digits.
_EORI_RE = re.compile(r'^[A-Z]{2}\d{6,15}$')

_RORO_MOVEMENT_TYPES = {'1', '1a', '3', '3a'}

_GOODS_TSS_TEXT_FIELDS = [
    ('goods_description', 'Goods Description'),
    ('package_marks', 'Package Marks'),
    ('invoice_number', 'Invoice Number'),
    ('ni_additional_information_codes', 'NI Additional Info'),
]

_UNSAFE_SAMPLE_EORIS = {
    'GB000000000000',
}


def _has_more_than_2_dp(value):
    if value in (None, ''):
        return False
    try:
        dec = Decimal(str(value).strip()).normalize()
    except (InvalidOperation, ValueError):
        return False
    exponent = -dec.as_tuple().exponent
    return exponent > 2


def normalise_decimal_scale(value, scale=2):
    if value in (None, ''):
        return value
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return value
    quant = Decimal('1').scaleb(-int(scale))
    rounded = dec.quantize(quant, rounding=ROUND_HALF_UP)
    return float(rounded)


def normalise_goods_decimal_fields(row, scale=2):
    updates = {}
    for field in ('gross_mass_kg', 'net_mass_kg', 'item_invoice_amount'):
        value = row.get(field)
        if _has_more_than_2_dp(value):
            updates[field] = normalise_decimal_scale(value, scale=scale)
    return updates


def load_cv(cursor, table, col='value'):
    try:
        cursor.execute(f"SELECT [{col}] FROM TSS.[{table}]")
        return set(r[0] for r in cursor.fetchall())
    except Exception:
        return set()


def build_validation_choice_sets(cursor):
    return {
        'country': load_cv(cursor, 'CV_country'),
        'type_of_package': load_cv(cursor, 'CV_type_of_package'),
        'sd_declaration_choice': load_cv(cursor, 'CV_sd_declaration_choice'),
        'incoterm': load_cv(cursor, 'CV_incoterm'),
        'procedure_code': load_cv(cursor, 'CV_procedure_code'),
        'currency': load_cv(cursor, 'CV_currency'),
    }


def _truthy(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}


def _normalise_yes_no(value, default=''):
    text = str(value or '').strip().lower()
    if not text:
        return default
    if text in {'yes', 'y', 'true', '1', 'on'}:
        return 'yes'
    if text in {'no', 'n', 'false', '0', 'off'}:
        return 'no'
    return text


def normalise_package_type(value, default=''):
    text = str(value or '').strip()
    if not text:
        return default
    key = text.lower().replace('_', ' ').strip()
    if ' - ' in key:
        key = key.split(' - ', 1)[0].strip()
    key = re.sub(r'[^\w\s]+$', '', key).strip()
    key_prefix = key.split(None, 1)[0] if key else key
    key_prefix = re.sub(r'[^\w]+$', '', key_prefix)
    if key.startswith(('\u00a3', '$', '\u20ac')):
        return default or 'PK'
    aliases = {
        'palle': 'pallets',
        'pallet': 'pallets',
        'pallets': 'pallets',
        'plt': 'pallets',
        'plts': 'pallets',
        'box': 'PK',
        'boxes': 'PK',
        'bx': 'PK',
        'carton': 'PK',
        'cartons': 'PK',
        'package': 'PK',
        'packages': 'PK',
        'pack': 'PK',
        'pk': 'PK',
        'bag': 'BG',
        'bags': 'BG',
        'bg': 'BG',
        'sack': 'BG',
        'sacks': 'BG',
        'each': 'PK',
        'ea': 'PK',
        'unit': 'PK',
        'units': 'PK',
        'set': 'PK',
        'sets': 'PK',
        'piece': 'PK',
        'pieces': 'PK',
        'pcs': 'PK',
        'pair': 'PK',
        'pairs': 'PK',
        'pr': 'PK',
        'm': 'PK',
        'mtr': 'PK',
        'mtrs': 'PK',
        'metre': 'PK',
        'metres': 'PK',
        'meter': 'PK',
        'meters': 'PK',
        'tub': 'TB',
        'tubs': 'TB',
        'tb': 'TB',
        'tube': 'TU',
        'tubes': 'TU',
        'tu': 'TU',
    }
    return aliases.get(key, aliases.get(key_prefix, text))


def strict_masterdata_validation_enabled(tenant_code=None):
    """Return whether strict party/carrier pre-checks should block local validation."""
    try:
        return _truthy(
            config_store.get(
                'VALIDATION',
                'STRICT_MASTERDATA_VALIDATION',
                fallback='false',
                tenant_code=tenant_code,
            )
        )
    except Exception:
        return False


def _linked_ens_header(row, cursor=None):
    staging_ens_id = row.get('staging_ens_id')
    if not staging_ens_id:
        return {}

    sql = f"""
        SELECT movement_type, carrier_eori, carrier_name, carrier_street_number,
               carrier_city, carrier_postcode, carrier_country
        FROM {_schema()}.StagingEnsHeaders
        WHERE staging_id=?
    """

    try:
        if cursor is not None:
            cursor.execute(sql, [staging_ens_id])
            record = cursor.fetchone()
        else:
            with db_cursor() as own_cursor:
                own_cursor.execute(sql, [staging_ens_id])
                record = own_cursor.fetchone()
    except Exception:
        return {}

    if not record:
        return {}

    return {
        'movement_type': str(record[0] or '').strip(),
        'carrier_eori': str(record[1] or '').strip(),
        'carrier_name': str(record[2] or '').strip(),
        'carrier_street_number': str(record[3] or '').strip(),
        'carrier_city': str(record[4] or '').strip(),
        'carrier_postcode': str(record[5] or '').strip(),
        'carrier_country': str(record[6] or '').strip(),
    }


def _linked_ens_movement_type(row, cursor=None):
    movement_type = str(row.get('ens_movement_type') or row.get('movement_type') or '').strip()
    if movement_type:
        return movement_type

    header = _linked_ens_header(row, cursor)
    if header.get('movement_type'):
        return header['movement_type']
    return ''


def _normalise_party_name(value):
    text = (value or '').upper().replace('&', ' AND ')
    replacements = {
        'LIMITED': 'LTD',
        'COMPANY': 'CO',
        'INCORPORATED': 'INC',
    }
    for source, target in replacements.items():
        text = re.sub(rf'\b{source}\b', target, text)
    text = re.sub(r'\bTHE\b', '', text)
    return re.sub(r'[^A-Z0-9]+', '', text)


def _party_names_match(form_name, master_name):
    form = _normalise_party_name(form_name)
    master = _normalise_party_name(master_name)
    if not form or not master:
        return False
    return form in master or master in form


def _master_records_for_eori(cursor, eori):
    if cursor is None or not eori:
        return []

    records = []
    schema = _schema()
    try:
        cursor.execute(
            f"""
            SELECT partner_name, partner_type, account_ref
            FROM {schema}.Partners
            WHERE active = 1
              AND (
                    UPPER(COALESCE(eori, '')) = UPPER(?)
                 OR UPPER(COALESCE(eori_gb, '')) = UPPER(?)
              )
            """,
            [eori, eori],
        )
        for row in cursor.fetchall():
            records.append({
                'name': str(row[0] or '').strip(),
                'role': str(row[1] or '').strip(),
                'reference': str(row[2] or '').strip(),
            })
    except Exception:
        pass

    try:
        cursor.execute(
            f"""
            SELECT COALESCE(NULLIF(trading_name, ''), company_name), company_type, tss_number
            FROM {schema}.CompanyMaster
            WHERE UPPER(COALESCE(eori_xi, '')) = UPPER(?)
               OR UPPER(COALESCE(eori_gb, '')) = UPPER(?)
            """,
            [eori, eori],
        )
        for row in cursor.fetchall():
            records.append({
                'name': str(row[0] or '').strip(),
                'role': str(row[1] or 'CompanyMaster').strip(),
                'reference': str(row[2] or '').strip(),
            })
    except Exception:
        pass

    deduped = []
    seen = set()
    for record in records:
        key = (_normalise_party_name(record['name']), record.get('reference') or '', record.get('role') or '')
        if not record['name'] or key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def _format_master_names(records):
    names = []
    for record in records:
        name = record.get('name') or ''
        reference = record.get('reference') or ''
        label = f"{name} ({reference})" if reference else name
        if label and label not in names:
            names.append(label)
    if len(names) > 3:
        return ', '.join(names[:3]) + ', ...'
    return ', '.join(names)


def _validate_linked_ens_carrier(row, cursor, errors):
    movement_type = _linked_ens_movement_type(row, cursor)
    if movement_type not in _RORO_MOVEMENT_TYPES:
        return

    header = _linked_ens_header(row, cursor)
    carrier_eori = (row.get('carrier_eori') or header.get('carrier_eori') or '').strip().upper()
    if not carrier_eori:
        errors.append("REQUIRED: Linked ENS Header Carrier EORI is required before sending this consignment to TSS")
    elif not _EORI_RE.match(carrier_eori):
        errors.append(f"FORMAT: Linked ENS Header Carrier EORI '{carrier_eori}' is not a valid EORI")
    elif carrier_eori in _UNSAFE_SAMPLE_EORIS:
        errors.append(f"INVALID: Linked ENS Header Carrier EORI '{carrier_eori}' is a placeholder/test EORI; use a carrier EORI accepted by TSS")

    required_address = [
        ('carrier_name', 'Carrier Name'),
        ('carrier_street_number', 'Carrier Street & Number'),
        ('carrier_city', 'Carrier City'),
        ('carrier_postcode', 'Carrier Postcode'),
        ('carrier_country', 'Carrier Country'),
    ]
    missing = [label for field, label in required_address if not (row.get(field) or header.get(field) or '').strip()]
    if missing:
        errors.append("REQUIRED: Linked ENS Header carrier full address is required for this movement type (" + ', '.join(missing) + ")")


def _validate_party_masterdata(row, cursor, errors, has_full_address):
    if cursor is None:
        return

    party_specs = [
        ('consignor', 'Consignor', 'street_number'),
        ('consignee', 'Consignee', 'street_number'),
        ('importer', 'Importer', 'street_number'),
        ('exporter', 'Exporter', 'street_number'),
    ]
    if (row.get('buyer_same_as_importer') or '').strip() == 'no':
        party_specs.append(('buyer', 'Buyer', 'street_and_number'))
    if (row.get('seller_same_as_exporter') or '').strip() == 'no':
        party_specs.append(('seller', 'Seller', 'street_and_number'))

    for prefix, label, street_field in party_specs:
        eori = (row.get(f'{prefix}_eori') or '').strip().upper()
        if not eori or not _EORI_RE.match(eori):
            continue

        name = (row.get(f'{prefix}_name') or '').strip()
        records = _master_records_for_eori(cursor, eori)
        if records:
            if name and not any(_party_names_match(name, record['name']) for record in records):
                errors.append(
                    f"INVALID: {label} EORI {eori} belongs to {_format_master_names(records)}, "
                    f"but {label} Name is '{name}'. Use Master Data Quick-fill or correct the EORI/name pair."
                )
            continue

        if not has_full_address(prefix, street_field=street_field):
            errors.append(
                f"REQUIRED: Full {label.lower()} name/address is required because EORI {eori} "
                "is not in local master data and TSS may not auto-populate it"
            )


def validate_consignment(row, cv, cursor=None, strict_party_checks=False):
    errors = []

    def chk(field, label):
        if not (row.get(field) or '').strip():
            errors.append(f"REQUIRED: {label}")

    def has_full_address(prefix, street_field='street_number'):
        return all((row.get(field) or '').strip() for field in (
            f'{prefix}_name',
            f'{prefix}_{street_field}',
            f'{prefix}_city',
            f'{prefix}_postcode',
            f'{prefix}_country',
        ))

    def chk_eori_or_address(prefix, label, street_field='street_number'):
        if (row.get(f'{prefix}_eori') or '').strip():
            return
        if has_full_address(prefix, street_field=street_field):
            return
        errors.append(f"REQUIRED: {label} EORI or full {label.lower()} address")

    chk('goods_description', 'Goods Description')
    chk('transport_document_number', 'Transport Document Number')
    chk('importer_eori', 'Importer EORI')
    chk('controlled_goods', 'Controlled Goods (yes/no)')
    chk('container_indicator', 'Container Indicator (0=Uncontainerised, 1=Containerised)')
    chk_eori_or_address('consignor', 'Consignor')
    chk_eori_or_address('consignee', 'Consignee')
    chk_eori_or_address('exporter', 'Exporter')

    movement_type = _linked_ens_movement_type(row, cursor)
    if movement_type in _RORO_MOVEMENT_TYPES:
        if strict_party_checks:
            _validate_linked_ens_carrier(row, cursor, errors)

    for field, label in [
        ('importer_eori', 'Importer EORI'),
        ('exporter_eori', 'Exporter EORI'),
        ('consignor_eori', 'Consignor EORI'),
        ('consignee_eori', 'Consignee EORI'),
        ('buyer_eori', 'Buyer EORI'),
        ('seller_eori', 'Seller EORI'),
    ]:
        val = (row.get(field) or '').strip().upper()
        if strict_party_checks and val and not _EORI_RE.match(val):
            errors.append(f"FORMAT: {label} '{val}' is not a valid EORI (expected e.g. GB123456789000)")
        elif strict_party_checks and val in _UNSAFE_SAMPLE_EORIS:
            errors.append(f"INVALID: {label} '{val}' is a placeholder/test EORI; choose a registered EORI from master data")

    if not row.get('staging_ens_id'):
        errors.append("REQUIRED: Must link to an ENS Header (staging_ens_id)")

    controlled_goods = _normalise_yes_no(row.get('controlled_goods'))
    if controlled_goods and controlled_goods not in {'yes', 'no'}:
        errors.append("INVALID: Controlled Goods must be yes or no")

    if controlled_goods == 'yes':
        if not (row.get('goods_domestic_status') or '').strip():
            errors.append("REQUIRED: Goods Domestic Status required when controlled_goods=yes")

    buyer_same = _normalise_yes_no(row.get('buyer_same_as_importer'), default='yes')
    seller_same = _normalise_yes_no(row.get('seller_same_as_exporter'), default='yes')
    for value, label in [
        (buyer_same, 'Buyer Same as Importer'),
        (seller_same, 'Seller Same as Exporter'),
    ]:
        if value and value not in {'yes', 'no'}:
            errors.append(f"INVALID: {label} must be yes or no")
    if buyer_same == 'no' and not (
        (row.get('buyer_eori') or '').strip()
        or all((row.get(field) or '').strip() for field in (
            'buyer_name', 'buyer_street_and_number', 'buyer_city',
            'buyer_postcode', 'buyer_country',
        ))
    ):
        errors.append("REQUIRED: Buyer EORI or full buyer address required when Buyer Same as Importer=no")
    if seller_same == 'no' and not (
        (row.get('seller_eori') or '').strip()
        or all((row.get(field) or '').strip() for field in (
            'seller_name', 'seller_street_and_number', 'seller_city',
            'seller_postcode', 'seller_country',
        ))
    ):
        errors.append("REQUIRED: Seller EORI or full seller address required when Seller Same as Exporter=no")

    if strict_party_checks:
        _validate_party_masterdata(row, cursor, errors, has_full_address)

    use_importer_sde = _normalise_yes_no(row.get('use_importer_sde'))
    declaration_choice = (row.get('declaration_choice') or '').strip()
    generate_sd = _normalise_yes_no(row.get('generate_SD'))

    if use_importer_sde and use_importer_sde not in {'yes', 'no'}:
        errors.append("INVALID: Use Importer SDE must be yes or no")
    if use_importer_sde == 'yes':
        if not declaration_choice:
            errors.append("REQUIRED: Declaration Choice required when Use Importer SDE=yes")
        if declaration_choice in {'H1', 'H3', 'H4'} and not generate_sd:
            errors.append("REQUIRED: Generate SD required when Declaration Choice is H1/H3/H4 and Use Importer SDE=yes")
    if generate_sd and generate_sd not in {'yes', 'no'}:
        errors.append("INVALID: Generate SD must be yes or no")

    if declaration_choice in {'H2', 'H3', 'H4'} and not (row.get('supervising_customs_office') or '').strip():
        errors.append("REQUIRED: Supervising Customs Office required when Declaration Choice is H2/H3/H4")
    if declaration_choice == 'H2' and not (row.get('customs_warehouse_identifier') or '').strip():
        errors.append("REQUIRED: Customs Warehouse Identifier required when Declaration Choice is H2")

    desc = (row.get('goods_description') or '').strip()
    if desc and len(desc) > 254:
        errors.append(f"LENGTH: Goods Description exceeds 254 chars ({len(desc)})")

    tdoc = (row.get('transport_document_number') or '').strip()
    if tdoc and len(tdoc) > 35:
        errors.append(f"LENGTH: Transport Doc exceeds 35 chars ({len(tdoc)})")

    container_indicator = (row.get('container_indicator') or '').strip()
    if container_indicator and container_indicator not in {'0', '1'}:
        errors.append("INVALID: Container Indicator must be 0 or 1")

    return errors


def _linked_consignment_context(row):
    container_indicator = str(row.get('cons_container_indicator') or row.get('container_indicator') or '').strip()
    movement_type = str(row.get('ens_movement_type') or row.get('movement_type') or '').strip()
    staging_cons_id = row.get('staging_cons_id')

    if (container_indicator and movement_type) or not staging_cons_id:
        return container_indicator, movement_type

    try:
        with db_cursor() as cursor:
            cursor.execute(
                f"""
                SELECT c.container_indicator, e.movement_type
                FROM {_schema()}.StagingConsignments c
                LEFT JOIN {_schema()}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
                WHERE c.staging_id = ?
                """,
                [staging_cons_id],
            )
            record = cursor.fetchone()
            if record:
                container_indicator = container_indicator or str(record[0] or '').strip()
                movement_type = movement_type or str(record[1] or '').strip()
    except Exception:
        return container_indicator, movement_type

    return container_indicator, movement_type


def validate_goods(row, cv):
    row.update(normalise_goods_decimal_fields(row))
    errors = []

    def chk(field, label):
        if not (row.get(field) or '').strip():
            errors.append(f"REQUIRED: {label}")

    chk('goods_description', 'Goods Description')
    chk('type_of_packages', 'Type of Packages')
    chk('package_marks', 'Package Marks')
    chk('controlled_goods', 'Controlled Goods (yes/no)')
    chk('item_invoice_currency', 'Currency')

    if not row.get('staging_cons_id'):
        errors.append("REQUIRED: Must link to a Consignment (staging_cons_id)")

    controlled_goods = (row.get('controlled_goods') or '').strip()
    if controlled_goods and controlled_goods not in {'yes', 'no'}:
        errors.append("INVALID: Controlled Goods must be yes or no")

    try:
        gross = float(row.get('gross_mass_kg') or 0)
        if gross <= 0:
            errors.append("REQUIRED: Gross Mass KG must be > 0")
    except ValueError:
        errors.append("FORMAT: Gross Mass KG must be numeric")
        gross = 0
    else:
        if _has_more_than_2_dp(row.get('gross_mass_kg')):
            errors.append("FORMAT: Gross Mass KG must use no more than 2 decimal places")

    net = row.get('net_mass_kg')
    if net not in (None, ''):
        try:
            net_val = float(net)
            if net_val > gross:
                errors.append("INVALID: Net mass cannot exceed gross mass")
        except ValueError:
            errors.append("FORMAT: Net Mass KG must be numeric")
        else:
            if _has_more_than_2_dp(net):
                errors.append("FORMAT: Net Mass KG must use no more than 2 decimal places")

    pkgs = row.get('number_of_packages')
    if not pkgs or int(pkgs or 0) < 1:
        errors.append("REQUIRED: Number of Packages must be >= 1")

    tp = normalise_package_type(row.get('type_of_packages'))
    pkg_cv = cv.get('type_of_package', set())
    pkg_match = None
    if tp and pkg_cv:
        pkg_match = next((value for value in pkg_cv if str(value).lower() == tp.lower()), None)
    if tp and pkg_cv and not pkg_match:
        errors.append(f"INVALID: Type of Packages '{tp}' not in allowed values")

    cc = (row.get('commodity_code') or '').strip()
    if cc and len(cc) < 8:
        errors.append(f"FORMAT: Commodity Code must be at least 8 digits (got {len(cc)})")

    errors.extend(validate_taric_code(row.get('taric_code')))

    for field, label in _GOODS_TSS_TEXT_FIELDS:
        warning = tss_unsafe_value_message(label, row.get(field))
        if warning:
            errors.append(f"FORMAT: {warning}")

    invoice_amount = row.get('item_invoice_amount')
    if invoice_amount not in (None, ''):
        try:
            float(invoice_amount)
        except ValueError:
            errors.append("FORMAT: Invoice Amount must be numeric")
        else:
            if _has_more_than_2_dp(invoice_amount):
                errors.append("FORMAT: Invoice Amount must use no more than 2 decimal places")

    container_indicator, movement_type = _linked_consignment_context(row)

    if controlled_goods == 'yes':
        if row.get('net_mass_kg') in (None, ''):
            errors.append("REQUIRED: Net Mass KG required when Controlled Goods=yes")
        if not (row.get('controlled_goods_type') or '').strip():
            errors.append("REQUIRED: Controlled Type required when Controlled Goods=yes")
        if not cc:
            errors.append("REQUIRED: Commodity Code required when Controlled Goods=yes")
        if not (row.get('procedure_code') or '').strip():
            errors.append("REQUIRED: Procedure Code required when Controlled Goods=yes")
        if not (row.get('additional_procedure_code') or '').strip():
            errors.append("REQUIRED: Additional Procedure required when Controlled Goods=yes")
        if invoice_amount in (None, ''):
            errors.append("REQUIRED: Invoice Amount required when Controlled Goods=yes")

    if container_indicator == '1' and not (row.get('equipment_number') or '').strip():
        errors.append("REQUIRED: Equipment Number required when the parent consignment is Containerised")

    return errors


def validate_supdec(row, cv):
    errors = []

    dc = (row.get('declaration_choice') or '').strip()
    if dc and dc not in cv.get('sd_declaration_choice', set()):
        errors.append(f"INVALID: Declaration Choice '{dc}' not in allowed values")

    inc = (row.get('incoterm') or '').strip()
    if inc and inc not in cv.get('incoterm', set()):
        errors.append(f"INVALID: Incoterm '{inc}' not in allowed values")

    dlc = (row.get('delivery_location_country') or '').strip()
    if dlc and dlc not in cv.get('country', set()):
        errors.append(f"INVALID: Delivery Country '{dlc}' not in country list")

    return errors


def _row_as_dict(cursor):
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [d[0] for d in cursor.description]
    return dict(zip(columns, row))


def _normalised_update_parts(normalised_values):
    safe_fields = {'gross_mass_kg', 'net_mass_kg', 'item_invoice_amount'}
    updates = [(field, value) for field, value in (normalised_values or {}).items() if field in safe_fields]
    return [f"{field}=?" for field, _ in updates], [value for _, value in updates]


def _apply_validation_result(cursor, table_name, sid, errors, normalised_values=None):
    normalised_sets, normalised_params = _normalised_update_parts(normalised_values)
    if errors:
        status = 'FAILED'
        error_text = ' | '.join(errors)[:4000]
        sets = normalised_sets + ["status='FAILED'", "error_message=?", "updated_at=SYSUTCDATETIME()"]
        cursor.execute(
            f"UPDATE {_schema()}.{table_name} SET {', '.join(sets)} WHERE staging_id=?",
            normalised_params + [error_text, sid],
        )
        return {'ok': False, 'status': status, 'errors': errors, 'message': error_text}

    status = 'VALIDATED'
    sets = normalised_sets + ["status='VALIDATED'", "error_message=NULL", "updated_at=SYSUTCDATETIME()"]
    cursor.execute(
        f"UPDATE {_schema()}.{table_name} SET {', '.join(sets)} WHERE staging_id=?",
        normalised_params + [sid],
    )
    return {'ok': True, 'status': status, 'errors': [], 'message': ''}


def auto_validate_goods_record(staging_id):
    with db_cursor() as cursor:
        cv = build_validation_choice_sets(cursor)
        cursor.execute(f"SELECT * FROM {_schema()}.StagingGoodsItems WHERE staging_id=?", [staging_id])
        row = _row_as_dict(cursor)
        if row is None:
            return None
        normalised_values = normalise_goods_decimal_fields(row)
        row.update(normalised_values)
        return _apply_validation_result(
            cursor,
            'StagingGoodsItems',
            staging_id,
            validate_goods(row, cv),
            normalised_values=normalised_values,
        )


def auto_validate_consignment_record(staging_id):
    with db_cursor() as cursor:
        cv = build_validation_choice_sets(cursor)
        strict_party_checks = strict_masterdata_validation_enabled()
        cursor.execute(f"SELECT * FROM {_schema()}.StagingConsignments WHERE staging_id=?", [staging_id])
        row = _row_as_dict(cursor)
        if row is None:
            return None
        return _apply_validation_result(
            cursor,
            'StagingConsignments',
            staging_id,
            validate_consignment(row, cv, cursor, strict_party_checks=strict_party_checks),
        )
