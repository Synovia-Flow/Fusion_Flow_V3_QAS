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

- **DB connection** → `Configuration_Layer/Fusion_Flow_QAS.ini` `[database]` (gitignored).
- **Parameters** → `CFG.Application_Parameters`.
- **Routing** → `CFG.Clients`, `CFG.Email_Rules`, `CFG.Folder_Paths`.

## Run

```bash
python ingest.py --client BKD --dry-run          # discover/report only, no writes
python ingest.py --client BKD --channel FILE_DROP
```

Requires `pyodbc` (and `openpyxl` once XLSX parsing is implemented). Lands into
`ING.Inbound_File` / `ING.Raw_Record` / `ING.Source_Email` (see SQL files 004–005).
