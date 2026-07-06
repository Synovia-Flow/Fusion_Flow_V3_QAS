<#
  Fusion Flow V3 QAS - register the on-prem jobs as Windows Scheduled Tasks.

  Hybrid model (see ARCHITECTURE.md): the hosted portal only ENQUEUES to
  EXC.Job_Queue. These local tasks are what actually execute the work - next to the
  mailbox / file drops and the allow-listed TSS + Azure SQL IP.

  Tasks registered under \FusionFlow\:
    FF-Worker      drains EXC.Job_Queue (portal-enqueued jobs)   every 1 min  (WORKER_ONCE)
    FF-Ingestion   ING_00_run_cycle                              every 30 min
    FF-Processing  PRS_01_engine                                 every 30 min (+10 offset)
    FF-Mirror      SUB_03_mirror  (TSS status check)             every 30 min
    FF-FetchJson   SUB_06_fetch_json                             every 30 min (+15 offset)
    FF-RefChoice   REF_01_choice_values                          weekly, Mon 03:00

  DB credentials resolve from Configuration\Fusion_Flow_QAS.ini (or DB_* env) exactly
  as when you run the scripts by hand.

  Run once in an ELEVATED PowerShell:
    .\register_tasks.ps1 `
       -Python "D:\Environments\environment_Manager\Fusion_vEnv_Production\Scripts\python.exe" `
       -Repo   "C:\Users\It.synoviasupport\source\repos\Synovia-Flow\Fusion_Flow_V3_QAS"

  Remove everything later:
    Get-ScheduledTask -TaskPath '\FusionFlow\' | Unregister-ScheduledTask -Confirm:$false
#>
param(
  [Parameter(Mandatory = $true)][string]$Python,
  [Parameter(Mandatory = $true)][string]$Repo,
  [string]$User = "$env:USERDOMAIN\$env:USERNAME"
)

$ErrorActionPreference = 'Stop'
$TaskPath = '\FusionFlow\'

function New-Every30Trigger([datetime]$start) {
  $t = New-ScheduledTaskTrigger -Once -At $start
  $t.Repetition = (New-ScheduledTaskTrigger -Once -At $start `
      -RepetitionInterval (New-TimeSpan -Minutes 30)).Repetition
  return $t
}

function Register-FFTask([string]$Name, [string]$Execute, [string]$Argument, $Trigger) {
  $action   = New-ScheduledTaskAction -Execute $Execute -Argument $Argument -WorkingDirectory $Repo
  $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
      -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Hours 2)
  Register-ScheduledTask -TaskName $Name -TaskPath $TaskPath -Action $action -Trigger $Trigger `
      -Settings $settings -User $User -RunLevel Highest -Force | Out-Null
  Write-Host "registered $TaskPath$Name"
}

$six = (Get-Date).Date.AddHours(6)

# Worker: drain the queue once, every minute (WORKER_ONCE via a cmd wrapper so it exits).
$workerArg = "/c set WORKER_ONCE=1&& `"$Python`" `"$Repo\Modules\Global\job_worker.py`""
$minutely  = New-ScheduledTaskTrigger -Once -At (Get-Date)
$minutely.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)).Repetition
Register-FFTask 'FF-Worker' "$env:SystemRoot\System32\cmd.exe" $workerArg $minutely

# Recurring jobs (every 30 min). Scripts read scope from CFG.Application_Parameters.
Register-FFTask 'FF-Ingestion'  $Python "`"$Repo\Modules\Ingestion\ING_00_run_cycle.py`""   (New-Every30Trigger $six)
Register-FFTask 'FF-Processing' $Python "`"$Repo\Modules\Processing\PRS_01_engine.py`""      (New-Every30Trigger $six.AddMinutes(10))
Register-FFTask 'FF-Mirror'     $Python "`"$Repo\Modules\Submission\SUB_03_mirror.py`""      (New-Every30Trigger $six)
Register-FFTask 'FF-FetchJson'  $Python "`"$Repo\Modules\Submission\SUB_06_fetch_json.py`""  (New-Every30Trigger $six.AddMinutes(15))

# Reference refresh: weekly, Monday 03:00.
Register-FFTask 'FF-RefChoice'  $Python "`"$Repo\Modules\Global\REF_01_choice_values.py`""   (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 3:00AM)

Write-Host "`nDone. Review/adjust under Task Scheduler -> \FusionFlow."
Write-Host "The worker drains the portal queue every minute; the rest run on their own cadence."
