from __future__ import annotations

import re
from typing import Any

from .file_introspection import clean_cell
from .mapping_suggestions import TARGET_FIELDS, normalise, target_lookup
from .tss_submission import CONSIGNMENT_REQUIRED_FIELDS, compact

MAX_GOODS_PER_CONSIGNMENT = 99
GOODS_REQUIRED_FIELDS = (
    "type_of_packages",
    "number_of_packages",
    "package_marks",
    "gross_mass_kg",
    "goods_description",
)
GOODS_ATTENTION_FIELDS = GOODS_REQUIRED_FIELDS + ("net_mass_kg", "commodity_code")

CONSIGNMENT_DISPLAY_FIELDS = (
    "declaration_number",
    "consignment_number",
    "goods_description",
    "trader_reference",
    "transport_document_number",
    "controlled_goods",
    "goods_domestic_status",
    "destination_country",
    "consignor_eori",
    "consignor_name",
    "consignor_country",
    "consignee_eori",
    "consignee_name",
    "consignee_country",
    "importer_eori",
    "importer_name",
    "importer_country",
    "exporter_eori",
    "exporter_name",
    "exporter_country",
    "container_indicator",
)

GOODS_DISPLAY_FIELDS = (
    "goods_id",
    "goods_description",
    "commodity_code",
    "type_of_packages",
    "number_of_packages",
    "package_marks",
    "gross_mass_kg",
    "net_mass_kg",
    "country_of_origin",
    "item_invoice_amount",
    "item_invoice_currency",
    "invoice_number",
    "controlled_goods",
    "controlled_goods_type",
    "procedure_code",
    "additional_procedure_code",
    "preference",
    "nature_of_transaction",
)

FIELD_LABELS = {
    "declaration_number": "Declaration number",
    "consignment_number": "Consignment number",
    "goods_description": "Description",
    "trader_reference": "Trader reference",
    "transport_document_number": "Transport document",
    "controlled_goods": "Controlled goods",
    "goods_domestic_status": "Goods domestic status",
    "destination_country": "Destination country",
    "consignor_eori": "Consignor EORI",
    "consignor_name": "Consignor name",
    "consignor_country": "Consignor country",
    "consignee_eori": "Consignee EORI",
    "consignee_name": "Consignee name",
    "consignee_country": "Consignee country",
    "importer_eori": "Importer EORI",
    "importer_name": "Importer name",
    "importer_country": "Importer country",
    "exporter_eori": "Exporter EORI",
    "exporter_name": "Exporter name",
    "exporter_country": "Exporter country",
    "container_indicator": "Container indicator",
    "goods_id": "Goods ID",
    "commodity_code": "Commodity code",
    "type_of_packages": "Package type",
    "number_of_packages": "Packages",
    "package_marks": "Package marks",
    "gross_mass_kg": "Gross mass kg",
    "net_mass_kg": "Net mass kg",
    "country_of_origin": "Origin country",
    "item_invoice_amount": "Invoice amount",
    "item_invoice_currency": "Currency",
    "invoice_number": "Invoice number",
    "controlled_goods_type": "Controlled goods type",
    "procedure_code": "Procedure code",
    "additional_procedure_code": "Additional procedure",
    "preference": "Preference",
    "nature_of_transaction": "Nature of transaction",
}

FIELD_VALUE_COLUMNS = ("source_value", "value", "api_value", "target_value", "field_value")
FIELD_NAME_COLUMNS = ("api_field", "field", "target_field", "api field", "target")

_PREFIX_RE = re.compile(r"^(prs)?(consignment|goodsitem|goods_item|goods|item|items)[._\-\s:]+", re.I)
_GOODS_INDEX_RE = re.compile(r"(?:goods(?:_item)?|item|items)[._\-\s\[]*(\d+)", re.I)
_TRAILING_INDEX_RE = re.compile(r"(?:^|[._\-\s])(?:line|row|item)[._\-\s]*(\d+)(?:$|[._\-\s])", re.I)


def _column_lookup(row: dict[str, Any]) -> dict[str, str]:
    return {normalise(key): key for key in row}


def _row_value(row: dict[str, Any], candidates: tuple[str, ...]) -> str:
    lookup = _column_lookup(row)
    for candidate in candidates:
        key = lookup.get(normalise(candidate))
        if key is not None:
            return clean_cell(row.get(key))
    return ""


def _rows_from_structure(structure: dict[str, Any]) -> list[dict[str, str]]:
    rows = structure.get("dataRows") or structure.get("sampleRows") or []
    return [row for row in rows if isinstance(row, dict)]


def _is_field_value_mode(rows: list[dict[str, Any]], columns: list[dict[str, Any]]) -> bool:
    names = {normalise(column.get("name")) for column in columns}
    if any(normalise(name) in names for name in FIELD_NAME_COLUMNS) and any(normalise(name) in names for name in FIELD_VALUE_COLUMNS):
        return True
    if not rows:
        return False
    lookup = _column_lookup(rows[0])
    return any(normalise(name) in lookup for name in FIELD_NAME_COLUMNS) and any(normalise(name) in lookup for name in FIELD_VALUE_COLUMNS)


def _target_tail(value: str) -> str:
    tail = re.sub(r"^PRS[._\-\s:]+", "", value, flags=re.I)
    tail = re.sub(
        r"^(consignment|goods_item|goodsitem|goods|item|items)(?:[._\-\s:\[]+\d+\]?)*[._\-\s:]+",
        "",
        tail,
        flags=re.I,
    )
    tail = re.sub(r"\[[0-9]+\]", "", tail)
    return tail


def _path_candidates(value: str) -> list[str]:
    clean = clean_cell(value)
    if not clean:
        return []
    stripped = re.sub(r"\[[0-9]+\]", "", clean)
    stripped = re.sub(r"(?:^|[._\-\s:])\d+(?=$|[._\-\s:])", ".", stripped)
    stripped = re.sub(r"^(payload|body|data|request|response|api|tss)[._\-\s:]+", "", stripped, flags=re.I)
    parts = [part for part in re.split(r"[._\-\s:]+", stripped) if part]
    candidates: list[str] = []
    for index in range(len(parts)):
        candidates.append(".".join(parts[index:]))
    if parts:
        candidates.append(parts[-1])
    return candidates


def _target_for(raw_field: str) -> tuple[str, str] | None:
    clean = clean_cell(raw_field)
    if not clean:
        return None
    forced_table = None
    if re.search(r"(?:^|[._\-\s:])(?:prs[._\-\s:]*)?consignments?(?:[._\-\s:]|$)", clean, flags=re.I):
        forced_table = "PRS.Consignment"
    elif re.search(r"(?:^|[._\-\s:])(?:prs[._\-\s:]*)?(?:goods_items?|goodsitems?|items?)(?:[._\-\s:\[]|$)", clean, flags=re.I):
        forced_table = "PRS.Goods_Item"

    candidates = [clean]
    candidates.append(re.sub(r"^PRS[._\-\s:]", "", clean, flags=re.I))
    candidates.append(_PREFIX_RE.sub("", clean))
    candidates.append(re.sub(r"\[[0-9]+\]", "", clean))
    candidates.append(re.sub(r"[._\-\s]+[0-9]+$", "", clean))
    candidates.append(_target_tail(clean))
    candidates.extend(_path_candidates(clean))

    if forced_table:
        for candidate in candidates:
            candidate_key = normalise(candidate)
            for column in TARGET_FIELDS.get(forced_table, set()):
                if normalise(column) == candidate_key:
                    return forced_table, column

    lookup = target_lookup()
    for candidate in candidates:
        target = lookup.get(normalise(candidate))
        if target:
            return target
    return None


def _goods_index(raw_field: str, default: int = 1) -> int:
    for pattern in (_GOODS_INDEX_RE, _TRAILING_INDEX_RE):
        match = pattern.search(raw_field or "")
        if match:
            try:
                return max(1, int(match.group(1)))
            except ValueError:
                return default
    return default


def _assign_first(values: dict[str, Any], sources: dict[str, dict[str, Any]], field: str, value: Any, source: dict[str, Any]) -> bool:
    if compact(value) is None:
        return False
    if compact(values.get(field)) is not None:
        return False
    values[field] = clean_cell(value)
    sources[field] = source
    return True


def _compact_payload(values: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in fields:
        value = compact(values.get(field))
        if value is not None:
            payload[field] = value
    return payload


def _tss_payload_preview(values: dict[str, Any], goods_payload: list[dict[str, Any]], consignment_status: str) -> dict[str, Any]:
    ens_value = compact(values.get("declaration_number"))
    consignment_number = compact(values.get("consignment_number"))
    update_payload = {"op_type": "update", **_compact_payload(values, CONSIGNMENT_DISPLAY_FIELDS)}
    if ens_value:
        update_payload["declaration_number"] = ens_value
    submit_payload = {
        "op_type": "submit",
        "declaration_number": ens_value,
        "consignment_number": consignment_number,
    }
    submit_payload = {key: value for key, value in submit_payload.items() if compact(value) is not None}
    goods_items = [
        {
            "ordinal": item.get("ordinal"),
            "status": item.get("status"),
            **_compact_payload(item.get("values") or {}, GOODS_DISPLAY_FIELDS),
        }
        for item in goods_payload
    ]
    return {
        "mode": "preview_only",
        "databaseWrite": False,
        "tssWrite": False,
        "ready": consignment_status == "READY" and bool(goods_payload) and not any((item.get("status") != "READY") for item in goods_payload),
        "operations": [
            {
                "operationCode": "UPDATE_CONSIGNMENT_WITH_ENS",
                "payload": update_payload,
            },
            {
                "operationCode": "SUBMIT_CONSIGNMENT",
                "payload": submit_payload,
            },
        ],
        "goodsItems": goods_items,
        "goodsItemCount": len(goods_items),
    }


def _numeric(value: Any) -> tuple[bool, float | None]:
    text = clean_cell(value).replace(",", "")
    if not text:
        return False, None
    try:
        return True, float(text)
    except ValueError:
        return False, None


def _field_issue(scope: str, field: str, value: Any, required: bool) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if compact(value) is None:
        if required:
            issues.append({"severity": "error", "message": f"{FIELD_LABELS.get(field, field)} is required before TSS processing."})
        elif field == "net_mass_kg":
            issues.append({"severity": "warning", "message": "Net mass is blank; confirm fallback before live processing."})
        return issues
    if field in {"gross_mass_kg", "net_mass_kg", "item_invoice_amount"}:
        ok, number = _numeric(value)
        if not ok:
            issues.append({"severity": "error", "message": f"{FIELD_LABELS.get(field, field)} must be numeric."})
        elif field == "gross_mass_kg" and (number or 0) <= 0:
            issues.append({"severity": "error", "message": "Gross mass must be greater than zero."})
        elif field == "net_mass_kg" and (number or 0) < 0:
            issues.append({"severity": "error", "message": "Net mass cannot be negative."})
    if scope == "goods" and field == "number_of_packages":
        ok, number = _numeric(value)
        if not ok:
            issues.append({"severity": "error", "message": "Number of packages must be numeric."})
        elif (number or 0) <= 0:
            issues.append({"severity": "error", "message": "Number of packages must be greater than zero."})
    return issues


def _fields_payload(
    *,
    scope: str,
    values: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    display_fields: tuple[str, ...],
    required_fields: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    fields: list[dict[str, Any]] = []
    missing_required: list[str] = []
    issues: list[dict[str, Any]] = []
    for field in display_fields:
        value = values.get(field)
        required = field in required_fields
        field_issues = _field_issue(scope, field, value, required)
        if required and compact(value) is None:
            missing_required.append(field)
        for issue in field_issues:
            issues.append({"field": field, "label": FIELD_LABELS.get(field, field), **issue})
        fields.append({
            "field": field,
            "label": FIELD_LABELS.get(field, field),
            "value": compact(value),
            "required": required,
            "missing": compact(value) is None,
            "source": sources.get(field),
            "issues": field_issues,
        })
    return fields, missing_required, issues


def _field_value_preview(rows: list[dict[str, Any]], demo_ens: dict[str, Any] | None, *, source_sheet: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    consignment: dict[str, Any] = {}
    consignment_sources: dict[str, dict[str, Any]] = {}
    goods_by_index: dict[int, dict[str, Any]] = {}
    goods_sources: dict[int, dict[str, dict[str, Any]]] = {}
    unmatched: list[dict[str, Any]] = []
    matched = 0

    for row_number, row in enumerate(rows, 1):
        api_field = _row_value(row, FIELD_NAME_COLUMNS)
        source_value = _row_value(row, FIELD_VALUE_COLUMNS)
        target = _target_for(api_field)
        if not target:
            unmatched.append({"rowNumber": row_number, "apiField": api_field, "sourceValue": source_value, "reason": "No safe PRS/TSS target match.", **({"sourceSheet": source_sheet} if source_sheet else {})})
            continue
        table_name, target_column = target
        source = {"rowNumber": row_number, "apiField": api_field, "sourceColumn": "source_value", **({"sourceSheet": source_sheet} if source_sheet else {})}
        matched += 1
        if table_name == "PRS.Consignment":
            _assign_first(consignment, consignment_sources, target_column, source_value, source)
        elif table_name == "PRS.Goods_Item":
            index = _goods_index(api_field, 1)
            goods = goods_by_index.setdefault(index, {})
            sources = goods_sources.setdefault(index, {})
            _assign_first(goods, sources, target_column, source_value, source)

    goods_items = []
    for index in sorted(goods_by_index):
        goods = goods_by_index[index]
        goods.setdefault("goods_id", str(index))
        goods_items.append({"values": goods, "sources": goods_sources.get(index, {}), "sourceRowNumber": index})
    if not goods_items and any(compact(consignment.get(field)) is not None for field in TARGET_FIELDS.get("PRS.Goods_Item", [])):
        goods_items.append({"values": {}, "sources": {}, "sourceRowNumber": 1})
    return [{"values": consignment, "sources": consignment_sources, "goods": goods_items}], unmatched, matched


def _wide_row_preview(rows: list[dict[str, Any]], *, source_sheet: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    lookup = target_lookup()
    groups: dict[str, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []
    matched = 0

    for row_number, row in enumerate(rows, 1):
        consignment: dict[str, Any] = {}
        consignment_sources: dict[str, dict[str, Any]] = {}
        goods: dict[str, Any] = {}
        goods_sources: dict[str, dict[str, Any]] = {}
        row_matched = 0
        for source_column, raw_value in row.items():
            value = clean_cell(raw_value)
            if not value:
                continue
            target = lookup.get(normalise(source_column))
            if not target:
                continue
            table_name, target_column = target
            source = {"rowNumber": row_number, "sourceColumn": source_column, **({"sourceSheet": source_sheet} if source_sheet else {})}
            matched += 1
            row_matched += 1
            if table_name == "PRS.Consignment":
                _assign_first(consignment, consignment_sources, target_column, value, source)
            elif table_name == "PRS.Goods_Item":
                _assign_first(goods, goods_sources, target_column, value, source)
        if row_matched == 0:
            unmatched.append({"rowNumber": row_number, "reason": "No columns in this row matched PRS/TSS targets.", **({"sourceSheet": source_sheet} if source_sheet else {})})
            continue
        group_key = (
            clean_cell(consignment.get("consignment_number"))
            or clean_cell(consignment.get("transport_document_number"))
            or clean_cell(consignment.get("trader_reference"))
            or "__single__"
        )
        group = groups.setdefault(group_key, {"values": {}, "sources": {}, "goods": [], "groupKey": group_key})
        for field, value in consignment.items():
            _assign_first(group["values"], group["sources"], field, value, consignment_sources.get(field, {"rowNumber": row_number}))
        if goods:
            group["goods"].append({"values": goods, "sources": goods_sources, "sourceRowNumber": row_number})

    return list(groups.values()), unmatched, matched


def _split_groups(groups: list[dict[str, Any]], demo_ens: dict[str, Any] | None) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for group_index, group in enumerate(groups, 1):
        base_values = dict(group.get("values") or {})
        base_sources = dict(group.get("sources") or {})
        if demo_ens and compact(base_values.get("declaration_number")) is None:
            base_values["declaration_number"] = demo_ens.get("declaration_number") or demo_ens.get("declarationNumber")
            base_sources["declaration_number"] = {"source": "demoEns"}
        goods_items = list(group.get("goods") or [])
        if compact(base_values.get("goods_description")) is None and goods_items:
            first_description = goods_items[0].get("values", {}).get("goods_description")
            if compact(first_description) is not None:
                base_values["goods_description"] = first_description
                base_sources["goods_description"] = goods_items[0].get("sources", {}).get("goods_description", {"source": "firstGoodsItem"})
        chunks = [goods_items[index:index + MAX_GOODS_PER_CONSIGNMENT] for index in range(0, len(goods_items), MAX_GOODS_PER_CONSIGNMENT)] or [[]]
        part_count = len(chunks)
        group_key = clean_cell(group.get("groupKey"))
        original_number = clean_cell(base_values.get("consignment_number")) or (group_key if group_key and group_key != "__single__" else f"PREVIEW-{group_index:03d}")
        for part_index, chunk in enumerate(chunks, 1):
            values = dict(base_values)
            sources = dict(base_sources)
            if part_count > 1:
                values["original_consignment_number"] = original_number
                values["consignment_number"] = f"{original_number}-{part_index:02d}"
                sources["consignment_number"] = {"source": "splitRule", "originalValue": original_number}
            elif compact(values.get("consignment_number")) is None:
                values["consignment_number"] = original_number
                sources["consignment_number"] = {"source": "previewGenerated"}
            output.append({
                "values": values,
                "sources": sources,
                "goods": chunk,
                "split": {
                    "isSplit": part_count > 1,
                    "part": part_index,
                    "partCount": part_count,
                    "maxGoodsPerConsignment": MAX_GOODS_PER_CONSIGNMENT,
                    "originalConsignmentNumber": original_number,
                },
            })
    return output


def _worksheet_structures(structure: dict[str, Any]) -> list[dict[str, Any]]:
    worksheets = structure.get("worksheets")
    if isinstance(worksheets, list) and worksheets:
        return [worksheet for worksheet in worksheets if isinstance(worksheet, dict)]
    return [structure]


def _group_goods_count(groups: list[dict[str, Any]]) -> int:
    return sum(len(group.get("goods") or []) for group in groups)


def _merge_default_groups(data_groups: list[dict[str, Any]], default_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not data_groups:
        return default_groups
    for group in data_groups:
        for default in default_groups:
            default_values = default.get("values") or {}
            default_sources = default.get("sources") or {}
            for field, value in default_values.items():
                _assign_first(group["values"], group["sources"], field, value, default_sources.get(field, {"source": "workbookDefault"}))
            if not group.get("goods") and default.get("goods"):
                group["goods"].extend(default.get("goods") or [])
    return data_groups


def _groups_from_structure(structure: dict[str, Any], demo_ens: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int, str, list[dict[str, Any]]]:
    data_groups: list[dict[str, Any]] = []
    default_groups: list[dict[str, Any]] = []
    all_unmatched: list[dict[str, Any]] = []
    matched_total = 0
    source_rows_total = 0
    source_sheets: list[dict[str, Any]] = []
    row_modes: list[str] = []

    for worksheet in _worksheet_structures(structure):
        rows = _rows_from_structure(worksheet)
        columns = worksheet.get("columns") or []
        sheet_name = clean_cell(worksheet.get("sheetName")) or None
        row_mode = "api_field_value" if _is_field_value_mode(rows, columns) else "wide_rows"
        if row_mode == "api_field_value":
            groups, unmatched, matched_count = _field_value_preview(rows, demo_ens, source_sheet=sheet_name)
            default_groups.extend(groups)
        else:
            groups, unmatched, matched_count = _wide_row_preview(rows, source_sheet=sheet_name)
            data_groups.extend(groups)

        all_unmatched.extend(unmatched)
        matched_total += matched_count
        source_rows_total += len(rows)
        if rows or columns:
            row_modes.append(row_mode)
            source_sheets.append({
                "sheetName": sheet_name,
                "rowMode": row_mode,
                "sourceRows": len(rows),
                "detectedColumns": len(columns),
                "mappedFieldCount": matched_count,
                "unmatchedFieldCount": len(unmatched),
                "consignmentGroupCount": len(groups),
                "goodsItemCount": _group_goods_count(groups),
            })

    groups = _merge_default_groups(data_groups, default_groups)
    row_mode = "multi_sheet" if len(source_sheets) > 1 else (row_modes[0] if row_modes else "wide_rows")
    return groups, all_unmatched, matched_total, source_rows_total, row_mode, source_sheets


def build_processing_preview(*, profile: dict[str, Any], structure: dict[str, Any], demo_ens: dict[str, Any] | None) -> dict[str, Any]:
    groups, unmatched, matched_count, source_row_count, row_mode, source_sheets = _groups_from_structure(structure, demo_ens)

    split_groups = _split_groups(groups, demo_ens)
    consignments: list[dict[str, Any]] = []
    total_goods = 0
    issue_count = 0
    missing_count = 0
    split_count = 0

    for index, group in enumerate(split_groups, 1):
        values = group["values"]
        sources = group["sources"]
        cons_fields, cons_missing, cons_issues = _fields_payload(
            scope="consignment",
            values=values,
            sources=sources,
            display_fields=CONSIGNMENT_DISPLAY_FIELDS,
            required_fields=CONSIGNMENT_REQUIRED_FIELDS,
        )
        goods_payload: list[dict[str, Any]] = []
        goods_has_blockers = False
        for goods_index, raw_goods in enumerate(group.get("goods") or [], 1):
            goods_values = dict(raw_goods.get("values") or {})
            goods_sources = dict(raw_goods.get("sources") or {})
            goods_values.setdefault("goods_id", str(goods_index))
            goods_fields, goods_missing, goods_issues = _fields_payload(
                scope="goods",
                values=goods_values,
                sources=goods_sources,
                display_fields=GOODS_DISPLAY_FIELDS,
                required_fields=GOODS_REQUIRED_FIELDS,
            )
            goods_status = "READY" if not goods_missing and not any(issue.get("severity") == "error" for issue in goods_issues) else "NEEDS_REVIEW"
            if goods_status != "READY":
                goods_has_blockers = True
            goods_payload.append({
                "ordinal": goods_index,
                "sourceRowNumber": raw_goods.get("sourceRowNumber"),
                "values": goods_values,
                "fields": goods_fields,
                "missingRequired": goods_missing,
                "issues": goods_issues,
                "status": goods_status,
            })
            issue_count += len(goods_issues)
            missing_count += len(goods_missing)
        if group["split"].get("isSplit"):
            split_count += 1
        total_goods += len(goods_payload)
        issue_count += len(cons_issues)
        missing_count += len(cons_missing)
        consignment_status = "READY" if not cons_missing and not any(issue.get("severity") == "error" for issue in cons_issues) and goods_payload and not goods_has_blockers else "NEEDS_REVIEW"
        consignments.append({
            "previewId": f"PREVIEW-{index:03d}",
            "ordinal": index,
            "status": consignment_status,
            "values": values,
            "fields": cons_fields,
            "missingRequired": cons_missing,
            "issues": cons_issues,
            "goodsItems": goods_payload,
            "goodsItemCount": len(goods_payload),
            "tssPayloadPreview": _tss_payload_preview(values, goods_payload, consignment_status),
            "split": group["split"],
        })

    return {
        "clientCode": profile.get("clientCode"),
        "portalClientCode": profile.get("portalClientCode"),
        "rowMode": row_mode,
        "sourceSheets": source_sheets,
        "maxGoodsPerConsignment": MAX_GOODS_PER_CONSIGNMENT,
        "summary": {
            "sourceRows": source_row_count,
            "mappedFieldCount": matched_count,
            "unmatchedFieldCount": len(unmatched),
            "consignmentCount": len(consignments),
            "goodsItemCount": total_goods,
            "splitConsignmentCount": split_count,
            "issueCount": issue_count,
            "missingRequiredCount": missing_count,
            "databaseWrite": False,
            "tssWrite": False,
        },
        "consignments": consignments,
        "unmatchedRows": unmatched[:50],
        "isTruncated": bool(structure.get("isTruncated")),
        "fieldCatalog": {
            "consignment": [{"field": field, "label": FIELD_LABELS.get(field, field), "required": field in CONSIGNMENT_REQUIRED_FIELDS} for field in CONSIGNMENT_DISPLAY_FIELDS],
            "goodsItem": [{"field": field, "label": FIELD_LABELS.get(field, field), "required": field in GOODS_REQUIRED_FIELDS, "attention": field in GOODS_ATTENTION_FIELDS} for field in GOODS_DISPLAY_FIELDS],
        },
        "notes": [
            "Preview only: no ING, PRS, STG, API, DB, or TSS write is performed.",
            f"Consignments are split every {MAX_GOODS_PER_CONSIGNMENT} goods rows for TSS readiness.",
        ],
    }
