# Control Tower — traceability upgrade proposal

Based on a read-only inspection of `Fusion_Flow_V3_QAS` (Azure SQL, 2026-07-07).
Finding: **the database already stores a complete lineage graph; the Control Tower
(`GET /api/control-tower`, `main.py:1898`) surfaces none of it** — only flat counts and
disconnected "recent N" lists.

## What the DB already has (verified live)

| Evidence | Table | Live rows | Key columns |
| --- | --- | --- | --- |
| Email provenance | `ING.Source_Email` | 1 | `GraphMessageID`, `InternetMessageID`, `Sender`, `Subject`, `ReceivedUtc` |
| File provenance | `ING.Inbound_File` | 12 | `FileHash`, `SourcePath`, `Mailbox`, `Sender`, `RowsLanded`, `FailReason` |
| Raw row lineage | `ING.BKD_Raw_ENS` / `ING.BKD_Raw_Sales_Orders` | 14 / 730 | `SourceFile`, `SourceCsv`, `RowNumber`, `RowHash`, `DedupKey`, `ExecutionID` |
| **Field-level audit** | `EXC.Data_Processing_Enhancement` | **10,705** | `EntityRef` (`MK=…`), `ColumnName`, `OldValue→NewValue`, `RuleApplied`, `ExecutionID` |
| Run spine | `EXC.Execution` / `EXC.Transaction` | 406 / 462 | every table carries `ExecutionID` + `TransactionID` |
| Stage stamps | `STG.BKD_ENS_Header` | 14 | `PromoteExecutionID`, `SubmitExecutionID`, `MirrorExecutionID` |
| TSS evidence | `API.Call` + `API.Response_Document` | 140 / 44 | **`RequestJson`, `ResponseJson`**, `RequestUrl`, headers, `DurationMs`, `MovementKey` |
| Errors | `EXC.Error` / `LOG.Error_Log` | 83 / 115 | linked by ExecutionID — **not shown anywhere today** |
| Ready-made views | `API.vw_Call_Errors/_Log`, `PRS.vw_BKD_ENS_Header_Reasons/Rejected/Resolved/Status`, `SRV.vw_Processing_Errors`, `CFG.vw_Choice_Sync_Summary` | — | unused by the portal |

DPE rule distribution today (top): `DP-FR-01:MAP_SO_GOODS` 4,372 · `ASSUMPTION:GOODS_DESCRIPTION_FROM_ITEM_CODE` 678 ·
`MASTERDATA:BKD_PRODUCT_MASTER_*` 446×6 · `ASSUMPTION:PACKAGE_TYPE_FROM_UOM` 378 ·
`ASSUMPTION:BKD_DEFAULT_CONTROLLED_GOODS_NO` 258 · `ASSUMPTION:BKD_IMPORTER_FALLBACK (Rule 13)` 148.

## Proposed improvements (priority order)

### 1. Movement Trace — the lineage chain, one movement end-to-end

New endpoint `GET /api/trace/{movementKey}` that walks the graph and returns one
chronological chain; portal renders it as a vertical timeline in the movement drill-down:

```
Email (Sender, Subject, ReceivedUtc, GraphMessageID)
  └─ File (SourceFile, FileHash, RowsLanded)
      └─ Raw rows (RowNumber, RowHash)                      [ING, ExecutionID]
          └─ PRS header/consignments/goods                  [ExecutionID]
              ├─ Field changes: N mapped / N enriched / N assumed   [DPE]
              └─ Validation verdict + reasons                [vw_BKD_ENS_Header_Reasons]
                  └─ Promote → STG                           [PromoteExecutionID]
                      └─ Submit → TSS                        [SubmitExecutionID, API.Call → RequestJson]
                          └─ Response (StatusCode, ens ref)  [ResponseJson]
                              └─ Mirror status               [MirrorExecutionID, TSS.BKD_ENS_Header]
```

Join sketch (all existing columns, no schema change):
`DPE.EntityRef LIKE 'MK=' + @mk + '%'` · `API.Call.MovementKey = @mk` ·
`STG.BKD_ENS_Header.MovementKey = @mk` → its three ExecutionIDs → `EXC.Execution` ·
raw rows via the header's ExecutionID + `SourceFile`.

### 2. Field provenance panel (consignment/goods detail modal)

Per field: current value + badge `MAP` / `MASTERDATA` / `ASSUMPTION` + old→new + when +
ExecutionID. Source: DPE filtered by `EntityRef`. This answers the audit question
"why does TSS have this value?" in one click. Assumption count per movement is the
risk signal (portal already shows a validation mini-badge — wire it to DPE evidence).

### 3. Execution drill-down

Executions list rows become clickable → `GET /api/executions/{id}`: its
`LOG.Process_Log` lines, DPE changes, `API.Call`s, `EXC.Error` rows. Today the Log page
shows flat lists; the join key (`ExecutionID`) is already in every row.

### 4. TSS evidence viewer

`API.Call.RequestJson` / `ResponseJson` + `API.Response_Document` rendered as
pretty-printed JSON per declaration (read-only). The exact payload sent and TSS's exact
answer = the compliance evidence pack. Redact credentials in `RequestHeaders`.

### 5. Assumption dashboard (data-quality debt)

Aggregate DPE by `RuleApplied` prefix over time: mapped vs masterdata vs assumed.
678 goods descriptions falling back to item code and 258 controlled_goods defaulted to
`no` are operational risks that today are invisible. Trend per client/week; drill to the
movements affected.

### 6. Surface errors + use the views

- Health strip: `EXC.Error` (83) + `LOG.Error_Log` (115) counts + latest, linked to executions.
- Replace raw-table queries with the curated views (`API.vw_Call_Errors`,
  `PRS.vw_BKD_ENS_Header_Rejected/Resolved`, `SRV.vw_Processing_Errors`) — they already
  encode the join logic.
- Choice/commodity cache freshness from `CFG.vw_Choice_Sync_Summary`
  (`LastSyncExecutionID`) — stale reference data is a silent submit-failure cause.

### 7. Transaction correlation

`EXC.Transaction` (462) groups executions of one logical run. Control Tower executions
list should group by `TransactionID` so one portal action reads as one story, not five
disconnected rows.

## Known lineage break (ties to mapping doc gaps)

Masterdata enrichment (`MASTER_ENRICH`, `MASTERDATA:*` DPE rows) points at
`CFG.Partner_Master` (219 rows) / `CFG.Product_Master` (7,932 rows) — tables **without**
lineage columns. The DPE row proves *that* a value came from masterdata but not *which
source row/version*. Fixed by the `ING.Customer_Master`/`ING.Product_Master` target
(mapping doc gaps 1-2): DPE `RuleApplied` should then carry the masterdata `RowHash` or
source ID, closing the last gap in the chain.

## Implementation notes

- All read-only; no schema changes needed for items 1-4, 6-7. Item 5 is a GROUP BY.
- Add to `fusion_api/app/main.py` following the existing `safe_rows`/`object_exists`
  pattern (missing table → empty, portal degrades gracefully).
- DPE at 10.7k rows is fine to query filtered by EntityRef/ExecutionID; add
  `TOP`/paging like the other endpoints. If it grows, an index on `EntityRef` is the
  only DB change worth proposing.
