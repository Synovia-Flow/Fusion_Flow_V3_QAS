from __future__ import annotations

import base64
import csv
import json
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO, StringIO
from typing import Any
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Modules.Processing.preview_enrichment import normalise_package_type

MAX_DATA_ROWS = 1000
MAX_SCAN_ROWS = MAX_DATA_ROWS + 20
MAX_SAMPLE_ROWS = 5
ASSUMPTION_META_KEY = "_assumption_sources"

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



PDF_OUTPUT_COLUMNS = [
    "consignment_number",
    "goods_description",
    "trader_reference",
    "transport_document_number",
    "destination_country",
    "consignor_eori",
    "consignor_name",
    "consignor_street_number",
    "consignor_city",
    "consignor_postcode",
    "consignor_country",
    "consignee_eori",
    "consignee_name",
    "consignee_street_number",
    "consignee_city",
    "consignee_postcode",
    "consignee_country",
    "importer_eori",
    "importer_name",
    "importer_street_number",
    "importer_city",
    "importer_postcode",
    "importer_country",
    "exporter_eori",
    "exporter_name",
    "exporter_street_number",
    "exporter_city",
    "exporter_postcode",
    "exporter_country",
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
]

EORI_RE = re.compile(r"\b(?:(?:GB|XI)\d{12}[A-Z0-9]{0,3}|IE[A-Z0-9]{8,12})\b", re.I)
POSTCODE_RE = re.compile(r"\b(?:[A-Z]{1,2}\d[A-Z\d]?\s*[0-9O][A-Z]{2}|[A-Z]\d{2}\s?[A-Z0-9]{4})\b", re.I)
COMMODITY_RE = re.compile(r"(?<!\d)\d{8,10}(?!\d)")
MONEY_RE = re.compile(r"(?:(?:\u00c2\u00a3|[\u00a3\u0141])\s*|GBP\s*)?([0-9][0-9,]*\.\d{2})(?:\s*GBP)?", re.I)
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
COUNTY_HINT_RE = re.compile(r"^(?:West Sussex|East Sussex|Cambridgeshire|Leicestershire|Hertfordshire|Essex|Kent|Surrey|Lancashire|Yorkshire|Middlesex)$", re.I)
PDF_REVERSED_METRIC_RE = re.compile(
    r"^(?P<amount>\d+\.\d{2})0%(?P<unit_price>\d+\.\d{2})(?P<weight>\d+\.\d{3})(?P<quantity>\d+\.\d{2})(?P<trailing>.+)$",
    re.I,
)

COUNTRY_ALIASES = [
    ("United Kingdom", "GB"),
    ("Great Britain", "GB"),
    ("Northern Ireland", "GB"),
    ("United States", "US"),
    ("Hong Kong", "HK"),
    ("Ireland", "IE"),
    ("India", "IN"),
    ("China", "CN"),
    ("UK", "GB"),
    ("GB", "GB"),
]


def structure_from_records(format_name: str, records: list[dict[str, str]], columns: list[str], *, warning: str | None = None) -> dict[str, Any]:
    active_columns = [column for column in columns if any(clean_cell(record.get(column)) for record in records)]
    if not active_columns:
        active_columns = columns[:]
    rows = [active_columns]
    for record in records:
        rows.append([record.get(column, "") for column in active_columns])
    structure = structure_from_rows(format_name, rows)
    limited_records = records[:MAX_DATA_ROWS]
    structure["sampleRows"] = limited_records[:MAX_SAMPLE_ROWS]
    structure["dataRows"] = limited_records
    structure["dataRowCount"] = len(limited_records)
    structure["isTruncated"] = len(records) > MAX_DATA_ROWS
    structure["warning"] = warning
    return structure


def _mark_assumption(record: dict[str, Any], field: str, reason: str) -> None:
    record.setdefault(ASSUMPTION_META_KEY, {})[field] = {
        "source": "assumption",
        "label": "ASSUMPTION",
        "assumption": True,
        "reason": reason,
    }


def _merge_assumptions(*records: dict[str, Any]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        assumptions = record.get(ASSUMPTION_META_KEY) or {}
        if isinstance(assumptions, dict):
            merged.update(assumptions)
    return merged


def extract_pdf_text_pages(content: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError("PDF text extraction requires pypdf in the backend environment.") from exc
    reader = PdfReader(BytesIO(content))
    return [page.extract_text() or "" for page in reader.pages]


PDF_METADATA_SUMMARY_KEYS = ("Title", "Author", "Subject", "Producer", "Creator", "CreationDate", "ModDate")

PDF_REFERENCE_PATTERNS = (
    ("ENS", re.compile(r"\bENS\d{9,18}\b", re.I)),
    ("SUP", re.compile(r"\bSUP\d{9,18}\b", re.I)),
    ("DEC", re.compile(r"\bDEC\d{9,18}\b", re.I)),
    ("SALES_ORDER", re.compile(r"\bS-?ORD\d{4,}\b", re.I)),
    ("COMMERCIAL_INVOICE", re.compile(r"\bCI-\d{6,}(?:\s+\d+)?\b", re.I)),
)


def extract_pdf_metadata(content: bytes) -> dict[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError("PDF metadata extraction requires pypdf in the backend environment.") from exc
    try:
        reader = PdfReader(BytesIO(content))
        metadata = reader.metadata or {}
    except Exception:
        return {}
    return {str(key).lstrip("/"): clean_cell(value) for key, value in metadata.items() if clean_cell(value)}


def _pdf_metadata_summary(metadata: dict[str, str] | None) -> dict[str, str]:
    metadata = metadata or {}
    summary: dict[str, str] = {}
    for key in PDF_METADATA_SUMMARY_KEYS:
        value = clean_cell(metadata.get(key))
        if value:
            summary[key] = value[:500]
    return summary

def _pdf_reference_source(value: str, filename: str, metadata: dict[str, str]) -> str:
    if value and re.search(re.escape(value), filename or "", re.I):
        return "filename"
    for key, meta_value in metadata.items():
        if value and re.search(re.escape(value), meta_value or "", re.I):
            return f"metadata.{key}"
    return "pdf"


def _pdf_detected_references(filename: str | None, metadata: dict[str, str]) -> list[dict[str, str]]:
    haystack = " ".join([filename or "", *metadata.values()])
    references: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref_type, pattern in PDF_REFERENCE_PATTERNS:
        for match in pattern.finditer(haystack):
            value = match.group(0).upper().replace("SORD", "S-ORD")
            key = (ref_type, value)
            if key in seen:
                continue
            seen.add(key)
            references.append({"type": ref_type, "value": value, "source": _pdf_reference_source(match.group(0), filename or "", metadata)})
    return references


def _pdf_empty_structure(
    warning: str,
    *,
    filename: str | None,
    pages: list[str] | None = None,
    text: str = "",
    metadata: dict[str, str] | None = None,
    requires_ocr: bool = False,
    ocr_configured: bool | None = None,
    ocr_attempted: bool = False,
    ocr_used: bool = False,
    ocr_error: str = "",
) -> dict[str, Any]:
    metadata = metadata or {}
    structure = empty_structure("pdf", warning)
    structure["pdfPageCount"] = len(pages or []) if pages is not None else None
    structure["pdfTextLength"] = len(text or "")
    structure["pdfRequiresOcr"] = requires_ocr
    structure["pdfOcrConfigured"] = pdf_ocr_configured() if ocr_configured is None else ocr_configured
    structure["pdfOcrAttempted"] = ocr_attempted
    structure["pdfOcrUsed"] = ocr_used
    if ocr_error:
        structure["pdfOcrError"] = clean_cell(ocr_error)[:500]
    references = _pdf_detected_references(filename, metadata)
    if references:
        structure["pdfDetectedReferences"] = references
    metadata_summary = _pdf_metadata_summary(metadata)
    if metadata_summary:
        structure["pdfMetadata"] = metadata_summary
    if metadata.get("Title"):
        structure["pdfTitle"] = metadata["Title"]
    return structure


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _azure_document_intelligence_config() -> dict[str, str] | None:
    endpoint = (os.getenv("PDF_OCR_AZURE_ENDPOINT") or os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT") or "").strip().rstrip("/")
    key = (os.getenv("PDF_OCR_AZURE_KEY") or os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY") or "").strip()
    enabled = _env_flag("PDF_OCR_ENABLED", bool(endpoint and key))
    if not enabled or not endpoint or not key:
        return None
    return {
        "endpoint": endpoint,
        "key": key,
        "model_id": (os.getenv("PDF_OCR_AZURE_MODEL_ID") or "prebuilt-layout").strip(),
        "api_version": (os.getenv("PDF_OCR_AZURE_API_VERSION") or "2024-11-30").strip(),
        "features": (os.getenv("PDF_OCR_AZURE_FEATURES") or "").strip(),
    }


def pdf_ocr_configured() -> bool:
    return _azure_document_intelligence_config() is not None


def _document_intelligence_pages(result: dict[str, Any]) -> list[str]:
    analyze_result = result.get("analyzeResult") or {}
    pages = []
    for page in analyze_result.get("pages") or []:
        lines = [_one_line(line.get("content")) for line in page.get("lines") or [] if _one_line(line.get("content"))]
        pages.append("\n".join(lines))
    if any(_one_line(page) for page in pages):
        return pages
    content = _one_line(analyze_result.get("content"))
    return [content] if content else []


def _read_error_body(error: urllib.error.HTTPError) -> str:
    try:
        return clean_cell(error.read(1200).decode("utf-8", errors="replace"))
    except Exception:
        return ""


def extract_pdf_ocr_text_pages(content: bytes) -> list[str]:
    config = _azure_document_intelligence_config()
    if not config:
        return []

    params = {
        "_overload": "analyzeDocument",
        "api-version": config["api_version"],
        "stringIndexType": "unicodeCodePoint",
    }
    if config.get("features"):
        params["features"] = config["features"]
    model_id = urllib.parse.quote(config["model_id"], safe="")
    analyze_url = f"{config['endpoint']}/documentintelligence/documentModels/{model_id}:analyze?{urllib.parse.urlencode(params)}"
    body = json.dumps({"base64Source": base64.b64encode(content).decode("ascii")}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Ocp-Apim-Subscription-Key": config["key"],
    }
    request = urllib.request.Request(analyze_url, data=body, headers=headers, method="POST")
    request_timeout = _env_int("PDF_OCR_REQUEST_TIMEOUT_SECONDS", 30, minimum=5, maximum=120)
    poll_timeout = _env_int("PDF_OCR_POLL_TIMEOUT_SECONDS", 90, minimum=10, maximum=600)
    poll_interval = _env_int("PDF_OCR_POLL_INTERVAL_SECONDS", 2, minimum=1, maximum=15)
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            operation_url = response.headers.get("Operation-Location")
            retry_after = response.headers.get("Retry-After")
    except urllib.error.HTTPError as error:
        detail = _read_error_body(error)
        raise RuntimeError(f"Azure Document Intelligence OCR request failed with HTTP {error.code}. {detail}".strip()) from error
    if not operation_url:
        raise RuntimeError("Azure Document Intelligence OCR did not return an Operation-Location header.")
    try:
        poll_interval = min(max(int(retry_after or poll_interval), 1), 15)
    except ValueError:
        pass

    deadline = time.monotonic() + poll_timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        poll_request = urllib.request.Request(operation_url, headers={"Ocp-Apim-Subscription-Key": config["key"]}, method="GET")
        try:
            with urllib.request.urlopen(poll_request, timeout=request_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = _read_error_body(error)
            raise RuntimeError(f"Azure Document Intelligence OCR poll failed with HTTP {error.code}. {detail}".strip()) from error
        status = str(payload.get("status") or "").lower()
        if status == "succeeded":
            return _document_intelligence_pages(payload)
        if status == "failed":
            error = payload.get("error") or {}
            raise RuntimeError(f"Azure Document Intelligence OCR failed: {clean_cell(error.get('message') or error.get('code') or 'unknown error')}")
    raise TimeoutError("Azure Document Intelligence OCR timed out before returning a result.")

def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", clean_cell(value)).strip(" ,")


def _first_match(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            return _one_line(match.group(1))
    return ""


def _normalize_eori(value: str) -> str:
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    gb_xi = re.search(r"(GB|XI)(\d{12})", text)
    if gb_xi:
        return f"{gb_xi.group(1)}{gb_xi.group(2)}"
    ie = re.search(r"IE[A-Z0-9]{8,12}", text)
    return ie.group(0).removesuffix("WWW") if ie else ""


def _all_eoris(text: str) -> list[str]:
    seen = set()
    values = []
    for match in EORI_RE.finditer(text or ""):
        value = _normalize_eori(match.group(0))
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def _eori_near(text: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        match = re.search(label + r"[^\n:]*:?\s*([^\n]{0,160})", text, re.I)
        if match:
            eoris = _all_eoris(match.group(0) + " " + match.group(1))
            if eoris:
                return eoris[0]
    return ""


def _generic_consignor_eori(text: str) -> str:
    for line in str(text or "").splitlines():
        if not re.search(r"\bEORI\b", line, re.I):
            continue
        if re.search(r"\b(?:Customer|Receiver|Consignee|Buyer)\b", line, re.I):
            continue
        eoris = _all_eoris(line)
        if eoris:
            return eoris[0]
    return ""


def _country_code(value: str) -> str:
    text = value or ""
    for name, code in COUNTRY_ALIASES:
        if re.search(r"\b" + re.escape(name) + r"\b", text, re.I):
            return code
    return ""


def _postcode(value: str) -> str:
    match = POSTCODE_RE.search(value or "")
    if not match:
        return ""
    postcode = re.sub(r"\s+", " ", match.group(0).upper()).strip()
    postcode = re.sub(r"\sO([A-Z]{2})$", r" 0\1", postcode)
    return postcode


def _postcode_key(value: str) -> str:
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return re.sub(r"([0-9A-Z])O([A-Z]{2})$", r"\g<1>0\2", text)


def _postcode_matches_line(postcode: str, line: str) -> bool:
    key = _postcode_key(postcode)
    if not key:
        return False
    return key in _postcode_key(line)


def _is_country_only_line(line: str) -> bool:
    clean = _one_line(line)
    if not clean:
        return False
    return bool(_country_code(clean)) and len(clean.split()) <= 3


def _city_before_postcode(lines: list[str], postcode: str) -> str:
    for index, line in enumerate(lines):
        if not _postcode_matches_line(postcode, line) or index <= 0:
            continue
        for candidate in reversed(lines[:index]):
            candidate = candidate.strip(" ,")
            if not candidate or COUNTY_HINT_RE.match(candidate) or _is_country_only_line(candidate):
                continue
            return candidate
    return ""

def _party_block(text: str, labels: tuple[str, ...], stops: tuple[str, ...]) -> str:
    stop_pattern = "|".join(re.escape(stop) for stop in stops)
    for label in labels:
        match = re.search(r"(?:^|\n)\s*" + label + r"\s*:?\s*(.*?)(?=\n\s*(?:" + stop_pattern + r")\b|$)", text, re.I | re.S)
        if match:
            block = match.group(1).strip()
            if block:
                return block
    return ""

def _normalize_party_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def _seller_name_from_context(text: str) -> str:
    hints = {
        "frisco.co.uk": "Frisco UK Sales Ltd",
        "zok.com": "ZOK International Group Ltd",
        "goodman-bros.com": "Goodman Bros",
    }
    lowered = str(text or "").lower()
    for domain, name in hints.items():
        if domain in lowered:
            return name
    return ""


def _seller_contact_address_fields(text: str, fallback_name: str = "", fallback_country: str = "GB") -> dict[str, str]:
    for raw_line in str(text or "").splitlines():
        line = _clean_party_line(raw_line)
        if not re.search(r"(?:\bRegistered Office\b|T:)", line, re.I):
            continue
        line = re.sub(r"^Registered Office\s*:\s*", "", line, flags=re.I).strip()
        line = re.sub(r"^T:\s*\+?[\d\s]+(?=[A-Za-z])", "", line, flags=re.I).strip()
        postcode = _postcode(line)
        if not postcode:
            continue
        country = _country_code(line) or fallback_country
        parts = [part.strip(" .") for part in line.split(",") if part.strip(" .")]
        if len(parts) < 2:
            continue
        city = ""
        for index, part in enumerate(parts):
            if _postcode_matches_line(postcode, part) and index > 0:
                city = parts[index - 1]
                break
        return {
            "name": fallback_name,
            "street_number": parts[0],
            "city": city,
            "postcode": postcode,
            "country": country,
        }
    return {}

def _top_party_block(text: str, expected_name: str, stops: tuple[str, ...]) -> str:
    first_line = next((_one_line(line) for line in str(text or "").splitlines() if _one_line(line)), "")
    if not expected_name or _normalize_party_name(first_line) != _normalize_party_name(expected_name):
        return ""
    stop_pattern = "|".join(re.escape(stop) for stop in stops)
    match = re.search(r"^\s*(.*?)(?=\n\s*(?:" + stop_pattern + r")\b|$)", text, re.I | re.S)
    return match.group(1).strip() if match else ""


def _clean_party_line(line: str) -> str:
    cleaned = _one_line(line)
    cleaned = re.sub(r"\bShipment\s+Detail\s*:.*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bInvoice\s+(?:No\.?|Date)\s*:.*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bShipping\s+Company\s*:.*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:Total\s+Weight|Shipment\s+Weight)\b.*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bDelivery\s+Contact\b.*$", "", cleaned, flags=re.I)
    return cleaned.strip(" ,")


def _is_party_noise_line(line: str) -> bool:
    if not line:
        return True
    if re.search(r"^(?:N\.W\.|G\.W\.|Pieces\b|Terms\b|Email\b|Customer\s+(?:EORI|VAT)|Code\s+Description|PriceDiscount|Order\s+Total|Currency|Commercial\s+Invoice|Invoice\b|Address:)", line, re.I):
        return True
    if re.search(r"\b(?:Nature\s+of\s+Transaction|No\.\s+of\s+Packages|Incoterms|Payment\s+Terms|Registered\s+in\s+England|exporter\s+of\s+the\s+products|preferential\s+origin|Signed\s+for|Sort\s+Code|Swift\s+Code|IBAN|VAT\s+Reg)\b", line, re.I):
        return True
    if COMMODITY_RE.search(line) or MONEY_RE.search(line):
        return True
    return False


def _party_fields(block: str, *, fallback_name: str = "", fallback_country: str = "") -> dict[str, str]:
    raw_lines = [_clean_party_line(line) for line in str(block or "").splitlines()]
    lines = [line for line in raw_lines if line and not _is_party_noise_line(line)]
    name = fallback_name or (lines[0] if lines else "")
    postcode = _postcode("\n".join(lines) or block)
    country = _country_code("\n".join(lines) or block) or fallback_country
    street = ""
    city = ""
    city_hint = ""
    address_lines = []
    for line in lines[1:]:
        if line == name:
            continue
        if country and _country_code(line) == country and len(line.split()) <= 3:
            continue
        if re.search(r"\bDelivery\s+City\b", line, re.I):
            before, after = re.split(r"\bDelivery\s+City\b", line, maxsplit=1, flags=re.I)
            line = before.strip(" ,")
            city_hint = after.strip(" ,") or city_hint
        if postcode and _postcode_matches_line(postcode, line):
            without_postcode = POSTCODE_RE.sub("", line).strip(" ,")
            without_postcode = re.sub(r"\b(?:Great\s+Britain|United\s+Kingdom|Ireland|UK|GB)\b", "", without_postcode, flags=re.I).strip(" ,")
            if without_postcode and not city_hint:
                city_hint = without_postcode
            continue
        if line:
            address_lines.append(line)
    if address_lines:
        street = address_lines[0]
    city = city_hint
    if not city and postcode:
        city = _city_before_postcode(lines, postcode)
    elif not city and len(address_lines) > 1:
        city = address_lines[-1]
    return {"name": name, "street_number": street, "city": city, "postcode": postcode, "country": country}


def _decimal_text(value: Any) -> str:
    text = clean_cell(value).replace(",", "")
    if not text:
        return ""
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    normalized = number.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP).normalize()
    return format(normalized, "f")


def _first_number(value: str) -> str:
    match = NUMBER_RE.search(value or "")
    return match.group(0) if match else ""


def _package_type(text: str) -> str:
    lowered = (text or "").lower()
    if "pallet" in lowered:
        candidate = "pallets"
    elif "carton" in lowered or "ctn" in lowered or "box" in lowered:
        candidate = "Boxes"
    elif "can" in lowered:
        candidate = "Cans"
    else:
        candidate = text or "PK"
    return normalise_package_type(candidate, default="PK") or "PK"


def _money_values(text: str) -> list[str]:
    return [match.group(1).replace(",", "") for match in MONEY_RE.finditer(text or "")]


def _pdf_reversed_metrics(line: str) -> dict[str, str]:
    match = PDF_REVERSED_METRIC_RE.match(_one_line(line))
    if not match:
        return {}
    trailing = match.group("trailing")
    origin_code = _country_code(trailing)
    description_prefix = trailing
    for country_name, _code in COUNTRY_ALIASES:
        if trailing.lower().startswith(country_name.lower()):
            origin_code = _code
            description_prefix = trailing[len(country_name):].strip(" -")
            break
    return {
        "amount": _decimal_text(match.group("amount")),
        "weight": _decimal_text(match.group("weight")),
        "quantity": _decimal_text(match.group("quantity")),
        "origin_code": origin_code,
        "description_prefix": description_prefix,
    }


def _pdf_product_code_from_text(text: str) -> str:
    match = re.search(r"\b((?:CZ)?IN-[A-Z0-9-]+|[A-Z]{2,}\d[A-Z0-9-]{2,})\s*$", str(text or "").strip(), re.I)
    return match.group(1) if match else ""

def _split_pdf_description_product_code(description: str) -> tuple[str, str]:
    text = _one_line(description)
    patterns = (
        r"^(?P<description>.+?)(?P<code>(?:CZ)?IN-[A-Z0-9-]+)$",
        r"^(?P<description>.+?)(?P<code>[A-Z]{2,}-[A-Z0-9-]+)$",
        r"^(?P<description>.+?)(?P<code>[A-Z]{3,}\d{3,}[A-Z0-9-]*)$",
        r"^(?P<description>.+?)(?P<code>[A-Z]{2,}\s*-\s*\d[A-Z0-9-]+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, text)
        if not match:
            continue
        clean_description = match.group("description").strip(" -")
        code = re.sub(r"\s+", " ", match.group("code").strip())
        if clean_description and code:
            return clean_description, code
    return text, ""


def _multiply_decimal(left: str, right: str) -> str:
    try:
        value = Decimal(str(left).replace(",", "")) * Decimal(str(right).replace(",", ""))
    except (InvalidOperation, ValueError):
        return _decimal_text(right)
    return _decimal_text(value)


PDF_DOCUMENTATION_HINT_RE = re.compile(
    r"TSS\s+How-To\s+Guides|Step-by-step\s+guide|User\s+Guide|API\s+Reference|Migration\s+Manual|Document\s+control|Copyright\s+©?\s*\d{4}\s+Trader\s+Support\s+Service|Repository\s+visibility",
    re.I,
)

PDF_COMMERCIAL_HINTS = (
    r"\bCommercial\s+Invoice\b",
    r"\bInvoice\s+(?:No\.?|Number|Address|Date)\b",
    r"\bInvoiced\s+To\b",
    r"\bCustomer\s+Ref\b",
    r"\bPurchase\s+order\s+No\b",
    r"\bDelivery\s+Address\b",
    r"\bSeller\s*:",
    r"\bBuyer\s*:",
    r"\bReceiver'?s\s+EORI\b",
    r"\bShipper'?s\s+EORI\b",
    r"\bNo\.\s+of\s+Packages\b",
    r"\bPieces\b",
    r"\bG\.W\.\s*\(Kg\)",
    r"\bN\.W\.\s*\(Kg\)",
    r"\bGross\s+weight\s+of\s+consignment\b",
    r"\bNet\s+weight\s+of\s+consignment\b",
    r"\bShipment\s+Weight\b",
    r"\bTariff\s+Number\b",
    r"\bTerms\s+of\s+Trading\b",
    r"\bReason\s+for\s+Export\b",
)


def _has_pdf_commercial_context(text: str) -> bool:
    compact_text = _one_line(text)
    if not compact_text:
        return False
    if PDF_DOCUMENTATION_HINT_RE.search(compact_text):
        return False
    score = sum(1 for pattern in PDF_COMMERCIAL_HINTS if re.search(pattern, compact_text, re.I))
    has_reference = bool(re.search(r"\b(?:Invoice\s+(?:No\.?|Number)|Commercial\s+Invoice\s+Number|Customer\s+Ref|Purchase\s+order\s+No)\b", compact_text, re.I))
    has_party_or_weight = bool(re.search(r"\b(?:Invoiced\s+To|Delivery\s+Address|Invoice\s+Address|Seller\s*:|Buyer\s*:|Receiver'?s\s+EORI|Customer\s+EORI|G\.W\.\s*\(Kg\)|Gross\s+weight\s+of\s+consignment|Shipment\s+Weight|No\.\s+of\s+Packages|Pieces)\b", compact_text, re.I))
    return score >= 2 and (has_reference or has_party_or_weight)

def _pdf_meta(text: str, page_number: int = 1) -> dict[str, Any]:
    invoice_number = _first_match(text, (
        r"Invoice\s+No\.?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/ ]+)",
        r"Commercial\s+Invoice\s+Number\s*([A-Z0-9][A-Z0-9\-/ ]+)",
        r"Invoice\s+Number\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/ ]+)",
        r"\bNumber:\s*Date:\s*(?:[^\n]*\n)?\s*\S+\s+\S+\s+\S+\s+([A-Z0-9\-/]+)",
    ))
    customer_ref = _first_match(text, (
        r"Customer\s+Ref\s*[:#]?\s*([A-Z0-9\-/]+)",
        r"PO\s+No\s*[:#]?\s*([A-Z0-9\-/]+)",
        r"Purchase\s+order\s+No\.?\s*(?:Number:)?\s*(?:[^\n]*\n)?\s*\S+\s+\S+\s+([A-Z0-9\-/]+)",
    ))
    consignment_number = customer_ref or invoice_number or f"PDF-PAGE-{page_number:03d}"

    explicit_seller_name = _first_match(text, (r"^\s*([A-Z][^\n]{2,80}(?:Ltd|Limited|Group|Bros|International)[^\n]*)",))
    seller_name = explicit_seller_name or _seller_name_from_context(text)
    seller_name_assumed = bool(seller_name and not explicit_seller_name)
    seller_block = _party_block(text, (r"Seller", r"Exporter"), ("Buyer", "Receiver", "Invoice Address", "Delivery Address", "Commercial Invoice Number", "Invoice"))
    if not seller_block:
        seller_block = _top_party_block(text, seller_name, ("Invoice Address", "Delivery Address", "Commercial Invoice Number", "Invoice", "Page"))
    buyer_block = _party_block(text, (r"Delivery Address", r"Buyer", r"Invoiced To", r"Invoice Address"), ("Invoice", "Page", "Tel", "Currency", "Quantity", "PO No", "Terms", "Reason for Export", "Payment Terms"))
    buyer_name = _first_match(text, (r"Invoiced\s+To:\s*(.*?)(?:Shipment\s+Detail|Invoice\s+No\.|N\.W\.|G\.W\.|Pieces|Customer\s+Ref)",))
    seller = _party_fields(seller_block, fallback_name=seller_name, fallback_country="GB")
    seller_contact_assumption_fields: list[str] = []
    if not seller.get("postcode"):
        seller_contact = _seller_contact_address_fields(text, fallback_name=seller.get("name") or seller_name, fallback_country="GB")
        for key, value in seller_contact.items():
            if value and not seller.get(key):
                seller[key] = value
                if key != "name":
                    seller_contact_assumption_fields.append(key)
    buyer = _party_fields(buyer_block, fallback_name=buyer_name)

    eoris = _all_eoris(text)
    consignor_eori = _eori_near(text, (r"Shipper'?s\s+EORI", r"Exporter\s+(?:Reference\s+No\s+)?EORI", r"ZOK\s+EORI", r"Seller\s+EORI")) or _generic_consignor_eori(text) or (eoris[0] if eoris else "")
    consignee_eori = _eori_near(text, (r"Customer\s+EORI", r"Receiver'?s\s+EORI", r"Consignee\s+EORI", r"Buyer\s+EORI"))
    if not consignee_eori and len(eoris) > 1:
        consignee_eori = eoris[1]

    package_text = _first_match(text, (
        r"Pieces\s*[:#]?\s*([^\n]+)",
        r"No\.\s+of\s+Packages\s*[:#]?\s*([^\n]+)",
        r"Packed\s+onto\s+([^\n]+)",
    ))
    gross_mass = _first_match(text, (
        r"G\.W\.\s*\(Kg\)\s*[:#]?\s*([0-9][0-9,.]*)",
        r"Gross\s+weight\s+of\s+consignment\s*[:#]?\s*([0-9][0-9,.]*)\s*kg",
        r"Shipment\s+Weight\s*[:#]?\s*([0-9][0-9,.]*)\s*Gross\s*kg",
    ))
    net_mass = _first_match(text, (
        r"N\.W\.\s*\(Kg\)\s*[:#]?\s*([0-9][0-9,.]*)",
        r"Net\s+weight\s+of\s+consignment\s*[:#]?\s*([0-9][0-9,.]*)\s*kg",
        r"Total\s+Weight\s*[:#]?\s*([0-9][0-9,.]*)\s*Nett\s*kg",
    ))
    currency = _first_match(text, (r"Currency(?:\s+Code)?\s*[:#]?\s*(Pound Sterling|[A-Z]{3}\b)",))
    if currency.lower() == "pound sterling":
        currency = "GBP"

    destination_country = buyer.get("country") or ("GB" if buyer.get("postcode") else "")
    package_type = _package_type(package_text) if package_text else "PK"
    meta: dict[str, Any] = {
        "consignment_number": consignment_number,
        "trader_reference": customer_ref or invoice_number,
        "transport_document_number": consignment_number,
        "destination_country": destination_country,
        "consignor_eori": consignor_eori,
        "consignor_name": seller.get("name", ""),
        "consignor_street_number": seller.get("street_number", ""),
        "consignor_city": seller.get("city", ""),
        "consignor_postcode": seller.get("postcode", ""),
        "consignor_country": seller.get("country", ""),
        "consignee_eori": consignee_eori,
        "consignee_name": buyer.get("name", ""),
        "consignee_street_number": buyer.get("street_number", ""),
        "consignee_city": buyer.get("city", ""),
        "consignee_postcode": buyer.get("postcode", ""),
        "consignee_country": buyer.get("country", "") or destination_country,
        "importer_eori": consignee_eori,
        "importer_name": buyer.get("name", ""),
        "importer_street_number": buyer.get("street_number", ""),
        "importer_city": buyer.get("city", ""),
        "importer_postcode": buyer.get("postcode", ""),
        "importer_country": buyer.get("country", "") or destination_country,
        "exporter_eori": consignor_eori,
        "exporter_name": seller.get("name", ""),
        "exporter_street_number": seller.get("street_number", ""),
        "exporter_city": seller.get("city", ""),
        "exporter_postcode": seller.get("postcode", ""),
        "exporter_country": seller.get("country", ""),
        "type_of_packages": package_type,
        "number_of_packages": _first_number(package_text),
        "gross_mass_kg": _decimal_text(gross_mass),
        "net_mass_kg": _decimal_text(net_mass),
        "item_invoice_currency": currency or "GBP",
        "invoice_number": invoice_number,
    }
    if not (customer_ref or invoice_number):
        _mark_assumption(meta, "consignment_number", "No invoice/customer reference was found; generated a page-based preview reference.")
        _mark_assumption(meta, "transport_document_number", "No transport document reference was found; generated a page-based preview reference.")
    if destination_country and not buyer.get("country"):
        reason = "Country inferred as GB from a UK-style postcode; confirm before live TSS submission."
        for field in ("destination_country", "consignee_country", "importer_country"):
            _mark_assumption(meta, field, reason)
    if seller.get("country") == "GB" and not _country_code(seller_block):
        for field in ("consignor_country", "exporter_country"):
            _mark_assumption(meta, field, "Country defaulted to GB for the exporter/consignor; confirm before live TSS submission.")
    if seller_name_assumed:
        for field in ("consignor_name", "exporter_name"):
            _mark_assumption(meta, field, "Exporter name inferred from email/domain context because the PDF text did not expose a seller name block.")
    if seller_contact_assumption_fields:
        suffix_map = {
            "street_number": ("consignor_street_number", "exporter_street_number"),
            "city": ("consignor_city", "exporter_city"),
            "postcode": ("consignor_postcode", "exporter_postcode"),
            "country": ("consignor_country", "exporter_country"),
        }
        for source_key in seller_contact_assumption_fields:
            for field in suffix_map.get(source_key, ()):
                _mark_assumption(meta, field, "Exporter address inferred from contact/registered-office text because no explicit seller address block was available.")
    if not package_text:
        _mark_assumption(meta, "type_of_packages", "Package type defaulted to PK because no package wording was found in the PDF text.")
    if not currency:
        _mark_assumption(meta, "item_invoice_currency", "Currency defaulted to GBP because no currency value was found in the PDF text.")
    return meta


def _pdf_multiline_stop(line: str) -> bool:
    return bool(re.search(r"^(?:Commercial Invoice|Seller:|Buyer:|InvoicedTax|Order Total|GBP$|The exporter of the products|Delivery Contact|PriceDiscountPrice)", _one_line(line), re.I))


def _parse_pdf_multiline_goods(lines: list[str], meta: dict[str, Any]) -> tuple[list[dict[str, Any]], set[int]]:
    items: list[dict[str, Any]] = []
    consumed: set[int] = set()
    index = 0
    while index < len(lines):
        metrics = _pdf_reversed_metrics(lines[index])
        if not metrics:
            index += 1
            continue

        commodity_index = index + 1
        while commodity_index < len(lines):
            candidate = _one_line(lines[commodity_index])
            if _pdf_multiline_stop(candidate) or _pdf_reversed_metrics(candidate):
                break
            if re.fullmatch(r"\d{8,10}", candidate):
                break
            commodity_index += 1
        if commodity_index >= len(lines) or not re.fullmatch(r"\d{8,10}", _one_line(lines[commodity_index])):
            index += 1
            continue

        desc_index = commodity_index + 1
        description_lines: list[str] = []
        while desc_index < len(lines):
            candidate = _one_line(lines[desc_index])
            if not candidate:
                desc_index += 1
                continue
            if _pdf_multiline_stop(candidate) or _pdf_reversed_metrics(candidate):
                break
            if re.fullmatch(r"\d{8,10}", candidate):
                break
            description_lines.append(candidate)
            desc_index += 1

        description = _one_line(" ".join(description_lines))
        description, stock_code = _split_pdf_description_product_code(description)
        if not description:
            description = f"Goods {_one_line(lines[commodity_index])}"
        item = {
            "commodity_code": _one_line(lines[commodity_index]),
            "goods_description": description[:500],
            "country_of_origin": metrics.get("origin_code"),
            "number_of_packages": metrics.get("quantity") or "1",
            "type_of_packages": meta.get("type_of_packages") or "PK",
            "package_marks": stock_code or meta.get("transport_document_number") or meta.get("consignment_number"),
            "gross_mass_kg": metrics.get("weight") or meta.get("gross_mass_kg", ""),
            "net_mass_kg": metrics.get("weight") or meta.get("net_mass_kg", ""),
            "item_invoice_amount": metrics.get("amount"),
            "item_invoice_currency": meta.get("item_invoice_currency") or "GBP",
            "invoice_number": meta.get("invoice_number", ""),
        }
        if not stock_code:
            _mark_assumption(item, "package_marks", "Product code was not visible in the multi-line PDF goods block; inherited consignment reference for preview.")
        _mark_assumption(item, "net_mass_kg", "Net mass defaulted to goods-line gross mass because no separate line-level net mass was found for this PDF goods block.")
        items.append(item)
        consumed.update(range(index, max(desc_index, commodity_index + 1)))
        index = max(desc_index, commodity_index + 1)
    return items, consumed

def _parse_pdf_goods_line(line: str, next_line: str, meta: dict[str, Any], previous_line: str = "") -> dict[str, Any] | None:
    commodity_match = COMMODITY_RE.search(line or "")
    if not commodity_match:
        return None
    reversed_metrics = _pdf_reversed_metrics(previous_line) or _pdf_reversed_metrics(line)
    commodity = commodity_match.group(0)
    before = line[:commodity_match.start()].strip()
    after = line[commodity_match.end():].strip()
    if re.search(r"EORI|VAT|IBAN|Account|Acc\.\s*No|Swift|Sort Code|Registered|Company No|Invoice\s+(?:No|Number|Date)|Payment|Tel:|Email:|Customer\s+Ref|Pieces|Shipment\s+Detail|Customer\s+VAT", line, re.I):
        return None

    stock_match = re.match(r"^([A-Z0-9][A-Z0-9./\-]{1,})\s+", before)
    stock_code = stock_match.group(1) if stock_match else ""
    next_line_product_code = _pdf_product_code_from_text(next_line)
    if next_line_product_code and (not stock_code or stock_code.lower() in {"content", "contents"}):
        stock_code = next_line_product_code
    description_source = before[len(stock_code):].strip() if stock_code else before
    if reversed_metrics.get("description_prefix") and (not description_source or "%" in description_source or re.match(r"^\d", description_source)):
        description_source = COMMODITY_RE.sub("", reversed_metrics["description_prefix"]).strip(" -")
    origin_code = reversed_metrics.get("origin_code") or _country_code(line)
    for country_name, _code in COUNTRY_ALIASES:
        idx = description_source.lower().rfind(country_name.lower())
        if idx > -1:
            description_source = description_source[:idx].strip()
            break
    qty = ""
    if reversed_metrics.get("quantity"):
        qty = reversed_metrics["quantity"]
    else:
        qty_match = re.search(r"\s(\d+(?:\.\d+)?)\s+(?:(?:\u00c2\u00a3|[\u00a3\u0141])|GBP)?\s*\d", before, re.I)
        if qty_match:
            qty = qty_match.group(1)
            description_source = description_source[:description_source.rfind(qty)].strip() if qty in description_source else description_source
    description_source = re.sub(
        r"\s+\d+(?:\.\d+)?\s+(?:(?:\u00c2\u00a3|[\u00a3\u0141])|GBP)\s*\d[\d,.]*(?:\.\d+)?\s*$",
        "",
        description_source,
        flags=re.I,
    ).strip(" -")
    description_source = re.sub(r"\s+(?:(?:\u00c2\u00a3|[\u00a3\u0141])\s*)?\d[\d,.]*(?:\.\d+)?\s*$", "", description_source).strip(" -")

    after_description = re.sub(r"^(?:[\s\d.,GBP]|\u00c2\u00a3|[\u00a3\u0141])+", "", after, flags=re.I).strip(" -")
    if len(after_description) > len(description_source):
        description_source = after_description
        if next_line and not COMMODITY_RE.search(next_line) and not re.search(r"Invoice|Order Total|Exporter|Payment", next_line, re.I):
            description_source = f"{description_source} {next_line.strip()}"

    split_description, description_product_code = _split_pdf_description_product_code(description_source)
    if description_product_code and (not stock_code or stock_code.lower() in {"content", "contents"}):
        description_source = split_description
        stock_code = description_product_code

    money_values = _money_values(line)
    amount = reversed_metrics.get("amount") or (money_values[-1] if money_values else "")
    weight = ""
    if reversed_metrics.get("weight"):
        weight = reversed_metrics["weight"]
    else:
        after_numbers = NUMBER_RE.findall(after)
        if after_numbers:
            weight = after_numbers[0]
    if reversed_metrics and weight:
        gross_mass = _decimal_text(weight)
    else:
        gross_mass = _multiply_decimal(qty, weight) if qty and weight else _decimal_text(weight or meta.get("gross_mass_kg"))
    net_mass = gross_mass if gross_mass else meta.get("net_mass_kg", "")
    package_count = _decimal_text(qty) or meta.get("number_of_packages") or "1"
    package_type = meta.get("type_of_packages") or "PK"

    item: dict[str, Any] = {
        "commodity_code": commodity,
        "goods_description": _one_line(description_source)[:500] or f"Goods {commodity}",
        "country_of_origin": origin_code,
        "number_of_packages": package_count,
        "type_of_packages": package_type,
        "package_marks": stock_code or meta.get("transport_document_number") or meta.get("consignment_number"),
        "gross_mass_kg": gross_mass,
        "net_mass_kg": net_mass,
        "item_invoice_amount": amount,
        "item_invoice_currency": meta.get("item_invoice_currency") or "GBP",
        "invoice_number": meta.get("invoice_number", ""),
    }
    if not qty and not meta.get("number_of_packages"):
        _mark_assumption(item, "number_of_packages", "Package count defaulted to 1 because no quantity/package count was found for this goods line.")
    elif not qty and meta.get("number_of_packages"):
        _mark_assumption(item, "number_of_packages", "Package count inherited from the consignment summary; confirm per goods item before live submission.")
    if not meta.get("type_of_packages"):
        _mark_assumption(item, "type_of_packages", "Package type defaulted to PK because no package wording was found for this goods line.")
    if gross_mass and not weight and meta.get("gross_mass_kg"):
        _mark_assumption(item, "gross_mass_kg", "Gross mass inherited from the consignment summary; confirm per goods item before live submission.")
    if gross_mass:
        _mark_assumption(item, "net_mass_kg", "Net mass defaulted to gross mass because no separate line-level net mass was found for this goods item.")
    if not meta.get("item_invoice_currency"):
        _mark_assumption(item, "item_invoice_currency", "Currency defaulted to GBP because no currency value was found in the PDF text.")
    return item


def _summary_goods(text: str, meta: dict[str, Any]) -> dict[str, Any]:
    commodity = _first_match(text, (r"Tariff\s+Number\s*[:#]?\s*(?:[A-Z]+\s*)?(\d{8,10})", r"\b(?:HS|Commodity|Comm\.)\s+Code\b[^\n]*(\d{8,10})"))
    description = _first_match(text, (
        r"Terms\s+of\s+Trading\s*[:#]?\s*(.+?)(?:\n|Net\s*[\u00a3\u0141]|Reason\s+for\s+Export)",
        r"Reason\s+for\s+Export\s*[:#]?\s*(.+?)(?:Packed\s+onto|Net\s+weight|Gross\s+weight|\n)",
        r"Consignment\s+Description\s*[:#]?\s*(.+?)(?:\n|$)",
    ))
    item: dict[str, Any] = {
        "commodity_code": commodity,
        "goods_description": _one_line(description or "Commercial invoice goods")[:500],
        "type_of_packages": meta.get("type_of_packages") or "PK",
        "number_of_packages": meta.get("number_of_packages") or "1",
        "package_marks": meta.get("transport_document_number") or meta.get("consignment_number"),
        "gross_mass_kg": meta.get("gross_mass_kg", ""),
        "net_mass_kg": meta.get("net_mass_kg", ""),
        "item_invoice_currency": meta.get("item_invoice_currency") or "GBP",
        "invoice_number": meta.get("invoice_number", ""),
    }
    if not description:
        _mark_assumption(item, "goods_description", "Goods description defaulted from a generic commercial invoice label; confirm before live submission.")
    if not meta.get("type_of_packages"):
        _mark_assumption(item, "type_of_packages", "Package type defaulted to PK because no package wording was found in the PDF text.")
    if not meta.get("number_of_packages"):
        _mark_assumption(item, "number_of_packages", "Package count defaulted to 1 because no package count was found in the PDF text.")
    return item


def _page_has_goods_lines(lines: list[str]) -> bool:
    return any(_parse_pdf_goods_line(line, lines[index + 1] if index + 1 < len(lines) else "", {}, lines[index - 1] if index > 0 else "") for index, line in enumerate(lines))


def _continuation_meta(meta: dict[str, Any], page_number: int) -> dict[str, Any]:
    carried = {key: value for key, value in (meta or {}).items() if key != ASSUMPTION_META_KEY}
    reason = f"Inherited from the previous commercial invoice page for PDF continuation page {page_number}; confirm before live submission."
    for field in (
        "consignment_number",
        "trader_reference",
        "transport_document_number",
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
        "type_of_packages",
        "number_of_packages",
        "item_invoice_currency",
        "invoice_number",
    ):
        if clean_cell(carried.get(field)):
            _mark_assumption(carried, field, reason)
    return carried


def pdf_records_from_text(text: str, *, page_number: int = 1, continuation_meta: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    clean_text = (text or "").replace("\r", "\n")
    lines = [_one_line(line) for line in clean_text.splitlines() if _one_line(line)]
    if not lines:
        return []
    page_text = "\n".join(lines)
    is_commercial_page = _has_pdf_commercial_context(page_text)
    if is_commercial_page:
        meta = _pdf_meta(page_text, page_number=page_number)
        allow_summary = True
    elif continuation_meta and _page_has_goods_lines(lines):
        meta = _continuation_meta(continuation_meta, page_number)
        allow_summary = False
    else:
        return []

    goods_items, consumed_line_indexes = _parse_pdf_multiline_goods(lines, meta)
    for index, line in enumerate(lines):
        if index in consumed_line_indexes:
            continue
        item = _parse_pdf_goods_line(line, lines[index + 1] if index + 1 < len(lines) else "", meta, lines[index - 1] if index > 0 else "")
        if item:
            if not is_commercial_page:
                _mark_assumption(item, "goods_description", f"Parsed from a PDF continuation page without its own invoice header; confirm before live submission.")
            goods_items.append(item)
    if not goods_items and allow_summary:
        summary = _summary_goods("\n".join(lines), meta)
        has_meaningful_meta = bool(meta.get("invoice_number") or meta.get("trader_reference") or meta.get("gross_mass_kg") or meta.get("net_mass_kg"))
        has_meaningful_summary = bool(summary.get("commodity_code") or summary.get("goods_description") != "Commercial invoice goods")
        if not (has_meaningful_meta or has_meaningful_summary):
            return []
        goods_items.append(summary)

    records = []
    for item in goods_items:
        record = {key: value for key, value in meta.items() if key != ASSUMPTION_META_KEY}
        record.update({key: value for key, value in item.items() if key != ASSUMPTION_META_KEY and clean_cell(value)})
        assumptions = _merge_assumptions(meta, item)
        if assumptions:
            record[ASSUMPTION_META_KEY] = assumptions
        records.append(record)
    return records
def _records_from_pdf_pages(pages: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    last_commercial_meta: dict[str, Any] | None = None
    for page_number, page_text in enumerate(pages, 1):
        page_records = pdf_records_from_text(page_text, page_number=page_number, continuation_meta=last_commercial_meta)
        records.extend(page_records)
        clean_page_text = "\n".join(_one_line(line) for line in (page_text or "").replace("\r", "\n").splitlines() if _one_line(line))
        if _has_pdf_commercial_context(clean_page_text):
            last_commercial_meta = _pdf_meta(clean_page_text, page_number=page_number)
    return records


def _structure_from_pdf_records(
    records: list[dict[str, Any]],
    *,
    pages: list[str],
    text: str,
    filename: str | None,
    metadata: dict[str, str],
    ocr_used: bool = False,
) -> dict[str, Any]:
    structure = structure_from_records("pdf", records, PDF_OUTPUT_COLUMNS)
    structure["pdfPageCount"] = len(pages)
    structure["pdfTextLength"] = len(text)
    structure["pdfRequiresOcr"] = False
    structure["pdfOcrConfigured"] = pdf_ocr_configured()
    structure["pdfOcrAttempted"] = ocr_used
    structure["pdfOcrUsed"] = ocr_used
    references = _pdf_detected_references(filename, metadata)
    if references:
        structure["pdfDetectedReferences"] = references
    metadata_summary = _pdf_metadata_summary(metadata)
    if metadata_summary:
        structure["pdfMetadata"] = metadata_summary
    if metadata.get("Title"):
        structure["pdfTitle"] = metadata["Title"]
    structure["rawTextSample"] = _one_line(text)[:1000]
    return structure


def inspect_pdf(content: bytes, filename: str | None = None) -> dict[str, Any]:
    pages = extract_pdf_text_pages(content)
    metadata = extract_pdf_metadata(content)
    text = "\n".join(pages)
    if not _one_line(text):
        ocr_configured = pdf_ocr_configured()
        ocr_pages: list[str] = []
        ocr_error = ""
        if ocr_configured:
            try:
                ocr_pages = extract_pdf_ocr_text_pages(content)
            except Exception as error:
                ocr_error = f"{type(error).__name__}: {error}"
        ocr_text = "\n".join(ocr_pages)
        if _one_line(ocr_text):
            records = _records_from_pdf_pages(ocr_pages)
            if records:
                return _structure_from_pdf_records(
                    records,
                    pages=ocr_pages,
                    text=ocr_text,
                    filename=filename,
                    metadata=metadata,
                    ocr_used=True,
                )
            return _pdf_empty_structure(
                "OCR text was extracted, but no invoice/consignment fields could be mapped safely.",
                filename=filename,
                pages=ocr_pages,
                text=ocr_text,
                metadata=metadata,
                requires_ocr=False,
                ocr_configured=ocr_configured,
                ocr_attempted=True,
                ocr_used=True,
            )
        warning = "No extractable PDF text found. The file is likely scanned/image-only; OCR is required before automatic mapping."
        if ocr_configured and ocr_error:
            warning += " Optional OCR was configured but failed."
        elif not ocr_configured:
            warning += " Configure Azure Document Intelligence OCR env vars to attempt scanned-PDF mapping."
        return _pdf_empty_structure(
            warning,
            filename=filename,
            pages=pages,
            text=text,
            metadata=metadata,
            requires_ocr=True,
            ocr_configured=ocr_configured,
            ocr_attempted=ocr_configured,
            ocr_used=False,
            ocr_error=ocr_error,
        )

    records = _records_from_pdf_pages(pages)
    if not records:
        return _pdf_empty_structure(
            "PDF text was extracted, but no invoice/consignment fields could be mapped safely.",
            filename=filename,
            pages=pages,
            text=text,
            metadata=metadata,
            requires_ocr=False,
        )
    return _structure_from_pdf_records(
        records,
        pages=pages,
        text=text,
        filename=filename,
        metadata=metadata,
    )
def inspect_upload(filename: str | None, content: bytes) -> dict[str, Any]:
    suffix = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    try:
        if suffix == "csv":
            return inspect_csv(content)
        if suffix == "xlsx":
            return inspect_xlsx(content)
        if suffix == "pdf":
            return inspect_pdf(content, filename=filename)
        if suffix == "xls":
            return empty_structure("xls", "Legacy .xls header introspection is not supported yet; upload .xlsx or .csv for automatic mapping preview.")
        return empty_structure(suffix or "unknown", "Unsupported file extension for header introspection.")
    except (ET.ParseError, zipfile.BadZipFile, OSError, UnicodeError, csv.Error, RuntimeError) as error:
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
