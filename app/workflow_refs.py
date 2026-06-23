from collections.abc import Mapping

from flask import url_for


def _as_mapping(record):
    if record is None:
        return {}
    if isinstance(record, Mapping):
        return record
    mapping = getattr(record, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    return getattr(record, "__dict__", {}) or {}


def _first_value(record, *keys):
    if isinstance(record, str):
        return record.strip()
    data = _as_mapping(record)
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _first_int(record, *keys):
    value = _first_value(record, *keys)
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ens_reference(record):
    return _first_value(record, "ens_reference", "external_ref", "tss_ens_header_ref", "declaration_number")


def consignment_reference(record):
    return _first_value(
        record,
        "dec_reference",
        "consignment_number",
        "ens_consignment_ref",
        "ens_consignment_reference",
    )


def sfd_reference(record):
    return _first_value(record, "sfd_reference", "sfd_number")


def supdec_reference(record):
    return _first_value(record, "sup_dec_number", "sup_dec_reference")


def gmr_reference(record):
    return _first_value(record, "gmr_id")


def goods_reference(record):
    return _first_value(record, "goods_id")


def ens_detail_url(record):
    header_id = _first_int(record, "stg_header_id", "pipeline_staging_id", "staging_ens_id", "ens_staging_id", "linked_pipeline_staging_id", "staging_id")
    if header_id is not None:
        return url_for("declarations.header_detail", staging_id=header_id)
    dec_id = _first_int(record, "staging_declaration_id", "declaration_id", "id")
    if dec_id is not None:
        return url_for("declarations.detail", dec_id=dec_id)
    ref = ens_reference(record)
    if ref:
        return url_for("declarations.detail_by_ref", ens_ref=ref)
    return url_for("declarations.list_declarations")


def consignment_detail_url(record):
    sid = _first_int(record, "stg_consignment_id", "consignment_staging_id", "staging_cons_id", "staging_id", "id")
    if sid is not None:
        return url_for("consignments.detail", sid=sid)
    ref = consignment_reference(record) or sfd_reference(record)
    if ref:
        return url_for("consignments.detail_by_ref", cons_ref=ref)
    return url_for("consignments.list_view")


def goods_detail_url(record):
    ref = goods_reference(record)
    if ref:
        return url_for("goods.detail_by_ref", goods_ref=ref)
    sid = _first_int(record, "staging_id", "id")
    return url_for("goods.detail", sid=sid) if sid is not None else url_for("goods.list_view")


def supdec_detail_url(record):
    ref = supdec_reference(record)
    if ref:
        return url_for("sdi_detail_alias", sup_ref=ref)
    sid = _first_int(record, "staging_id", "id")
    return url_for("supdec.detail", sid=sid) if sid is not None else url_for("supdec.list_view")


def gmr_detail_url(record):
    ref = gmr_reference(record)
    if ref:
        return url_for("gmr.detail_by_ref", gmr_ref=ref)
    sid = _first_int(record, "staging_id", "id")
    return url_for("gmr.detail", sid=sid) if sid is not None else url_for("gmr.list_view")
