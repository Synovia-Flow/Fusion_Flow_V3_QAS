#!/usr/bin/env python3
"""Stdlib XLSX reader for Fusion Flow ingestion.

Ports the proven business-sheet / header-row detection from the V2 downloader so
templated workbooks (Jet/NL() exports with a 'Report' template sheet) resolve to
the real sales-order header row and data. No openpyxl dependency - pure stdlib.

read_xlsx_rows(content) -> (headers: list[str], rows: list[dict])
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

NS = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/package/2006/relationships",
    "rid": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

BUSINESS_WORDS = {
    "address", "amount", "city", "code", "commodity", "consignee", "consignor", "country",
    "currency", "customer", "description", "document", "email", "eori", "exporter", "gross",
    "importer", "invoice", "item", "line", "marks", "measure", "name", "number", "origin",
    "package", "packages", "postcode", "price", "quantity", "qty", "reference", "ship",
    "tariff", "unit", "vat", "weight",
}
TECHNICAL_WORDS = {
    "autohide", "autotable", "fit", "fields", "filters", "formula", "formulas", "formulasonly",
    "headers", "hide", "linkfield", "links", "lookup", "option", "tables", "title", "values",
}


def normalize_column_name(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[_\-/]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(t.text or "" for t in si.findall(".//s:t", NS)) for si in root.findall("s:si", NS)]


def _col_index(ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", ref.upper())
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return max(idx - 1, 0)


def _worksheet_paths(zf: zipfile.ZipFile) -> list[str]:
    book = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    relmap = {r.get("Id"): r.get("Target", "") for r in rels}
    paths = []
    for sheet in book.findall("s:sheets/s:sheet", NS):
        rid = sheet.get(f"{{{NS['rid']}}}id")
        tgt = relmap.get(rid, "")
        if tgt:
            paths.append(tgt if tgt.startswith("xl/") else "xl/" + tgt.lstrip("/"))
    if not paths:
        raise ValueError("Workbook has no worksheets")
    return paths


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    ctype = cell.get("t", "")
    if ctype == "inlineStr":
        return "".join(x.text or "" for x in cell.findall(".//s:t", NS)).strip()
    v = cell.find("s:v", NS)
    if v is None or v.text is None:
        return ""
    raw = v.text.strip()
    if ctype == "s" and raw.isdigit():
        i = int(raw)
        return shared[i].strip() if 0 <= i < len(shared) else ""
    return raw


def _raw_rows(sheet_xml: bytes, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(sheet_xml)
    rows: list[list[str]] = []
    for row in root.findall(".//s:sheetData/s:row", NS):
        vals: list[str] = []
        for cell in row.findall("s:c", NS):
            ci = _col_index(cell.get("r", "A1"))
            while len(vals) <= ci:
                vals.append("")
            vals[ci] = _cell_text(cell, shared)
        if any(v.strip() for v in vals):
            rows.append(vals)
    return rows


def _header_score(row: list[str]) -> int:
    norms = [normalize_column_name(v) for v in row if str(v).strip()]
    if len(norms) < 2:
        return -100
    score = min(len(norms), 25)
    for n in norms:
        words = set(n.split())
        score += len(words & BUSINESS_WORDS)
        if words & TECHNICAL_WORDS:
            score -= 8
    return score


def _select_header(raw_rows: list[list[str]]) -> int:
    best_i, best_s = 0, -999
    for i, row in enumerate(raw_rows[:60]):
        s = _header_score(row)
        if s > best_s:
            best_i, best_s = i, s
    return best_i


def _unique_headers(row: list[str]) -> list[str]:
    headers, seen = [], {}
    for i, v in enumerate(row):
        h = v.strip() or f"Column {i + 1}"
        seen[h] = seen.get(h, 0) + 1
        headers.append(f"{h} ({seen[h]})" if seen[h] > 1 else h)
    return headers


def _is_noise_row(row_data: dict[str, str]) -> bool:
    vals = [normalize_column_name(v) for v in row_data.values() if str(v).strip()]
    if not vals:
        return True
    # Drop Jet definition rows and total rows.
    if any(v.startswith("nl ") or v in {"fields", "headers", "hide"} for v in vals):
        return True
    if any(str(v).strip().startswith("=NL(") for v in row_data.values()):
        return True
    return False


def read_xlsx_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Return (headers, rows) for the best business worksheet in the workbook."""
    best: tuple[int, int, list[str], list[dict[str, str]]] | None = None
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        shared = _shared_strings(zf)
        for path in _worksheet_paths(zf):
            raw = _raw_rows(zf.read(path), shared)
            if not raw:
                continue
            hidx = _select_header(raw)
            headers = _unique_headers(raw[hidx])
            rows = []
            for r in raw[hidx + 1:]:
                row_data = {h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(headers)}
                if any(v.strip() for v in row_data.values()) and not _is_noise_row(row_data):
                    rows.append(row_data)
            score = _header_score(raw[hidx])
            candidate = (score, len(rows), headers, rows)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        return [], []
    return best[2], best[3]
