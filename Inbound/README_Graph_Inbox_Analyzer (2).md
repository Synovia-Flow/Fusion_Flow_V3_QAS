# Graph Inbox Analyzer

`Graph_Inbox_Analyzer.py` connects to a Microsoft 365 mailbox through the
Microsoft Graph API (app-only / client-credentials flow), lists every mail
folder together with how many emails it holds, analyses the Inbox, and writes
the results to an Excel workbook.

## What it does

1. **Reads credentials** from the `Parameters` tab of
   `Ingestion_Setup.xlsx` — `GRAPH_CLIENT_SECRET`, `GRAPH_CLIENT_ID`,
   `GRAPH_TENANT_ID`. Nothing sensitive is stored in source control; the secret
   only ever lives in the workbook.
2. **Validates** the client id against `Config/Manifest_V2.json` (the registered
   *Fusion Flow Mail Reader* app) and reports the Graph permissions it declares.
3. **Authenticates** app-only with MSAL and obtains a Graph token.
4. **Enumerates every folder** in the target mailbox recursively (including
   hidden folders), capturing total and unread email counts per folder.
5. **Analyses the Inbox** — totals, read/unread, attachments, high-importance,
   top senders, oldest/newest, and volume-by-day.
6. **Writes an Excel report** to the output directory with three sheets:
   `Folder Summary`, `Inbox Analysis`, and `Run Info`.

The tool is **read-only**. It relies on the `Mail.Read` / `Mail.ReadBasic.All`
*application* permissions already declared in `Manifest_V2.json`, which must be
admin-consented in Entra ID for the app to read the mailbox.

## Prerequisites

```powershell
pip install -r requirements.txt
```

`Mail.Read` / `Mail.ReadBasic.All` (Application) must be granted **admin
consent** in the tenant. The `Parameters` tab must contain valid values for the
three `GRAPH_*` keys.

## Usage

Run with the deployment defaults (paths and `nexus@synoviaflow.cloud` mailbox):

```powershell
python Graph_Inbox_Analyzer.py
```

Common overrides:

```powershell
python Graph_Inbox_Analyzer.py --mailbox someone@synoviaflow.cloud
python Graph_Inbox_Analyzer.py --setup-xlsx "C:\path\Ingestion_Setup.xlsx" --output-dir "C:\out"
python Graph_Inbox_Analyzer.py --inbox-sample 5000 --verbose
```

| Option | Default | Description |
| --- | --- | --- |
| `--setup-xlsx` | `F:\Synovia_Flow_Quality\Documentation_Layer\Project_Build_Files\Ingestion_Setup.xlsx` | Parameters workbook |
| `--parameters-sheet` | `Parameters` | Tab holding the `GRAPH_*` key/value rows |
| `--manifest` | `E:\Git\FLow_3_1\Flow_3_1_Development\Development_Tools\Config\Manifest_V2.json` | App manifest used for validation |
| `--output-dir` | `F:\Synovia_Flow_Quality\Documentation_Layer\Graph` | Where the report is written |
| `--mailbox` | `nexus@synoviaflow.cloud` | Mailbox (UPN) to analyse |
| `--inbox-sample` | `2000` | Max Inbox messages pulled for the analysis sheet |
| `--verbose` | off | Debug logging |

The output file is named
`Mailbox_Report_<mailbox>_<YYYYMMDD_HHMMSS>.xlsx` and its full path is printed
on success.

## Output sheets

- **Folder Summary** — one row per folder (indented by depth), full path, total
  emails, unread, sub-folder count, hidden flag, plus a grand-total row.
- **Inbox Analysis** — headline metrics, top 25 senders, and emails-received by
  day.
- **Run Info** — mailbox, app, tenant, permissions, counts, and timestamp.
