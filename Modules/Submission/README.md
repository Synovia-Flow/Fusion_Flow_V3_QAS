# Module 3 — Submission (BKD ENS Header)

Takes VALIDATED ENS headers from processing, submits them to TSS, and mirrors the
authoritative live record back. Every TSS call is logged in full to `API.Call` and
tied to the `EXC` execution spine; the movement advances through the shared
`CFG.Status_Vocabulary` lifecycle.

```
PRS.BKD_ENS_Header_Submission (VALIDATED)
  → promote_ens.py  → STG.BKD_ENS_Header            (STG_MATERIALISED → READY)
  → submit_ens.py   → POST /headers (create)        (SUBMITTING → SUBMITTED; captures ENS number)
  → mirror_ens.py   → GET /headers/<ENS number>     (RECONCILING → RECONCILED; writes TSS.BKD_ENS_Header)
  → update_ens / cancel_ens (stubs) operate against the TSS.* live mirror
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
python Modules\Submission\promote_ens.py     # VALIDATED -> STG (READY)
python Modules\Submission\submit_ens.py       # create; dry-run unless SUBMISSION_DRY_RUN=0
python Modules\Submission\mirror_ens.py       # get-back -> TSS.* live mirror; mark complete
```

## Where things land

- `STG.BKD_ENS_Header` — submission-ready copy + lifecycle (`Fusion_Status`).
- `API.Call` — one row per TSS call (request + response + status + duration + dry-run flag);
  see `API.vw_Call_Log` / `API.vw_Call_Errors`.
- `TSS.BKD_ENS_Header` — the authoritative **live mirror** of what's in TSS (raw JSON + parsed).
- `EXC.Execution` / `EXC.Transaction` / `LOG.*` — the run spine and per-movement transitions.

`update_ens.py` / `cancel_ens.py` are registered as jobs (`SUB_UPDATE_BKD_ENS`,
`SUB_CANCEL_BKD_ENS`, inactive) and will operate against `TSS.BKD_ENS_Header`.
