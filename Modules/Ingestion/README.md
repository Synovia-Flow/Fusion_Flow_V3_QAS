# Module 1 - Ingestion

Ingestion is only about getting source data in and proving what arrived.

It should not decide customs values, fix product data, submit to TSS or hide
bad source data. It lands evidence and lets Processing deal with meaning.

## What it does

- read configured mailboxes or file locations,
- classify the message/file,
- save the original file,
- capture email metadata, hashes, paths and timestamps,
- load raw rows into `ING`,
- create an `EXC.Execution` record for the run,
- log technical details to `LOG`.

## Current BKD route

| Step | Job | Purpose |
| --- | --- | --- |
| 1 | `ING_BKD_ACQUIRE_EMAIL` | Pull relevant emails/files from Microsoft Graph. |
| 2 | `ING_BKD_PARSE_ENS` | Parse `DETAILS FOR...` / `Tss Details` body text into ENS source data. |
| 3 | `ING_BKD_LOAD_RAW` | Load ENS and Sales Orders rows into `ING`. |

The runner should read active jobs from `CFG.Job`. If the table is not ready,
the BKD fallback order above is the safe default.

## Evidence rule

The customer file is evidence.

Do not rewrite it to make Fusion happier. If we need derived values, store them
in database rows with provenance. The original file should still be available.

## Tables involved

Typical V3 target:

- `ING.Inbound_File`
- `ING.Raw_Record`
- `ING.Source_Email`
- `EXC.Execution`
- `LOG.Process_Log`

BKD production currently also has:

- `ING.BKD_EmailMessage`
- `ING.BKD_EmailAttachment`
- `ING.BKD_SourceFileLog`
- `ING.BKD_ProcessLog`
- `ING.BKD_SalesOrderLine`

## Run

```powershell
python Modules\Ingestion\ING_00_run_cycle.py
```

The script should take behaviour from config, not from hardcoded tenant logic.
Use dry-run or a test mailbox first when changing classification rules.

## Keep out of this module

- product enrichment,
- partner matching,
- TSS choice validation,
- PRS/STG writes,
- TSS API calls,
- customer notifications.

Those belong to Processing, Submission or Notifications.
