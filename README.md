# Fusion Flow V3 QAS

Minimal QAS starting point for Fusion Flow V3.

The project is organised by layers so the first working process, Microsoft Graph email ingestion, is easy to understand, review and extend tenant by tenant.

## Current Scope

- Read the shared mailbox with Microsoft Graph.
- Match inbound emails to a tenant/customer using sender rules.
- Save allowed attachments into the correct tenant folder under `Integration_Layer`.
- Keep customer routing in one YML file per customer.
- Keep database objects minimal while `Fusion_Flow_V3_QAS` is being defined.
- Keep ENS, consignments and TSS submission work in test/QAS until confirmed.

## Project Structure

```text
.env.example                     # Local/runtime setting names only, no secrets
Configuration_Layer/
  SQL/                           # Minimal database SQL for CFG, EXC, ING, STG, TSS
Documentation_Layer/             # Quality/design notes and operating model
Integration_Layer/
  Graph/                         # Current Microsoft Graph downloader and customer YML files
  FLOW_V3/                       # Simple 01-05 execution entrypoints
  App/                           # Small local Flask/app placeholder for future QAS services
  BKD/                           # Tenant file destination folders
  CWH/
  PLE/
```

No extra deployment folders are included yet. They should be added only when the Azure deployment shape is confirmed.

## Graph Configuration

Customer routing lives here:

```text
Integration_Layer/Graph/config/customers/*.yml
```

Each customer file defines the tenant code, sender domains or addresses, allowed file types, historic start date and destination folder.

Current tenant status:

| Tenant | Code | Status |
| --- | --- | --- |
| Birkdale | BKD | Active Graph route |
| Country Wide Homes | CWH | Configured, inactive until sender/source is confirmed |
| Primeline Express | PLE | Configured, inactive until sender/source is confirmed |

## How To Run

From the repository root:

```powershell
cd "\\pl-az-sdf-plint\Fusion_Production\Scratch\Fusion_Flow_V3_QAS"
```

Run the Graph downloader directly:

```powershell
python Integration_Layer\Graph\graph_mail_customer_downloader.py
```

Or run it through the FLOW V3 step 01 wrapper:

```powershell
python Integration_Layer\FLOW_V3\01_graph_email_ing.py --run-mode daily
```

Historic one-off run:

```powershell
python Integration_Layer\Graph\graph_mail_customer_downloader.py --run-mode historic
```

Dry-run check:

```powershell
python Integration_Layer\Graph\graph_mail_customer_downloader.py --dry-run --max-messages 5
```

## Database Direction

The QAS database is `Fusion_Flow_V3_QAS`.

The current model is intentionally small:

| Layer | Purpose |
| --- | --- |
| `CFG` | Tenant/customer Graph configuration and routing. |
| `EXC` | Execution run status. |
| `ING` | First inbound Graph email/file trace. |
| `STG` | Future validated business staging, for example `STG.SalesOrder`. |
| `TSS` | Future official TSS references and submission state. |

Graph is only the information-arrival layer. ENS, consignments and submission logic should be added after the source data and test API behaviour are confirmed.

## Quality Notes

- The Graph downloader is a single commented script for readability.
- The script explains each step inline to support quality review and maintenance.
- Customer-specific routing is outside the code in YML files.
- Real credentials must stay in `.env`, CFG tables or secure deployment settings, never in GitHub.
- Future tenant differences should be handled by configuration first, not hard-coded branches.
