#!/usr/bin/env python3
"""
NOT FOR PRD: reads/writes legacy BKD.Staging* tables removed by migration 078. Do not run against Fusion_TSS_Automation_PRD.

Discover supplementary declarations from TSS and stage them locally.

Adapted from the older Birkdale repo to the current Fusion Flow schema.
Safe behavior:
  - only works on consignments that have a local or synced SFD reference
  - only creates missing local SDI header/goods rows
  - never writes to main branch; script is local/runtime only
"""
import logging
import os
import sys
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from dateutil import parser as date_parser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_standalone_connection
from app.job_logger import JobRun
from app.tenant import get_tenant, tenant_aware_cursor
from app.tss_api import build_cfg_client

S = get_tenant()["schema"]

logger = logging.getLogger(__name__)
DEFAULT_ADDITIONAL_PROCEDURE_CODE = '000'
SDI_FILTER_STATUSES = ('draft', 'trader input required')
SDI_READ_FIELDS = (
    'status',
    'sfd_number',
    'trader_reference',
    'transport_document_number',
    'arrival_date_time',
    'submission_due_date',
    'error_message',
)
DEFAULT_SDI_FILTER_LIMIT = 250


def arrival_datetime_sql(column_expr):
    """Return SQL that parses tenant arrival dates without implicit conversion.

    Once migration 070 has been applied for every tenant this is a no-op: the
    column is already DATETIME2 and all TRY_CONVERT styles return it unchanged.
    Kept here so the script remains correct against tenants that have not yet
    been migrated.
    """
    text_expr = f"NULLIF(LTRIM(RTRIM(CAST({column_expr} AS NVARCHAR(50)))), '')"
    return (
        "COALESCE("
        f"TRY_CONVERT(datetime2, {text_expr}, 126),"
        f"TRY_CONVERT(datetime2, {text_expr}, 121),"
        f"TRY_CONVERT(datetime2, {text_expr}, 120),"
        f"TRY_CONVERT(datetime2, {text_expr}, 103),"
        f"TRY_CONVERT(datetime2, {column_expr})"
        ")"
    )


def coerce_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    raw = str(value).strip()
    if not raw:
        return None

    for fmt in (
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M',
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    try:
        return date_parser.parse(raw, dayfirst=True)
    except (TypeError, ValueError):
        return None


def table_columns(cursor, table_name):
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [S, table_name],
    )
    return {row[0].lower() for row in cursor.fetchall()}


def first_in_columns(columns, *candidates):
    for candidate in candidates:
        if candidate and candidate.lower() in columns:
            return candidate
    return None


def first_existing(cursor, table_name, *candidates):
    columns = table_columns(cursor, table_name)
    return first_in_columns(columns, *candidates)


def first_text(*values, default=''):
    for value in values:
        if value in (None, ''):
            continue
        cleaned = str(value).strip()
        if cleaned:
            return cleaned
    return default


def clean_ref(value):
    return str(value or '').strip().upper()


def unique_columns(*columns):
    seen = set()
    result = []
    for column in columns:
        if not column:
            continue
        key = column.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(column)
    return result


def sdi_reference_from_item(item):
    """Return the SUP reference from normalized or raw ServiceNow payloads."""
    item = item or {}
    return first_text(
        item.get('sup_dec_number'),
        item.get('reference'),
        item.get('supplementary_declaration_number'),
        item.get('number'),
    )


def sdi_status_from_item(item):
    """Return the TSS SDI status from normalized or raw ServiceNow payloads."""
    item = item or {}
    return first_text(item.get('status'), item.get('state'), default='DRAFT')


def sdi_sfd_reference_from_item(item, fallback=''):
    """Return the SFD parent reference from normalized or raw ServiceNow payloads."""
    item = item or {}
    return first_text(
        item.get('sfd_reference'),
        item.get('sfd_number'),
        item.get('parent'),
        item.get('u_parent'),
        fallback,
    )


def sdi_trader_reference_from_item(item):
    """Return trader reference from TSS API or ServiceNow export payloads."""
    item = item or {}
    return first_text(item.get('trader_reference'), item.get('u_trader_reference'))


def sdi_transport_document_from_item(item):
    """Return transport document ref from TSS API or ServiceNow export payloads."""
    item = item or {}
    return first_text(
        item.get('transport_document_number'),
        item.get('transport_document_reference'),
        item.get('u_transport_document_number'),
        item.get('u_transport_document_reference'),
    )


def goods_reference_from_item(item):
    item = item or {}
    return first_text(item.get('goods_id'), item.get('reference'), item.get('number'), item.get('sys_id'))


def sdi_matches_consignment(item, sfd_ref, trader_reference='', transport_document_number=''):
    """True when a filtered SDI row belongs to the local consignment candidate."""
    target_sfd = clean_ref(sfd_ref)
    target_trader = clean_ref(trader_reference)
    target_transport_doc = clean_ref(transport_document_number)

    item_sfd = clean_ref(sdi_sfd_reference_from_item(item))
    if item_sfd:
        return bool(target_sfd and item_sfd == target_sfd)

    item_trader = clean_ref(sdi_trader_reference_from_item(item))
    if target_trader and item_trader == target_trader:
        return True

    item_transport_doc = clean_ref(sdi_transport_document_from_item(item))
    return bool(target_transport_doc and item_transport_doc == target_transport_doc)


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_filtered_sdi_details(client, act_as, cache, limit=DEFAULT_SDI_FILTER_LIMIT):
    """Read Draft/TIR SDIs through the documented status filter endpoint.

    This is a fallback for the real TSS behaviour observed in the portal: some
    SDIs appear in the SD list export while a direct sfd_number lookup can still
    return an empty result. The fallback still only uses official TSS endpoints.
    """
    cache_key = act_as or ''
    if cache_key in cache:
        return cache[cache_key]

    details = []
    seen_refs = set()
    for status in SDI_FILTER_STATUSES:
        try:
            items = client.filter_sdi_items(f'status={status}', act_as=act_as)
        except Exception as exc:
            logger.warning('SDI status filter failed for %s: %s', status, exc)
            continue

        for item in items[:limit]:
            sup_ref = sdi_reference_from_item(item)
            if not sup_ref or sup_ref in seen_refs:
                continue
            seen_refs.add(sup_ref)

            try:
                result = client.read_sdi(sup_ref, fields=SDI_READ_FIELDS, act_as=act_as)
                detail = result.get('response') if isinstance(result, dict) else {}
            except Exception as exc:
                logger.warning('SDI read failed for %s: %s', sup_ref, exc)
                detail = {}

            if not isinstance(detail, dict):
                detail = {}
            detail = {**item, **detail}
            detail.setdefault('sup_dec_number', sup_ref)
            detail.setdefault('reference', sup_ref)
            details.append(detail)

    cache[cache_key] = details
    return details


def fallback_sdi_items_for_consignment(
    client,
    sfd_ref,
    trader_reference='',
    transport_document_number='',
    act_as=None,
    cache=None,
):
    """Find TSS-created SDIs by status filter when direct SFD lookup is empty."""
    cache = cache if cache is not None else {}
    limit = _safe_int(os.environ.get('DISCOVER_SDI_FILTER_LIMIT'), DEFAULT_SDI_FILTER_LIMIT)
    details = _read_filtered_sdi_details(client, act_as, cache, limit=limit)
    return [
        item for item in details
        if sdi_matches_consignment(item, sfd_ref, trader_reference, transport_document_number)
    ]


def load_linked_sfd_refs(cursor, consignment_rows):
    """Map staging consignment ids to SFD refs from the synced Sfds table.

    TSS stores generated SDIs under the SFD DEC, not the original ENS
    consignment DEC. Some tenant rows have that SFD only in the synced Sfds
    table, so discovery must resolve it before calling
    /supplementary_declarations?sfd_number=...
    """
    if not consignment_rows:
        return {}

    sfd_columns = table_columns(cursor, 'Sfds')
    if not sfd_columns:
        return {}

    reference_columns = unique_columns(
        first_in_columns(sfd_columns, 'sfd_reference'),
        first_in_columns(sfd_columns, 'sfd_number'),
        first_in_columns(sfd_columns, 'reference'),
    )
    match_columns = unique_columns(
        first_in_columns(sfd_columns, 'declaration_number'),
        first_in_columns(sfd_columns, 'consignment_number'),
        first_in_columns(sfd_columns, 'ens_consignment_reference'),
        *reference_columns,
    )
    if not reference_columns or not match_columns:
        return {}

    ref_to_consignment_ids = {}
    for staging_cons_id, dec_reference, sfd_reference, _ens_header_ref, _arrival_dt, *_rest in consignment_rows:
        for ref in {clean_ref(dec_reference), clean_ref(sfd_reference)} - {''}:
            ref_to_consignment_ids.setdefault(ref, []).append(staging_cons_id)

    refs = sorted(ref_to_consignment_ids)
    if not refs:
        return {}

    optional_columns = [
        column for column in ('id', 'updated_at', 'created_at', 'synced_at')
        if column in sfd_columns
    ]
    select_columns = unique_columns(*reference_columns, *match_columns, *optional_columns)
    select_sql = ', '.join(f'[{column}] AS [{column}]' for column in select_columns)
    placeholders = ', '.join('?' for _ in refs)
    where_parts = [
        f"UPPER(LTRIM(RTRIM(CAST([{column}] AS NVARCHAR(80))))) IN ({placeholders})"
        for column in match_columns
    ]
    params = []
    for _column in match_columns:
        params.extend(refs)

    order_col = first_in_columns(sfd_columns, 'updated_at', 'synced_at', 'created_at', 'id')
    order_sql = f'ORDER BY [{order_col}] DESC' if order_col else ''
    cursor.execute(
        f"""
        SELECT TOP 1000 {select_sql}
        FROM {S}.Sfds
        WHERE {' OR '.join(where_parts)}
        {order_sql}
        """,
        params,
    )
    column_names = [desc[0] for desc in cursor.description]

    resolved = {}
    for row_values in cursor.fetchall():
        row = dict(zip(column_names, row_values))
        sfd_ref = first_text(*(row.get(column) for column in reference_columns))
        if not sfd_ref:
            continue
        row_refs = {
            clean_ref(row.get(column))
            for column in match_columns
        } - {''}
        for row_ref in row_refs:
            for staging_cons_id in ref_to_consignment_ids.get(row_ref, []):
                resolved.setdefault(staging_cons_id, sfd_ref)

    return resolved


def next_sdi_deadline(arrival_value):
    arrival_value = coerce_datetime(arrival_value)
    if not arrival_value:
        return None
    if arrival_value.tzinfo is None:
        arrival_value = arrival_value.replace(tzinfo=timezone.utc)

    year = arrival_value.year
    month = arrival_value.month + 1
    if month == 13:
        month = 1
        year += 1
    return datetime(year, month, 10, 23, 59, 59, tzinfo=timezone.utc)


def run(triggered_by='manual'):
    load_dotenv()
    client = build_cfg_client()
    lines = []

    with JobRun('discover_sdi', triggered_by=triggered_by) as jr:
        conn = get_standalone_connection()
        cursor = tenant_aware_cursor(conn.cursor())
        default_act_as = (getattr(client, 'default_act_as', '') or '').strip() or None

        header_id_col = first_existing(cursor, 'StagingSupDecHeaders', 'staging_id', 'id')
        goods_parent_col = first_existing(cursor, 'StagingSupDecGoods', 'staging_supdec_id', 'supdec_header_id')
        goods_item_col = first_existing(cursor, 'StagingSupDecGoods', 'item_number', 'goods_item_number')
        goods_remote_col = first_existing(cursor, 'StagingSupDecGoods', 'sup_goods_id', 'tss_goods_id_sdi', 'goods_id')
        header_columns = table_columns(cursor, 'StagingSupDecHeaders')
        goods_columns = table_columns(cursor, 'StagingGoodsItems')
        supdec_goods_columns = table_columns(cursor, 'StagingSupDecGoods')
        has_act_as = 'act_as' in header_columns
        if not header_id_col or not goods_parent_col or not goods_item_col or not goods_remote_col:
            raise RuntimeError('SDI schema columns are missing')

        parsed_arrival = arrival_datetime_sql('e.arrival_date_time')
        consignment_columns = table_columns(cursor, 'StagingConsignments')
        trader_reference_sql = (
            'c.trader_reference'
            if 'trader_reference' in consignment_columns
            else 'CAST(NULL AS NVARCHAR(100))'
        )
        transport_document_sql = (
            'c.transport_document_number'
            if 'transport_document_number' in consignment_columns
            else 'CAST(NULL AS NVARCHAR(100))'
        )
        cursor.execute(
            f"""
            SELECT c.staging_id, c.dec_reference, c.sfd_reference,
                   e.ens_reference, arrival_parsed.arrival_dt,
                   {trader_reference_sql} AS trader_reference,
                   {transport_document_sql} AS transport_document_number
            FROM {S}.StagingConsignments c
            JOIN {S}.StagingEnsHeaders e ON e.staging_id = c.staging_ens_id
            CROSS APPLY (VALUES ({parsed_arrival})) arrival_parsed(arrival_dt)
            WHERE (
                  NULLIF(LTRIM(RTRIM(c.sfd_reference)), '') IS NOT NULL
                  OR NULLIF(LTRIM(RTRIM(c.dec_reference)), '') IS NOT NULL
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM {S}.StagingSupDecHeaders sd
                  WHERE sd.staging_cons_id = c.staging_id
                    AND sd.sup_dec_number IS NOT NULL
                    AND sd.sup_dec_number <> ''
              )
            ORDER BY c.staging_id
            """
        )
        raw_consignment_rows = cursor.fetchall()
        synced_sfd_refs = load_linked_sfd_refs(cursor, raw_consignment_rows)
        consignment_rows = []
        for (
            staging_cons_id,
            ens_cons_ref,
            local_sfd_ref,
            ens_header_ref,
            arrival_dt,
            trader_reference,
            transport_document_number,
        ) in raw_consignment_rows:
            resolved_sfd_ref = first_text(local_sfd_ref, synced_sfd_refs.get(staging_cons_id))
            if not resolved_sfd_ref:
                continue
            consignment_rows.append((
                staging_cons_id,
                ens_cons_ref,
                resolved_sfd_ref,
                ens_header_ref,
                arrival_dt,
                trader_reference,
                transport_document_number,
            ))
        lines.append(f'Candidate consignments before SFD resolution: {len(raw_consignment_rows)}')
        logger.info(lines[-1])
        lines.append(f'SFD refs resolved from synced Sfds: {len(synced_sfd_refs)}')
        logger.info(lines[-1])
        lines.append(f'Eligible consignments: {len(consignment_rows)}')
        logger.info(lines[-1])

        discovered = 0
        goods_created = 0
        sdi_filter_cache = {}

        for (
            staging_cons_id,
            ens_cons_ref,
            sfd_ref,
            ens_header_ref,
            arrival_dt,
            trader_reference,
            transport_document_number,
        ) in consignment_rows:
            items = client.lookup_sdi_items(sfd_ref, act_as=default_act_as)
            if not items:
                items = fallback_sdi_items_for_consignment(
                    client,
                    sfd_ref,
                    trader_reference=trader_reference,
                    transport_document_number=transport_document_number,
                    act_as=default_act_as,
                    cache=sdi_filter_cache,
                )
                if items:
                    msg = f'{sfd_ref}: direct lookup empty, matched {len(items)} SDI(s) via TSS status filter'
                    lines.append(msg)
                    logger.info(msg)
            if not items:
                msg = (
                    f'{sfd_ref}: no TSS draft SDI yet '
                    f'(direct sfd_number lookup and status-filter fallback returned 0)'
                )
                lines.append(msg)
                logger.info(msg)
                continue

            for item in items:
                sup_ref = sdi_reference_from_item(item)
                if not sup_ref:
                    continue

                deadline = next_sdi_deadline(arrival_dt)
                status = sdi_status_from_item(item)
                item_sfd_ref = sdi_sfd_reference_from_item(item, fallback=sfd_ref)

                cursor.execute(
                    f"""
                    SELECT {header_id_col}, sup_dec_number
                    FROM {S}.StagingSupDecHeaders
                    WHERE sup_dec_number = ?
                       OR (staging_cons_id = ? AND status IN ('PENDING', 'IMPORTED'))
                    """,
                    [sup_ref, staging_cons_id],
                )
                existing_header = cursor.fetchone()
                if existing_header:
                    sd_id = existing_header[0]
                    if not existing_header[1]:
                        cursor.execute(
                            f"""
                            UPDATE {S}.StagingSupDecHeaders
                            SET sup_dec_number = ?,
                                sfd_reference = COALESCE(NULLIF(sfd_reference, ''), ?),
                                ens_consignment_ref = COALESCE(NULLIF(ens_consignment_ref, ''), ?),
                                ens_header_ref = COALESCE(NULLIF(ens_header_ref, ''), ?),
                                {"act_as = COALESCE(NULLIF(act_as, ''), ?)," if has_act_as else ""}
                                tss_status = ?,
                                updated_at = SYSUTCDATETIME()
                            WHERE {header_id_col} = ?
                            """,
                            [
                                sup_ref,
                                item_sfd_ref,
                                ens_cons_ref,
                                ens_header_ref,
                                *((default_act_as,) if has_act_as else ()),
                                status,
                                sd_id,
                            ],
                        )
                        conn.commit()
                else:
                    columns = [
                        'label',
                        'staging_cons_id',
                        'sfd_reference',
                        'ens_consignment_ref',
                        'ens_header_ref',
                        'sup_dec_number',
                        'status',
                        'tss_status',
                        'submission_due_date',
                        'source',
                        'created_at',
                        'updated_at',
                    ]
                    placeholders = [
                        '?',
                        '?',
                        '?',
                        '?',
                        '?',
                        '?',
                        '?',
                        '?',
                        '?',
                        '?',
                        'SYSUTCDATETIME()',
                        'SYSUTCDATETIME()',
                    ]
                    values = [
                        f'Auto-discovered {sup_ref}',
                        staging_cons_id,
                        item_sfd_ref,
                        ens_cons_ref,
                        ens_header_ref,
                        sup_ref,
                        'IMPORTED',
                        status,
                        deadline,
                        'Birkdale_Discovery',
                    ]
                    if has_act_as:
                        columns.insert(8, 'act_as')
                        placeholders.insert(8, '?')
                        values.insert(8, default_act_as)

                    cursor.execute(
                        f"""
                        INSERT INTO {S}.StagingSupDecHeaders (
                            {', '.join(columns)}
                        )
                        VALUES ({', '.join(placeholders)})
                        """,
                        values,
                    )
                    conn.commit()
                    cursor.execute(
                        f"SELECT {header_id_col} FROM {S}.StagingSupDecHeaders WHERE sup_dec_number = ?",
                        [sup_ref],
                    )
                    sd_id = cursor.fetchone()[0]
                    discovered += 1

                sdi_goods = client.lookup_sdi_goods(sup_ref, act_as=default_act_as)
                source_additional_procedure_sql = (
                    'additional_procedure_code'
                    if 'additional_procedure_code' in goods_columns
                    else 'CAST(NULL AS NVARCHAR(4)) AS additional_procedure_code'
                )
                cursor.execute(
                    f"""
                    SELECT staging_id, goods_description, commodity_code, procedure_code,
                           number_of_packages, type_of_packages, package_marks,
                           gross_mass_kg, net_mass_kg, country_of_origin,
                           {source_additional_procedure_sql}
                    FROM {S}.StagingGoodsItems
                    WHERE staging_cons_id = ?
                    ORDER BY staging_id
                    """,
                    [staging_cons_id],
                )
                source_goods = cursor.fetchall()

                for index, goods_item in enumerate(sdi_goods, start=1):
                    remote_goods_id = goods_reference_from_item(goods_item)
                    if not remote_goods_id:
                        continue

                    cursor.execute(
                        f"""
                        SELECT COUNT(*)
                        FROM {S}.StagingSupDecGoods
                        WHERE {goods_parent_col} = ? AND {goods_remote_col} = ?
                        """,
                        [sd_id, remote_goods_id],
                    )
                    if cursor.fetchone()[0]:
                        continue

                    source_row = source_goods[index - 1] if index - 1 < len(source_goods) else None
                    goods_description = goods_item.get('goods_description')
                    commodity_code = goods_item.get('commodity_code')
                    procedure_code = goods_item.get('procedure_code')
                    number_of_packages = goods_item.get('number_of_packages')
                    type_of_packages = goods_item.get('type_of_packages')
                    package_marks = goods_item.get('package_marks')
                    gross_mass_kg = goods_item.get('gross_mass_kg')
                    net_mass_kg = goods_item.get('net_mass_kg')
                    country_of_origin = goods_item.get('country_of_origin')
                    source_additional_procedure_code = None

                    if source_row:
                        (
                            _src_id,
                            src_desc,
                            src_code,
                            src_proc,
                            src_pkgs,
                            src_type,
                            src_marks,
                            src_gross,
                            src_net,
                            src_origin,
                            src_additional_procedure_code,
                        ) = source_row
                        goods_description = goods_description or src_desc
                        commodity_code = commodity_code or src_code
                        procedure_code = procedure_code or src_proc
                        number_of_packages = number_of_packages or src_pkgs
                        type_of_packages = type_of_packages or src_type
                        package_marks = package_marks or src_marks
                        gross_mass_kg = gross_mass_kg or src_gross
                        net_mass_kg = net_mass_kg or src_net
                        country_of_origin = country_of_origin or src_origin

                    additional_procedure_code = first_text(
                        goods_item.get('additional_procedure_code'),
                        goods_item.get('additional_procedure_codes'),
                        source_additional_procedure_code,
                        default=DEFAULT_ADDITIONAL_PROCEDURE_CODE,
                    )
                    insert_columns = [
                        goods_parent_col,
                        goods_item_col,
                        goods_remote_col,
                        'label',
                        'status',
                        'goods_description',
                        'commodity_code',
                        'procedure_code',
                    ]
                    insert_values = [
                        sd_id,
                        index,
                        remote_goods_id,
                        f'SDI item {index}',
                        'PENDING',
                        goods_description,
                        commodity_code,
                        procedure_code,
                    ]
                    if 'additional_procedure_code' in supdec_goods_columns:
                        insert_columns.append('additional_procedure_code')
                        insert_values.append(additional_procedure_code)
                    insert_columns.extend([
                        'number_of_packages',
                        'type_of_packages',
                        'package_marks',
                        'gross_mass_kg',
                        'net_mass_kg',
                        'country_of_origin',
                        'source',
                    ])
                    insert_values.extend([
                        number_of_packages,
                        type_of_packages,
                        package_marks,
                        gross_mass_kg,
                        net_mass_kg,
                        country_of_origin,
                        'Birkdale_Discovery',
                    ])
                    placeholders = ', '.join('?' for _ in insert_values)
                    cursor.execute(
                        f"""
                        INSERT INTO {S}.StagingSupDecGoods (
                            {', '.join(insert_columns)}, created_at, updated_at
                        )
                        VALUES ({placeholders}, SYSUTCDATETIME(), SYSUTCDATETIME())
                        """,
                        insert_values,
                    )
                    goods_created += 1

                conn.commit()
                msg = f'{sfd_ref} -> {sup_ref} (goods staged: {len(sdi_goods)})'
                lines.append(msg)
                logger.info(msg)

        summary = f'Done. SDIs discovered={discovered}, goods rows created={goods_created}'
        lines.append(summary)
        logger.info(summary)

        jr.rows_processed = discovered
        jr.log_lines = lines
        conn.close()
        return lines


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [discover_sdi] %(levelname)s %(message)s',
    )
    for line in run():
        print(line)
