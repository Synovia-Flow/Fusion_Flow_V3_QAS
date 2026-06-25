# Consolidate Mailbox Reports

`Consolidate_Mailbox_Reports.py` is a **standalone** tool (only `openpyxl`, no
Microsoft Graph) that merges several `Mailbox_Report_*.xlsx` workbooks — the
output of `Graph_Inbox_Analyzer.py` — into a **single consolidated workbook**.

## What it does

1. **Discovers reports** — either the files/folders you pass as arguments, or
   every `Mailbox_Report_*.xlsx` found in `--input-dir`.
2. **Reads each report's** Attachments, Processed Messages and Run Info sheets,
   dropping the per-file title/spacer/totals rows.
3. **Writes one consolidated workbook** to `--output-dir` (default
   `F:\Synovia_Flow_Quality\Documentation_Layer`):
   `Consolidated_Mailbox_Report_<YYYYMMDD_HHMMSS>.xlsx`, with these tabs:
   - **Runs Summary** — one row per source report, its Run Info pivoted to
     columns, plus per-report row counts.
   - **Attachments** — every attachment that was **actually downloaded**
     (`Status == saved`). Skipped images, inline, non-file and error rows are
     excluded as noise. Each row is prefixed with **Source Report** and **Run
     Generated**.
   - **Processed Messages** — only the messages that **yielded a saved file**
     (`Files Saved > 0`), similarly prefixed.
   - **Analysis Prompt** — a ready-to-paste prompt describing the data and
     asking an assistant how to **analyse and sort** the downloaded files
     (profiling, duplicate detection, a folder/taxonomy scheme, and a per-file
     destination + rename plan). The prompt also calls out **forwarder
     addresses** (`--forwarders`, default `nexus@synoviaintegration.com` and
     `aidan.harrington@synoviadigital.com`) whose rows show the forwarder rather
     than the true sender, instructing the assistant to use the original headers
     from the forwarded body instead.

## Usage

```powershell
# Default: scan the Inbound_Stage folder, write to Documentation_Layer
python Consolidate_Mailbox_Reports.py

# Specific files
python Consolidate_Mailbox_Reports.py reportA.xlsx reportB.xlsx

# A different source folder / output location
python Consolidate_Mailbox_Reports.py --input-dir "C:\reports" --output-dir "C:\out"
```

| Option | Default | Description |
| --- | --- | --- |
| `inputs` | _(none)_ | Report `.xlsx` files or folders to consolidate. If omitted, `--input-dir` is scanned. |
| `--input-dir` | `F:\Synovia_Flow_Quality\Integration_Layer\BKD\Inbound\Inbound_Stage` | Folder scanned when no inputs are given |
| `--pattern` | `Mailbox_Report_*.xlsx` | Glob pattern for discovery |
| `--output-dir` | `F:\Synovia_Flow_Quality\Documentation_Layer` | Where the consolidated workbook is written |
| `--output-name` | `Consolidated_Mailbox_Report_<timestamp>.xlsx` | Override the output file name |
| `--forwarders` | `nexus@synoviaintegration.com,aidan.harrington@synoviadigital.com` | Forwarder addresses noted on the Analysis Prompt tab (`''` to omit the note) |
| `--forwarded-headers` | _(none)_ | A `Forwarded_Headers_*.xlsx` mapping (from `Extract_Forwarded_Headers.py`). Adds an **Original Headers** tab and backfills **True Sender** / **Original Sent** onto the Attachments rows. |
| `--verbose` | off | Debug logging |

Already-consolidated workbooks (those with a `Runs Summary` / `Analysis Prompt`
sheet) are detected and skipped, so re-running over an output folder won't
double-stack the data.

The full path of the consolidated workbook is printed on success.
