# Fusion Flow V3 QAS — Build Status & Process Summary

_Regroup snapshot — 2026-06-30. Single reference for where the platform stands
before the next phase. Authoritative DDL lives in `Configuration/SQL/`; the live
deployed schema is mirrored to `Documentation/DB_Schema.md` on every deploy run._

---

## 1. Schemas (11 + deploy audit)

| Schema | Role |
|--------|------|
| **CFG** | Configuration — clients, jobs, parameters, choice values, processing map, translations |
| **ING** | Ingestion landing — raw inbound artefacts (verbatim) |
| **PRS** | Processing — canonical / submission-shaped objects + tracking |
| **STG** | Staging — `PRS.Processed` records promoted here for submission |
| **EXC** | Execution spine — one run, per-record transactions, per-field change audit |
| **LOG** | Process / error / API trace logs |
| **API** | TSS API call records |
| **CTL** | Control |
| **ARC** | Archive of reconciled terminal records |
| **SRV** | Serve — cross-client views |
| **BKD** | Birkdale client schema (CWD / PLE follow the same pattern) |
| **CHG** | Deploy audit — `Deployment`, `Change_Log` |

---

## 2. Database migration set (canonical, `Configuration/SQL/`)

All 26 scripts (000–025) are idempotent and consolidated in `Configuration/SQL/`
as the single source of truth. Stage pending scripts with `stage_queue.py`, then
`deploy.py` (moves to `Archive/<run-stamp>/`, logs CHG, regenerates `DB_Schema.md`).

| # | File | Deployed? |
|---|------|-----------|
| 000–024 | schemas, CFG seed, EXC/LOG, ING, PRS, jobs, BKD ENS tables, choice map/sync/align, processing map, error views, reprocess, value translation, snapshot + reference-list jobs | ✅ deployed |
| **025** | alignment cleanup: deactivate superseded jobs, consolidate CountryWide → CWD, `DEFAULT_ENV`→`TST`, vocabulary reconcile + widened `Fusion_Status` CHECKs | ⏳ **pending deploy** |

> The live DB is at **024**. Staging + deploying 025 applies the cleanup and regenerates `DB_Schema.md`.

---

## 3. Jobs / processes (CFG.Job)

| Job code | Module | Purpose | Entry point |
|----------|--------|---------|-------------|
| `ING_BKD_CYCLE` | Ingestion | Orchestrates the Birkdale ingestion cycle | `run_ingestion` |
| `ING_BKD_ACQUIRE_EMAIL` | Ingestion | Pull Birkdale email attachments (Graph) | `graph_email` |
| `ING_BKD_PARSE_ENS` | Ingestion | Parse ENS headers → CSV | `ens_headers` |
| `ING_BKD_LOAD_RAW` | Ingestion | Load raw ENS + Sales Orders → `ING` | `load_raw` |
| `ING_ACQUIRE_FILE_DROP` / `_SFTP` / `_API` | Ingestion | Alternate acquisition channels (registered, inactive) | — |
| `REF_FETCH_CHOICE_VALUES` | Global | Refresh the 35 TSS choice-value sets → `CFG.Choice_Value_Cache` | `fetch_choice_values` |
| `REF_FETCH_COMMODITY_CODES` | Global | Refresh ~35k commodity codes → `CFG.Commodity_Code_Cache` | `fetch_commodity_codes` |
| `PRS_ENGINE_BKD_ENS` | Processing | Config-driven engine: ING → PRS for new BKD ENS rows | `process_engine` |
| `PRS_REPROCESS_BKD_ENS` | Processing | Re-run already-processed BKD ENS rows, close off resolved errors | `reprocess_engine` |
| `REP_DB_SNAPSHOT_XLSX` | Reporting | Full DB → Excel (tab per table, Zero Records, Summary, Column Analysis) | `export_db_snapshot` |
| `REP_REFERENCE_LISTS_XLSX` | Reporting | Curated CFG option lists → Excel (Vocabulary etc. + Index) | `export_reference_lists` |
| ~~`PRS_PROCESS_BKD`~~ / ~~`PRS_STAGE_BKD_ENS`~~ | Processing | **Deactivated (025)** — superseded by the engine | — |

All runners are **CLI-free** — driven by `CFG.Application_Parameters`. The scheduler
just runs `python <script>.py`.

---

## 4. Status models

### 4a. `Fusion_Status` — the per-movement lifecycle (PRS tables)
`STAGED → VALIDATED → STG_MATERIALISED → READY → SUBMITTING → SUBMITTED`, with
`REJECTED` (validation failed, reason held) and `CANCELLED`. Here **`STAGED` means
staged into the client submission table pre-validation** (aligned to the vocabulary
in migration 025). Reprocess moves `REJECTED → VALIDATED` in place and stamps
`ResolvedAt` so the record leaves the error views. The `Fusion_Status` CHECK now
allows the full movement lifecycle (a consistent subset of the vocabulary), not
just five values.

### 4b. `CFG.Status_Vocabulary` — the full shared process model
The end-to-end model every module reports against (terminal/exception flagged):

`INGESTED → NORMALISED → ENRICHED → CONSTRUCTED → STAGED → VALIDATED` (or `REJECTED`)
`→ STG_MATERIALISED → READY → LINKED → SUBMITTING → SUBMITTED → ACKNOWLEDGED / IN_PROGRESS
→ RECONCILED` (or `MISMATCH`) `→ ARCHIVED`. Exception states: `ERROR`, `ON_HOLD`,
`CANCELLED`. Reference-data refresh: `SYNCING → SYNCED` / `SYNC_FAILED`.
`STAGED` = staged into the submission table (pre-validation, SortOrder 45);
`STG_MATERIALISED` = validated record materialised into the STG schema (60).

---

## 5. Processing engine mechanics (`Modules/Processing/process_engine.py`)

One engine processes every principal; per-client behaviour is **configuration, not code**:

- **`CFG.Processing_Profile`** — per client+entity: source ING table → target PRS tables + keys.
- **`CFG.Processing_Field_Map`** — per field: source column, transform type, choice set,
  mandatory/conditional rule, max length, step order.
- **Transform types**: `CONST`, `PASSTHROUGH`, `CODE`, `YESNO`, `DATE_UTC`, `CHOICE`,
  `MASTER_ENRICH`, `QAS`, `READONLY`, `API_RETURN`, `DERIVE`.
- **Value resolution order for a field**:
  1. **`CFG.Value_Translation`** (local translation) — a specific *file* (or whole
     client/field) defines the output; most-specific `SourceFile` wins over `'*'`.
  2. **`CFG.Choice_Value_Cache`** resolver — exact code → exact name → token-boundary
     prefix → token-set match (bracket/punctuation/order-proof).
  3. otherwise pass through / flag unresolved.
- **`CFG.Carrier_Master`** — `MASTER_ENRICH` of the carrier block by EORI.
- Every field change is logged to **`EXC.Data_Processing_Enhancement`** (old → new + rule);
  one `EXC.Execution` per run, one `EXC.Transaction` per record; failures to `EXC.Error` / `LOG.Error_Log`.

### Modes
- **NEW** (default) — process untracked ING rows.
- **REPROCESS** — re-run already-processed rows (scope `REJECTED` | `ALL`, optional
  `MOVEMENT_KEY`); selects candidates from the **submission** table by `Fusion_Status`
  and matches raw rows by `MovementKey` (+ `SourceEnsLoadID` fallback), upserts in place,
  bumps `ReprocessCount`, and closes off resolved errors.

### Error / rejection visibility
`SRV.vw_Processing_Errors` (all clients) · `PRS.vw_BKD_ENS_Header_Status` /
`_Rejected` / `_Reasons` (rank failures) · `PRS.vw_BKD_ENS_Header_Resolved` (closed-off).

---

## 6. Local translations seeded (BKD ENS)

| Field | Incoming | → Output |
|-------|----------|----------|
| `arrival_port` | `Belfast Port` | `GBAUBELBELBEL` |
| `movement_type` | `RoRo Accompanied ICS2` | `3a` |

Both scoped to all BKD ENS files (`SourceFile = '*'`). To let one file define its own
mapping, add a row with that file's name — it takes precedence.

---

## 7. Repository hygiene

- Canonical migration set restored to `Configuration/SQL/` (was fragmented across `Archive/`).
- Removed stray `NewFile`, empty `Integration_Layer/`, and `__pycache__/`.
- Secret scan clean: no leaked DB/TSS/Graph secrets in the tracked tree. Live
  `*.ini` and `tss_credentials.json` remain gitignored (templates only are committed).
- `Deprecated/` retained intentionally (prior layer kept for reference). `Archive/<stamp>/`
  snapshots retained as the deploy audit trail.

---

## 8. Next-phase candidates

1. **Deploy 025** (alignment cleanup).
2. Onboard CWD / PLE: seed their `Processing_Profile` + `Field_Map` (no code change).
3. Build the **STG promotion** step (VALIDATED → `STG_MATERIALISED` into the STG schema)
   and the TSS **submission** module (Route A), using the now-widened `Fusion_Status`.
4. Reconcile any orphaned `SYNCING` executions.
