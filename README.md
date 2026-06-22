# Fusion Flow V3 QAS

Version-controlled starting point for the Fusion Flow V3 QAS ingestion work.

The current objective is deliberately small: use Microsoft Graph to read customer
emails, identify the customer/tenant from the sender rule, and save inbound files
into the correct Integration Layer folder. The project is being kept simple so it
can pass Quality review, be maintained easily, and later move configuration into
`Fusion_Flow_V3_QAS` database tables.

## Current Scope

- Microsoft Graph mailbox ingestion for `nexus@synoviaflow.cloud`.
- One customer configuration file per tenant/customer under `Graph/config/customers`.
- Initial confirmed tenant: `BKD` / Birkdale.
- Birkdale sender rule: `birkdalesales.com`.
- Current save behaviour: attachments only, `.xlsx` files.
- Emails without file attachments are skipped by the current Graph script.
- The email body may be needed later for ENS and consignment creation, but that
  execution must run through the test API environment first.
- Historic download has been run from `2026-05-07` to `2026-06-18`.
- Daily mode is now the default and reads only today's Graph emails.

## Repository Structure

```text
.env.example
Graph/
  graph_mail_customer_downloader.py
  README.md
  config/customers/BKD.yml
Documentation_Layer/
  Base_Ingestion_Configuration.md
  Base_Ingestion_Configuration.minimum.csv
Configuration_Layer/
  SQL/
    001_create_minimal_graph_tables.sql
```

## Operational Configuration

The QAS configuration workbook location is:

```text
\\pl-az-sdf-plint\Fusion_Production\Scratch\Fusion_Flow_V3_QAS\Documentation_Layer\Base_Ingestion_Configuration.xlsx
```

The version-controlled CSV in this repo is sanitised and uses placeholders. Do
not commit real Graph secrets to GitHub. Real values should live in the workbook,
`CFG.Graph`, environment variables, or secure deployment configuration.

The script accepts both configuration naming styles:

```text
GRAPH.TENANT_ID
GRAPH.CLIENT_ID
GRAPH.CLIENT_SECRET
GRAPH.MAILBOX
GRAPH.FOLDER
```

or:

```text
GRAPH_TENANT_ID
GRAPH_CLIENT_ID
GRAPH_CLIENT_SECRET
GRAPH_MAILBOX
GRAPH_FOLDER
```

## Customer Configuration

Each customer has one YML file. For BKD:

```text
Graph/config/customers/BKD.yml
```

That file defines:

- tenant name and code;
- whether the customer is active;
- sender domains or sender addresses;
- allowed file types;
- historic start date;
- destination folder;
- future body/API processing status for ENS and consignments.

This keeps customer routing readable and makes it easy to add future tenants
without changing the main script logic.

## Current BKD Destination

```text
\\PL-AZ-SDF-PLINT\Fusion_Production\Scratch\Fusion_Flow_V3_QAS\Integration_Layer\BKD\Inbound\Sales_Order_files
```

## How To Run

From the repository root:

```powershell
cd "\\pl-az-sdf-plint\Fusion_Production\Scratch\Fusion_Flow_V3_QAS"
```

Daily run, default behaviour:

```powershell
python Graph\graph_mail_customer_downloader.py
```

Historic one-off run:

```powershell
python Graph\graph_mail_customer_downloader.py --run-mode historic
```

Manual custom window:

```powershell
python Graph\graph_mail_customer_downloader.py --run-mode custom --received-from 2026-06-01 --received-to 2026-06-10
```

Dry-run check:

```powershell
python Graph\graph_mail_customer_downloader.py --dry-run --max-messages 5
```

## Daily Scheduling

Use Windows Task Scheduler on a machine/server that has access to the shared
Integration Layer path.

Suggested action:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "cd '\\pl-az-sdf-plint\Fusion_Production\Scratch\Fusion_Flow_V3_QAS'; python Graph\graph_mail_customer_downloader.py"
```

Because daily mode is the default, no date arguments are required for normal
scheduled execution.

## Database Strategy

The database name for the QAS design is:

```text
Fusion_Flow_V3_QAS
```

The initial model is intentionally minimal:

| Layer | Table | Purpose |
| --- | --- | --- |
| `CFG` | `Graph` | One active Graph route/configuration per tenant/customer. |
| `EXC` | `Graph` | One row per Graph execution/run; a run can process multiple tenants. |
| `ING` | `Graph` | First inbound Graph message/file trace, linked to `CFG.Graph` by `ConfigID`. |
| `STG` | `SalesOrder` | Parsed sales order staging data from the future API/test processing stage. |
| `TSS` | `Submission` | Future TSS submission/reference tracking. |

The SQL script creates the schemas and tables and now includes foreign keys for
the process flow. ING.Graph is the first data-arrival table, so it stores
TenantCode and links each inbound email/file back to CFG.Graph.ConfigID:

```text
CFG.Graph -> ING.Graph -> STG.SalesOrder -> TSS.Submission
EXC.Graph -> ING.Graph
```

No detailed log tables are included at this stage.

## Quality Notes

- The script is intentionally written as a single clear Python script.
- Comments explain each step for Quality review and future maintenance.
- Customer-specific routing is outside the code in YML files.
- Failed Graph/API actions do not mark or move mailbox messages.
- The current script does not mark messages as read; it relies on date windows
  and existing-file checks to avoid duplicate saved files.
- No-attachment emails are not parsed by the current Graph flow.
- Body extraction is documented as future downstream API/test processing because
  the body can contain the data needed to create ENS records and consignments.
- The focus of this repository section remains Graph configuration: mailbox,
  sender rules, file types, destination folders, and tenant routing.



