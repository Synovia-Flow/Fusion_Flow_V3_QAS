<p align="center">
  <img src="Branding/synovia-flow-logo.png" alt="Synovia Flow" width="260">
</p>

# Fusion Flow V3 QAS

Fusion Flow V3 is the target shape for the TSS automation work.

The goal is not to have a big script doing everything. The goal is to have a
clear pipeline where we can always answer:

- what did the customer send?
- what did we derive from it?
- what did we send to TSS?
- what did TSS return?
- who changed something manually, and why?

## Core flow

```text
Email / files
  -> ING raw evidence
  -> PRS processed + validated records
  -> STG submission-ready records
  -> API / TSS submit + mirror
  -> notifications and audit
```

## Main folders

| Folder | Purpose |
| --- | --- |
| `Automation/` | Compact view of the five automation steps we want in V3. |
| `Modules/Ingestion/` | Pull emails/files and land raw evidence in `ING`. |
| `Modules/Processing/` | Build and validate canonical `PRS` records. |
| `Modules/Submission/` | Promote, submit, update, cancel, mirror and fetch TSS evidence. |
| `Modules/Global/` | Shared helpers, credentials, reference data and reports. |
| `Portal/fusion_portal/` | Current Vite operator portal deployed as `fusion-flow-portal`. |
| `Portal/fusion_api/` | Current FastAPI backend deployed as `fusion-flow-api`. |
| `liveWeb/` | Alternative single-service portal stack kept for the module-driven product shape. |
| `Configuration/SQL/` | Database setup and seed scripts. |
| `Development/Deploy/` | Deployment tooling and change audit. |
| `Deprecated/` | Old prototypes kept for reference only. |

## Portal and Render

The real React website is in `Portal/fusion_portal`. After login it opens the
native V3 Control Tower; the simpler portal shortcuts remain available as
`Portal Home`.

The top-level `fusion_portal/` folder is only a compatibility adapter for the
existing Render service that still has the old Root Directory saved. It builds
the real portal and copies its `dist`. New Render services should use
`Portal/fusion_portal`, as declared in `render.yaml`.

Notification switches and recipients are stored in the existing
`CFG.Application_Parameters`. Control Tower reads `LOG.Notification` only when
that table is already deployed. The portal does not create a notification table
or start a scheduler.

## Database model

| Schema | Role |
| --- | --- |
| `ING` | Raw evidence exactly as received. |
| `CFG` | Approved config, masterdata, choice values and runtime gates. |
| `PRS` | Canonical records before submission. |
| `STG` | Submission-ready operational layer. |
| `API` | Request/response evidence. |
| `TSS` | Mirror of official TSS state. |
| `EXC` / `LOG` | Runs, transactions, technical logs and errors. |
| `CHG` | Deployments and manual changes. |

## What matters most

- Keep evidence first. Never lose the original source.
- Validate before TSS. Known-bad data should not become an API call.
- Use `CFG` for approved masterdata, not raw `ING` rows.
- Use `PRS` to make the mapping obvious before promotion/submission.
- Keep the portal thin. It can trigger, review and edit, but modules should own
  the work.
- Keep all live actions auditable.

## Running locally

Most scripts read configuration from the database or from local env/ini files.
Use dry-run settings first, especially for submission.

```powershell
python Modules\Ingestion\ING_00_run_cycle.py
python Modules\Processing\PRS_01_engine.py
python Modules\Submission\SUB_01_promote.py
python Modules\Submission\SUB_02_submit.py
python Modules\Submission\SUB_03_mirror.py
```

For the simpler end-to-end view, start with:

```powershell
python Modules\run_all.py --list
```

## Current note

Some of the proven production behaviour still lives in the BKD V2 automation
repo. The `Automation/` folder documents how that behaviour should be brought
into V3 without copying the old structure as-is.
