# Module 1 — Ingestion (SKELETON)

Acquires inbound data per client and lands it **verbatim** into `ING`, opening
one `EXC.Execution` (Transaction_ID) per run with full provenance and dedup
hashing.

> **Status: skeleton.** The orchestration, config loading, EXC/LOG wiring and
> verbatim-landing helpers are implemented. The per-route fetch/parse logic is
> **stubbed** because each client/channel differs — implement one class to add a
> route.

## Channels (routes)

| Class | Name | Implement |
|---|---|---|
| `FileDropChannel` | FILE_DROP | scan client `INBOUND` folder, read files |
| `EmailGraphChannel` | EMAIL | Graph mailbox harvest per `CFG.Email_Rules` |
| `SftpChannel` | SFTP | SFTP listing + get |
| `RestChannel` | REST | REST pull |

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
