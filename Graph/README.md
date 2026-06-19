# Graph Mail Downloader

This folder contains the first simple Microsoft Graph script for Fusion Flow V3
QAS.

The current script reads a configured mailbox, matches messages to a tenant by
sender domain, and saves file attachments into the tenant Integration Layer
folder.

Current confirmed tenant:

| Tenant | Code | Sender rule | Destination |
| --- | --- | --- | --- |
| Birkdale | BKD | `birkdalesales.com` | `\\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Production\Integration_Layer\BKD\Inbound\Sales_Order_files` |

Body extraction is intentionally disabled until Aidan confirms the required
markers and tenant-specific rules.

## Run Example

```powershell
$env:GRAPH_TENANT_ID = "<tenant-id>"
$env:GRAPH_CLIENT_ID = "<client-id>"
$env:GRAPH_CLIENT_SECRET = "<client-secret>"
$env:GRAPH_MAILBOX = "nexus@synoviaflow.cloud"

python Graph\graph_mail_customer_downloader.py --received-from 2026-05-07 --dry-run
```

Remove `--dry-run` only when the destination folder and configuration have been
confirmed.

## Customer Configuration

Customer routing is stored as one YML file per tenant/customer:

```text
Graph/config/customers/BKD.yml
```

Each file defines the tenant code, sender domains or addresses, allowed file
extensions, and the destination folder where attachments should be saved.
## Daily and Historic Runs

Run the one-off historic download first. It uses `historic_start_date` from the
customer YML and reads up to yesterday:

```powershell
python Graph\graph_mail_customer_downloader.py --run-mode historic
```

Daily execution is the default. It reads only today's Graph emails:

```powershell
python Graph\graph_mail_customer_downloader.py
```

Use `--run-mode custom --received-from YYYY-MM-DD --received-to YYYY-MM-DD` for
manual backfills or investigations.
