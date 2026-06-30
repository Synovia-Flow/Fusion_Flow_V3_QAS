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
| 3 | `003_seed_cfg.sql` | Seed: parameters (incl. integration + documentation roots), clients (BKD active; CWF + PLE inactive), credentials (refs only), per-client folder paths, email rules, API version, BKD Route A process map, 35 choice fields, status vocabulary |
| 4 | `004_exc_log_tables.sql` | EXC spine (`Execution`, `Transaction`, `Error`, `Data_Processing_Enhancement`) + LOG (`Process_Log`, `Error_Log`, `API_Trace`) |
| 5 | `005_ing_tables.sql` | ING ingestion landing (`Inbound_File`, `Raw_Record`, `Source_Email`) |
| 6 | `006_seed_graph_params.sql` | Seed Microsoft Graph app config into `CFG.Application_Parameters` (client id, tenant, mailbox, secret **reference**) |
| 7 | `007_cfg_ingestion_source.sql` | `CFG.Ingestion_Source` — DB-driven acquisition-channel registry per client (EMAIL active; SFTP/AS2/API registered + inactive) + processed subfolder |
| 8 | `008_ing_bkd_raw_tables.sql` | `ING.BKD_Raw_ENS` (typed, from ENS CSV) + `ING.BKD_Raw_Sales_Orders` (verbatim row JSON) |
| 9 | `009_cfg_tss_credentials_environments.sql` | `CFG.TSS_Environment` + `CFG.TSS_Credential` (per client/env: username, active, last verification result; passwords set in DB) |
| 10 | `010_prs_tables.sql` | PRS canonical-object tables: `ENS_Header`, `Consignment`, `Goods_Item` + 10 nested child tables (Module 2 Data Processing output; 1 header → many consignments → ≤99 goods + nested arrays) |
| 11 | `011_seed_processing_params.sql` | Module 2 run-control parameters in `CFG.Application_Parameters` (`PROCESSING_CLIENT`, `PROCESSING_TRANSACTION_MODE`, `PROCESSING_DRY_RUN`) — the runner has **no CLI** |
| 12 | `012_cfg_jobs.sql` | `CFG.Job` — canonical registry of scheduled jobs (purpose, module, client/channel, step order, entry point). Seeds the ingestion cycle + steps, the channel-acquire stubs, and the PRS processing job; plus `INGESTION_CLIENT` / `INGESTION_DRY_RUN`. The ingestion runner reads this table to drive the cycle. |
| 13 | `013_cfg_portal_tss_profiles.sql` | `CFG.Portal_Client_Profile`, `CFG.File_Profile`, `CFG.File_Profile_Column_Map`, `CFG.TSS_Submission_Route` - portal upload/TSS route profiles for PLE and CW. PLE uses attached file #1; CW uses attached file #2; both require ENS update before submit. |

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
