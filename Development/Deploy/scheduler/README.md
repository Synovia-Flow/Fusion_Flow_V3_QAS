# On-prem job scheduler (hybrid)

In the hybrid model the hosted **portal only enqueues** jobs (`EXC.Job_Queue`); the
**real execution happens here, on-prem**, next to the mailbox, file drops, and the
allow-listed TSS + Azure SQL IP. See [`../../../ARCHITECTURE.md`](../../../ARCHITECTURE.md).

There are two kinds of local jobs:

- **The worker** — drains the portal's queue. Run it **drain-once, frequently**
  (`WORKER_ONCE=1`, every ~1 min) so portal clicks (submit/promote/update/cancel/
  reprocess) execute here within a minute.
- **The recurring jobs** — ingestion, processing, TSS status mirror, fetch-json,
  reference — on their own cadence.

DB credentials resolve exactly as when you run the scripts by hand: `DB_*` env vars if
set, else `Configuration\Fusion_Flow_QAS.ini`.

## Quick setup (recommended)

Elevated PowerShell:

```powershell
cd C:\Users\It.synoviasupport\source\repos\Synovia-Flow\Fusion_Flow_V3_QAS\Development\Deploy\scheduler
.\register_tasks.ps1 `
  -Python "D:\Environments\environment_Manager\Fusion_vEnv_Production\Scripts\python.exe" `
  -Repo   "C:\Users\It.synoviasupport\source\repos\Synovia-Flow\Fusion_Flow_V3_QAS"
```

Registers everything under **Task Scheduler → `\FusionFlow\`**. Review/adjust cadences
there. Remove all tasks with:

```powershell
Get-ScheduledTask -TaskPath '\FusionFlow\' | Unregister-ScheduledTask -Confirm:$false
```

## Manual alternative (`schtasks`)

If you prefer not to run the script, the equivalent (adjust the two paths):

```bat
set PY=D:\Environments\environment_Manager\Fusion_vEnv_Production\Scripts\python.exe
set REPO=C:\Users\It.synoviasupport\source\repos\Synovia-Flow\Fusion_Flow_V3_QAS

:: worker - drain the queue every minute
schtasks /Create /TN "FusionFlow\FF-Worker" /SC MINUTE /MO 1 /RL HIGHEST /F ^
  /TR "cmd /c set WORKER_ONCE=1&& \"%PY%\" \"%REPO%\Modules\Global\job_worker.py\""

:: recurring jobs - every 30 min
schtasks /Create /TN "FusionFlow\FF-Ingestion"  /SC MINUTE /MO 30 /RL HIGHEST /F /TR "\"%PY%\" \"%REPO%\Modules\Ingestion\ING_00_run_cycle.py\""
schtasks /Create /TN "FusionFlow\FF-Processing" /SC MINUTE /MO 30 /RL HIGHEST /F /TR "\"%PY%\" \"%REPO%\Modules\Processing\PRS_01_engine.py\""
schtasks /Create /TN "FusionFlow\FF-Mirror"     /SC MINUTE /MO 30 /RL HIGHEST /F /TR "\"%PY%\" \"%REPO%\Modules\Submission\SUB_03_mirror.py\""
schtasks /Create /TN "FusionFlow\FF-FetchJson"  /SC MINUTE /MO 30 /RL HIGHEST /F /TR "\"%PY%\" \"%REPO%\Modules\Submission\SUB_06_fetch_json.py\""

:: reference - weekly, Monday 03:00
schtasks /Create /TN "FusionFlow\FF-RefChoice"  /SC WEEKLY /D MON /ST 03:00 /RL HIGHEST /F /TR "\"%PY%\" \"%REPO%\Modules\Global\REF_01_choice_values.py\""
```

## Notes

- **Run ONE scheduler.** These local tasks and the (now commented-out) `ff-cron-*`
  services in `render.yaml` do the same work — don't enable both or jobs double-run.
- **Single worker.** With one worker draining in `QueueID` order, a portal
  "Fix arrival & resubmit" (which enqueues reprocess → promote → submit) runs in that
  order. If you ever run multiple workers, they won't hand the same row out twice
  (`READPAST`/`UPDLOCK`), but strict ordering across the three steps needs one worker.
- **Dry-run safety.** Submission jobs still honour `SUBMISSION_ENV` (e.g. `TST`) and
  `SUBMISSION_DRY_RUN` from `CFG.Application_Parameters`.
