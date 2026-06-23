"""Helpers for linking PRD SDI/SupDec rows back to visible cargo records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.db import query_all
from app.tenant import get_tenant


def _as_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    return getattr(row, "__dict__", {}) or {}


def _first_text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _clean_ref(value: Any) -> str:
    return str(value or "").strip().upper()


def _same_id(left: Any, right: Any) -> bool:
    if left in (None, "") or right in (None, ""):
        return False
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return str(left).strip() == str(right).strip()


def _unique_values(values: Sequence[Any] | None, *, numeric: bool = False) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values or []:
        if value in (None, ""):
            continue
        if numeric:
            try:
                cleaned: Any = int(value)
            except (TypeError, ValueError):
                continue
        else:
            cleaned = str(value).strip()
            if not cleaned:
                continue
            cleaned = cleaned.upper()
        if cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _chunks(values: Sequence[Any], size: int) -> list[list[Any]]:
    size = max(1, int(size or 1))
    items = list(values or [])
    return [items[idx:idx + size] for idx in range(0, len(items), size)]


def normalise_prd_sdi_link(row: Any) -> dict[str, Any]:
    """Return a STG.BKD_SDI_Headers row in the shape legacy templates expect."""
    data = _as_dict(row)
    sup_ref = _first_text(
        data,
        "sup_dec_number",
        "tss_sup_dec_number",
        "sup_dec_reference",
        "SupDecNumber",
    )
    sfd_ref = _first_text(
        data,
        "sfd_reference",
        "sfd_number",
        "tss_sfd_consignment_ref",
        "SfdReference",
    )
    cons_ref = _first_text(
        data,
        "ens_consignment_ref",
        "ens_consignment_reference",
        "tss_consignment_ref",
        "DeclarationNumber",
        "ConsignmentReference",
        "consignment_number",
        "trader_reference",
        "transport_document_number",
    )
    staging_id = data.get("staging_id") or data.get("stg_sdi_id") or data.get("TssSdiHeaderId")
    consignment_id = data.get("stg_consignment_id") or data.get("staging_cons_id")
    status = _first_text(data, "status", "sub_status", "tss_status", "TssStatus")

    normalised = dict(data)
    source_table = data.get("source_table") or (
        "STG.BKD_SDI_Headers" if data.get("stg_sdi_id") else (
            "TSS.BKD_SDI_Headers" if data.get("TssSdiHeaderId") else ""
        )
    )
    normalised.update(
        {
            "staging_id": staging_id,
            "stg_sdi_id": data.get("stg_sdi_id") or staging_id,
            "sup_dec_number": sup_ref,
            "sup_dec_reference": sup_ref,
            "sfd_reference": sfd_ref,
            "sfd_number": sfd_ref,
            "ens_consignment_ref": cons_ref,
            "ens_consignment_reference": cons_ref,
            "staging_cons_id": consignment_id,
            "stg_consignment_id": consignment_id,
            "status": status,
            "submission_due_date": (
                data.get("submission_due_date")
                or data.get("tss_submission_due_date")
                or data.get("SubmissionDueDate")
            ),
            "tss_movement_reference_number": (
                data.get("tss_movement_reference_number")
                or data.get("MovementReferenceNumber")
            ),
            "source_table": source_table,
        }
    )
    return normalised


def load_prd_sdi_links_for_context(
    *,
    client_code: str | None = None,
    consignment_ids: Sequence[Any] | None = None,
    consignment_refs: Sequence[Any] | None = None,
    sfd_refs: Sequence[Any] | None = None,
    sup_refs: Sequence[Any] | None = None,
    limit: int = 250,
) -> list[dict[str, Any]]:
    """Load staged PRD SDIs that match the supplied cargo/SFD references."""
    client_code = (client_code or get_tenant().get("code") or "BKD").upper()
    limit = max(1, min(int(limit or 250), 5000))
    ids = _unique_values(consignment_ids, numeric=True)
    refs = _unique_values([*(consignment_refs or []), *(sfd_refs or []), *(sup_refs or [])])
    if not ids and not refs:
        return []

    rows: list[Any] = []

    def query_stg(where_sql: str, params: list[Any]) -> None:
        try:
            rows.extend(query_all(
                f"""
                SELECT TOP ({limit})
                    h.stg_sdi_id,
                    h.ClientCode,
                    h.sub_status,
                    h.stg_consignment_id,
                    h.tss_consignment_ref,
                    h.tss_sup_dec_number,
                    h.tss_sfd_consignment_ref,
                    h.tss_submission_due_date,
                    h.tss_status,
                    h.tss_movement_reference_number,
                    h.trader_reference,
                    h.transport_document_number,
                    h.validation_errors_json,
                    h.auto_submit_error,
                    h.sdi_ready_at,
                    h.submitted_at,
                    h.updated_at
                FROM [STG].[BKD_SDI_Headers] h
                WHERE h.ClientCode = ?
                  AND ({where_sql})
                ORDER BY COALESCE(h.updated_at, h.sdi_ready_at, h.submitted_at) DESC, h.stg_sdi_id DESC
                """,
                params,
            ))
        except Exception:
            pass

    for id_batch in _chunks(ids, 400):
        placeholders = ", ".join("?" for _ in id_batch)
        query_stg(f"h.stg_consignment_id IN ({placeholders})", [client_code, *id_batch])

    stg_ref_columns = (
        "h.tss_consignment_ref",
        "h.tss_sfd_consignment_ref",
        "h.tss_sup_dec_number",
        "h.trader_reference",
        "h.transport_document_number",
    )
    for ref_batch in _chunks(refs, 250):
        placeholders = ", ".join("?" for _ in ref_batch)
        criteria = [
            f"UPPER(LTRIM(RTRIM(COALESCE({column}, '')))) IN ({placeholders})"
            for column in stg_ref_columns
        ]
        params: list[Any] = [client_code]
        for _column in stg_ref_columns:
            params.extend(ref_batch)
        query_stg(" OR ".join(criteria), params)

    for ref_batch in _chunks(refs, 100):
        tss_criteria = []
        tss_params: list[Any] = [client_code]
        placeholders = ", ".join("?" for _ in ref_batch)
        for column in (
            "h.SfdReference",
            "h.SupDecNumber",
            "h.MovementReferenceNumber",
        ):
            tss_criteria.append(f"UPPER(LTRIM(RTRIM(COALESCE({column}, '')))) IN ({placeholders})")
            tss_params.extend(ref_batch)
        for json_path in (
            "$.consignment_number",
            "$.consignmentNumber",
            "$.declaration_number",
            "$.declarationNumber",
            "$.trader_reference",
            "$.traderReference",
            "$.transport_document_number",
            "$.transportDocumentNumber",
            "$.sfd_reference",
            "$.sfdReference",
            "$.sfd_number",
            "$.sfdNumber",
            "$.parent",
            "$.u_parent",
        ):
            tss_criteria.append(
                "UPPER(LTRIM(RTRIM(COALESCE("
                f"CASE WHEN ISJSON(h.RawJson) = 1 THEN JSON_VALUE(h.RawJson, '{json_path}') END"
                f", '')))) IN ({placeholders})"
            )
            tss_params.extend(ref_batch)
        try:
            rows.extend(query_all(
                f"""
                SELECT TOP ({limit})
                    h.TssSdiHeaderId,
                    h.ClientCode,
                    h.SupDecNumber,
                    COALESCE(
                        NULLIF(LTRIM(RTRIM(h.SfdReference)), ''),
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.sfd_reference'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.sfdReference'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.sfd_number'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.sfdNumber'))), '') END
                    ) AS SfdReference,
                    COALESCE(
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.consignment_number'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.consignmentNumber'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.declaration_number'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.declarationNumber'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.trader_reference'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.traderReference'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.transport_document_number'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.transportDocumentNumber'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.parent'))), '') END,
                        CASE WHEN ISJSON(h.RawJson) = 1 THEN NULLIF(LTRIM(RTRIM(JSON_VALUE(h.RawJson, '$.u_parent'))), '') END
                    ) AS DeclarationNumber,
                    h.MovementReferenceNumber,
                    h.TssStatus,
                    h.SubmissionDueDate,
                    h.UpdatedAt,
                    'TSS.BKD_SDI_Headers' AS source_table
                FROM [TSS].[BKD_SDI_Headers] h
                WHERE h.ClientCode = ?
                  AND ({' OR '.join(tss_criteria)})
                ORDER BY COALESCE(h.UpdatedAt, h.LastSyncedAt, h.CreatedAt) DESC, h.TssSdiHeaderId DESC
                """,
                tss_params,
            ))
        except Exception:
            pass
    return merge_sdi_links([normalise_prd_sdi_link(row) for row in rows])


def sdi_matches_consignment(link: Mapping[str, Any], consignment: Mapping[str, Any]) -> bool:
    if not link or not consignment:
        return False

    link_ids = (link.get("stg_consignment_id"), link.get("staging_cons_id"))
    cons_ids = (consignment.get("stg_consignment_id"), consignment.get("staging_id"))
    if any(_same_id(left, right) for left in link_ids for right in cons_ids):
        return True

    cons_refs = {
        _clean_ref(consignment.get("tss_consignment_ref")),
        _clean_ref(consignment.get("dec_reference")),
        _clean_ref(consignment.get("sfd_reference")),
        _clean_ref(consignment.get("synced_sfd_reference")),
        _clean_ref(consignment.get("linked_sfd_reference")),
        _clean_ref(consignment.get("trader_reference")),
        _clean_ref(consignment.get("transport_document_number")),
        _clean_ref(consignment.get("document_no")),
    } - {""}
    sdi_refs = {
        _clean_ref(link.get("ens_consignment_ref")),
        _clean_ref(link.get("ens_consignment_reference")),
        _clean_ref(link.get("tss_consignment_ref")),
        _clean_ref(link.get("sfd_reference")),
        _clean_ref(link.get("sfd_number")),
        _clean_ref(link.get("tss_sfd_consignment_ref")),
        _clean_ref(link.get("trader_reference")),
        _clean_ref(link.get("transport_document_number")),
    } - {""}
    return bool(cons_refs & sdi_refs)


def merge_sdi_links(*groups: Sequence[Any]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for group in groups:
        for row in group or []:
            link = normalise_prd_sdi_link(row)
            key = _clean_ref(link.get("sup_dec_number"))
            if key:
                key = f"SUP:{key}"
            else:
                key = f"ID:{link.get('source_table') or ''}:{link.get('staging_id') or id(row)}"
            if key not in merged:
                merged[key] = link
                order.append(key)
            else:
                merged[key].update({k: v for k, v in link.items() if v not in (None, "")})
    return [merged[key] for key in order]


def attach_sdi_links_to_consignments(
    consignments: Sequence[Mapping[str, Any]],
    links: Sequence[Mapping[str, Any]],
) -> dict[Any, list[dict[str, Any]]]:
    """Mutate consignment dicts with linked_supdecs and return them by id."""
    normalised_links = [normalise_prd_sdi_link(link) for link in links or []]
    by_consignment: dict[Any, list[dict[str, Any]]] = {}
    for cons in consignments or []:
        if not isinstance(cons, dict):
            continue
        key = cons.get("stg_consignment_id") or cons.get("staging_id")
        matched = [link for link in normalised_links if sdi_matches_consignment(link, cons)]
        matched = merge_sdi_links(matched)
        cons["linked_supdecs"] = matched
        if matched:
            first = matched[0]
            cons["sdi_reference"] = first.get("sup_dec_number")
            cons["sdi_status"] = first.get("status")
            cons["sdi_submission_due_date"] = first.get("submission_due_date")
        if key is not None:
            by_consignment[key] = matched
    return by_consignment
