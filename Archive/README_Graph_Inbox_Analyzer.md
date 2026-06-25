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
6. **Scans every folder in the mailbox** under one consistent rule — top-level
   folders outside the Inbox and Inbox sub-folders alike. The **Inbox root
   itself is skipped** (its sub-folders are still scanned), and the well-known
   system folders (**Sent Items, Drafts, Deleted Items, Junk Email, Outbox,
   Archive**, etc.) are skipped along with everything beneath them.
7. **Downloads all non-image attachments** from those messages to
   `F:\Synovia_Flow_Quality\Integration_Layer\BKD\Inbound\Inbound_Stage`. Every
   file type is kept **except images** (png/jpg/gif/bmp/tiff/webp/svg/heic and
   anything with an `image/*` content type). Files are saved with a
   collision-safe name (`<received>_<msgid>_<original name>`).
8. **Moves every scanned message** into `Fusion_Processed` — a folder created
   directly **beneath the Inbox** if missing — after attachments are
   downloaded. Use `--only-with-attachments` to move only messages that yielded
   a saved file.
9. **Writes an Excel report** into the same `Inbound_Stage` folder with five
   sheets: `Folder Summary`, `Inbox Analysis`, `Attachments`,
   `Processed Messages`, and `Run Info`.

## Prerequisites

```powershell
pip install -r requirements.txt
```

The app uses these **application** permissions (already declared in
`Manifest_V2.json`), which must be granted **admin consent** in Entra ID:

- `Mail.Read` — read mailbox folders, messages, and attachments.
- `Mail.ReadWrite` — **required** to create the `Inbox/Fusion_Processed` folder
  and to move messages. Without it, folder enumeration and downloads still work,
  but folder creation/moves return HTTP 403 (the script logs this and carries on
  with `--no-move-processed` behaviour).

The `Parameters` tab must contain valid values for the three `GRAPH_*` keys.

## Usage

Run with the deployment defaults (paths and `nexus@synoviaflow.cloud` mailbox):

```powershell
python Graph_Inbox_Analyzer.py
```

Common overrides:

```powershell
# Download only, don't move anything (dry-run of the file pull)
python Graph_Inbox_Analyzer.py --no-move-processed

# Move only the messages that actually had a (non-image) attachment
python Graph_Inbox_Analyzer.py --only-with-attachments

# Keep absolutely everything, images included
python Graph_Inbox_Analyzer.py --exclude-extensions none
```

| Option | Default | Description |
| --- | --- | --- |
| `--setup-xlsx` | `F:\Synovia_Flow_Quality\Documentation_Layer\Project_Build_Files\Ingestion_Setup.xlsx` | Parameters workbook |
| `--parameters-sheet` | `Parameters` | Tab holding the `GRAPH_*` key/value rows |
| `--manifest` | `E:\Git\FLow_3_1\Flow_3_1_Development\Development_Tools\Config\Manifest_V2.json` | App manifest used for validation |
| `--output-dir` | `F:\Synovia_Flow_Quality\Integration_Layer\BKD\Inbound\Inbound_Stage` | Where the report is written |
| `--attachments-dir` | `F:\Synovia_Flow_Quality\Integration_Layer\BKD\Inbound\Inbound_Stage` | Where downloaded attachments are saved |
| `--mailbox` | `nexus@synoviaflow.cloud` | Mailbox (UPN) to analyse |
| `--inbox-sample` | `2000` | Max Inbox messages pulled for the analysis sheet |
| `--exclude-extensions` | image types | File types to **exclude** from download (`none` = keep everything) |
| `--processed-folder` | `Fusion_Processed` | Folder created beneath the Inbox to move scanned messages into |
| `--only-with-attachments` | off | Move only messages that yielded a saved attachment |
| `--no-move-processed` | off | Download attachments but do not move messages |
| `--no-download-attachments` | off | Skip downloading attachments |
| `--skip-inline` | off | Skip inline attachments (signature images, etc.) |
| `--verbose` | off | Debug logging |

The output file is named
`Mailbox_Report_<mailbox>_<YYYYMMDD_HHMMSS>.xlsx` and its full path is printed
on success.

## Output sheets

- **Folder Summary** — one row per folder (indented by depth), full path, total
  emails, unread, sub-folder count, hidden flag, plus a grand-total row.
- **Inbox Analysis** — headline metrics, top 25 senders, and emails-received by
  day.
- **Attachments** — every attachment found (message, sender, subject, file
  name, size, inline flag, saved file name, and status incl. why it was
  skipped — e.g. `skipped (image: .png)`), plus a totals row.
- **Processed Messages** — each scanned message: received time, sender,
  subject, source folder, number of files saved, and move status.
- **Run Info** — mailbox, app, tenant, permissions, counts, attachment totals,
  messages matched/moved, move destination, and timestamp.
