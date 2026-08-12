# Fusion Flow V3 - Status Summary

Short version: V3 has the right shape, but not every production behaviour has
been moved into that shape yet.

## What is clear

- The target model is `ING -> PRS -> STG -> API/TSS`.
- The portal should not own business logic.
- Jobs should be driven by `CFG`, not hardcoded switches.
- Raw source data and manual changes need proper audit.
- The BKD production flow gives us the real behaviours we need to carry across.

## Current layer ownership

| Layer | Meaning |
| --- | --- |
| `ING` | What arrived. Emails, files, raw rows, hashes, paths. |
| `CFG` | What we trust as config/masterdata/choice values. |
| `PRS` | What we plan to submit after processing and validation. |
| `STG` | Operational copy ready for submission. |
| `API` | TSS request/response evidence. |
| `TSS` | Local mirror of official TSS state. |
| `EXC` / `LOG` | Runs, transactions, technical trace and failures. |
| `CHG` | Deployments and manual changes. |

## What still needs care

- Do not copy V2 as-is into V3. Pull the working behaviour into modules.
- Keep product matching SKU-first.
- Keep SDI goods mapping stable by source item / SKU / TSS goods id.
- Keep original files untouched.
- Keep notification logic based on official TSS status.
- Keep submit/update/cancel actions gated and auditable.

## Practical next step

Use the `Automation/` folder as the working map:

1. ingest,
2. process + validate,
3. promote + submit + sync,
4. notify,
5. SDI/SupDec automation.

That is the clean path from what works today to where V3 should land.
