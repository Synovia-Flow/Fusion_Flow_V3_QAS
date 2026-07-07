# Fusion Flow V3 - Modular Product Skeleton

## Purpose

This document explains, in simple terms, what we are building and how the V3 product should work from now on.

The direction is clear: `Modules/` is the product backbone. The portal is only the user interface and control surface.

## Core Idea

Fusion Flow V3 should not be a set of disconnected scripts.

It should be a modular pipeline:

```text
Graph / Email / Files
  -> Modules/Ingestion
  -> ING
  -> Modules/Processing
  -> PRS
  -> Modules/Submission
  -> STG
  -> API / TSS
  -> Sync / Status
```

Each layer has one job. Data should move forward through the layers, not jump directly from email or portal to TSS.

## Folder Ownership

### Modules/Ingestion

Owns everything that enters the system.

Responsibilities:

- Read Microsoft Graph emails.
- Detect BKD DETAILS body emails.
- Detect Sales Orders / consignment attachments.
- Save original evidence: email metadata, trimmed body, attachments, source files.
- Write raw records into `ING`.
- Move files to the correct operational folders only after landing and DB registration.

Main direction from `Master`:

- `Modules/Ingestion/run_ingestion.py`
- `Modules/Ingestion/birkdale_sales_orders.py`
- `Modules/Ingestion/ens_headers.py`
- `Modules/Ingestion/load_raw.py`

`run_ingestion.py` should stay thin. It reads `CFG.Job`, decides which ingestion steps run, and dispatches to smaller modules.

### Modules/Processing

Owns the transformation from raw input to canonical business records.

Responsibilities:

- Read raw rows from `ING`.
- Normalize source fields.
- Enrich data using CFG, choice caches, product masterdata, partner masterdata, and approved defaults.
- Tag assumptions clearly.
- Build canonical records in `PRS`.
- Validate before anything reaches submission.

Main direction:

- `Modules/Processing/process_data.py`
- `Modules/Processing/mapping.py`

`mapping.py` should stay side-effect-free where possible. It should map and normalize, not write to DB or move files.

### Modules/Submission

Owns the controlled path from canonical records to TSS.

Responsibilities:

- Promote valid `PRS` records into `STG`.
- Build TSS payloads.
- Enforce dry-run and live-submit gates.
- Write every TSS request/response into `API.Call`.
- Reflect official TSS responses into `TSS` mirrors.
- Trigger or support status sync after responses.

Submission should not consume raw emails or attachments directly.

### Modules/Global

Owns shared utilities.

Responsibilities:

- Credential seeding.
- TSS endpoint checks.
- Shared config helpers.
- Masterdata load/sync utilities when they are not part of one specific run.

### Integration_Layer/Portal

The portal is the UI and control surface.

It can:

- Preview files.
- Show consignments.
- Show status.
- Let a user review/edit before commit.
- Trigger approved module actions.
- Configure settings.
- Open Swagger/API docs.

It should not become the place where ingestion, processing, or submission business logic lives.

## Database Layer Ownership

### CFG - Configuration

What should happen.

Examples:

- Tenants / clients.
- Jobs.
- Ingestion sources.
- Folder paths.
- Routes.
- TSS credentials and environments.
- Dry-run and submit gates.
- Product and partner masterdata references.

### ING - Raw Ingestion

What came in.

Examples:

- Inbound files.
- Source emails.
- BKD raw ENS rows.
- BKD raw Sales Orders rows.
- Original body/attachment evidence.

No business transformation should live here.

### PRS - Processed Canonical Records

What we believe the business object is.

Examples:

- `PRS.ENS_Header`
- `PRS.Consignment`
- `PRS.Goods_Item`

This is the main business layer before submission. It is where normalized, enriched, validated records live.

### STG - Submission Ready

What is ready to be submitted.

STG should not be used as an ingestion dumping ground. It is for records that have passed the PRS process and are ready for TSS workflows.

### API - API Calls

What we sent to external APIs and what came back.

Every TSS call should be traceable here.

### TSS - TSS Mirror

What we last observed from TSS.

This is not a live source of truth by itself. It should reflect the latest
successful TSS responses we have stored, including create responses, GET syncs,
and reconciliation results. If we need the current official state, we call TSS
again and then update the mirror.

### EXC / LOG - Execution And Technical Logging

What happened while running.

Use these for:

- Execution runs.
- Transactions.
- Errors.
- Field enhancements.
- Technical process logs.

## BKD ENS Flow

The next ENS creation should follow this path:

```text
1. Graph email arrives: "Details for DD.MM.YY"
2. Modules/Ingestion parses the DETAILS body
3. Body is trimmed using the approved stop rule
4. Evidence is saved to folders and ING
5. Raw ENS is loaded into ING.BKD_Raw_ENS
6. Modules/Processing reads ING
7. PRS.ENS_Header is created or updated
8. Sales Orders attachment is loaded into ING.BKD_Raw_Sales_Orders
9. Modules/Processing links Sales Orders to the ENS movement
10. PRS.Consignment and PRS.Goods_Item are created
11. Validation runs in PRS
12. Valid records can be promoted to STG
13. Modules/Submission handles TSS/API
14. TSS response is stored and synced back
```

No direct shortcut should create an ENS in TSS from an email body without leaving the `ING -> PRS -> STG -> API/TSS` trail.

## BKD Consignment Flow

Sales Orders / DEC data should follow this path:

```text
Attachment
  -> saved as evidence
  -> ING.BKD_Raw_Sales_Orders
  -> processing/enrichment
  -> PRS.Consignment
  -> PRS.Goods_Item
  -> validation
  -> STG when ready
  -> TSS submission when gates allow
```

Important rules:

- Do not create orphan consignments.
- Sales Orders must link to an existing ENS movement or be blocked for review.
- More than 99 goods must be split safely.
- Numeric TSS payload values must be rounded/formatted as TSS expects.
- Assumed values must be marked as assumptions.

## Masterdata Direction

We are adding masterdata so PRS can be better before TSS.

Current direction:

- `CFG.Product_Master` for product/item defaults.
- `CFG.Partner_Master` for consignee/importer/exporter/consignor enrichment.
- BKD product CSV and PRD partner data can be used to seed/sync QAS masterdata.

Masterdata should improve PRS quality. It should not silently hardcode values like one postcode for every consignment.

## Portal Direction

The portal should respect the same backend pipeline.

Examples:

- Upload preview can show mapping and validation.
- Preview can be editable.
- View Consignments should read PRS first, then show STG/TSS/API status when available.
- Settings can update CFG.
- Test API should respect environment and gates.
- Demo mode should not write operational DB records or call live TSS.

## Do Not Do

- Do not put business pipeline logic in React.
- Do not make FastAPI endpoints bypass `Modules/`.
- Do not submit to TSS without API logging.
- Do not use STG as raw ingestion.
- Do not create new crons/watchers without explicit approval.
- Do not assume BKD-specific values globally.
- Do not use `Deprecated/` as the primary direction.

## Current Priority

The current product priority is:

1. BKD parity with production behavior.
2. Clean ingestion through `Modules/Ingestion`.
3. Reliable `ING -> PRS` processing.
4. Correct masterdata enrichment.
5. Controlled `PRS -> STG -> API/TSS` submission.
6. Portal as a clean operational UI over that pipeline.

## Short Version

The product should be modular, auditable, and PRS-first.

```text
Portal observes and controls.
Modules do the work.
CFG decides.
ING records source evidence.
PRS builds the business truth.
STG prepares submission.
API/TSS records external reality.
EXC/LOG explain what happened.
```
