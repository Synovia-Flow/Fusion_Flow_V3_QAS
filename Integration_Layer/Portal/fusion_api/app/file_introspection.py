from __future__ import annotations

import csv
import re
import zipfile
from io import BytesIO, StringIO
from typing import Any
from xml.etree import ElementTree as ET

MAX_DATA_ROWS = 1000
MAX_SCAN_ROWS = MAX_DATA_ROWS + 20
MAX_SAMPLE_ROWS = 5

CELL_REF_RE = re.compile(r"([A-Z]+)")


def column_index(cell_ref: str) -> int:
    match = CELL_REF_RE.match(cell_ref or "")
    if not match:
        return 0
    value = 0
    for char in match.group(1):
        value = value * 26 + (ord(char) - ord("A") + 1)
    return max(0, value - 1)


def clean_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def unique_headers(row: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    headers: list[str] = []
    for index, raw in enumerate(row, 1):
        header = clean_cell(raw) or f"Column {index}"
        key = header.lower()
        seen[key] = seen.get(key, 0) + 1
        headers.append(header if seen[key] == 1 else f"{header} ({seen[key]})")
    return headers


def choose_header_row(rows: list[list[str]]) -> tuple[int | None, list[str]]:
    for index, row in enumerate(rows, 1):
        compact = [clean_cell(value) for value in row]
        non_empty = [value for value in compact if value]
        if len(non_empty) >= 2:
            while compact and not compact[-1]:
                compact.pop()
            return index, unique_headers(compact)
    return None, []


def records_from_rows(rows: list[list[str]], header_row_number: int | None, headers: list[str], limit: int) -> list[dict[str, str]]:
    if not header_row_number or not headers:
        return []
    out: list[dict[str, str]] = []
    for row in rows[header_row_number: header_row_number + limit]:
        record = {header: clean_cell(row[index] if index < len(row) else "") for index, header in enumerate(headers)}
        if any(record.values()):
            out.append(record)
    return out


def sample_rows(rows: list[list[str]], header_row_number: int | None, headers: list[str]) -> list[dict[str, str]]:
    return records_from_rows(rows, header_row_number, headers, MAX_SAMPLE_ROWS)


def data_rows(rows: list[list[str]], header_row_number: int | None, headers: list[str]) -> list[dict[str, str]]:
    return records_from_rows(rows, header_row_number, headers, MAX_DATA_ROWS)


def empty_structure(format_name: str, warning: str, *, sheet_name: str | None = None, sheet_names: list[str] | None = None) -> dict[str, Any]:
    return {
        "format": format_name,
        "sheetName": sheet_name,
        "sheetNames": sheet_names or [],
        "headerRowNumber": None,
        "columns": [],
        "sampleRows": [],
        "dataRows": [],
        "dataRowCount": 0,
        "isTruncated": False,
        "warning": warning,
    }


def structure_from_rows(format_name: str, rows: list[list[str]], *, sheet_name: str | None = None, sheet_names: list[str] | None = None) -> dict[str, Any]:
    header_row_number, headers = choose_header_row(rows)
    all_data_rows = data_rows(rows, header_row_number, headers)
    return {
        "format": format_name,
        "sheetName": sheet_name,
        "sheetNames": sheet_names or [],
        "headerRowNumber": header_row_number,
        "columns": [{"ordinal": index + 1, "name": name} for index, name in enumerate(headers)],
        "sampleRows": sample_rows(rows, header_row_number, headers),
        "dataRows": all_data_rows,
        "dataRowCount": len(all_data_rows),
        "isTruncated": len(rows) >= MAX_SCAN_ROWS,
        "warning": None,
    }


def inspect_csv(content: bytes) -> dict[str, Any]:
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096])
    except csv.Error:
        dialect = csv.excel
    rows = [[clean_cell(cell) for cell in row] for row in csv.reader(StringIO(text), dialect)]
    return structure_from_rows("csv", rows[:MAX_SCAN_ROWS])


def xml_root(zip_file: zipfile.ZipFile, path: str) -> ET.Element:
    return ET.fromstring(zip_file.read(path))


def read_shared_strings(zip_file: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zip_file.namelist():
        return []
    root = xml_root(zip_file, "xl/sharedStrings.xml")
    values: list[str] = []
    for si in root.iter():
        if si.tag.endswith("}si") or si.tag == "si":
            text_parts = [node.text or "" for node in si.iter() if node.tag.endswith("}t") or node.tag == "t"]
            values.append(clean_cell("".join(text_parts)))
    return values


def workbook_sheets(zip_file: zipfile.ZipFile) -> list[dict[str, str]]:
    workbook = xml_root(zip_file, "xl/workbook.xml")
    rels = xml_root(zip_file, "xl/_rels/workbook.xml.rels")
    rel_map: dict[str, str] = {}
    for rel in rels:
        rel_id = rel.attrib.get("Id")
        target = rel.attrib.get("Target", "")
        if rel_id and target:
            target_path = target.lstrip("/")
            rel_map[rel_id] = target_path if target_path.startswith("xl/") else "xl/" + target_path
    sheets: list[dict[str, str]] = []
    for sheet in workbook.iter():
        if not (sheet.tag.endswith("}sheet") or sheet.tag == "sheet"):
            continue
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        path = rel_map.get(rel_id or "")
        if path:
            sheets.append({"name": sheet.attrib.get("name", "Sheet"), "path": path})
    return sheets


def cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return clean_cell("".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t") or node.tag == "t"))
    value_node = next((child for child in cell if child.tag.endswith("}v") or child.tag == "v"), None)
    value = value_node.text if value_node is not None else ""
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (ValueError, IndexError):
            return ""
    return clean_cell(value)


def inspect_xlsx(content: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(BytesIO(content)) as zip_file:
        shared_strings = read_shared_strings(zip_file)
        sheets = workbook_sheets(zip_file)
        if not sheets:
            return empty_structure("xlsx", "No worksheets found in workbook.")
        sheet_names = [sheet["name"] for sheet in sheets]
        worksheet_structures: list[dict[str, Any]] = []
        for sheet in sheets:
            sheet_root = xml_root(zip_file, sheet["path"])
            rows: list[list[str]] = []
            for row_node in sheet_root.iter():
                if not (row_node.tag.endswith("}row") or row_node.tag == "row"):
                    continue
                values: list[str] = []
                for cell in row_node:
                    if not (cell.tag.endswith("}c") or cell.tag == "c"):
                        continue
                    index = column_index(cell.attrib.get("r", ""))
                    while len(values) <= index:
                        values.append("")
                    values[index] = cell_value(cell, shared_strings)
                rows.append(values)
                if len(rows) >= MAX_SCAN_ROWS:
                    break
            worksheet_structures.append(structure_from_rows("xlsx", rows, sheet_name=sheet["name"], sheet_names=sheet_names))
        selected_structure = dict(worksheet_structures[0])
        selected_structure["worksheets"] = worksheet_structures
        return selected_structure


def inspect_upload(filename: str | None, content: bytes) -> dict[str, Any]:
    suffix = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    try:
        if suffix == "csv":
            return inspect_csv(content)
        if suffix == "xlsx":
            return inspect_xlsx(content)
        if suffix == "xls":
            return empty_structure("xls", "Legacy .xls header introspection is not supported yet; upload .xlsx or .csv for automatic mapping preview.")
        return empty_structure(suffix or "unknown", "Unsupported file extension for header introspection.")
    except (ET.ParseError, zipfile.BadZipFile, OSError, UnicodeError, csv.Error) as error:
        return empty_structure(suffix or "unknown", f"Could not inspect file headers: {error}")


def summarise_mapping(columns: list[dict[str, Any]], mappings: list[dict[str, Any]]) -> dict[str, Any]:
    detected = [clean_cell(column.get("name")) for column in columns if clean_cell(column.get("name"))]
    mapped_sources = {clean_cell(row.get("SourceColumn")).lower(): row for row in mappings if row.get("IsActive", True)}
    mapped = [name for name in detected if name.lower() in mapped_sources]
    missing = [name for name in detected if name.lower() not in mapped_sources]
    return {
        "configuredMappings": len(mapped_sources),
        "detectedColumns": len(detected),
        "mappedColumns": len(mapped),
        "missingMappings": missing,
        "status": "MAPPED" if detected and not missing and mapped_sources else "AWAITING_COLUMN_MAP",
    }
