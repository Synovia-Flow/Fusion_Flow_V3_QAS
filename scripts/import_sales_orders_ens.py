"""
NOT FOR PRD: writes BKD.StagingConsignments / BKD.StagingGoodsItems removed by migration 078.
             Use STG.BKD_ENS_Consignments / STG.BKD_GoodsItems for new pipeline work.

Import Synovia Sales Orders Excel -> BKD.StagingConsignments + BKD.StagingGoodsItems.

Each unique "Sell-to Customer No." becomes one consignment.
Each row in that group becomes one goods item.

The Excel must be the Business Central Sales Orders report (Sheet1).
Headers are on row 14, data starts on row 15.

Run:
    python scripts/import_sales_orders_ens.py --file path/to/Sales_Orders.xlsx
    python scripts/import_sales_orders_ens.py --file ... --declaration-id 42
    python scripts/import_sales_orders_ens.py --file ... --staging-ens-id 7
    python scripts/import_sales_orders_ens.py --file ... --declaration-id 42 --dry-run

ENS header link (staging_ens_id) can be supplied as:
  --declaration-id   ID from BKD.StagingDeclarations (portal/email ENS header)
  --staging-ens-id   Direct staging_id from BKD.StagingEnsHeaders

If neither is given, consignments are created unlinked and must be linked manually
before submit.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import openpyxl
import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.db_connection import build_connection_string

# ── Excel column indices (0-based) ───────────────────────────────────────────
# Sheet1: row 14 = headers, row 15+ = data, last row = totals marker.
_COL_MARKER      = 0   # A: 'AutoTable' on data rows, 'Total' on totals row
_COL_CUSTOMER_NO = 3   # D: Sales Header - Sell-to Customer No.
_COL_SHIP_NAME   = 4   # E: Sales Header - Ship-to Name
_COL_SHIP_ADDR1  = 5   # F: Sales Header - Ship-to Address
_COL_SHIP_ADDR2  = 6   # G: Sales Header - Ship-to Address 2
_COL_SHIP_CITY   = 7   # H: Sales Header - Ship-to City
_COL_SHIP_COUNTY = 8   # I: Sales Header - Ship-to County
_COL_DOC_NO      = 11  # L: Document No.  (sales order number)
_COL_ITEM_NO     = 12  # M: No.           (product code)
_COL_QTY         = 14  # O: Quantity      (number of packages)
_COL_LINE_AMT    = 17  # R: Line Amount Excl. VAT
_COL_UOM         = 20  # U: Unit of Measure Code

# ── UOM → TSS type_of_packages code ─────────────────────────────────────────
_UOM_MAP = {
    'box': 'BX',
    'bx':  'BX',
    'carton': 'CT',
    'ctn': 'CT',
    'pallet': 'PX',
    'plt': 'PX',
    'bag': 'BG',
    'drum': 'DR',
    'roll': 'RL',
    'piece': 'PK',
    'pcs': 'PK',
    'each': 'PK',
}


def _v(row, col: int) -> str:
    try:
        v = row[col]
        return str(v).strip() if v is not None else ''
    except IndexError:
        return ''


def _map_uom(uom: str) -> str:
    lower = (uom or '').lower()
    for k, code in _UOM_MAP.items():
        if k in lower:
            return code
    return 'PK'


def _safe_int(val) -> int:
    try:
        return max(1, int(float(val)))
    except (TypeError, ValueError):
        return 1


def _safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ── Excel reader ─────────────────────────────────────────────────────────────

def load_rows(path: str) -> list[dict]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb['Sheet1']
    rows = []
    for row in ws.iter_rows(min_row=15, values_only=True):
        if _v(row, _COL_MARKER) != 'AutoTable':
            continue
        customer_no = _v(row, _COL_CUSTOMER_NO)
        # Skip totals row and blank customer numbers
        if not customer_no or customer_no.lower() == 'total':
            continue
        rows.append({
            'customer_no': customer_no,
            'ship_name':   _v(row, _COL_SHIP_NAME),
            'ship_addr1':  _v(row, _COL_SHIP_ADDR1),
            'ship_addr2':  _v(row, _COL_SHIP_ADDR2),
            'ship_city':   _v(row, _COL_SHIP_CITY),
            'ship_county': _v(row, _COL_SHIP_COUNTY),
            'doc_no':      _v(row, _COL_DOC_NO),
            'item_no':     _v(row, _COL_ITEM_NO),
            'qty':         row[_COL_QTY],
            'line_amt':    row[_COL_LINE_AMT],
            'uom':         _v(row, _COL_UOM),
        })
    wb.close()
    return rows


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r['customer_no'], []).append(r)
    return groups


# ── DB helpers ───────────────────────────────────────────────────────────────

def resolve_staging_ens_id(conn, declaration_id: int) -> int | None:
    cur = conn.cursor()
    cur.execute(
        'SELECT TOP 1 staging_id FROM BKD.StagingEnsHeaders'
        ' WHERE staging_declaration_id = ?',
        [declaration_id],
    )
    row = cur.fetchone()
    cur.close()
    return int(row[0]) if row else None


_CONS_INSERT = """
    INSERT INTO BKD.StagingConsignments (
        staging_ens_id, label, goods_description, trader_reference,
        transport_document_number, controlled_goods, goods_domestic_status,
        destination_country, supervising_customs_office, customs_warehouse_identifier,
        ducr, no_sfd_reason,
        consignor_eori, consignor_name, consignor_street_number,
        consignor_city, consignor_postcode, consignor_country,
        consignee_eori, consignee_name, consignee_street_number,
        consignee_city, consignee_postcode, consignee_country,
        importer_eori, importer_name, importer_street_number,
        importer_city, importer_postcode, importer_country,
        exporter_eori, exporter_name, exporter_street_number,
        exporter_city, exporter_postcode, exporter_country,
        buyer_same_as_importer, seller_same_as_exporter,
        buyer_eori, buyer_name, buyer_street_and_number,
        buyer_city, buyer_postcode, buyer_country,
        seller_eori, seller_name, seller_street_and_number,
        seller_city, seller_postcode, seller_country,
        container_indicator, align_ukims, use_importer_sde,
        declaration_choice, generate_SD,
        status, retry_count, max_retries, created_at, source
    )
    OUTPUT INSERTED.staging_id
    VALUES (
        ?,?,?,?,?,?,?,?,?,?,?,?,
        ?,?,?,?,?,?,?,?,?,?,?,?,
        ?,?,?,?,?,?,?,?,?,?,?,?,
        ?,?,?,?,?,?,?,?,?,?,?,?,
        ?,?,?,?,?,?,?,?,?,
        'PENDING', 0, 3, SYSUTCDATETIME(), 'excel_import'
    )
"""

_GOODS_INSERT = """
    INSERT INTO BKD.StagingGoodsItems (
        staging_cons_id, item_number, label,
        goods_description, type_of_packages, number_of_packages, package_marks,
        gross_mass_kg, net_mass_kg,
        controlled_goods, controlled_goods_type,
        commodity_code, procedure_code, additional_procedure_code,
        country_of_origin, preference,
        item_invoice_amount, item_invoice_currency,
        valuation_method, invoice_number,
        ni_additional_information_codes,
        status, retry_count, max_retries, created_at, source
    )
    OUTPUT INSERTED.staging_id
    VALUES (
        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
        'PENDING', 0, 3, SYSUTCDATETIME(), 'excel_import'
    )
"""


def insert_consignment(cursor, staging_ens_id: int | None,
                        customer_rows: list[dict], dry_run: bool) -> int | None:
    first = customer_rows[0]
    doc_nos = sorted({r['doc_no'] for r in customer_rows if r['doc_no']})
    label = f"{first['ship_name']} ({first['customer_no']})"[:200]
    consignee_name = (first['ship_name'] or '')[:70] or None
    consignee_addr = (first['ship_addr1'] or '')[:140] or None
    consignee_city = (first['ship_city'] or '')[:70] or None
    trader_ref = ', '.join(doc_nos)[:100] or None

    params = [
        staging_ens_id,   # staging_ens_id
        label,            # label
        None,             # goods_description
        trader_ref,       # trader_reference
        None,             # transport_document_number
        'no',             # controlled_goods
        None,             # goods_domestic_status
        'GB',             # destination_country (Northern Ireland)
        None, None,       # supervising_customs_office, customs_warehouse_identifier
        None, None,       # ducr, no_sfd_reason
        # consignor (6)
        None, None, None, None, None, None,
        # consignee (6)
        None, consignee_name, consignee_addr, consignee_city, None, 'GB',
        # importer (6)
        None, None, None, None, None, None,
        # exporter (6)
        None, None, None, None, None, None,
        # buyer/seller flags + buyer (8)
        'yes', 'yes',
        None, None, None, None, None, None,
        # seller (6)
        None, None, None, None, None, None,
        # remaining flags (5)
        'no',   # container_indicator
        None,   # align_ukims
        None,   # use_importer_sde
        None,   # declaration_choice
        None,   # generate_SD
    ]

    if dry_run:
        print(f'    [DRY RUN] consignment: {label}  trader_ref={trader_ref}')
        return None

    cursor.execute(_CONS_INSERT, params)
    row = cursor.fetchone()
    return int(row[0]) if row else None


def insert_goods_item(cursor, staging_cons_id: int, item: dict,
                       item_number: int, dry_run: bool) -> int | None:
    label = f"Item {item_number}: {item['item_no']}"[:200]
    qty = _safe_int(item['qty'])
    line_amt = _safe_float(item['line_amt'])
    type_pkg = _map_uom(item['uom'])

    if dry_run:
        print(f'    [DRY RUN] goods {item_number}: {item["item_no"]}  '
              f'qty={qty}  uom={item["uom"]}  amt={line_amt}')
        return None

    cursor.execute(_GOODS_INSERT, [
        staging_cons_id, item_number, label,
        item['item_no'] or None,  # goods_description = product code
        type_pkg, qty, 'ADDR',
        0.0, None,                # gross/net mass not in Excel
        None, None,               # controlled_goods
        None, None, None,         # commodity, procedure, additional_procedure
        'GB', None,               # country_of_origin, preference
        line_amt, 'GBP',          # invoice amount / currency
        None,                     # valuation_method
        item['doc_no'] or None,   # invoice_number = sales order no.
        None,                     # ni_additional_information_codes
    ])
    row = cursor.fetchone()
    return int(row[0]) if row else None


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description='Import Synovia Sales Orders Excel -> BKD consignments + goods items',
    )
    ap.add_argument('--file', required=True,
                    help='Path to Sales Orders Excel file (.xlsx)')
    ap.add_argument('--declaration-id', type=int, default=None,
                    help='BKD.StagingDeclarations.id of the ENS header to link to')
    ap.add_argument('--staging-ens-id', type=int, default=None,
                    help='BKD.StagingEnsHeaders.staging_id (direct, skips declaration lookup)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Parse and print without writing to DB')
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f'ERROR: file not found: {path}')
        sys.exit(1)

    print(f'Reading {path.name} ...')
    rows = load_rows(str(path))
    print(f'  {len(rows)} data rows found.')

    if not rows:
        print('Nothing to import.')
        return

    groups = _group(rows)
    print(f'  {len(groups)} consignment(s): {", ".join(groups)}')

    staging_ens_id = args.staging_ens_id

    conn = cursor = None
    if not args.dry_run:
        conn = pyodbc.connect(build_connection_string(timeout=30), autocommit=False)
        cursor = conn.cursor()

    if staging_ens_id is None and args.declaration_id is not None:
        if args.dry_run:
            print(f'  [DRY RUN] Would resolve staging_ens_id from declaration_id={args.declaration_id}')
        else:
            staging_ens_id = resolve_staging_ens_id(conn, args.declaration_id)
            if staging_ens_id:
                print(f'  Resolved staging_ens_id={staging_ens_id} '
                      f'from declaration_id={args.declaration_id}')
            else:
                print(f'  WARN: no StagingEnsHeaders row for '
                      f'declaration_id={args.declaration_id} — consignments will be unlinked')

    if staging_ens_id is None:
        print('  WARN: no ENS link — link staging_ens_id manually before submit')

    total_cons = total_goods = errors = 0

    for customer_no, customer_rows in groups.items():
        first = customer_rows[0]
        print(f'\n  [{customer_no}] {first["ship_name"]} — {len(customer_rows)} goods item(s)')

        try:
            cons_id = insert_consignment(cursor, staging_ens_id, customer_rows, args.dry_run)
            total_cons += 1

            for i, item in enumerate(customer_rows, start=1):
                gid = insert_goods_item(cursor, cons_id or 0, item, i, args.dry_run)
                if gid:
                    print(f'    goods {i}: id={gid}  {item["item_no"]}  qty={_safe_int(item["qty"])}')
                total_goods += 1

        except Exception as exc:
            print(f'  ERROR [{customer_no}]: {exc}')
            errors += 1
            if conn:
                conn.rollback()
            continue

        if conn:
            conn.commit()
            if cons_id:
                print(f'  CREATED consignment id={cons_id}')

    if cursor:
        cursor.close()
    if conn:
        conn.close()
    print(f'\nDone. consignments={total_cons}  goods_items={total_goods}  errors={errors}')


if __name__ == '__main__':
    main()
