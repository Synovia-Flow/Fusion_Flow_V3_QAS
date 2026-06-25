# Graph Folder Structure

`Graph_Folder_Structure.py` connects to a Microsoft 365 mailbox through the
Microsoft Graph API (app-only / client-credentials flow) and outputs the **full
folder structure** — every folder and sub-folder, recursively — together with
the **email count** held in each.

It reuses the configuration, authentication and folder-enumeration code from
`Graph_Inbox_Analyzer.py`, so both tools read the same `Parameters` tab of
`Ingestion_Setup.xlsx` and behave consistently.

## What it does

1. **Reads credentials** (`GRAPH_CLIENT_SECRET`, `GRAPH_CLIENT_ID`,
   `GRAPH_TENANT_ID`) from the `Parameters` tab of `Ingestion_Setup.xlsx`.
2. **Authenticates** app-only with MSAL and obtains a Graph token.
3. **Enumerates every folder** recursively (including hidden folders), capturing
   total and unread email counts per folder.
4. **Prints an indented tree** to the console — each line shows the folder name
   and its total / unread counts — and writes a timestamped report:
   - `Folder_Structure_<mailbox>_<YYYYMMDD_HHMMSS>.xlsx` — one row per folder
     (indented name, full path, depth, total emails, unread, sub-folder count,
     hidden flag) plus a grand-total row.
   - `Folder_Structure_<mailbox>_<YYYYMMDD_HHMMSS>.txt` — the same tree as plain
     text.

This tool is **read-only** — it only needs the `Mail.Read` /
`Mail.ReadBasic.All` application permissions already declared in
`Manifest_V2.json`.

## Prerequisites

```powershell
pip install -r requirements.txt
```

`Graph_Folder_Structure.py` must sit alongside `Graph_Inbox_Analyzer.py` (it
imports the shared helpers from it). The `Parameters` tab must contain valid
values for the three `GRAPH_*` keys.

## Usage

```powershell
# Default mailbox (nexus@synoviaflow.cloud), report written to --output-dir
python Graph_Folder_Structure.py

# A different mailbox
python Graph_Folder_Structure.py --mailbox someone@synoviaflow.cloud

# Just print the tree, write nothing; and skip hidden folders
python Graph_Folder_Structure.py --no-files --no-hidden
```

| Option | Default | Description |
| --- | --- | --- |
| `--setup-xlsx` | `F:\Synovia_Flow_Quality\Documentation_Layer\Project_Build_Files\Ingestion_Setup.xlsx` | Parameters workbook |
| `--parameters-sheet` | `Parameters` | Tab holding the `GRAPH_*` key/value rows |
| `--manifest` | `E:\Git\FLow_3_1\Flow_3_1_Development\Development_Tools\Config\Manifest_V2.json` | App manifest used for validation |
| `--output-dir` | `F:\Synovia_Flow_Quality\Documentation_Layer\Graph` | Where the report files are written |
| `--mailbox` | `nexus@synoviaflow.cloud` | Mailbox (UPN) to enumerate |
| `--no-hidden` | off | Exclude hidden folders from the output |
| `--no-files` | off | Print the tree only; do not write report files |
| `--verbose` | off | Debug logging |
