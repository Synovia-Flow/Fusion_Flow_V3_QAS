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
| `Database_Model` | Minimal table model for `CFG.Graph`, `EXC.Graph`, `ING.Graph`, `STG.SalesOrder`, `TSS.Submission`. |
| `Load_Map` | Shows how workbook tabs map into the future database. |

## GitHub Rule

The workbook may contain operational values and secrets, so the `.xlsx` file is
not committed here. Use `Base_Ingestion_Configuration.minimum.csv` as the
sanitised version for version control.

