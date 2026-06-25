# Database Deployment — Queue → Deploy → Archive

A simple, auditable deployment pipeline for SQL DDL.

## Two runners

- **`deploy.py`** (recommended) — Python + `pyodbc`. Applies DDL directly
  (splits on `GO`), **prompts for a description**, logs every run + script to the
  **`CHG`** change-management schema, and moves succeeded scripts to `Archive/`.
  Self-bootstraps the `CHG` schema. No `sqlcmd` required.
- `Deploy-Database.ps1` — the original PowerShell runner (uses `Invoke-Sqlcmd`/`sqlcmd`).

### deploy.py usage

```powershell
cd "Development\Deploy"
python deploy.py --dry-run                                   # list the queue only
python deploy.py --description "Create CFG foundation"       # apply + log to CHG + archive
python deploy.py                                             # prompts: "Describe this deployment:"
python deploy.py --description "..." --promote-log           # also copy summary to logs\
```

Every run writes:
- `CHG.Deployment` — one row (run stamp, **description**, server, db, who, counts, status).
- `CHG.Change_Log` — one row per script (name, sha256 hash, batch count, SUCCESS/FAILED, archive path).

## How it works

```
Development/Deploy/Queue/   →  drop DDL *.sql files here (deployed in filename order)
Development/Deploy/Deploy-Database.ps1   →  the runner
Archive/<run-timestamp>/    →  scripts that deployed SUCCESSFULLY are moved here
logs/_Ignore/               →  full verbose run logs  (GITIGNORED)
logs/                       →  promoted run summaries  (committed → reaches repo / Claude)
Archive/_DeployManifest.csv →  append-only audit: when, what, where, success/fail
```

The runner picks up **everything** in `Queue/`, runs each script against the
database with abort-on-error, and **moves a script to `Archive/` only on success**.
The first failure stops the run (use `-ContinueOnError` to override); the failing
script stays in the Queue so you can fix and re-run.

## Usage (Windows PowerShell)

```powershell
cd "Development\Deploy"

# preview only
.\Deploy-Database.ps1 -Server tcp:<server>.database.windows.net -DryRun

# deploy the queue (integrated auth), and promote the summary log to logs\
.\Deploy-Database.ps1 -Server tcp:<server>.database.windows.net -PromoteLog

# SQL auth
.\Deploy-Database.ps1 -Server <server> -SqlUser <user> -SqlPassword <pwd>
```

### Connection from the .ini (recommended)

If you don't pass `-Server`, the runner reads the `[database]` section of
`Configuration_Layer\Fusion_Flow_QAS.ini` (server, database, user, password,
encrypt, trust_server_certificate). That file is **gitignored** because it holds
a password — copy `Fusion_Flow_QAS.example.ini` to `Fusion_Flow_QAS.ini` and set
the real password locally. So this is enough:

```powershell
.\Deploy-Database.ps1 -PromoteLog
```

You can also set `$env:FUSION_SQL_SERVER`, or override any value on the command line.

## Logs and the `_Ignore` convention

- All run logs are written to `logs\_Ignore\` which is **gitignored** — keep noisy,
  machine-specific logs out of version control by default.
- When you want a deploy result captured in the repo (and visible to Claude),
  add `-PromoteLog`: it copies the run summary **one level up** to `logs\`,
  which **is** committed.

## Typical flow for these scripts

1. Copy the DDL you want to deploy into `Queue\` (e.g. the
   `Configuration_Layer\SQL\001_*.sql … 003_*.sql` foundation scripts).
2. Run `Deploy-Database.ps1 -Server <server> -PromoteLog`.
3. On success the scripts are in `Archive\<timestamp>\`; the summary is in `logs\`.

> Scripts should be idempotent (the `Configuration_Layer\SQL` files are), so a
> re-run after a partial failure is safe.
