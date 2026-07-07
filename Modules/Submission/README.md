# Module 3 - Submission (BKD ENS Header)

Takes VALIDATED ENS headers from processing, submits them to TSS, and mirrors the
authoritative live record back. Every TSS call is logged in full to `API.Call` and
tied to the `EXC` execution spine; the movement advances through the shared
`CFG.Status_Vocabulary` lifecycle.

```
PRS.BKD_ENS_Header_Submission (VALIDATED)
  -> SUB_01_promote.py  -> STG.BKD_ENS_Header        (STG_MATERIALISED -> READY)
  -> SUB_02_submit.py   -> POST /headers (create)    (SUBMITTING -> SUBMITTED; captures ENS number + TSS status; syncs TSS mirror)
  -> SUB_03_mirror.py   -> GET /headers/<ENS number> (RECONCILING -> RECONCILED; writes full TSS.BKD_ENS_Header)
  -> SUB_04_update.py / SUB_05_cancel.py operate against the TSS.* live mirror
```

## Safety

- **Dry-run by default.** `SUBMISSION_DRY_RUN=1` builds and **logs** the exact request
  (`API.Call`, `IsDryRun=1`) but sends **nothing** to TSS. Set `0` to submit for real.
- **Environment.** `SUBMISSION_ENV=TST` (CDS dress rehearsal) by default; `PRD` (live
  HMRC) only via that param. Base URL + Basic-auth creds come from
  `CFG.TSS_Environment` / `CFG.TSS_Credential` (password may fall back to the
  gitignored `Configuration/tss_credentials.json`).
- Rate limited to 0.25s/call (Rule 14); `/headers`, never `/declaration_headers` (Rule 1).

## Controls (`CFG.Application_Parameters`, no CLI)

| Key | Default | Meaning |
|-----|---------|---------|
| `SUBMISSION_CLIENT` | `BKD` | Client to submit |
| `SUBMISSION_ENTITY` | `ENS_HEADER` | Entity |
| `SUBMISSION_ENV` | `TST` | TSS environment (`PRD`\|`TST`) |
| `SUBMISSION_DRY_RUN` | `1` | `1` = log only (no call); `0` = submit |
| `SUBMISSION_MOVEMENT_KEY` | *(blank)* | Optional single movement |
| `SUBMISSION_API_BASE_PATH` | `/x_fhmrc_tss_api/v1/tss_api` | Path prefix before the endpoint |

## Run (scheduler just runs the scripts)

```powershell
python Modules\Submission\SUB_01_promote.py    # VALIDATED -> STG (READY)
python Modules\Submission\SUB_02_submit.py     # create; dry-run unless SUBMISSION_DRY_RUN=0
python Modules\Submission\SUB_03_mirror.py     # get-back -> TSS.* live mirror; mark complete
python Modules\Submission\SUB_06_fetch_json.py # dump each submitted header's request+response JSON for analysis
```

`SUB_02_submit.py` syncs every successful TSS create response immediately into
`STG.BKD_ENS_Header`, `PRS.BKD_ENS_Header_Submission`,
`PRS.BKD_ENS_Header_Tracking`, and `TSS.BKD_ENS_Header`. `SUB_03_mirror.py` remains
the later GET/reconciliation step for the full authoritative TSS record.
`CFG.Job.SUB_MIRROR_BKD_ENS` is configured with `Schedule = Every 30 minutes`.

`SUB_06_fetch_json.py` GETs every submitted header (has a `declaration_number`)
back from TSS and writes one JSON file per movement - full **request + response** -
to `SUBMISSION_JSON_DIR` (default `<repo>\Development\json`). It's a read, so it
calls TSS regardless of `SUBMISSION_DRY_RUN`, and logs each call to `API.Call`. Use
the dumps to design the `TSS.BKD_ENS_Header` mirror and the next process step. The
`Development\json\` folder is gitignored (dumps may contain live TSS data).

## Where things land

- `STG.BKD_ENS_Header` - submission-ready copy + lifecycle (`Fusion_Status`).
- `API.Call` - one row per TSS call (request + response + status + duration + dry-run flag);
  see `API.vw_Call_Log` / `API.vw_Call_Errors`.
- `TSS.BKD_ENS_Header` - the authoritative live mirror of what's in TSS (raw JSON + parsed).
- `EXC.Execution` / `EXC.Transaction` / `LOG.*` - the run spine and per-movement transitions.

## TSS layer - update / cancel

- **`SUB_04_update.py`** (`SUB_UPDATE_BKD_ENS`) - full-replacement **update** (Rule 16):
  POST `/headers` with the full payload + `op_type=update` + `declaration_number` for
  live STG rows. Re-run `SUB_03_mirror.py` after to refresh the mirror.
- **`SUB_05_cancel.py`** (`SUB_CANCEL_BKD_ENS`) - **cancel**: POST `/headers` with
  `{op_type:"cancel", declaration_number}`. On success sets STG + tracking
  `Fusion_Status=CANCELLED` and marks the `TSS` mirror not-live (`IsLive=0`,
  `CancelledAt`). Destructive -> requires `SUBMISSION_MOVEMENT_KEY` or `_MAX_ROWS`.

Both are dry-run safe and log every call to `API.Call` / EXC, exactly like create.
Activated by migration `032`.

## Driving it from the portal

`liveWeb/app.py` exposes guarded action endpoints so the portal's pipeline buttons
run these jobs for one movement:

- `POST /api/action/<verb>?mk=<MovementKey>` - `verb` in promote, submit, mirror,
  update, cancel, reprocess. Sets the movement-key param, runs the runner (dry-run
  governed by `SUBMISSION_DRY_RUN`), returns the fresh status. Everything is tracked
  server-side.
- `POST /api/edit` - patch whitelisted STG payload fields for a movement (the Edit form).

Both run for real (no demo gate). Scope and safety come from `SUBMISSION_ENV`
(e.g. `TST`) and `SUBMISSION_DRY_RUN` in `CFG.Application_Parameters`.