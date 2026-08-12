# Module 3 - Submission

Submission is the controlled bridge between Fusion and TSS.

By this point the record should already be processed and validated. This module
should not be guessing missing business data. It should promote, submit, mirror
and record evidence.

## Flow

```text
PRS validated record
  -> promote to STG
  -> submit/update/cancel in TSS
  -> store API request/response
  -> mirror official TSS status
  -> expose result back to portal/notifications
```

## Jobs

| Script | Purpose |
| --- | --- |
| `SUB_01_promote.py` | Move validated records into the submission layer. |
| `SUB_02_submit.py` | Create declaration/header/goods in TSS. |
| `SUB_03_mirror.py` | GET from TSS and refresh local mirror/status. |
| `SUB_04_update.py` | Send controlled full updates where allowed. |
| `SUB_05_cancel.py` | Cancel live records where allowed. |
| `SUB_06_fetch_json.py` | Pull request/response JSON for evidence and review. |

## Safety gates

- Dry-run is the default until explicitly disabled.
- Environment must be clear: `TST` or `PRD`.
- Submit must be enabled in config.
- Validation blockers must be clear.
- Duplicate/risk gates must pass.
- Cancel/update must target a specific movement or controlled batch.

## Tables involved

- `PRS.*` - source canonical record.
- `STG.*` - submit-ready operational copy.
- `API.Call` - request/response evidence.
- `TSS.*` - official mirror from TSS.
- `EXC.*` / `LOG.*` - run status and technical trace.
- `CHG.*` - manual edits/actions where relevant.

## Portal behaviour

The portal should normally enqueue work, not own the work.

Preferred path:

1. portal writes to `EXC.Job_Queue`,
2. local worker claims the job,
3. worker runs the submission script,
4. status is written back to the DB,
5. portal reads the result.

## Important rules

- TSS status is the authority.
- Pending Payment is not automatically an error.
- Store the full TSS response before trying to summarise it.
- Do not call TSS for records we already know are invalid.
- Do not send SDI when the SDI kill switch is off.
- Notifications should use mirrored official status, not just local status.
