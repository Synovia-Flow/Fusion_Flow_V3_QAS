# Module 3 - Submission

Submission is the controlled bridge between Fusion and TSS.

By this point the data should already be clean. Submission should not be guessing missing business data.

## Flow

```text
PRS validated record
  -> promote to STG
  -> submit/update/cancel in TSS
  -> store API request/response
  -> mirror official TSS status
  -> show result in portal/notifications
```

## Jobs

| Script | Purpose |
| --- | --- |
| `SUB_01_promote.py` | Move validated records into the submission layer. |
| `SUB_02_submit.py` | Create declaration/header/goods in TSS. |
| `SUB_03_mirror.py` | GET from TSS and refresh local mirror/status. |
| `SUB_04_update.py` | Send controlled updates where allowed. |
| `SUB_05_cancel.py` | Cancel live records where allowed. |
| `SUB_06_fetch_json.py` | Pull request/response JSON for evidence and review. |

## Gates

Live writes need gates.

- Dry-run is default.
- Environment must be clear: `TST` or `PRD`.
- Submit must be enabled in config.
- Validation blockers must be clear.
- Duplicate/risk checks must pass.
- Cancel/update must target a specific movement or controlled batch.

## Tables

- `PRS.*` - clean source record.
- `STG.*` - submit-ready copy.
- `API.Call` - request/response evidence.
- `TSS.*` - official mirror from TSS.
- `EXC.*` / `LOG.*` - run status and trace.
- `CHG.*` - manual edits/actions where relevant.

## Portal Pattern

The portal should normally queue work, not own it.

1. portal writes to `EXC.Job_Queue`,
2. local worker claims the job,
3. worker runs the submission script,
4. status is written back to the DB,
5. portal reads the result.

## Important Rules

- TSS status is the authority.
- Pending Payment is not automatically an error.
- Store the full TSS response before summarising it.
- Do not call TSS for records we already know are invalid.
- Do not send SD if the SD kill switch is off.
- Notifications should use mirrored official status, not local guesswork.