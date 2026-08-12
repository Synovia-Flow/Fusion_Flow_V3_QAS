<p align="center">
  <img src="Branding/synovia-flow-logo.png" alt="Synovia Flow" width="260">
</p>

# Fusion Flow V3 QAS

This repo is the V3 direction for Fusion Flow.

The simple idea is this:

```text
Customer sends email/file
  -> we store exactly what arrived
  -> we clean and enrich it
  -> we build the data TSS needs
  -> we submit it when it is safe
  -> we store what TSS answered
```

No magic. No hidden portal logic. No random tables.

## The Main Rule

Each layer has one job.

| Layer | Plain meaning |
| --- | --- |
| `ING` | The original evidence. What the customer sent. |
| `CFG` | Trusted setup. Clients, products, partners, defaults and gates. |
| `PRS` | The cleaned version we plan to send. |
| `STG` | The submit-ready copy. |
| `API` | Every request and response we send/receive. |
| `TSS` | What TSS officially says now. |
| `EXC` / `LOG` | What ran, when it ran, and what failed. |
| `CHG` | Manual changes and deployments. |

If a value is guessed, defaulted, fixed or enriched, we should be able to say why.

## Main Folders

| Folder | What it is for |
| --- | --- |
| `Portal/fusion_portal/` | Current Vite portal shown to the user. |
| `Portal/fusion_api/` | Current FastAPI backend for the portal. |
| `Modules/Ingestion/` | Bring emails/files into `ING`. |
| `Modules/Processing/` | Turn raw data into clean `PRS` data. |
| `Modules/Submission/` | Promote, submit, mirror and store TSS evidence. |
| `Modules/Global/` | Shared helpers, credentials and reference data. |
| `Automation/` | Very short map of the five automation areas. |
| `Configuration/SQL/` | Database setup and seed scripts. |
| `Deprecated/` | Old prototypes/reference code. Do not build new flow there. |

`liveWeb/` is still in the repo, but the active deployed portal stack is `Portal/`.

## The Product Shape

The portal should let an operator:

1. see what arrived,
2. preview what Fusion mapped,
3. edit when needed,
4. approve or queue a job,
5. see the real TSS result.

The portal should not secretly become the ingestion engine. The modules do the work.

## Useful Words

| Word | Meaning |
| --- | --- |
| ENS | Movement/header level declaration. |
| DEC | Consignment/declaration row under an ENS. |
| SFD | Simplified Frontier Declaration, where that flow applies. |
| SD | Supplementary Declaration / SupDec flow. |
| Mirror | Our local copy of what TSS returned. Not a guess. |
| Dry-run | Build and log the plan, but do not send live writes. |

## Safety Rules

- Do not create new tables unless there is a proven gap.
- Do not add crons unless scheduling is explicitly approved.
- Keep original files untouched.
- Use `CFG` masterdata for enrichment.
- Make assumptions visible.
- Round TSS numeric payloads to the format TSS accepts.
- Log every TSS call in `API`.
- Treat TSS status as the official status.

## Local Commands

Use dry-run first, especially for submission.

```powershell
python Modules\Ingestion\ING_00_run_cycle.py
python Modules\Processing\PRS_01_engine.py
python Modules\Submission\SUB_01_promote.py
python Modules\Submission\SUB_02_submit.py
python Modules\Submission\SUB_03_mirror.py
```

For a quick list of module jobs:

```powershell
python Modules\run_all.py --list
```

## Where We Are

Some proven BKD behaviour still lives in the V2 production repo. We use V2 as the contract for what works, but V3 should keep the cleaner module shape above.