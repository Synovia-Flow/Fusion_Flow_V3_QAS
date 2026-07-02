# Synovia Flow 3 — Live Portal (`liveWeb/`)

A blueprint-driven, single-page operations portal for the Fusion Flow platform:
a splash/login → dashboard with floating pill navigation, per-client scoping,
interactive SVG charts, tiles, and views for Dashboard / Jobs / Analytics /
Clients / Submissions / Admin. Montserrat throughout; refreshed "Flow 3" palette
(midnight navy + flow-aqua + fusion-coral).

Static and self-contained — no build step, no framework, no external JS. It loads
Montserrat from Google Fonts (with a system fallback) and renders all charts by hand.

> **Deploying the full platform (portal + jobs + worker)?** Use the repo-root
> **`render.yaml`** and **`Dockerfile`**, not the ones in this folder. The root image
> bundles `Modules/` so the portal's action/enqueue buttons, the cron jobs and the
> background worker all work. `liveWeb/render.yaml` here deploys only the read-only
> portal (portal + `/api/blueprint`); its image has no `Modules/`, so `/api/action`
> and `/api/enqueue` won't run there. See the repo root `README`/`render.yaml`.

## Structure

| File | Purpose |
|------|---------|
| `index.html` | The whole app (styles + markup + logic inline). |
| `blueprint.json` | **The data blueprint** the portal renders — clients, KPIs, jobs, analytics, submissions, admin params. Edit this (or generate it from the DB) to change what's shown. The app falls back to an inline copy if the file can't be fetched. |
| `assets/` | Logos (`SynoviaFlowLogo.png`, `FusionLogo.jpg`, `SynoviaFlowJustLogo.png`, …). |
| `render.yaml` | Render blueprint — static site, **Frankfurt** region. |

## Run locally

```powershell
cd liveWeb
python -m http.server 8080
# open http://localhost:8080  (any email/password → Enter)
```

## Deploy to Render (Frankfurt)

1. Push the repo to GitHub (already on `Master`).
2. Render Dashboard → **New → Blueprint** → select this repo → Render reads
   `liveWeb/render.yaml` and provisions a static site in **Frankfurt**.
   (Or **New → Static Site**, Root Directory `liveWeb`, Publish Directory `.`.)
3. Every push to `Master` auto-deploys.

## Logos

Drop the real marks into `assets/` with these names (the splash references them and
falls back to a wordmark if missing):

- `assets/SynoviaFlowLogo.png` — Synovia Flow wordmark (login splash)
- `assets/FusionLogo.jpg` — Fusion product mark ("Powered by Fusion")
- `assets/SynoviaFlowJustLogo.png` — the Flow mark only (favicon / compact)

Copy from the brand share:
`\\PL-AZ-SDF-PLINT\Fsuion_Production_Application\...\Common\Branding\`

## Blueprint → live data

`blueprint.json` mirrors the platform's own tables, so it can be generated straight
from SQL later:

| Blueprint key | Source |
|---------------|--------|
| `clients` | `CFG.Clients` |
| `data.<CC>.jobs` | `CFG.Job` (per client) |
| `data.<CC>.params` | `CFG.Application_Parameters` |
| `data.<CC>.submissions` | `PRS.BKD_ENS_Header_Tracking` / `STG.*` / `TSS.*` |
| `data.<CC>.statusMix` / `rejectionReasons` | `PRS.vw_BKD_ENS_Header_*` |
| `data.<CC>.throughput` / `activity` | `EXC.Execution` / `LOG.Process_Log` |

## Live DB link (`tools/`)

Two scripts turn the live database into `blueprint.json` (needs `pyodbc` — use the
production venv):

```powershell
# 1) create the connection env from the .ini (writes liveWeb/.env — gitignored)
python liveWeb\tools\make_env.py --print

# 2) regenerate blueprint.json from the live DB
python liveWeb\tools\export_blueprint.py
```

- **`make_env.py`** reads `Configuration/Fusion_Flow_QAS.ini [database]` and writes
  `liveWeb/.env` (`DB_SERVER` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_DRIVER` /
  `DB_ENCRYPT` / `DB_TRUST`). `--print` echoes a Render-ready env block (password
  masked) to paste into the Render service's Environment.
- **`export_blueprint.py`** connects (prefers `liveWeb/.env`, else the `.ini`),
  reads `CFG.Clients` / `CFG.Job` / `CFG.Application_Parameters` and, for each client
  that has ENS tables, `PRS.<CC>_ENS_Header_Tracking` + the `vw_*` views + `LOG`/`EXC`,
  and writes `blueprint.json`. Every query is guarded, so inactive clients degrade to
  an onboarding placeholder. Secret-named params are masked.

### Render environment (txt env)

- `render.env.example` (committed, placeholders) is the uploadable template. On Render:
  **Service → Environment → "Add from .env"** and paste the lines, keeping
  `DB_PASSWORD` as a **secret**.
- Your real values come from `make_env.py` (writes the gitignored `liveWeb/.env`,
  same `KEY=VALUE` format — upload that file directly).
- Or declare them in the blueprint: `render.yaml` has the `DB_*` vars on the live
  service (`sync: false` → Render prompts for them).

Because the site is static, "live" can also mean: run `export_blueprint.py` (locally,
or as a Render cron), commit the refreshed `blueprint.json`, and Render auto-deploys —
the low-infra alternative to the API service below.

## Real-time: the live API service (`app.py`)

`app.py` is a small Flask service that serves the **same portal** plus the live
blueprint from the DB:

| Route | Returns |
|-------|---------|
| `GET /` + `/<file>` | the portal + assets |
| `GET /api/blueprint` | live blueprint built from the DB (30s cache); **falls back** to the committed `blueprint.json` (tagged `source:"static-fallback"`) if the DB is unreachable, so the page always loads |
| `GET /api/health` | liveness probe |
| `POST /api/action/<verb>?mk=…` | run a job for one movement — `promote`/`submit`/`mirror`/`update`/`cancel`/`reprocess` (dry-run governed by `SUBMISSION_DRY_RUN`); tracked in EXC + `API.Call` |
| `POST /api/edit` | patch whitelisted STG payload fields for a movement (Edit form) |

The pipeline drill-down (click a Submissions row) shows the stage rail, the ING→PRS
transforms, stage-advance buttons, and — once a movement is live in TSS — **Edit /
Update (Rule 16) / Cancel** on the TSS layer. Buttons call the endpoints above.

> **Actions are OFF by default.** Set `PORTAL_ACTIONS_ENABLED=1` on the service to let
> the buttons actually run jobs / call TSS; otherwise they advance the pipeline
> visually only (safe demo mode).

The front-end tries `/api/blueprint` first, then `blueprint.json`, then its inline
copy — so the exact same `index.html` works as a static site **or** behind this API.

Run locally:
```powershell
python liveWeb\tools\make_env.py     # creates liveWeb\.env
python liveWeb\app.py                 # http://localhost:8080  (live if the DB is reachable)
```

Deploy to Render (Frankfurt) — pick ONE service in `render.yaml`:
- **`synovia-flow-3-portal`** (static) — simplest; no DB needed; refresh via
  `export_blueprint.py` + commit.
- **`synovia-flow-3-live`** (Docker) — real-time `/api/blueprint`. The `Dockerfile`
  bundles the **Microsoft ODBC Driver 18** (pyodbc needs it — not in Render's stock
  image). Set the `DB_*` env (upload `.env` or the `sync:false` prompts). **The Azure
  SQL firewall must allow this service's outbound IPs** (Render → Settings shows them),
  and set `DB_DRIVER={ODBC Driver 18 for SQL Server}`.

> A scheduled `export_blueprint.py` (Render cron) + commit is the low-infra
> alternative if you'd rather not open the DB firewall to the web service.
