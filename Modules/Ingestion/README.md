# Module 1 — Ingestion (SKELETON)

Acquires inbound data per client and lands it **verbatim** into `ING`, opening
one `EXC.Execution` (Transaction_ID) per run with full provenance and dedup
hashing.

> **Status: skeleton.** The orchestration, config loading, EXC/LOG wiring and
> verbatim-landing helpers are implemented. The per-route fetch/parse logic is
> **stubbed** because each client/channel differs — implement one class to add a
> route.

## Channels (routes)

| Class | Name | Status |
|---|---|---|
| `EmailGraphChannel` | EMAIL | **Implemented** — Microsoft Graph (`graph_email.py`): scan mailbox, download non-image attachments, land to ING, move to `Fusion_Processed`. Config from `CFG.Application_Parameters` (seeded by `006`). |
| `FileDropChannel` | FILE_DROP | skeleton — scan client `INBOUND` folder, read files |
| `SftpChannel` | SFTP | skeleton — SFTP listing + get |
| `RestChannel` | REST | skeleton — REST pull |

### EMAIL route (Microsoft Graph)

`graph_email.py` adapts the proven `Inbound/Graph_Inbox_Analyzer.py` flow into
the CFG/ING/EXC architecture: app-only MSAL token → scan Inbox sub-folders
(skipping system folders + the processed target) → download every NON-IMAGE file
attachment → land verbatim into `ING.Inbound_File`/`ING.Raw_Record` (+ provenance
in `ING.Source_Email`) → move the message into `Inbox/Fusion_Processed`. Every
step is logged to `EXC.Execution` / `LOG`.

The client **secret is never stored in the DB**: it is resolved at runtime from
`GRAPH_CLIENT_SECRET` (env) or the `GRAPH_CLIENT_SECRET_REF` Key Vault reference.
Set `GRAPH_TENANT_ID` in `CFG.Application_Parameters` (it is not in the manifest).

To implement a route, fill in `discover()`, `fetch()` and `parse_rows()` on its
class, then replace the SKELETON block in `run()` with the documented landing loop.

## Config (no hardcoding)

- **DB connection** → `Configuration/Fusion_Flow_QAS.ini` `[database]` (gitignored).
- **Parameters** → `CFG.Application_Parameters`.
- **Routing** → `CFG.Clients`, `CFG.Email_Rules`, `CFG.Folder_Paths`.

## Jobs (CFG.Job)

The schedulable units of work are registered as data in **`CFG.Job`** (seeded by
`Configuration/SQL/012_cfg_jobs.sql`), so the job list is authoritative and
documented, not buried in code. The active Birkdale cycle:

| JobCode | Step | Purpose | Entry point |
|---|---|---|---|
| `ING_BKD_CYCLE` | — | Orchestrates the cycle below (one EXC.Execution per step) | `run_ingestion:main` |
| `ING_BKD_ACQUIRE_EMAIL` | 1 | Download `@birkdalesales.com` attachments via Graph; prefix + move mail to `Fusion_Processed/BKD`; land provenance | `birkdale_sales_orders:run` |
| `ING_BKD_PARSE_ENS` | 2 | Parse forwarded TSS *Details* mails into the timestamped ENS CSV (dedup on `DetailsDate\|ICR`) | `ens_headers:run_from_graph` |
| `ING_BKD_LOAD_RAW` | 3 | Load ENS CSV + Sales Order workbooks into `ING.BKD_Raw_*`; move files to Processed | `load_raw:run` |

Registered but **inactive** (modular, future channels): `ING_ACQUIRE_FILE_DROP`,
`ING_ACQUIRE_SFTP`, `ING_ACQUIRE_AS2`, `ING_ACQUIRE_API`.

## Run

The scheduler runs the cycle with **no CLI** — behaviour comes from `CFG.Job` and
`CFG.Application_Parameters` (`INGESTION_CLIENT`, `INGESTION_DRY_RUN`):

```bash
python run_ingestion.py        # runs the active CFG.Job steps for INGESTION_CLIENT
```

`ingest.py` remains the per-channel framework (one class per route); fill in
`discover()`/`fetch()`/`parse_rows()` to activate a new channel, then flip the
matching `CFG.Job` row to active.

Requires `pyodbc` (and `openpyxl` once XLSX parsing is implemented). Lands into
`ING.Inbound_File` / `ING.Raw_Record` / `ING.Source_Email` (see SQL files 004–005).
