# Module 1 - Ingestion

Ingestion answers one question:

```text
What did the customer send us?
```

It does not decide customs values. It does not submit to TSS. It does not hide bad data.

It lands evidence.

## What It Does

- read configured mailboxes or folders,
- classify the message/file,
- save the original evidence,
- store email metadata,
- store hashes and paths,
- load raw rows into `ING`,
- create execution/log records.

## BKD Route

| Step | Job | Purpose |
| --- | --- | --- |
| 1 | `ING_BKD_ACQUIRE_EMAIL` | Pull relevant emails/files from Microsoft Graph. |
| 2 | `ING_BKD_PARSE_ENS` | Parse `DETAILS FOR...` / `Tss Details` body text into ENS source data. |
| 3 | `ING_BKD_LOAD_RAW` | Load ENS and Sales Orders rows into `ING`. |

The runner should read active jobs from `CFG.Job`. If that is not ready, the BKD fallback order above is the safe default.

## Evidence Rule

The customer file is evidence.

Do not rewrite it. If Fusion derives a better value, store that derived value separately with provenance.

## Typical Tables

Target V3:

- `ING.Inbound_File`
- `ING.Raw_Record`
- `ING.Source_Email`
- `EXC.Execution`
- `LOG.Process_Log`

BKD production also has older/proven BKD-specific `ING.BKD_*` tables. Use them as behaviour reference, not as an excuse to duplicate structure.

## Run

```powershell
python Modules\Ingestion\ING_00_run_cycle.py
```

Use a test mailbox or dry-run style checks when changing classification rules.

## Keep Out Of Ingestion

- product enrichment,
- partner matching,
- TSS choice validation,
- PRS/STG writes,
- TSS API calls,
- customer notifications.