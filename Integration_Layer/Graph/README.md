# Graph Mail Downloader

This folder contains the current Microsoft Graph downloader for Fusion Flow V3
QAS.

The script reads a configured mailbox, matches each email to a tenant/customer
using sender rules, and saves allowed file attachments into the correct
Integration Layer destination folder.

## Current Tenants

| Tenant | Code | Sender rule | File type | Destination | Status |
| --- | --- | --- | --- | --- | --- |
| Birkdale | BKD | `birkdalesales.com` | `.xlsx` | `Integration_Layer\BKD\Inbound\Sales_Order_files` | Active |
| Country Wide Homes | CWH | `TBD` | `.xlsx`, `.csv` | `Integration_Layer\CWH\Inbound\Sales_Order_files` | Configured, Graph inactive |
| Primeline Express | PLE | `TBD` | `.xlsx`, `.csv` | `Integration_Layer\PLE\Inbound\Sales_Order_files` | Configured, Graph inactive |

BKD is the only active Graph route until the CWH/PLE sender rules and templates
are confirmed. CWH/PLE are expected to be existing-ENS consignment/goods uploads.
## Files

```text
Integration_Layer/Graph/graph_mail_customer_downloader.py
Integration_Layer/Graph/config/customers/BKD.yml
Integration_Layer/FLOW_V3/Run_History/Graph/   # generated locally, not committed
```

## Environment

The script loads `.env` automatically from the repo root or Integration_Layer/Graph folder. It
accepts either `GRAPH.KEY` or `GRAPH_KEY` style names.

Required values:

```text
GRAPH.TENANT_ID
GRAPH.CLIENT_ID
GRAPH.CLIENT_SECRET
GRAPH.MAILBOX
GRAPH.FOLDER
```

Optional:

```text
GRAPH.HISTORIC_START_DATE
GRAPH.ENV_CODE
GRAPH.DB_ENABLED
GRAPH.DB_CONNECTION_STRING
```

When `GRAPH.DB_CONNECTION_STRING` is set, the downloader reads active routing
rows from `CFG.Graph` for the configured mailbox and writes one `EXC.Graph`
execution row plus `ING.Graph` source trace rows for matched, unmatched, skipped
and saved file outcomes. New process/fail metadata lands in `ING.Graph` when those columns exist, while detailed execution messages belong in `EXC.ExecutionLog`. Database tracing requires the Python `pyodbc` package and a SQL Server ODBC driver. Use `--no-database` to force a file-only run.

## Customer YML

Each customer has one config file:

```text
Integration_Layer/Graph/config/customers/BKD.yml
```

The YML controls:

- customer/tenant code and display name;
- active flag;
- sender domains and exact sender addresses;
- allowed file extensions;
- historic start date;
- destination folder;
- future body/API processing status for ENS and consignments.

The parser is intentionally simple and dependency-free. Keep customer YML files
flat: key/value pairs and simple lists only.

## Run Modes

### Daily

Daily is the default. It reads only today's Graph emails using UTC dates:

```powershell
python Integration_Layer\Graph\graph_mail_customer_downloader.py
```

### Historic

Historic mode reads from the earliest active customer `historic_start_date` up
to yesterday:

```powershell
python Integration_Layer\Graph\graph_mail_customer_downloader.py --run-mode historic
```

### Custom

Custom mode uses the dates provided by the operator:

```powershell
python Integration_Layer\Graph\graph_mail_customer_downloader.py --run-mode custom --received-from 2026-06-01 --received-to 2026-06-10
```

### Dry Run

Dry-run validates mailbox access, sender matching and file counts without
writing files:

```powershell
python Integration_Layer\Graph\graph_mail_customer_downloader.py --dry-run --max-messages 5
```

## Current Behaviour

1. Load `.env`.
2. If database tracing is configured, load active `CFG.Graph` rows for the mailbox; otherwise load active customer YML files.
3. Create an `EXC.Graph` run row for non-dry database runs.
4. Get a Microsoft Graph app-only token.
5. Read mailbox messages for the selected date window.
6. Match each email to a tenant/customer by sender domain or sender address.
7. Read file attachments.
8. Filter attachments by the customer `file_types` list.
9. Save files as original name plus received date `dd.mm.yyyy`.
10. Insert `ING.Graph` trace rows when database tracing is active, including pack/source/process/fail metadata when the MVP schema extension has been applied.
11. Skip files that already exist unless `--overwrite` is passed.
12. Keep body-to-ENS/consignment logic out of the Graph downloader for now.
13. Write run history and validation reports under Integration_Layer/FLOW_V3/Run_History/Graph.

The business output is the customer file in the tenant Integration Layer folder.
The CSV history/report files are operational evidence for support and QA.

## Folder And Pack Contract

- `CFG.Tenant` stores tenant names and default folder ownership.
- `CFG.IngestionRoute` stores mailbox/sender/folder routing.
- `CFG.IngestionPackRule` stores how email parts become packs.
- BKD `ENS_PACK` is the email body target: `Sales Orders Synovia_{dd.MM.yyyy}.xlsx`, sheet `ENS PACK`.
- BKD `DEC_PACK` is the consignment attachment target: `Sales Orders Synovia_{dd.MM.yyyy}.xlsx`, sheet `DEC PACK`.
- `EXC` is for executions/logs only.
- `ING` is for source file/folder/process/fail records before `STG` validation.
## Notes

- The script does not mark emails as read or move them.
- Duplicate prevention is handled by the destination filename and existing-file
  checks.
- Emails without file attachments are skipped and counted in the run summary.
- Body data is treated as a future API/test environment concern because it can be
  needed to create ENS records and consignments.
- Future customers should be added as new YML files rather than hard-coded in
  the Python script.

