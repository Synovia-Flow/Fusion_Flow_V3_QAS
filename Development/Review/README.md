# Database Review — DataModel Analysis exporter

`export_datamodel.py` connects to the database, lets you pick schemas, and writes
a **styled Excel workbook** documenting the data model and its data.

## What it produces

- **Tab 1 `DataModel Analysis`** — database/metadata header, a **Schemas** summary
  (tables + row counts), a **Tables** index where each **table name is a hyperlink
  to that table's data tab**, and a **Relationships** section listing every
  foreign-key join (`from table.column -> to table.column`).
- **One tab per table** — all rows of the table (capped by `--max-rows`), styled
  header, frozen panes, and a `<- DataModel Analysis` link back to the front tab.

Output goes to `…\Documentation_Layer\Database_Analysis\Fusion_DataModel_Analysis_<stamp>.xlsx`
(the `Documentation_Layer` root is read from `CFG.Application_Parameters.DOCUMENTATION_OUTPUT_ROOT`).

## Usage

```powershell
cd "Development\Review"
python export_datamodel.py                  # lists schemas; pick e.g. 1,3,5 or A for All
python export_datamodel.py --all
python export_datamodel.py --schemas CFG,ING,EXC
python export_datamodel.py --schemas-only   # structure + joins only, no data tabs
python export_datamodel.py --all --max-rows 5000
python export_datamodel.py --all --out "C:\temp"   # override output folder
```

Connection is read from `Configuration\Fusion_Flow_QAS.ini` (same as the
deploy tool). Requires `pyodbc` and `openpyxl`.

## Interactive picker

```
Schemas in this database:
   1. CFG     (10 tables)
   2. CHG     (2 tables)
   3. EXC     (4 tables)
   ...
   A. All

Select schemas (e.g. 1,3,5  or  A for All):
```
