from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import Path
import re
from typing import Iterable

_CURRENCY_REPLACEMENTS = {
    '\u017d': '®',
    '\u02dd': '1/2',
    '\xa3': 'GBP',
    'Ł': 'GBP',
    '–': '-',
    '—': '-',
}

_UK_POSTCODE_RE = re.compile(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', re.I)


@dataclass
class ParsedParty:
    name: str = ''
    lines: list[str] = field(default_factory=list)
    postcode: str = ''
    country_text: str = ''
    country_code: str = ''

    @property
    def address_text(self) -> str:
        return ', '.join(part for part in self.lines if part)


@dataclass
class ParsedInvoiceLine:
    line_number: int
    commodity_code: str
    stock_code: str
    description: str
    quantity: str
    uom: str
    gross_mass_kg: str
    net_mass_kg: str
    line_value: str
    currency: str = 'GBP'


@dataclass
class ParsedInvoice:
    filename: str
    template_id: str
    supplier_name: str
    invoice_number: str
    carrier_reference: str
    external_document_number: str
    trade_terms: str
    credit_terms: str
    consignee: ParsedParty
    delivery_to: ParsedParty
    total_gross_weight_kg: str
    total_net_weight_kg: str
    total_invoice_value: str
    total_packages: str
    page_count: int
    raw_text: str
    lines: list[ParsedInvoiceLine]


def _clean_text(value: str) -> str:
    text = value or ''
    for src, dest in _CURRENCY_REPLACEMENTS.items():
        text = text.replace(src, dest)
    return text


def _collapse_ws(value: str) -> str:
    return re.sub(r'\s+', ' ', _clean_text(value or '')).strip()


def _clean_decimal(value: str) -> str:
    text = (value or '').strip().replace(',', '')
    if not text:
        return ''
    try:
        normalized = format(Decimal(text).normalize(), 'f')
    except (InvalidOperation, ValueError):
        return text
    if '.' in normalized:
        normalized = normalized.rstrip('0').rstrip('.')
    return normalized or '0'


def _sum_line_field(lines: Iterable['ParsedInvoiceLine'], attr: str) -> str:
    total = Decimal('0')
    found = False
    for line in lines:
        value = getattr(line, attr, '')
        if not value:
            continue
        try:
            total += Decimal(str(value))
            found = True
        except (InvalidOperation, ValueError):
            continue
    if not found:
        return ''
    normalized = format(total.normalize(), 'f')
    if '.' in normalized:
        normalized = normalized.rstrip('0').rstrip('.')
    return normalized or '0'


def _country_code_from_text(value: str) -> str:
    text = (value or '').strip().lower()
    if not text:
        return ''
    if 'great britain' in text or 'united kingdom' in text:
        return 'GB'
    if 'northern ireland' in text:
        return 'GB'
    if 'ireland' in text:
        return 'IE'
    if 'france' in text:
        return 'FR'
    if 'germany' in text:
        return 'DE'
    return ''


def _party_from_lines(name: str, lines: Iterable[str]) -> ParsedParty:
    clean_lines = [line.strip(' ,') for line in lines if line and line.strip(' ,')]
    postcode = ''
    for line in clean_lines:
        match = _UK_POSTCODE_RE.search(line)
        if match:
            postcode = match.group(0).upper().replace('  ', ' ')
            break
    country_text = clean_lines[-1] if clean_lines else ''
    return ParsedParty(
        name=_collapse_ws(name),
        lines=clean_lines,
        postcode=postcode,
        country_text=country_text,
        country_code=_country_code_from_text(country_text),
    )


def _extract_scalar(raw_text: str, label: str) -> str:
    match = re.search(rf'{re.escape(label)}\s*-\s*(.+)', raw_text, re.I)
    return _collapse_ws(match.group(1)) if match else ''


def _parse_parties(raw_text: str) -> tuple[ParsedParty, ParsedParty]:
    lines = [_clean_text(line.rstrip()) for line in raw_text.splitlines()]
    start_index = None
    for idx, line in enumerate(lines):
        if 'Cosignee' in line and 'Delivery To' in line:
            start_index = idx
            break
    if start_index is None:
        return ParsedParty(), ParsedParty()

    header_parts = re.split(r'\s{2,}', lines[start_index].strip(), maxsplit=1)
    left_header = header_parts[0] if header_parts else ''
    right_header = header_parts[1] if len(header_parts) > 1 else ''
    left_name = re.sub(r'^Cosignee\s+', '', left_header, flags=re.I).strip()
    right_name = re.sub(r'^Delivery To\s+', '', right_header, flags=re.I).strip()

    left_lines: list[str] = []
    right_lines: list[str] = []
    for line in lines[start_index + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('I declare all of the information'):
            break
        parts = re.split(r'\s{2,}', stripped, maxsplit=1)
        if len(parts) == 2:
            if parts[0].strip():
                left_lines.append(parts[0].strip())
            if parts[1].strip():
                right_lines.append(parts[1].strip())
        elif right_lines:
            right_lines.append(parts[0].strip())
        else:
            left_lines.append(parts[0].strip())

    return _party_from_lines(left_name, left_lines), _party_from_lines(right_name, right_lines)


def _extract_totals(raw_text: str) -> tuple[str, str, str, str]:
    collapsed = _collapse_ws(raw_text)
    match = re.search(
        r'Gross Weight\s+([\d,\.]+)\s+Net Weight\s+([\d,\.]+)\s+Value\s+([\d,\.]+)\s+No\. Packages\s+([\d,\.]+)',
        collapsed,
        re.I,
    )
    if not match:
        return '', '', '', ''
    return tuple(_clean_decimal(part) for part in match.groups())


def _split_item_blocks(section: str) -> list[str]:
    starts = list(re.finditer(r'\d{8,10}\s+[A-Z0-9]+\s+', section))
    blocks = []
    for idx, match in enumerate(starts):
        start = match.start()
        end = starts[idx + 1].start() if idx + 1 < len(starts) else len(section)
        blocks.append(section[start:end].strip())
    return blocks


def _parse_line_block(block: str, line_number: int) -> ParsedInvoiceLine | None:
    header = re.match(r'^(\d{8,10})\s+([A-Z0-9]+)\s+(.*)$', block)
    if not header:
        return None
    commodity_code, stock_code, rest = header.groups()

    tail = re.match(r'^(.*)\s+(\d[\d,\.]*)\s+(\d[\d,\.]*)\s+(\d[\d,\.]*)\s*$', rest)
    if not tail:
        return None
    preamble, gross_mass, net_mass, line_value = tail.groups()

    body = re.match(r'^(.*)\s+(\d[\d,\.]*)\s+([A-Za-z].*)$', preamble)
    if not body:
        return None
    description, quantity, uom = body.groups()

    return ParsedInvoiceLine(
        line_number=line_number,
        commodity_code=commodity_code,
        stock_code=stock_code,
        description=_collapse_ws(description),
        quantity=_clean_decimal(quantity),
        uom=_collapse_ws(uom),
        gross_mass_kg=_clean_decimal(gross_mass),
        net_mass_kg=_clean_decimal(net_mass),
        line_value=_clean_decimal(line_value),
    )


def parse_birkdale_invoice_text(text: str, filename: str = 'invoice.pdf', page_count: int = 1) -> ParsedInvoice:
    raw_text = _clean_text(text or '')
    collapsed = _collapse_ws(raw_text)
    if 'Commercial Invoice No.' not in raw_text or 'Carrier Reference' not in raw_text or 'Full Code Stock Code Description QTY' not in raw_text:
        raise ValueError(f'{filename} does not look like a supported Birkdale commercial invoice.')

    consignee, delivery_to = _parse_parties(raw_text)
    total_gross, total_net, total_value, total_packages = _extract_totals(raw_text)

    items_section = raw_text.split('Full Code Stock Code Description QTY', 1)[1]
    items_section = _collapse_ws(items_section)
    lines: list[ParsedInvoiceLine] = []
    for idx, block in enumerate(_split_item_blocks(items_section), start=1):
        parsed = _parse_line_block(block, idx)
        if parsed:
            lines.append(parsed)

    if not lines:
        raise ValueError(f'{filename} line items could not be parsed from the invoice table.')

    if not total_gross:
        total_gross = _sum_line_field(lines, 'gross_mass_kg')
    if not total_net:
        total_net = _sum_line_field(lines, 'net_mass_kg')
    if not total_value:
        total_value = _sum_line_field(lines, 'line_value')

    return ParsedInvoice(
        filename=filename,
        template_id='BIRKDALE_COMMERCIAL_V1',
        supplier_name='Birkdale Sales Ltd',
        invoice_number=_extract_scalar(raw_text, 'Commercial Invoice No.'),
        carrier_reference=_extract_scalar(raw_text, 'Carrier Reference'),
        external_document_number=_extract_scalar(raw_text, 'External Document No.'),
        trade_terms=_extract_scalar(raw_text, 'Trade Terms'),
        credit_terms=_extract_scalar(raw_text, 'Credit Terms'),
        consignee=consignee,
        delivery_to=delivery_to,
        total_gross_weight_kg=total_gross,
        total_net_weight_kg=total_net,
        total_invoice_value=total_value,
        total_packages=total_packages,
        page_count=page_count,
        raw_text=collapsed,
        lines=lines,
    )


def parse_birkdale_invoice_pdf(file_bytes: bytes, filename: str) -> ParsedInvoice:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PDF parsing requires the 'pypdf' package to be installed."
        ) from exc
    reader = PdfReader(BytesIO(file_bytes))
    text = '\n'.join((page.extract_text() or '') for page in reader.pages)
    return parse_birkdale_invoice_text(text, filename=filename, page_count=len(reader.pages))


def parse_birkdale_mapped_csv_text(text: str, filename: str = 'mapped.csv') -> ParsedInvoice:
    reader = csv.DictReader(StringIO(text))
    rows = [row for row in reader if any((value or '').strip() for value in row.values())]
    if not rows:
        raise ValueError(f'{filename} does not contain any mapped rows.')
    required = {'trader_reference', 'transport_document_number', 'commodity_code', 'goods_description'}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError(f'{filename} is not a supported mapped Birkdale CSV export.')

    first = rows[0]
    lines: list[ParsedInvoiceLine] = []
    for idx, row in enumerate(rows, start=1):
        lines.append(
            ParsedInvoiceLine(
                line_number=idx,
                commodity_code=_collapse_ws(row.get('commodity_code', '')),
                stock_code=_collapse_ws(row.get('package_marks', '')) or f'ROW{idx}',
                description=_collapse_ws(row.get('goods_description', '')),
                quantity=_clean_decimal(row.get('number_of_packages', '') or '1'),
                uom=_collapse_ws(row.get('type_of_packages', '')) or 'PK',
                gross_mass_kg=_clean_decimal(row.get('gross_mass_kg', '')),
                net_mass_kg=_clean_decimal(row.get('net_mass_kg', '')),
                line_value=_clean_decimal(row.get('item_invoice_amount', '')),
                currency=_collapse_ws(row.get('item_invoice_currency', '')) or 'GBP',
            )
        )

    consignee = ParsedParty(
        name=_collapse_ws(first.get('consignee_name', '') or first.get('importer_name', '')),
        lines=[
            _collapse_ws(first.get('consignee_street_number', '')),
            _collapse_ws(first.get('consignee_city', '')),
            _collapse_ws(first.get('consignee_postcode', '')),
            _collapse_ws(first.get('consignee_country', '')),
        ],
        postcode=_collapse_ws(first.get('consignee_postcode', '')),
        country_text=_collapse_ws(first.get('consignee_country', '')),
        country_code=_collapse_ws(first.get('consignee_country', '')),
    )
    delivery_to = ParsedParty(
        name=_collapse_ws(first.get('importer_name', '') or first.get('consignee_name', '')),
        lines=[
            _collapse_ws(first.get('importer_street_number', '')),
            _collapse_ws(first.get('importer_city', '')),
            _collapse_ws(first.get('importer_postcode', '')),
            _collapse_ws(first.get('importer_country', '')),
        ],
        postcode=_collapse_ws(first.get('importer_postcode', '')),
        country_text=_collapse_ws(first.get('importer_country', '')),
        country_code=_collapse_ws(first.get('importer_country', '')),
    )

    return ParsedInvoice(
        filename=filename,
        template_id='BIRKDALE_MAPPED_CSV_V1',
        supplier_name=_collapse_ws(first.get('consignor_name', '') or first.get('exporter_name', '')),
        invoice_number=_collapse_ws(first.get('trader_reference', '')) or Path(filename).stem,
        carrier_reference=_collapse_ws(first.get('transport_document_number', '')) or Path(filename).stem,
        external_document_number=_collapse_ws(first.get('trader_reference', '')),
        trade_terms='',
        credit_terms='',
        consignee=consignee,
        delivery_to=delivery_to,
        total_gross_weight_kg=_sum_line_field(lines, 'gross_mass_kg'),
        total_net_weight_kg=_sum_line_field(lines, 'net_mass_kg'),
        total_invoice_value=_sum_line_field(lines, 'line_value'),
        total_packages=_sum_line_field(lines, 'quantity'),
        page_count=1,
        raw_text=_collapse_ws(text),
        lines=lines,
    )


def parse_birkdale_invoice_file(file_bytes: bytes, filename: str) -> ParsedInvoice:
    suffix = Path(filename).suffix.lower()
    if suffix == '.pdf':
        return parse_birkdale_invoice_pdf(file_bytes, filename)
    if suffix == '.csv':
        text = file_bytes.decode('utf-8-sig', errors='replace')
        return parse_birkdale_mapped_csv_text(text, filename=filename)
    raise ValueError(f'{filename} is not a supported Birkdale intake file.')
