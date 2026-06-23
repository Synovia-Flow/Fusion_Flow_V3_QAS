"""
PRD event-driven ENS status watcher.

After email automation submits an ENS header or stages consignments, this module
watches only that ENS — polling TSS API for header + consignment statuses until
all reach AUTHORISED_FOR_MOVEMENT, then fires the final movement notification.

No cron. No BKD-Staging tables. No legacy sync scripts.

Data contracts (PRD model only):
  Reads:  STG.BKD_ENS_Headers, STG.BKD_ENS_Consignments
  Writes: TSS.BKD_ENS_Headers, TSS.BKD_ENS_Consignments, TSS.BKD_SFD,
          STG.BKD_SFD_Tracking, TSS.BKD_GoodsItems, STG.BKD_GoodsItems,
          TSS.BKD_API_Exchanges
  Notifies via: check_and_notify_ens_authorised (stamps STG.BKD_ENS_Headers.movement_notified_at)

Threading model:
  start_ens_status_watcher spawns one daemon thread per ENS submission.
  Each poll iteration creates a fresh app context so connections are properly cleaned up.
  The thread stops when movement_notified_at is stamped, or after _MAX_ATTEMPTS polls.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

_POLL_INTERVAL_S: int = 30
_MAX_ATTEMPTS: int = 40  # ~20 min at 30 s/poll

SFD_READ_FIELDS = [
    'status',
    'tssStatus',
    'sfdStatus',
    'sfd_number',
    'sfdNumber',
    'reference',
    'consignment_number',
    'consignmentNumber',
    'sup_dec_number',
    'supDecNumber',
    'local_reference_number',
    'localReferenceNumber',
    'trader_reference',
    'traderReference',
    'client_job_number',
    'clientJobNumber',
    'consignor_eori',
    'consignorEori',
    'consignee_eori',
    'consigneeEori',
    'importer_eori',
    'importerEori',
    'goods_description',
    'goodsDescription',
    'controlled_goods',
    'controlledGoods',
    'arrival_date_time',
    'arrivalDateTime',
    'transport_document_number',
    'transportDocumentNumber',
    'ducr',
    'movement_reference_number',
    'movementReferenceNumber',
    'mrn',
    'eori_for_eidr',
    'eoriForEidr',
    'eidr',
    'ens_consignment_reference',
    'ensConsignmentReference',
    'error_message',
    'errorMessage',
    'control_status',
    'controlStatus',
]

CONSIGNMENT_READ_FIELDS = [
    'status',
    'tssStatus',
    'consignment_number',
    'consignmentNumber',
    'reference',
    'declaration_number',
    'declarationNumber',
    'movement_reference_number',
    'movementReferenceNumber',
    'mrn',
    'eori_for_eidr',
    'eoriForEidr',
    'importer_eori',
    'importerEori',
    'goods_description',
    'goodsDescription',
    'transport_document_number',
    'transportDocumentNumber',
    'trader_reference',
    'traderReference',
    'ducr',
    'control_status',
    'controlStatus',
]

GOODS_READ_FIELDS = [
    'status',
    'tssStatus',
    'goods_id',
    'goodsId',
    'reference',
    'item_number',
    'itemNumber',
    'goods_item_number',
    'goodsItemNumber',
    'goods_description',
    'goodsDescription',
    'commodity_code',
    'commodityCode',
    'gross_mass_kg',
    'grossMassKg',
    'gross_weight_kg',
    'grossWeightKg',
    'net_mass_kg',
    'netMassKg',
    'net_weight_kg',
    'netWeightKg',
    'number_of_packages',
    'numberOfPackages',
    'number_of_individual_pieces',
    'numberOfIndividualPieces',
    'type_of_packages',
    'typeOfPackages',
    'type_of_package',
    'typeOfPackage',
    'package_marks',
    'packageMarks',
    'controlled_goods',
    'controlledGoods',
    'country_of_origin',
    'countryOfOrigin',
    'procedure_code',
    'procedureCode',
    'additional_procedure_code',
    'additionalProcedureCode',
    'item_invoice_amount',
    'itemInvoiceAmount',
    'item_invoice_currency',
    'itemInvoiceCurrency',
    'valuation_method',
    'valuationMethod',
    'preference',
    'error_message',
    'errorMessage',
]


def _normalise_status(raw: str | None) -> str:
    if not raw:
        return ''
    return str(raw).strip().upper().replace(' ', '_').replace('-', '_')


def _business_status(raw: str | None) -> str:
    status = _normalise_status(raw)
    if status in {'OK', 'SUCCESS'}:
        return ''
    return status


def _extract_tss_status(result: dict) -> str:
    """Extract normalised TSS status string from a TSS API result dict."""
    status = (
        result.get('status')
        or (result.get('response') or {}).get('status')
        or ''
    )
    return _business_status(status) or _normalise_status(status)


def _first_text(*values: Any) -> str:
    for value in values:
        if value not in (None, ''):
            cleaned = str(value).strip()
            if cleaned:
                return cleaned
    return ''


def _response_items(api, result: dict) -> list:
    if not isinstance(result, dict):
        return []
    try:
        items = api.as_items((result or {}).get('response'))
    except Exception:
        return []
    if isinstance(items, list):
        return items
    if isinstance(items, tuple):
        return list(items)
    if isinstance(items, dict):
        return [items]
    return []


def _compact_key(value: str) -> str:
    return ''.join(ch for ch in str(value or '').lower() if ch.isalnum())


def _item_text(item: Any, *keys: str) -> str:
    if not isinstance(item, dict):
        return ''
    compact = {_compact_key(k): v for k, v in item.items()}
    for key in keys:
        value = item.get(key)
        if value not in (None, ''):
            return str(value).strip()
        value = compact.get(_compact_key(key))
        if value not in (None, ''):
            return str(value).strip()
    return ''


def _item_ref(item: Any, *keys: str) -> str:
    if isinstance(item, str):
        return item.strip().upper()
    return _item_text(item, *keys).upper()


def _sfd_status_from_item(item: Any) -> str:
    return _business_status(_item_text(item, 'status', 'tss_status', 'tssStatus', 'sfd_status', 'sfdStatus'))


def _sfd_status_from_result(result: dict) -> str:
    response = (result or {}).get('response') or {}
    if isinstance(response, dict):
        return _business_status(_first_text(response.get('status'), response.get('tss_status'), response.get('sfd_status')))
    return ''


def _goods_status_from_item(item: Any) -> str:
    return _business_status(_item_text(item, 'status', 'tss_status', 'tssStatus')) or 'CREATED'


def _goods_status_from_result(result: dict) -> str:
    response = (result or {}).get('response') or {}
    if isinstance(response, dict):
        return _business_status(_first_text(response.get('status'), response.get('tss_status'), response.get('tssStatus'))) or 'CREATED'
    return 'CREATED'


def _goods_item_number(item: Any, fallback: int | None = None) -> str:
    return _item_text(
        item,
        'item_number',
        'itemNumber',
        'goods_item_number',
        'goodsItemNumber',
        'item_seq',
        'itemSequence',
    ) or (str(fallback) if fallback is not None else '')


def _consignment_cache_fields(item: Any, dec_ref: str) -> dict[str, str]:
    return {
        'declaration_number': _item_text(
            item,
            'declaration_number',
            'declarationNumber',
            'consignment_number',
            'consignmentNumber',
            'reference',
        ) or dec_ref,
        'goods_description': _item_text(
            item,
            'goods_description',
            'goodsDescription',
            'description_of_goods',
            'descriptionOfGoods',
        ),
        'importer_eori': _item_text(
            item,
            'importer_eori',
            'importerEori',
            'importer_id',
            'importerId',
        ),
    }


def _tss_raw_json(value: Any) -> str:
    """Serialise the full TSS response for NVARCHAR(MAX) RawJson mirrors."""
    return json.dumps(value or {}, default=str)


def _should_lookup_sfd(tss_status: str) -> bool:
    return _normalise_status(tss_status) in {
        'AUTHORISED_FOR_MOVEMENT',
        'ARRIVED',
        'CLEARED',
        'COMPLETED',
    }


def _upsert_tss_ens_header_status(
    cur,
    *,
    client_code: str,
    ens_ref: str,
    tss_status: str,
    raw_json: str | None,
) -> None:
    cur.execute(
        """
        MERGE TSS.BKD_ENS_Headers AS target
        USING (SELECT ? AS ClientCode, ? AS DeclarationNumber) AS src
        ON target.ClientCode = src.ClientCode
           AND target.DeclarationNumber = src.DeclarationNumber
        WHEN MATCHED THEN
            UPDATE SET
                TssStatus    = ?,
                RawJson      = COALESCE(?, target.RawJson),
                LastSyncedAt = SYSUTCDATETIME(),
                UpdatedAt    = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (ClientCode, DeclarationNumber, TssStatus, RawJson, LastSyncedAt, UpdatedAt)
            VALUES (?, ?, ?, ?, SYSUTCDATETIME(), SYSUTCDATETIME());
        """,
        [
            client_code, ens_ref,
            tss_status, raw_json,
            client_code, ens_ref, tss_status, raw_json,
        ],
    )


def _upsert_tss_cons_status(
    cur,
    *,
    client_code: str,
    ens_ref: str,
    dec_ref: str,
    tss_status: str,
    consignment_record: dict | None = None,
    raw_json: str | None,
) -> None:
    cache = _consignment_cache_fields(consignment_record or {}, dec_ref)
    cur.execute(
        """
        MERGE TSS.BKD_ENS_Consignments AS target
        USING (SELECT ? AS ClientCode, ? AS ConsignmentReference) AS src
        ON target.ClientCode = src.ClientCode
           AND target.ConsignmentReference = src.ConsignmentReference
        WHEN MATCHED THEN
            UPDATE SET
                DeclarationNumber = COALESCE(NULLIF(?, ''), target.DeclarationNumber),
                TssStatus         = ?,
                EnsReference      = COALESCE(?, target.EnsReference),
                GoodsDescription  = COALESCE(NULLIF(?, ''), target.GoodsDescription),
                ImporterEori      = COALESCE(NULLIF(?, ''), target.ImporterEori),
                RawJson           = COALESCE(?, target.RawJson),
                LastSyncedAt      = SYSUTCDATETIME(),
                UpdatedAt         = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (ClientCode, DeclarationNumber, ConsignmentReference, EnsReference,
                    TssStatus, GoodsDescription, ImporterEori, RawJson, LastSyncedAt, UpdatedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME(), SYSUTCDATETIME());
        """,
        [
            client_code, dec_ref,
            cache['declaration_number'],
            tss_status,
            ens_ref,
            cache['goods_description'],
            cache['importer_eori'],
            raw_json,
            client_code,
            cache['declaration_number'],
            dec_ref,
            ens_ref,
            tss_status,
            cache['goods_description'],
            cache['importer_eori'],
            raw_json,
        ],
    )
    _sync_stg_consignment_submitted_for_arrived(
        cur,
        client_code=client_code,
        dec_ref=dec_ref,
        tss_status=tss_status,
    )


def _sync_stg_consignment_submitted_for_arrived(
    cur,
    *,
    client_code: str,
    dec_ref: str,
    tss_status: str,
) -> None:
    """Keep the local cargo status aligned once TSS has accepted arrival."""

    if _normalise_status(tss_status) != 'ARRIVED':
        return
    cur.execute(
        """
        UPDATE STG.BKD_ENS_Consignments
           SET sub_status = 'SUBMITTED',
               submitted_at = COALESCE(submitted_at, SYSUTCDATETIME()),
               last_sub_status_change = CASE
                   WHEN UPPER(COALESCE(sub_status, '')) <> 'SUBMITTED'
                   THEN SYSUTCDATETIME()
                   ELSE last_sub_status_change
               END,
               updated_at = SYSUTCDATETIME()
         WHERE ClientCode = ?
           AND tss_consignment_ref = ?
           AND UPPER(COALESCE(sub_status, '')) NOT IN ('SUBMITTED', 'COMPLETED', 'CANCELLED', 'DELETED')
        """,
        [client_code, dec_ref],
    )


def _upsert_tss_sfd(
    cur,
    *,
    client_code: str,
    ens_ref: str,
    dec_ref: str,
    sfd_ref: str,
    movement_reference_number: str,
    tss_status: str,
    raw_json: str | None,
) -> None:
    cur.execute(
        """
        MERGE TSS.BKD_SFD AS target
        USING (SELECT ? AS ClientCode, ? AS SfdReference) AS src
        ON target.ClientCode = src.ClientCode
           AND target.SfdReference = src.SfdReference
        WHEN MATCHED THEN
            UPDATE SET
                DeclarationNumber       = COALESCE(NULLIF(?, ''), target.DeclarationNumber),
                EnsReference            = COALESCE(NULLIF(?, ''), target.EnsReference),
                MovementReferenceNumber = COALESCE(NULLIF(?, ''), target.MovementReferenceNumber),
                TssStatus               = COALESCE(NULLIF(?, ''), target.TssStatus),
                RawJson                 = COALESCE(?, target.RawJson),
                LastSyncedAt            = SYSUTCDATETIME(),
                UpdatedAt               = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (ClientCode, SfdReference, DeclarationNumber, EnsReference,
                    MovementReferenceNumber, TssStatus, RawJson, LastSyncedAt, UpdatedAt)
            VALUES (?, ?, NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''),
                    ?, SYSUTCDATETIME(), SYSUTCDATETIME());
        """,
        [
            client_code, sfd_ref,
            dec_ref, ens_ref, movement_reference_number, tss_status, raw_json,
            client_code, sfd_ref, dec_ref, ens_ref, movement_reference_number, tss_status, raw_json,
        ],
    )


def _upsert_tss_goods(
    cur,
    *,
    client_code: str,
    dec_ref: str,
    goods_ref: str,
    item_number: str,
    tss_status: str,
    raw_json: str | None,
) -> None:
    cur.execute(
        """
        MERGE TSS.BKD_GoodsItems AS target
        USING (SELECT ? AS ClientCode, ? AS GoodsStage, ? AS GoodsId) AS src
        ON target.ClientCode = src.ClientCode
           AND target.GoodsStage = src.GoodsStage
           AND target.GoodsId = src.GoodsId
        WHEN MATCHED THEN
            UPDATE SET
                ParentReference = COALESCE(NULLIF(?, ''), target.ParentReference),
                ItemNumber      = COALESCE(TRY_CONVERT(INT, NULLIF(?, '')), target.ItemNumber),
                TssStatus       = COALESCE(NULLIF(?, ''), target.TssStatus),
                RawJson         = COALESCE(?, target.RawJson),
                LastSyncedAt    = SYSUTCDATETIME(),
                UpdatedAt       = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (ClientCode, GoodsStage, GoodsId, ParentReference, ItemNumber,
                    TssStatus, RawJson, LastSyncedAt, UpdatedAt)
            VALUES (src.ClientCode, src.GoodsStage, src.GoodsId, NULLIF(?, ''),
                    TRY_CONVERT(INT, NULLIF(?, '')), NULLIF(?, ''), ?,
                    SYSUTCDATETIME(), SYSUTCDATETIME());
        """,
        [
            client_code, 'ENS', goods_ref,
            dec_ref, item_number, tss_status, raw_json,
            dec_ref, item_number, tss_status, raw_json,
        ],
    )


def _upsert_stg_remote_goods(
    cur,
    *,
    client_code: str,
    stg_consignment_id: int,
    dec_ref: str,
    goods_ref: str,
    item: dict,
    item_number: str,
) -> None:
    package_type = _item_text(item, 'type_of_packages', 'typeOfPackages', 'type_of_package', 'typeOfPackage')
    try:
        from app.pipeline_validation import normalise_package_type
        package_type = normalise_package_type(package_type, 'PK') or 'PK'
    except Exception:
        package_type = package_type or 'PK'

    cur.execute(
        """
        MERGE STG.BKD_GoodsItems AS target
        USING (SELECT ? AS ClientCode, ? AS GoodsId) AS src
        ON target.ClientCode = src.ClientCode
           AND target.tss_hex_id = src.GoodsId
        WHEN MATCHED THEN
            UPDATE SET
                stg_consignment_id          = ?,
                tss_consignment_ref         = COALESCE(NULLIF(?, ''), target.tss_consignment_ref),
                goods_stage                 = COALESCE(NULLIF(?, ''), target.goods_stage),
                item_seq                    = COALESCE(TRY_CONVERT(INT, NULLIF(?, '')), target.item_seq),
                goods_description           = COALESCE(NULLIF(?, ''), target.goods_description),
                commodity_code              = COALESCE(NULLIF(?, ''), target.commodity_code),
                gross_mass_kg               = COALESCE(TRY_CONVERT(DECIMAL(16,3), NULLIF(?, '')), target.gross_mass_kg),
                net_mass_kg                 = COALESCE(TRY_CONVERT(DECIMAL(16,3), NULLIF(?, '')), target.net_mass_kg),
                number_of_packages          = COALESCE(TRY_CONVERT(INT, NULLIF(?, '')), target.number_of_packages),
                number_of_individual_pieces = COALESCE(TRY_CONVERT(INT, NULLIF(?, '')), target.number_of_individual_pieces),
                type_of_packages            = COALESCE(NULLIF(?, ''), target.type_of_packages),
                package_marks               = COALESCE(NULLIF(?, ''), target.package_marks),
                controlled_goods            = COALESCE(NULLIF(?, ''), target.controlled_goods),
                country_of_origin           = COALESCE(NULLIF(?, ''), target.country_of_origin),
                procedure_code              = COALESCE(NULLIF(?, ''), target.procedure_code),
                additional_procedure_code   = COALESCE(NULLIF(?, ''), target.additional_procedure_code),
                item_invoice_amount         = COALESCE(TRY_CONVERT(DECIMAL(16,2), NULLIF(?, '')), target.item_invoice_amount),
                item_invoice_currency       = COALESCE(NULLIF(?, ''), target.item_invoice_currency),
                valuation_method            = COALESCE(NULLIF(?, ''), target.valuation_method),
                preference                  = COALESCE(NULLIF(?, ''), target.preference),
                error_message               = NULLIF(?, ''),
                sub_status                  = CASE
                    WHEN UPPER(COALESCE(target.sub_status, '')) IN ('', 'PENDING', 'PENDING REVIEW', 'VALIDATED')
                    THEN 'CREATED'
                    ELSE target.sub_status
                END,
                updated_at                  = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (
                ClientCode, stg_consignment_id, sub_status, source, goods_stage,
                tss_hex_id, tss_consignment_ref, item_seq, goods_description,
                commodity_code, gross_mass_kg, net_mass_kg, number_of_packages,
                number_of_individual_pieces, type_of_packages, package_marks,
                controlled_goods, country_of_origin, procedure_code,
                additional_procedure_code, item_invoice_amount, item_invoice_currency,
                valuation_method, preference, error_message,
                last_sub_status_change, updated_at
            )
            VALUES (
                src.ClientCode, ?, 'IMPORTED', 'TSS_SYNC', 'ENS',
                src.GoodsId, NULLIF(?, ''), TRY_CONVERT(INT, NULLIF(?, '')), NULLIF(?, ''),
                NULLIF(?, ''), TRY_CONVERT(DECIMAL(16,3), NULLIF(?, '')),
                TRY_CONVERT(DECIMAL(16,3), NULLIF(?, '')), TRY_CONVERT(INT, NULLIF(?, '')),
                TRY_CONVERT(INT, NULLIF(?, '')), NULLIF(?, ''), COALESCE(NULLIF(?, ''), 'ADDR'),
                NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''),
                TRY_CONVERT(DECIMAL(16,2), NULLIF(?, '')), NULLIF(?, ''),
                NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''),
                SYSUTCDATETIME(), SYSUTCDATETIME()
            );
        """,
        [
            client_code,
            goods_ref,
            stg_consignment_id,
            dec_ref,
            'ENS',
            item_number,
            _item_text(item, 'goods_description', 'goodsDescription'),
            _item_text(item, 'commodity_code', 'commodityCode'),
            _item_text(item, 'gross_mass_kg', 'grossMassKg', 'gross_weight_kg', 'grossWeightKg'),
            _item_text(item, 'net_mass_kg', 'netMassKg', 'net_weight_kg', 'netWeightKg'),
            _item_text(item, 'number_of_packages', 'numberOfPackages'),
            _item_text(item, 'number_of_individual_pieces', 'numberOfIndividualPieces'),
            package_type,
            _item_text(item, 'package_marks', 'packageMarks'),
            _item_text(item, 'controlled_goods', 'controlledGoods'),
            _item_text(item, 'country_of_origin', 'countryOfOrigin'),
            _item_text(item, 'procedure_code', 'procedureCode'),
            _item_text(item, 'additional_procedure_code', 'additionalProcedureCode'),
            _item_text(item, 'item_invoice_amount', 'itemInvoiceAmount'),
            _item_text(item, 'item_invoice_currency', 'itemInvoiceCurrency'),
            _item_text(item, 'valuation_method', 'valuationMethod'),
            _item_text(item, 'preference'),
            _item_text(item, 'error_message', 'errorMessage'),
            stg_consignment_id,
            dec_ref,
            item_number,
            _item_text(item, 'goods_description', 'goodsDescription'),
            _item_text(item, 'commodity_code', 'commodityCode'),
            _item_text(item, 'gross_mass_kg', 'grossMassKg', 'gross_weight_kg', 'grossWeightKg'),
            _item_text(item, 'net_mass_kg', 'netMassKg', 'net_weight_kg', 'netWeightKg'),
            _item_text(item, 'number_of_packages', 'numberOfPackages'),
            _item_text(item, 'number_of_individual_pieces', 'numberOfIndividualPieces'),
            package_type,
            _item_text(item, 'package_marks', 'packageMarks'),
            _item_text(item, 'controlled_goods', 'controlledGoods'),
            _item_text(item, 'country_of_origin', 'countryOfOrigin'),
            _item_text(item, 'procedure_code', 'procedureCode'),
            _item_text(item, 'additional_procedure_code', 'additionalProcedureCode'),
            _item_text(item, 'item_invoice_amount', 'itemInvoiceAmount'),
            _item_text(item, 'item_invoice_currency', 'itemInvoiceCurrency'),
            _item_text(item, 'valuation_method', 'valuationMethod'),
            _item_text(item, 'preference'),
            _item_text(item, 'error_message', 'errorMessage'),
        ],
    )


def _upsert_stg_sfd_tracking(
    cur,
    *,
    client_code: str,
    dec_ref: str,
    sfd_ref: str,
    movement_reference_number: str,
    eori_for_eidr: str,
    tss_status: str,
    error_message: str = '',
) -> None:
    cur.execute(
        """
        MERGE STG.BKD_SFD_Tracking AS target
        USING (SELECT ? AS ClientCode, ? AS SfdNumber) AS src
        ON target.ClientCode = src.ClientCode
           AND target.tss_sfd_number = src.SfdNumber
        WHEN MATCHED THEN
            UPDATE SET
                sub_status                    = COALESCE(NULLIF(?, ''), target.sub_status),
                stg_polled_at                 = SYSUTCDATETIME(),
                poll_count                    = COALESCE(target.poll_count, 0) + 1,
                tss_consignment_ref           = COALESCE(NULLIF(?, ''), target.tss_consignment_ref),
                tss_sfd_status                = COALESCE(NULLIF(?, ''), target.tss_sfd_status),
                tss_movement_reference_number = COALESCE(NULLIF(?, ''), target.tss_movement_reference_number),
                tss_eori_for_eidr             = COALESCE(NULLIF(?, ''), target.tss_eori_for_eidr),
                tss_error_message             = NULLIF(?, '')
        WHEN NOT MATCHED THEN
            INSERT (ClientCode, sub_status, stg_polled_at, poll_count,
                    tss_consignment_ref, tss_sfd_number, tss_sfd_status,
                    tss_movement_reference_number, tss_eori_for_eidr, tss_error_message)
            VALUES (?, COALESCE(NULLIF(?, ''), 'SYNCED'), SYSUTCDATETIME(), 1,
                    NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''),
                    NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''));
        """,
        [
            client_code, sfd_ref,
            tss_status or 'SYNCED', dec_ref, tss_status, movement_reference_number,
            eori_for_eidr, error_message,
            client_code, tss_status or 'SYNCED', dec_ref, sfd_ref, tss_status,
            movement_reference_number, eori_for_eidr, error_message,
        ],
    )


def _log_sync_exchange(
    cur,
    *,
    client_code: str,
    stg_header_id: int,
    call_type: str,
    entity_ref: str,
    api_result: dict,
) -> None:
    """Log one TSS poll call to TSS.BKD_API_Exchanges. Never raises."""
    try:
        from app.data_model import insert_tss_api_exchange
        insert_tss_api_exchange(
            cur,
            schema_name=client_code,
            legacy_api_call_log_id=None,
            call_type=call_type,
            staging_id=stg_header_id,
            http_method='GET',
            url=(entity_ref or '')[:500],
            request_payload=None,
            http_status=api_result.get('http_status'),
            response_status=api_result.get('status') or '',
            response_message=api_result.get('message') or '',
            response_json=api_result.get('raw_response') or api_result.get('response'),
            duration_ms=api_result.get('duration_ms'),
            error_detail=None if api_result.get('success') else api_result.get('message'),
        )
    except Exception as exc:
        log.debug('_log_sync_exchange failed: %s', exc)


def sync_ens_status_once(
    stg_header_id: int,
    *,
    tenant_code: str = 'BKD',
    continue_after_notified: bool = False,
) -> dict:
    """Poll TSS API for this ENS; upsert status into TSS.BKD_*; check movement gate.

    Reads only STG.BKD_ENS_Headers and STG.BKD_ENS_Consignments.
    Writes only TSS.BKD_ENS_Headers, TSS.BKD_ENS_Consignments, TSS.BKD_API_Exchanges.
    No BKD-Staging tables. No legacy scripts. Never raises.

    Return keys:
      ok              True if sync completed (not necessarily notified)
      stage           init | header_not_found | no_tss_ref | already_notified | synced | error
      already_notified True if movement_notified_at was already set
      notified        True if this call sent the final movement email
      header_tss_status The TSS status string read from the API
      notify_ok / notify_reason From check_and_notify_ens_authorised
    """
    from app.db import db_cursor, get_db
    from app.tss_api import build_cfg_client

    client_code = str(tenant_code or 'BKD').strip().upper()
    out: dict[str, Any] = {
        'ok': False,
        'stage': 'init',
        'stg_header_id': stg_header_id,
        'tenant_code': client_code,
    }

    try:
        # ── read phase (no write, no TSS API) ───────────────────────────────
        ens_ref: str = ''
        cons_refs: list[dict] = []

        with db_cursor(commit=False) as cur:
            cur.execute(
                """
                SELECT tss_ens_header_ref, movement_notified_at
                FROM STG.BKD_ENS_Headers
                WHERE ClientCode = ? AND stg_header_id = ?
                """,
                [client_code, stg_header_id],
            )
            hrow = cur.fetchone()
            if not hrow:
                out['stage'] = 'header_not_found'
                return out

            hcols = [d[0] for d in cur.description]
            header = dict(zip(hcols, hrow))

            if header.get('movement_notified_at') is not None and not continue_after_notified:
                out['ok'] = True
                out['stage'] = 'already_notified'
                out['already_notified'] = True
                return out
            if header.get('movement_notified_at') is not None:
                out['already_notified'] = True

            ens_ref = str(header.get('tss_ens_header_ref') or '').strip()
            if not ens_ref:
                out['stage'] = 'no_tss_ref'
                return out

            cur.execute(
                """
                SELECT stg_consignment_id, tss_consignment_ref
                FROM STG.BKD_ENS_Consignments
                WHERE ClientCode = ?
                  AND stg_header_id = ?
                  AND UPPER(COALESCE(sub_status, '')) NOT IN ('CANCELLED', 'DELETED')
                """,
                [client_code, stg_header_id],
            )
            cons_rows = cur.fetchall()
            ccols = [d[0] for d in cur.description]
            cons_refs = [dict(zip(ccols, r)) for r in cons_rows]

        # ── TSS API phase (no DB connection held) ───────────────────────────
        api = build_cfg_client()

        header_api = api.read_header(ens_ref, fields=['status', 'declaration_number'])
        header_tss_status = _extract_tss_status(header_api)
        out['header_tss_status'] = header_tss_status

        cons_api: list[dict] = []
        sfd_api: list[dict] = []
        goods_api: list[dict] = []
        for cons in cons_refs:
            dec_ref = str(cons.get('tss_consignment_ref') or '').strip()
            if not dec_ref:
                cons_api.append({
                    'stg_consignment_id': cons.get('stg_consignment_id'),
                    'dec_ref': '',
                    'result': {},
                    'record': {},
                    'tss_status': '',
                })
                continue
            capi = api.read_consignment(dec_ref, fields=CONSIGNMENT_READ_FIELDS)
            detail_items = _response_items(api, capi)
            if detail_items and isinstance(detail_items[0], dict):
                cons_record = detail_items[0]
            else:
                response = capi.get('response') if isinstance(capi, dict) else {}
                cons_record = response if isinstance(response, dict) else {}
            cons_api.append({
                'stg_consignment_id': cons.get('stg_consignment_id'),
                'dec_ref': dec_ref,
                'result': capi,
                'record': cons_record,
                'tss_status': _extract_tss_status(capi),
            })

            lookup = api.lookup_ens_goods(dec_ref)
            items = _response_items(api, lookup)
            lookup_used = lookup
            if not items:
                fallback_lookup = api.lookup_goods(dec_ref, parent_type='consignment_number')
                fallback_items = _response_items(api, fallback_lookup)
                if fallback_items:
                    lookup_used = fallback_lookup
                    items = fallback_items

            goods_reads = []
            for index, item in enumerate(items, start=1):
                goods_ref = _item_ref(item, 'goods_id', 'goodsId', 'reference', 'id')
                if not goods_ref:
                    continue
                read_result = api.read_goods(goods_ref, fields=GOODS_READ_FIELDS)
                detail_items = _response_items(api, read_result)
                detail_item = detail_items[0] if detail_items else read_result.get('response') if isinstance(read_result, dict) else {}
                if isinstance(item, dict) and isinstance(detail_item, dict):
                    merged = dict(item)
                    merged.update({k: v for k, v in detail_item.items() if v not in (None, '')})
                elif isinstance(detail_item, dict):
                    merged = detail_item
                elif isinstance(item, dict):
                    merged = dict(item)
                else:
                    merged = {'goods_id': goods_ref}
                merged.setdefault('goods_id', goods_ref)
                merged.setdefault('reference', goods_ref)
                goods_reads.append({
                    'goods_ref': goods_ref,
                    'item': merged,
                    'item_number': _goods_item_number(merged, index),
                    'read_result': read_result,
                })

            goods_api.append({
                'stg_consignment_id': cons.get('stg_consignment_id'),
                'dec_ref': dec_ref,
                'lookup_result': lookup_used,
                'items': goods_reads,
            })

        for cr in cons_api:
            dec_ref = cr.get('dec_ref') or ''
            if not dec_ref or not _should_lookup_sfd(cr.get('tss_status')):
                continue
            result = api.lookup_sfd(dec_ref)
            items = _response_items(api, result)
            enriched_items = []
            for item in items:
                sfd_ref = _item_ref(
                    item,
                    'sfd_reference', 'reference', 'sfd_number',
                    'declaration_number', 'number',
                )
                if not sfd_ref:
                    enriched_items.append(item)
                    continue
                detail_result = api.read_sfd(sfd_ref, fields=SFD_READ_FIELDS)
                detail_items = _response_items(api, detail_result)
                detail_item = detail_items[0] if detail_items else detail_result.get('response')
                if isinstance(item, dict) and isinstance(detail_item, dict):
                    merged = dict(item)
                    merged.update({k: v for k, v in detail_item.items() if v not in (None, '')})
                    enriched_items.append(merged)
                else:
                    enriched_items.append(detail_item if detail_item else item)
                sfd_api.append({
                    'stg_consignment_id': cr.get('stg_consignment_id'),
                    'dec_ref': dec_ref,
                    'result': detail_result,
                    'items': [enriched_items[-1]],
                    'lookup_result': result,
                    'sfd_ref': sfd_ref,
                })
            if enriched_items:
                continue
            sfd_api.append({
                'stg_consignment_id': cr.get('stg_consignment_id'),
                'dec_ref': dec_ref,
                'result': result,
                'items': items,
                'lookup_result': result,
            })

        out['consignments_polled'] = len(cons_api)
        out['sfd_polled'] = len(sfd_api)
        out['goods_polled'] = sum(len(gr.get('items') or []) for gr in goods_api)

        # ── write phase: upsert statuses + run movement gate ────────────────
        with db_cursor() as cur:
            conn = get_db()

            _log_sync_exchange(cur, client_code=client_code, stg_header_id=stg_header_id,
                               call_type='SYNC_ENS_HEADER_STATUS', entity_ref=ens_ref,
                               api_result=header_api)
            _upsert_tss_ens_header_status(
                cur,
                client_code=client_code,
                ens_ref=ens_ref,
                tss_status=header_tss_status,
                raw_json=_tss_raw_json(header_api.get('response') or {}),
            )

            for cr in cons_api:
                dec_ref = cr['dec_ref']
                if not dec_ref:
                    continue
                _log_sync_exchange(cur, client_code=client_code, stg_header_id=stg_header_id,
                                   call_type='SYNC_ENS_CONSIGNMENT_STATUS', entity_ref=dec_ref,
                                   api_result=cr['result'])
                _upsert_tss_cons_status(
                    cur,
                    client_code=client_code,
                    ens_ref=ens_ref,
                    dec_ref=dec_ref,
                    tss_status=cr['tss_status'],
                    consignment_record=cr.get('record') or {},
                    raw_json=_tss_raw_json(cr['result'].get('response') or {}),
                )

            goods_synced = 0
            for gr in goods_api:
                dec_ref = gr.get('dec_ref') or ''
                if not dec_ref:
                    continue
                _log_sync_exchange(cur, client_code=client_code, stg_header_id=stg_header_id,
                                   call_type='SYNC_GOODS_LOOKUP', entity_ref=dec_ref,
                                   api_result=gr.get('lookup_result') or {})
                for item_row in gr.get('items') or []:
                    goods_ref = item_row.get('goods_ref') or ''
                    item = item_row.get('item') or {}
                    read_result = item_row.get('read_result') or {}
                    _log_sync_exchange(cur, client_code=client_code, stg_header_id=stg_header_id,
                                       call_type='READ_GOODS_STATUS',
                                       entity_ref=goods_ref,
                                       api_result=read_result)
                    tss_status = _goods_status_from_item(item) or _goods_status_from_result(read_result)
                    raw_json = _tss_raw_json(item if isinstance(item, dict) else {'goods_id': goods_ref})
                    _upsert_tss_goods(
                        cur,
                        client_code=client_code,
                        dec_ref=dec_ref,
                        goods_ref=goods_ref,
                        item_number=item_row.get('item_number') or '',
                        tss_status=tss_status,
                        raw_json=raw_json,
                    )
                    _upsert_stg_remote_goods(
                        cur,
                        client_code=client_code,
                        stg_consignment_id=int(gr.get('stg_consignment_id') or 0),
                        dec_ref=dec_ref,
                        goods_ref=goods_ref,
                        item=item,
                        item_number=item_row.get('item_number') or '',
                    )
                    goods_synced += 1

            sfd_synced = 0
            for sr in sfd_api:
                dec_ref = sr['dec_ref']
                lookup_result = sr.get('lookup_result') or sr['result']
                detail_result = sr['result']
                _log_sync_exchange(cur, client_code=client_code, stg_header_id=stg_header_id,
                                   call_type='SYNC_SFD_LOOKUP', entity_ref=dec_ref,
                                   api_result=lookup_result)
                if sr.get('lookup_result') is not sr.get('result'):
                    _log_sync_exchange(cur, client_code=client_code, stg_header_id=stg_header_id,
                                       call_type='READ_SFD_STATUS',
                                       entity_ref=sr.get('sfd_ref') or dec_ref,
                                       api_result=detail_result)
                for item in sr.get('items') or []:
                    sfd_ref = _item_ref(
                        item,
                        'sfd_reference', 'reference', 'sfd_number',
                        'declaration_number', 'number',
                    )
                    if not sfd_ref:
                        continue
                    movement_ref = _first_text(
                        _item_text(item, 'movement_reference_number', 'movementReferenceNumber', 'mrn'),
                    )
                    eidr_ref = _first_text(
                        _item_text(item, 'eori_for_eidr', 'eoriForEidr', 'eidr'),
                    )
                    sfd_status = (
                        _sfd_status_from_item(item)
                        or _sfd_status_from_result(detail_result)
                        or _sfd_status_from_result(lookup_result)
                        or cr.get('tss_status')
                    )
                    raw_json = _tss_raw_json(item if isinstance(item, dict) else {'reference': item})
                    _upsert_tss_sfd(
                        cur,
                        client_code=client_code,
                        ens_ref=ens_ref,
                        dec_ref=dec_ref,
                        sfd_ref=sfd_ref,
                        movement_reference_number=movement_ref,
                        tss_status=sfd_status,
                        raw_json=raw_json,
                    )
                    _upsert_stg_sfd_tracking(
                        cur,
                        client_code=client_code,
                        dec_ref=dec_ref,
                        sfd_ref=sfd_ref,
                        movement_reference_number=movement_ref,
                        eori_for_eidr=eidr_ref,
                        tss_status=sfd_status,
                    )
                    sfd_synced += 1

            attention_items: list[dict] = []
            if header_tss_status == 'TRADER_INPUT_REQUIRED':
                attention_items.append({
                    'entity_kind': 'ENS',
                    'stg_header_id': stg_header_id,
                    'tss_ref': ens_ref,
                    'tss_status': header_tss_status,
                })
            for cr in cons_api:
                if cr.get('tss_status') == 'TRADER_INPUT_REQUIRED':
                    attention_items.append({
                        'entity_kind': 'CONSIGNMENT',
                        'stg_header_id': stg_header_id,
                        'stg_consignment_id': cr.get('stg_consignment_id'),
                        'tss_ref': cr.get('dec_ref'),
                        'tss_status': cr.get('tss_status'),
                    })

            from app.ingestion.automation_notify import (
                check_and_notify_ens_authorised,
                notify_tss_status_attention,
            )
            attention_ok, attention_reason = (False, None)
            if attention_items:
                attention_ok, attention_reason = notify_tss_status_attention(
                    {
                        'stg_header_id': stg_header_id,
                        'tss_ens_header_ref': ens_ref,
                    },
                    attention_items,
                    tenant_code=client_code,
                )

            notify_ok, notify_reason = check_and_notify_ens_authorised(
                stg_header_id,
                cursor=cur,
                conn=conn,
                client_code=client_code,
            )

        out['ok'] = True
        out['stage'] = 'synced'
        out['notify_ok'] = notify_ok
        out['notify_reason'] = notify_reason
        out['notified'] = notify_ok
        out['sfd_synced'] = sfd_synced
        out['goods_synced'] = goods_synced
        out['attention_items'] = len(attention_items)
        out['attention_ok'] = attention_ok
        out['attention_reason'] = attention_reason

        if notify_ok:
            log.info(
                'ENS watcher: movement notified stg_header_id=%s (%s)',
                stg_header_id, client_code,
            )
        else:
            log.debug(
                'ENS watcher: stg_header_id=%s header_status=%s notify_reason=%s',
                stg_header_id, header_tss_status, notify_reason,
            )

    except Exception as exc:
        log.warning('sync_ens_status_once error stg_header_id=%s: %s', stg_header_id, exc)
        out['stage'] = 'error'
        out['error'] = str(exc)

    return out


def start_ens_status_watcher(
    stg_header_id: int,
    *,
    tenant_code: str = 'BKD',
    app_obj=None,
) -> None:
    """Start a daemon thread that watches this ENS until authorised or timeout.

    Each poll creates a fresh app context so DB connections are properly cleaned up.
    Stops when movement_notified_at is stamped or after _MAX_ATTEMPTS polls.
    Never raises.
    """
    def _run() -> None:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            time.sleep(_POLL_INTERVAL_S)
            try:
                if app_obj is not None:
                    with app_obj.app_context():
                        result = sync_ens_status_once(stg_header_id, tenant_code=tenant_code)
                else:
                    result = sync_ens_status_once(stg_header_id, tenant_code=tenant_code)

                if result.get('already_notified') or result.get('notified'):
                    log.info(
                        'ENS watcher stopping: stg_header_id=%s stage=%s attempt=%d',
                        stg_header_id, result.get('stage'), attempt,
                    )
                    return

                if result.get('stage') == 'header_not_found':
                    log.warning(
                        'ENS watcher: stg_header_id=%s not found, stopping', stg_header_id,
                    )
                    return

            except Exception as exc:
                log.warning(
                    'ENS watcher attempt %d error stg_header_id=%s: %s',
                    attempt, stg_header_id, exc,
                )

        log.info('ENS watcher: max attempts reached for stg_header_id=%s', stg_header_id)

    threading.Thread(
        target=_run,
        name=f'ens-watcher-{tenant_code}-{stg_header_id}',
        daemon=True,
    ).start()
    log.info('ENS status watcher started stg_header_id=%s (%s)', stg_header_id, tenant_code)
