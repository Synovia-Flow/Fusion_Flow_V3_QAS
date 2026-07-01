# Fusion Flow V3 QAS — Database Setup (Phase 1: Foundation)

SQL setup scripts for the Release 1 foundation, derived from
`Documentation/FusionFlow_R1_Functional_Specification.docx` and
`Documentation/TSS_API_DataModel_v2_Process_Overlay.xlsx`.

This phase provisions the **11 schemas** and builds + seeds the **CFG**
(configuration) layer. The processing/staging/client/API/control/archive/serve
tables (ING, EXC, LOG, PRS, STG, API, BKD, CTL, ARC, SRV) are added in the
later module phases.

## Prerequisites

- An Azure SQL (or SQL Server) database named **`Fusion_Flow_V3_QAS`** exists and is selected.
- The scripts do **not** create the database and store **no secrets**.

## Run order

| # | File | Creates |
|---|------|---------|
| 0 | `000_chg_schema.sql` | `CHG` change-management schema (`Deployment`, `Change_Log`) — deploy audit |
| 1 | `001_create_schemas.sql` | The 11 schemas: CFG, ING, EXC, LOG, PRS, STG, API, CTL, ARC, SRV, BKD |
| 2 | `002_cfg_tables.sql` | CFG tables + FKs + indexes |
| 3 | `003_seed_cfg.sql` | Seed: parameters (incl. integration + documentation roots), clients (BKD active; CWD + PLE inactive), credentials (refs only), per-client folder paths, email rules, API version, BKD Route A process map, 35 choice fields, status vocabulary |
| 4 | `004_exc_log_tables.sql` | EXC spine (`Execution`, `Transaction`, `Error`, `Data_Processing_Enhancement`) + LOG (`Process_Log`, `Error_Log`, `API_Trace`) |
| 5 | `005_ing_tables.sql` | ING ingestion landing (`Inbound_File`, `Raw_Record`, `Source_Email`) |
| 6 | `006_seed_graph_params.sql` | Seed Microsoft Graph app config into `CFG.Application_Parameters` (client id, tenant, mailbox, secret **reference**) |
| 7 | `007_cfg_ingestion_source.sql` | `CFG.Ingestion_Source` — DB-driven acquisition-channel registry per client (EMAIL active; SFTP/AS2/API registered + inactive) + processed subfolder |
| 8 | `008_ing_bkd_raw_tables.sql` | `ING.BKD_Raw_ENS` (typed, from ENS CSV) + `ING.BKD_Raw_Sales_Orders` (verbatim row JSON) |
| 9 | `009_cfg_tss_credentials_environments.sql` | `CFG.TSS_Environment` + `CFG.TSS_Credential` (per client/env: username, active, last verification result; passwords set in DB) |
| 10 | `010_prs_tables.sql` | PRS canonical-object tables: `ENS_Header`, `Consignment`, `Goods_Item` + 10 nested child tables (Module 2 Data Processing output; 1 header → many consignments → ≤99 goods + nested arrays) |
| 11 | `011_seed_processing_params.sql` | Module 2 run-control parameters in `CFG.Application_Parameters` (`PROCESSING_CLIENT`, `PROCESSING_TRANSACTION_MODE`, `PROCESSING_DRY_RUN`) — the runner has **no CLI** |
| 12 | `012_cfg_jobs.sql` | `CFG.Job` — canonical registry of scheduled jobs (purpose, module, client/channel, step order, entry point). Seeds the ingestion cycle + steps, the channel-acquire stubs, and the PRS processing job; plus `INGESTION_CLIENT` / `INGESTION_DRY_RUN`. The ingestion runner reads this table to drive the cycle. |
| 13 | `013_prs_bkd_ens_header.sql` | `PRS.BKD_ENS_Header_Submission` — ENS Declaration Header in exact TSS field shape (27 fields) + `Fusion_Status` (STAGED→VALIDATED→SUBMITTED) + TSS read-only status; and `PRS.BKD_ENS_Header_Tracking` — the parallel control/source spine (ING lineage, status, timeline) — one row per movement. |
| 14 | `014_cfg_choice_field_map.sql` | `CFG.Choice_Field_Map` — maps each TSS choice-value set (`Choice_Field_Registry`) to the schema column(s) it governs (e.g. CV `mode_of_transport` → `movement_type`), with `MatchOn` (NAME/VALUE). Adds `transport_charges`/`controlled_goods_type`/`package_type` to the registry; seeds downloader controls (`CHOICE_VALUES_*`) and registers the `REF_FETCH_CHOICE_VALUES` job. Fed by `Modules/Global/fetch_choice_values.py`. |
| 15 | `015_cfg_choice_value_sync.sql` | Makes `CFG.Choice_Value_Cache` refreshable: adds change-tracking columns (`RowHash`, `ChangeStatus` NEW/CHANGED/UNCHANGED/REMOVED, `FirstSeenAt`, `LastSyncedAt`, `LastSyncExecutionID`), the `SYNCING`/`SYNCED`/`SYNC_FAILED` statuses, and views `CFG.vw_Choice_Value_Changes` + `CFG.vw_Choice_Sync_Summary`. The refresh logs to `EXC.Execution`/`Transaction`/`Error`. |
| 16 | `016_cfg_choice_fields_align.sql` | Aligns the registry/map to the authoritative TSS Choice Fields reference (35 fields): removes the invalid `transport_charges`/`controlled_goods_type`/`package_type`, fixes header `movement_type` → CV `movement_type` (not `mode_of_transport`), corrects `CHOICE_VALUES_PATH`, and adds `UsedBy` + descriptions per the reference. |
| 17 | `017_cfg_choice_value_widen.sql` | (Optional) widens `CFG.Choice_Value_Cache`: `ChoiceName` → `nvarchar(max)`, `ChoiceValue` → `nvarchar(255)` so long names are stored in full. Not required — the downloader truncates to the column widths regardless. |
| 18 | `018_cfg_commodity_code.sql` | Gives `commodity_code` (~35k codes) its own table `CFG.Commodity_Code_Cache` (+ `vw_Commodity_Code_Changes`); removes it from the general choice flow (deactivates in registry, clears its cache + map rows); registers `REF_FETCH_COMMODITY_CODES`. Fed by `Modules/Global/fetch_commodity_codes.py`. |
| 19 | `019_cfg_processing_map.sql` | Config-driven processing: `CFG.Processing_Profile` (per client+entity: source ING table → target PRS tables), `CFG.Processing_Field_Map` (per field: source, transform type, choice set, mandatory/condition, max len), `CFG.Carrier_Master` (MASTER_ENRICH). Seeds the BKD ENS_HEADER profile + field map; registers `PRS_ENGINE_BKD_ENS`. Driven by `Modules/Processing/process_engine.py`. |
| 20 | `020_prs_error_views.sql` | Error/rejection views: `SRV.vw_Processing_Errors` (all clients, from `LOG.Error_Log`+`EXC`), `PRS.vw_BKD_ENS_Header_Status` / `_Rejected` (per movement + offending values), `PRS.vw_BKD_ENS_Header_Reasons` (one row per reason — `GROUP BY` to rank failures). |
| 21 | `021_prs_reprocess.sql` | Reprocess support: adds `ReprocessCount` / `ResolvedAt` / `ResolvedByExecutionID` to the tracking table + `PRS.vw_BKD_ENS_Header_Resolved`; seeds `PROCESSING_MODE` / `_REPROCESS_SCOPE` / `_MOVEMENT_KEY`; registers `PRS_REPROCESS_BKD_ENS`. Driven by `Modules/Processing/reprocess_engine.py`. |
| 22 | `022_cfg_value_translation.sql` | Local value translation: `CFG.Value_Translation` lets a **specific file** (or a whole client/field) DEFINE the output code for an incoming value, consulted before the choice-value resolver (most-specific `SourceFile` wins over `'*'`; `MatchMode` CI/EXACT/NORM). Seeds BKD ENS `arrival_port 'Belfast Port'→GBAUBELBELBEL` and `movement_type 'RoRo Accompanied ICS2'→3a`. Applied in `process_engine.py` (`TranslationResolver`). |
| 23 | `023_cfg_db_snapshot_job.sql` | Registers the full-DB snapshot report: seeds `DB_SNAPSHOT_OUTPUT_DIR` and the `REP_DB_SNAPSHOT_XLSX` job. Fed by `Modules/Global/export_db_snapshot.py` — every base table to one `.xlsx` (a tab per populated table, one "Zero Records" tab, a Summary tab, and a Column Analysis tab). |
| 24 | `024_cfg_reference_lists_job.sql` | Registers the reference/option-lists export (`REP_REFERENCE_LISTS_XLSX`). Fed by `Modules/Global/export_reference_lists.py` — curated CFG option lists to a separate `.xlsx`, one tab per list (Vocabulary = `Status_Vocabulary`, Clients, Jobs, Choice Fields/Map, Translations, Processing Profiles/Field Map, Carriers, Ingestion Sources, API Versions/Process Map, TSS Environments, Parameters) + an Index of links; secret values masked. |
| 25 | `025_cfg_alignment_cleanup.sql` | Alignment cleanup: deactivates the superseded `PRS_PROCESS_BKD` / `PRS_STAGE_BKD_ENS` jobs (engine is the live path); consolidates CountryWide to a single code **CWD** (removes the `CWF` duplicate + its empty schema across all CFG tables); standardises `DEFAULT_ENV` → `TST` (matches `CFG.TSS_Environment`); and reconciles the vocabulary — `STAGED` = pre-validation submission staging (SortOrder 45), new `STG_MATERIALISED` (60) for the STG-schema step, and widens the BKD ENS `Fusion_Status` CHECKs to the full movement lifecycle. |

All scripts are **idempotent** — safe to re-run (existence checks + `MERGE`).

Example (sqlcmd):

```bash
sqlcmd -S <server> -d Fusion_Flow_V3_QAS -i 001_create_schemas.sql
sqlcmd -S <server> -d Fusion_Flow_V3_QAS -i 002_cfg_tables.sql
sqlcmd -S <server> -d Fusion_Flow_V3_QAS -i 003_seed_cfg.sql
```

## CFG tables

| Table | Purpose |
|-------|---------|
| `CFG.Application_Parameters` | Global runtime settings — **all non-connection settings** (env, rate limit, GMR wait, SDI deadline, roots). The `.ini` holds only the DB connection. |
| `CFG.Clients` | Principal registry: 3-letter code → schema + STG prefix + route |
| `CFG.Credentials` | TSS API username + **Key Vault secret reference** (no plaintext secret) |
| `CFG.Folder_Paths` | Per-client INBOUND / ENS_SOURCE / PROCESS / FAIL / ARCHIVE folders |
| `CFG.Email_Rules` | Per-client mailbox / sender rule / allowed file types |
| `CFG.API_Version` | New/Old version switch + TEST/PROD base URLs per client/resource |
| `CFG.API_Process_Map` | Ordered API operations per client/route (endpoint, op_type, waits) |
| `CFG.Choice_Field_Registry` | The 35 choice fields to bootstrap from `GET /choice_values` |
| `CFG.Choice_Value_Cache` | Cached choice values (populated by the bootstrap job) |
| `CFG.Status_Vocabulary` | The single shared process/status model (Spec §3.3) |

## Review before production

The seed contains placeholders that **must** be confirmed:

- **Folder root** (`@Root` in `003`) — set to the confirmed operational file store.
- **TSS API usernames** (`CFG.Credentials.ApiUsername`) and Key Vault secret names.
- **PLE `ActAsSysId`** — obtain from `GET /agent_relationships` before activating.
- **BKD sender domain** (`birkdalesales.com`) and mailbox.

## Critical rules baked into the seed

`/headers` not `/declaration_headers` (Rule 1) · SFD lookup by `consignment_number` (Rule 2) ·
GMR = create + submit + 90s wait (Rule 19) · rate limit 0.25s (Rule 14) ·
Permission Grant result is a LIST (Rule 7) · update = full replacement (Rule 16).
