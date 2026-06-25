# Extract Forwarded Headers

`Extract_Forwarded_Headers.py` recovers the **original sender / sent date /
subject** of mail that was bulk-forwarded into the mailbox. For forwarded
messages the visible Sender is the *forwarder*, not the real origin — the true
headers live in the quoted `From:/Sent:/To:/Subject:` block inside the message
body. This tool reads those bodies and extracts them.

It reuses the auth + Graph code from `Graph_Inbox_Analyzer.py`, so all the tools
share the same `Ingestion_Setup.xlsx` Parameters tab.

## What it does

1. **Authenticates** app-only with MSAL and obtains a Graph token.
2. **Finds** the `Inbox/Fusion_Processed` folder (`--processed-folder`).
3. **Selects** messages whose sender is one of the `--forwarders` addresses
   (default `nexus@synoviaintegration.com`,
   `aidan.harrington@synoviadigital.com`).
4. **Reads each body**, flattens HTML to text, and parses the first forwarded
   `From:/Sent:/To:/Subject:` block (also handles a `Date:` label).
5. **Writes** `Forwarded_Headers_<mailbox>_<timestamp>.xlsx` to `--output-dir`
   (default `F:\Synovia_Flow_Quality\Documentation_Layer`) with an
   **Original Headers** sheet: `Msg Token`, `Received`, `Received Stamp`,
   `Forwarder`, `Forward Subject`, `Original From`, `Original Sender`,
   `Original Sent`, `Original To`, `Original Subject`, `Parse Status`.

`Msg Token` is the same 8-character id token that `Graph_Inbox_Analyzer.py`
embeds in each downloaded file's `Saved As` name, so the mapping joins straight
back onto the consolidated Attachments rows.

## Integrating with the consolidated report

```powershell
# 1) recover the original headers
python Extract_Forwarded_Headers.py

# 2) merge them into the consolidated workbook (adds an Original Headers tab and
#    backfills True Sender / Original Sent onto the Attachments rows)
python Consolidate_Mailbox_Reports.py --forwarded-headers "F:\...\Forwarded_Headers_..._.xlsx"
```

## Usage

| Option | Default | Description |
| --- | --- | --- |
| `--setup-xlsx` | `…\Project_Build_Files\Ingestion_Setup.xlsx` | Parameters workbook |
| `--parameters-sheet` | `Parameters` | Tab holding the `GRAPH_*` rows |
| `--manifest` | `…\Config\Manifest_V2.json` | App manifest for validation |
| `--output-dir` | `F:\Synovia_Flow_Quality\Documentation_Layer` | Where the mapping is written |
| `--mailbox` | `nexus@synoviaflow.cloud` | Mailbox (UPN) |
| `--processed-folder` | `Fusion_Processed` | Folder beneath the Inbox to search |
| `--forwarders` | `nexus@synoviaintegration.com,aidan.harrington@synoviadigital.com` | Forwarder addresses to match |
| `--verbose` | off | Debug logging |

## Notes

- Parsing is heuristic: it takes the **first** `From:/Sent:/Subject:` block in
  the body (the most-recent forwarded original). Deeply nested forward chains or
  unusual client formats may parse as `partial`; the `Parse Status` column flags
  these so you can review them.
- Read-only: needs `Mail.Read` / `Mail.ReadBasic.All` (already in
  `Manifest_V2.json`).
