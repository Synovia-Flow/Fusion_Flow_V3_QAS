# Fusion Flow V3 QAS — Code Audit (2026-07-07)

Senior-level audit of the whole repo, five areas in parallel, criticals verified by hand.
Read-only DB + code inspection; no changes made. Line refs are at audit time on `dev`.

## Verdict

Two systemic themes dominate and both are release-blockers:

1. **No access control anywhere.** Neither portal authenticates. `fusion_api` has 0
   `Depends`/`Security` guards (verified); liveWeb's login is `preventDefault()` theatre.
   Anyone who can reach either service can read every tenant's data and fire **live HMRC
   TSS submit/cancel** with no credential.
2. **No "one declaration per movement" guarantee.** The submit path has no row claim, no
   idempotency key, no pre-submit check for an existing declaration number, and every
   ambiguous failure funnels back to a fresh `op_type=create` — duplicate live
   declarations under normal operational events (retry, re-send, crash, batch+worker
   overlap).

Plus a **secrets-in-git-history** incident (live TSS + DB passwords pushed to GitHub,
verified present in history) requiring rotation now.

## CRITICAL (fix before any production use)

| # | Area | Finding | File |
| --- | --- | --- | --- |
| K1 | Secrets | Live TSS passwords for BKD/CWF/PLE (incl. PRD) committed in `d64a1d3`, only removed from HEAD in `fb30965` — **still in history, pushed to GitHub** (blob verified, 804 bytes). Rotate all 6 + purge history. | `Configuration/tss_credentials.json` |
| K2 | Secrets | Azure SQL `SynFW_DB` password in history twice (`0055f02` `Fusion_Flow_QAS..ini` double-dot slipped `.gitignore`; `2a17caf` `Configuration_Layer/...ini`). Transitively re-leaks TSS creds (stored plaintext in `CFG.TSS_Credential`). Rotate + purge. | history |
| K3 | liveWeb | Zero auth on all `/api/*`; login form is cosmetic. `POST /api/action/submit\|cancel`, `/api/enqueue`, `/api/edit` all open; binds `0.0.0.0`. | `liveWeb/app.py:153,182,208` |
| K4 | Stack B | Zero auth on all endpoints (0 `Depends`, verified). `/api/tss/consignments/{id}/submit?confirm_live=true` fires live TSS unauthenticated; `/api/admin/settings` rewrites TSS creds/Graph secret/env. | `fusion_api/app/main.py` |
| K5 | Stack B | Client-side auth forgeable: `isAuthenticated` from `sessionStorage`; any `{authenticated:true,tenantCode:"SYNOVIA"}` yields CentralAdmin. Role never checked server-side. | `App.jsx:155-181,3162` |
| K6 | Submission | No claim/lock on STG READY rows before POST (`db.transition→SUBMITTING` is log-only, verified). Batch + worker (or two enqueued submits) both read READY → two live `create`. | `SUB_02_submit.py:175-201` |
| K7 | Submission | Ambiguous outcomes (timeout after server processed / unexpected 2xx envelope / crash in `sync_create_response`) recorded ERROR or left READY while declaration is live → recovery re-`create`s it. No post-failure GET reconciliation, no idempotency key. | `SUB_02_submit.py:208-274`, `tss_client.py:80-91` |
| K8 | Ingestion | Crash between `ING.Inbound_File` commit and row-landing → hash-dedup skips the file forever, message never moved: **permanent silent data loss** (generic Graph route). | `graph_email.py:252-257`, `ingest.py:199-240` |
| K9 | Ingestion | Duplicate attachment written to disk even when DB-deduped; ING_03 dedup is by **filename** only → re-sent/`_2` file double-loads → **doubled goods/weights/values** in the declaration. | `ING_01:120`, `ING_03:107-128`, `process_data.py:598` |

## HIGH

| # | Area | Finding | File |
| --- | --- | --- | --- |
| H1 | Submission | `IsLive` has two meanings — SUB_02 sets it per-env, SUB_03 sets it `=1` unconditionally; SUB_04/05 treat `=1` as "confirmed live, safe to update/cancel". In PRD the mirror-confirmed gate is bypassed. | `SUB_02:72`, `SUB_03:113`, `SUB_04:63` |
| H2 | Submission | Cancel guard disabled by shared `SUBMISSION_MAX_ROWS`: set it to throttle a submit batch, run SUB_05 without MK → cancels 50 live declarations. | `SUB_05_cancel.py:49-51` |
| H3 | Submission | Queued job with NULL MovementKey degrades to unscoped batch; for `cancel` cancels an arbitrary declaration. | `job_worker.py:66`, `SUB_05:49` |
| H4 | Submission | Worker crash between claim and completion strands job `RUNNING` forever — no lease, no max-attempts sweep. | `job_worker.py:45-96` |
| H5 | Submission | No EnvCode stamp on STG rows: flip `SUBMISSION_ENV=PRD` at go-live and SUB_04/05 retarget existing **TST** decl numbers at the **production** endpoint. | `SUB_03/04/05` |
| H6 | liveWeb | Unauthenticated stored XSS: `/api/edit` writes arbitrary strings → `index.html` renders DB values into `innerHTML` unescaped → script runs in operator session, drives any action. | `app.py:208` → `index.html:489,801` |
| H7 | liveWeb | Static handler denylist (`startswith("api/")`, `==".env"`) serves `synovia-flow-3.env` (real infra IDs), `app.py`, `tools/*.py` source. | `app.py:347-354` |
| H8 | Stack B | IDOR: `/api/consignments/{id}` has no ClientCode filter (list endpoint does) → enumerate to read every tenant's consignments. | `main.py:2232-2281` |
| H9 | Stack B | Demo sample data replaces a legitimately-empty result set AND masks API/DB outage — operators can't tell fake from real; demo rows carry real-looking IDs that trigger route-checks. | `App.jsx:2829,3241` |
| H10 | Stack B | TSS/portal password compared + stored plaintext (`hmac.compare_digest(password, row.TssPassword)`); settings endpoint reads it back. | `main.py:1159,287` |
| H11 | Processing | Rule-4 auto-bump fabricates arrival date: date-only "today" arrival at midnight < now → silently rewritten to tomorrow and VALIDATED; stale arrivals bumped not rejected. Two engines disagree (process_data rejects). | `mapping.py:657-670`, `PRS_01:317` |
| H12 | Processing | Orphaned tracking row (commit-on-log) permanently blocks a movement: NEW excludes it, REPROCESS-REJECTED can't see it. Silently dropped, absent from error views. | `PRS_01:297-336,469` |
| H13 | Processing | Shrinking reprocess leaves stale consignments/goods (upsert, no delete of surplus) then stamps them VALIDATED into submission. | `process_data.py:1331,1534` |
| H14 | Ingestion | ENS↔Sales-Order join uses two different dates (email-received `FileDate` vs movement `DetailsDate`) → zero-match → movement REJECTED though goods data is present. | `ING_03:46`, `process_data.py:1560` |
| H15 | Deploy | commit-then-archive gap: script applied but recorded FAILED/unrecorded if archive move or `record_change` fails → re-applied next run (non-idempotent seeds duplicate). | `deploy.py:239-261` |

## MEDIUM (selected — 25 total in area reports)

- Submission: promote resets to READY unconditionally → re-create of submitted movement
  (`SUB_01:60`); SUB_04 unscoped, selects any non-CANCELLED incl. ERROR (`SUB_04:60`);
  rejected TSS record marked RECONCILED+IsLive=1 (`SUB_03:97`); queued jobs inherit live
  DRY_RUN/ENV at execution not enqueue (`job_worker:66`); re-seed nulls operator-set
  password (`seed_credentials:161`); env-fault (`import requests`) mass-corrupts row
  statuses to ERROR (`tss_client:73`).
- Processing: `to_yes_no` coerces "N/A"→"yes" for controlled_goods (`mapping:511`); no real
  transaction boundaries, log commits pending DML incl. after exception (`process_data:310`);
  config identifiers f-stringed into SQL — 2nd-order injection (`PRS_01:256`,
  `process_data:1290`); `_upsert` never writes NULLs despite contract, DPE log disagrees
  (`PRS_01:315`); over-aggressive fuzzy choice match → wrong-but-valid code (`PRS_01:93`);
  unparseable/empty DetailsDate → SQL convert error or pulls entire table into one movement
  (`process_data:541`).
- Ingestion: DedupKey uses raw date string (format change → duplicate movement)
  (`ING_02:244`); dedup-skipped emails rescanned forever (`graph_email:255`); `Retry-After`
  HTTP-date crashes retry loop, no 401 refresh (`graph_email:98`); duplicate CSV headers
  drop data on Graph route (`graph_email:181`).
- liveWeb: `/api/health/db` leaks host/user/version unauthenticated (`app.py:77`); raw
  `str(e)` returned across endpoints; real jobs run sync in 2 gunicorn workers, 60s timeout,
  no rate limit → DoS + partial TSS state.
- Stack B: username collision across clients logs into wrong tenant (`main.py:1142`);
  unbounded upload read → memory DoS, extension-only content check (`main.py:2481`);
  "Save Detail" implies persistence but only local state, refresh discards (`App.jsx:2598`);
  `test_tss_connection` mutates DB + calls TSS on a GET (`main.py:1369`).
- Deploy: no deployment lock (`deploy.py:227`); `stage_queue` fails open — DB down stages
  everything (`stage_queue:147`); canonical SQL back-catalogue destroyed by move-on-success,
  only 034/035 remain, DR rebuild needs archaeology (`Configuration/SQL/`); both Dockerfiles
  run as root; dual scheduler (Render crons vs Windows tasks) guarded only by comments →
  double-execution of cron entrypoints (which bypass the safe queue claim).

## LOW (highlights)

`check_choice.py` imports non-existent `process_engine` → tool crashes; no `.dockerignore`
+ `COPY . .` bakes `.git`/secrets into image; `Development/jsonx/*.json` real customer
declaration committed (GDPR, `.gitignore` missed the `jsonx` sibling); wildcard dep pins;
no CI/secret-scanning (`.github/workflows` empty); build toolchain left in runtime image;
`_split_reference` can strip its own `-02` suffix → two parts share transport doc number.

## Things done well (context)

- `deploy.py` is per-script transactional with SHA-256 change logging — design sound,
  findings are gaps in it.
- `job_worker` **queue** claim is correctly atomic (`READPAST, UPDLOCK, ROWLOCK`) — worker
  scale-out is safe; the danger is the cron entrypoints that bypass it.
- Parameterised SQL used consistently for values everywhere; injection surface is
  identifiers only (gated by CFG write access).
- `sync:false` on every secret in both `render.yaml`; `EXC.Data_Processing_Enhancement`
  gives genuine per-field audit (see Control Tower proposal).

## Recommended order

1. **Rotate** all 6 TSS creds + `SynFW_DB` password (compromised since 2026-06-25); purge
   git history (`git filter-repo`), force-push; add `.dockerignore`.
2. **Auth**: put a real auth layer in front of both portals before either is reachable;
   gate mutating verbs behind operator identity; log who.
3. **Idempotency**: STG row claim (`UPDATE…SET SUBMITTING WHERE READY`) + pre-submit
   existing-declaration check + post-failure GET reconciliation. Kills K6/K7 and the
   promote/reprocess re-create paths.
4. **Ingestion integrity**: content-hash (not filename) dedup for sales orders; land rows
   in the same transaction as the file row; fix the ENS↔SO date-join key.
5. **Transaction boundaries**: stop committing on every log; one commit per movement,
   real rollback.
6. Then work the HIGH/MEDIUM lists per module.
