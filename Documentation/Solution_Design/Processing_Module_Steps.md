# Fusion Flow — Processing Module: Full Step & Process Specification

**Module 2 — Data Processing (PRS).** How a raw `ING` record becomes a complete,
validated, TSS-shaped record in `PRS`, ready to hand off to `STG` for API
submission. Grounded in a real **Primeline (PLE)** ENS header alignment
(`Workbook_AMH_AM_30062026_mapped.xlsx`).

> **Principal-specific by design.** The *sequence* of processes below is common to
> every principal; the *content* of each step (which source column feeds which
> target, which lookup table, which default) is **configuration per principal**,
> because each client's source data is shaped differently. PLE arrives with
> `movement_type = "RoRo Accompanied ICS2"` and an empty carrier block (enriched
> from a carrier master); BKD arrives differently. Same pipeline, different map.

---

## 1. The processing stages (status lifecycle)

```
ING (INGESTED)                                                   STG (submission)
   │                                                                   ▲
   ▼                                                                   │
[STAGE] → [NORMALISE] → [ENRICH] → [DERIVE] → [VALIDATE] → [PROCESSED] ─┘ handoff
 STAGED      (working)   (working)  (working)  VALIDATED    PROCESSED      SUBMITTED
                                               / REJECTED                  (set in STG)
```

`Fusion_Status` on `PRS.BKD_ENS_Header_Submission` (+ the parallel
`…_Tracking` spine):

| Status | Meaning |
|--------|---------|
| `STAGED` | Row created from the raw record; mapping begun. |
| `VALIDATED` | Passed all mandatory/conditional + Critical-Rule checks. |
| `REJECTED` | Failed validation (reason in `Fusion_Status_Reason`). |
| `PROCESSED` | Complete & validated; **ready to move to STG**. (terminal in PRS) |
| `SUBMITTED` / `CANCELLED` | Set in **STG** once the TSS API call is made. |

> Adds `PROCESSED` to the existing `STAGED/VALIDATED/SUBMITTED/REJECTED/CANCELLED`
> set — a one-line widening of the `CK_…_Status` CHECK on the `013` tables.

---

## 2. The full process list (what the engine does, in order)

Each numbered process is one logical step. **Every** field set/changed/cleared is
logged to `EXC.Data_Processing_Enhancement` (old → new + rule + Transaction_ID);
each record is one `EXC.Transaction`; each run is one `EXC.Execution`.

| # | Process | What it does | EXC effect |
|---|---------|--------------|-----------|
| **P1** | **Claim source** | Select the next `INGESTED` `ING` record(s) for the principal not yet staged; open `EXC.Execution` + a per-record `Transaction_ID`. | Execution opened |
| **P2** | **Initialise** | Create the tracking row (source lineage: channel, LoadID, file, DetailsDate/ICR) and the submission row; `Fusion_Status = STAGED`. | Transaction: → STAGED |
| **P3** | **Map** | For each target TSS field, read its source column **from the principal's field map** (CFG-driven). | per-field DPE |
| **P4** | **Normalise** | Trim, case-fold codes, normalise yes/no, strip stray suffixes. e.g. `place_of_acceptance_same_as_loading "Yes" → "yes"`. | per-field DPE |
| **P5** | **Resolve dates** | Relative/loose dates → strict `DD/MM/YYYY HH:MM:SS` UTC + derived `…_utc`. e.g. `"Tomorrow's Date / 06:30" → "27/06/2026 06:30:00"` (Rule 4). | per-field DPE |
| **P6** | **Choice lookup** | Replace human values with TSS codes from the `CV_*` reference sets (introspect columns first — Rule 9). e.g. `movement_type "RoRo Accompanied ICS2" → "3a"` via `CV_mode_of_transport`. | per-field DPE |
| **P7** | **Master enrich** | Join reference/master data to **add** fields absent from source. e.g. carrier block from a **carrier master keyed by `carrier_eori`**: `XI894542681000 → Primeline Express Limited, Unit 14 Eagle Park Drive, Warrington, WA2 8JA, GB`. | per-field DPE |
| **P8** | **Client/QAS rules** | Apply per-principal constants/fallbacks/fixed values (e.g. BKD `arrival_port=GBAUBELBELBEL` [R12], `transport_charges=Y` [R11], importer fallback `XI379692092000` [R13]). | per-field DPE |
| **P9** | **Derive** | Compute fields from others (defaults, concatenations, conditional blanks). `op_type ← create`; `route` left blank (READ-only, TSS auto-sets). | per-field DPE |
| **P10** | **Validate** | Mandatory + conditional by `movement_type` (incl. the 3a effectively-mandatory set), max-length, choice membership, arrival-date bounds. Pass → `VALIDATED`; fail → `REJECTED` + reason. | Transaction: → VALIDATED/REJECTED; LOG on reject |
| **P11** | **Mark processed** | All checks clear and the record is complete → `Fusion_Status = PROCESSED`, `ValidatedAt` set. | Transaction: → PROCESSED |
| **P12** | **Hand off to STG** | Copy the PROCESSED record into the client STG submission table (`STG.<CLIENT>_ENS_Header`) where Module 3 manages the TSS API call; STG sets `SUBMITTED`. | Transaction: → handed off |

`declaration_number` and `status` are **not** produced here — they are returned by
TSS on submission (e.g. `ENS000000002686701`, `created`) and recorded back in STG.

---

## 3. Transform-type catalogue (the reusable vocabulary)

Every field maps to exactly one transform type. This is the kit the engine
implements once; principals compose their map from it.

| Type | Rule | PLE example (incoming → complete) |
|------|------|-----------------------------------|
| `PASSTHROUGH` | trim only | `identity_no_of_transport IMO9244116#V80MMF → (same)` |
| `NORMALISE_CODE` | trim + upper | (country/EORI codes) |
| `YESNO` | → `yes`/`no` | `place_of_acceptance_same_as_loading "Yes" → "yes"` |
| `DATE_UTC` | resolve + format `DD/MM/YYYY HH:MM:SS` UTC | `arrival_date_time "Tomorrow's Date / 06:30" → "27/06/2026 06:30:00"` |
| `CHOICE` | lookup a `CV_*` table | `movement_type "RoRo Accompanied ICS2" → "3a"` (`CV_mode_of_transport`) |
| `MASTER_ENRICH` | join a master by a key | carrier block by `carrier_eori` (carrier master) |
| `CONST` | fixed value | `op_type → create` |
| `QAS` | per-client fixed/fallback | BKD `arrival_port → GBAUBELBELBEL` |
| `DERIVE` | computed from other fields | conditional blanks, concatenations |
| `READONLY` | leave NULL (TSS sets) | `route`, `status`, `error_message` |
| `API_RETURN` | filled from the TSS response | `declaration_number → ENS000…` |

---

## 4. PLE worked example — every header field

| Target field | Incoming (raw) | Complete (final) | Transform | Lookup |
|---|---|---|---|---|
| op_type | — | `create` | CONST | — |
| declaration_number | — | `ENS000000002686701` | API_RETURN | (on submit) |
| movement_type | `RoRo Accompanied ICS2` | `3a` | CHOICE | `CV_mode_of_transport` |
| type_of_passive_transport | `Truck, tautliner, 25 tonne` | `3103` | CHOICE | `CV_passive_transport_types` |
| identity_no_of_transport | `IMO9244116#V80MMF` | (same) | PASSTHROUGH | — |
| nationality_of_transport | `United Kingdom` | `GB` | CHOICE | `CV_country` |
| conveyance_ref | `ICR2524784` | (same) | PASSTHROUGH | — |
| arrival_date_time | `Tomorrow's Date / 06:30` | `27/06/2026 06:30:00` | DATE_UTC | — |
| arrival_port | `Belfast Port` | `GBAUBELBELBEL` | CHOICE | `CV_port` |
| place_of_loading | `Warwick` | `Warwick` | PASSTHROUGH | — |
| place_of_unloading | `Mallusk` | `Mallusk` | PASSTHROUGH | — |
| place_of_acceptance_same_as_loading | `Yes` | `yes` | YESNO | — |
| place_of_delivery_same_as_unloading | `Yes` | `yes` | YESNO | — |
| transport_charges | `Account holder with carrier` | `Y` | CHOICE | `CV_transport_charges` |
| carrier_eori | `XI894542681000` | (same) | PASSTHROUGH | — |
| carrier_name | *(blank)* | `Primeline Express Limited` | MASTER_ENRICH | carrier master ← `carrier_eori` |
| carrier_street_number | *(blank)* | `Unit 14, Eagle Park Drive` | MASTER_ENRICH | carrier master |
| carrier_city | *(blank)* | `Warrington` | MASTER_ENRICH | carrier master |
| carrier_postcode | *(blank)* | `WA2 8JA` | MASTER_ENRICH | carrier master |
| carrier_country | *(blank)* | `GB` | MASTER_ENRICH | carrier master |
| status | — | `created` | READONLY/API_RETURN | (TSS) |

**This is where principals diverge.** For PLE the carrier block is `MASTER_ENRICH`
from a carrier master; for BKD the same fields are sourced/handled differently
(and `arrival_port`/`transport_charges` are fixed `QAS` constants). Same pipeline,
different per-field map.

---

## 5. Per-principal configuration model (the engine)

The mechanism that makes the steps principal-specific without code changes:
a CFG-driven field map. Proposed:

```
CFG.Processing_Field_Map
  ClientCode      char(3)        -- BKD | PLE | CWF
  EntityKind      varchar(20)    -- ENS_HEADER | CONSIGNMENT | GOODS_ITEM
  TargetField     varchar(64)    -- e.g. movement_type
  SourceColumn    varchar(64)    -- ING column (NULL for CONST/MASTER_ENRICH/DERIVE)
  TransformType   varchar(20)    -- PASSTHROUGH | CHOICE | DATE_UTC | MASTER_ENRICH | CONST | QAS | YESNO | DERIVE | READONLY | API_RETURN
  LookupRef       varchar(64)    -- CV_* table or master name (for CHOICE/MASTER_ENRICH)
  LookupKeyField  varchar(64)    -- join key for MASTER_ENRICH (e.g. carrier_eori)
  ConstValue      nvarchar(200)  -- for CONST/QAS
  RuleRef         varchar(40)    -- Critical-Rule citation for the audit
  StepNo          int            -- order
  IsMandatory     varchar(4)     -- YES | NO | COND
  CondExpression  nvarchar(200)  -- when IsMandatory=COND (e.g. movement_type=3a)
  IsActive        bit
```

The processing engine then becomes **generic**: for a principal it reads its map,
runs the transform types in order, and logs each to EXC — so onboarding a new
principal is seeding rows here, not writing code. The current BKD ENS stager
(`stage_bkd_ens_header.py`) is the hard-coded reference implementation of exactly
this map for BKD; the next build generalises it to read `CFG.Processing_Field_Map`.

---

## 6. Lookup / CV reference sets used (header)

`CV_mode_of_transport` · `CV_passive_transport_types` · `CV_country` · `CV_port` ·
`CV_transport_charges`. Cached in `CFG.Choice_Value_Cache` via the choice-value
bootstrap (`GET /choice_values/<field>`); column names vary per set, so the engine
introspects `INFORMATION_SCHEMA` before each lookup (Rule 9).

Master/reference (enrichment): **carrier master** (keyed by `carrier_eori`), and
per-client party/importer masters as principals require.

---

## 7. Audit — every step is logged

- **Field change** (P3–P9) → one `EXC.Data_Processing_Enhancement` row: `SchemaName`,
  `TableName`, `ColumnName`, `EntityRef` (MovementKey path), `OldValue`, `NewValue`,
  `RuleApplied` (transform + rule), `TransactionID`.
- **Stage transition** (P2, P10, P11, P12) → `EXC.Transaction` row.
- **Run** → one `EXC.Execution`; technical narration → `LOG.Process_Log`;
  rejects/errors → `LOG.Error_Log`.

So the raw record (`ING`), the final record (`PRS`), and **every transformation in
between** (`EXC.Data_Processing_Enhancement`) are all recoverable — before, after,
and why.

---

## 8. Build sequence from here

1. Widen `Fusion_Status` CHECK to include `PROCESSED` (013 tables).
2. Create + seed `CFG.Processing_Field_Map` (BKD + PLE header maps, from §4).
3. Generalise the stager into a **config-driven processing engine** that reads the
   map, applies the transform-type catalogue (§3), validates (P10), marks
   `PROCESSED`, and hands off to `STG`.
4. Add the `CV_*` choice-value bootstrap + the carrier master table/seed.
5. Build the STG client submission table + the Module 3 API submission manager.
