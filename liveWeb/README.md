# Synovia Flow 3 — Live Portal (`liveWeb/`)

A blueprint-driven, single-page operations portal for the Fusion Flow platform:
a splash/login → dashboard with floating pill navigation, per-client scoping,
interactive SVG charts, tiles, and views for Dashboard / Jobs / Analytics /
Clients / Submissions / Admin. Montserrat throughout; refreshed "Flow 3" palette
(midnight navy + flow-aqua + fusion-coral).

Static and self-contained — no build step, no framework, no external JS. It loads
Montserrat from Google Fonts (with a system fallback) and renders all charts by hand.

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

Because the site is static, "live" means: run `export_blueprint.py` (locally, or as a
scheduled job / Render cron), commit the refreshed `blueprint.json`, and Render
auto-deploys. For real-time, the same query layer can be exposed as a tiny read-only
Render **web service** the portal fetches from — the front-end already fetches
`blueprint.json` at load and would just point at that endpoint instead.
