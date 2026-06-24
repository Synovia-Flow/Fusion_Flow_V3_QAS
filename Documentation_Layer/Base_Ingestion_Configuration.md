# Base Ingestion Configuration

Operational workbook:

```text
\\pl-az-sdf-plint\Fusion_Production\Scratch\Fusion_Flow_V3_QAS\Documentation_Layer\Base_Ingestion_Configuration.xlsx
```

Database for the current QAS design:

```text
Fusion_Flow_V3_QAS
```

The workbook should contain only the minimum values needed to process emails and
save files.

## Workbook Tabs

| Tab | Purpose |
| --- | --- |
| `Application_Paramters` | Graph mailbox, credentials keys, root paths and process flags. |
| `Principals` | Tenant/client list, for example `BKD`, `CWH`, `PLE`, `AVN`. |
| `Sender_Rules` | Rules that map sender domains or addresses to tenant folders. |
| `Database_Model` | Minimal table model for `CFG.Graph`, `CFG.TenantSetting`, `EXC.Graph`, `ING.Graph`, `STG.SalesOrder`, `TSS.Submission`. |
| `Load_Map` | Shows how workbook tabs map into the future database. |

## GitHub Rule

The workbook may contain operational values and secrets, so the `.xlsx` file is
not committed here. Use `Base_Ingestion_Configuration.minimum.csv` as the
sanitised version for version control.

## MVP Tenant/Folder Contract

- `CFG.Tenant` owns tenant identity and default folders for `BKD`, `CWH`, and `PLE`.
- `CFG.IngestionRoute` owns mailbox/sender/folder routing.
- `CFG.IngestionPackRule` owns `ENS_PACK` and `DEC_PACK` file/sheet rules.
- `CFG.TenantSetting` owns runtime gates such as `TSS_SUBMIT_ENABLED`, `TSS_DRY_RUN`, and `TSS_ENVIRONMENT`.
- `EXC` is reserved for execution logs.
- `ING` records source files, process folders, fail folders, generated CSV paths, and load rows before validation.
- `STG` owns validation/business staging.
- `TSS` owns official API references and status mirrors.

`CWH` and `PLE` are configured but Graph-inactive until sender rules and template mappings are confirmed.

## Source Of Truth

Microsoft Graph is the source for inbound email identity and message state. The filesystem is staging for saved body/attachment/process/fail artifacts, and those paths must be recorded in `ING.ProcessFile`.

`ING` is the database trace of what entered the platform. It should not become the execution-log store; execution outcomes belong in `EXC`.
