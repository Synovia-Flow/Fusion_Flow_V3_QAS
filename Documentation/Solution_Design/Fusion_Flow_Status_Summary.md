# Fusion Flow V3 - Status Summary

Plain version: V3 has the right shape. We are still moving proven production behaviour into that shape.

## The Shape

```text
ING -> PRS -> STG -> API/TSS
```

That means:

- `ING` keeps what arrived.
- `PRS` keeps what we cleaned and validated.
- `STG` keeps what is ready to submit.
- `API` keeps every call.
- `TSS` mirrors the official TSS answer.

## What Is Clear

- The portal should not own business logic.
- Jobs should use `CFG`, not hardcoded client switches.
- Raw source data needs audit.
- Manual changes need audit.
- BKD V2 is the proof of behaviour, not the structure to copy blindly.

## What Still Needs Care

- Product lookup must stay SKU-first.
- Partner/masterdata enrichment must come from `CFG`.
- Any assumed value must be visible as an assumption.
- TSS payload values must be formatted how TSS accepts them.
- Submit/update/cancel must stay gated.
- Notifications should use official mirrored TSS status.

## Practical Order

1. Ingest the source.
2. Process and validate.
3. Promote, submit and sync.
4. Notify from real status.
5. Add SD/SupDec once the first flow is stable.

That is the clean path.