# Fusion Flow V3 Automation

This folder is the simple map of the automation we want.

The production BKD V2 repo already proves the business flow. V3 should keep the behaviour, but split it cleanly into modules.

## One Sentence

```text
Get the source -> clean it -> validate it -> submit it -> sync what TSS says.
```

## The Five Areas

| Step | Module area | What it owns |
| --- | --- | --- |
| 1 | Ingestion | Get emails/files and save raw evidence in `ING`. |
| 2 | Processing | Map, enrich and validate into `PRS`. |
| 3 | Submission | Promote to `STG`, call TSS, store `API` evidence. |
| 4 | Status / notifications | Read real TSS status and notify from that. |
| 5 | SD / SupDec | Handle supplementary declarations when that flow is in scope. |

## Current Vs Target

Current BKD production works, but some logic is spread across routes, scripts and helper files.

Target V3:

```text
Modules/Ingestion  -> ING
Modules/Processing -> PRS, using CFG
Modules/Submission -> STG, API, TSS
Portal             -> review, edit, trigger, display
```

## Non-Negotiables

- Original files are evidence. Do not rewrite them.
- Product matching starts with SKU.
- Description is not a safe primary key.
- Weights must follow the business rule, not a random multiplier.
- ENS transport document number should come from the movement/ICR context, not just an `S-ORD`.
- SD goods must keep a stable link to source goods and TSS goods id.
- Pending Payment is not automatically an error.
- Manual edits, submits and cancels must be auditable.

## What This Folder Is Not

This folder is not a second product.

It is the plain map for how the working V2 behaviour should land in V3.