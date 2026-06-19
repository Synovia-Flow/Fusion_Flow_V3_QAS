# Graph Mail Downloader

This folder contains the current Microsoft Graph downloader for Fusion Flow V3
QAS.

The script reads a configured mailbox, matches each email to a tenant/customer
using sender rules, and saves allowed file attachments into the correct
Integration Layer destination folder.

## Current Confirmed Tenant

| Tenant | Code | Sender rule | File type | Destination |
| --- | --- | --- | --- | --- |
| Birkdale | BKD | `birkdalesales.com` | `.xlsx` | `\\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Production\Integration_Layer\BKD\Inbound\Sales_Order_files` |

Emails without file attachments are skipped. Current BKD processing is
attachment-only.

## Files

```text
Graph/graph_mail_customer_downloader.py
Graph/config/customers/BKD.yml
Graph/run_logs/                 # generated locally, not committed
```

## Environment

The script loads `.env` automatically from the repo root or Graph folder. It
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
```

## Customer YML

Each customer has one config file:

```text
Graph/config/customers/BKD.yml
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
python Graph\graph_mail_customer_downloader.py
```

### Historic

Historic mode reads from the earliest active customer `historic_start_date` up
to yesterday:

```powershell
python Graph\graph_mail_customer_downloader.py --run-mode historic
```

### Custom

Custom mode uses the dates provided by the operator:

```powershell
python Graph\graph_mail_customer_downloader.py --run-mode custom --received-from 2026-06-01 --received-to 2026-06-10
```

### Dry Run

Dry-run validates mailbox access, sender matching and file counts without
writing files:

```powershell
python Graph\graph_mail_customer_downloader.py --dry-run --max-messages 5
```

## Current Behaviour

1. Load `.env`.
2. Load active customer YML files.
3. Get a Microsoft Graph app-only token.
4. Read mailbox messages for the selected date window.
5. Match each email to a tenant/customer by sender domain or sender address.
6. Read file attachments.
7. Filter attachments by the customer `file_types` list.
8. Save files as original name plus received date `dd.mm.yyyy`.
9. Skip files that already exist unless `--overwrite` is passed.
10. Keep body-to-ENS/consignment logic out of the Graph downloader for now.
11. Write a small technical run log under Graph/run_logs.

The business output is the customer file in the Integration Layer. The CSV run
log is only for support checks.

## Notes

- The script does not mark emails as read or move them.
- Duplicate prevention is handled by the destination filename and existing-file
  checks.
- Emails without file attachments are skipped and counted in the run summary.
- Body data is treated as a future API/test environment concern because it can be
  needed to create ENS records and consignments.
- Future customers should be added as new YML files rather than hard-coded in
  the Python script.
