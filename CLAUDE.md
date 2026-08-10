# CLAUDE.md — Fusion Flow V3 QAS

Working context for Claude/agent sessions on this repo. Read before changing code.

## Identity & remotes

- This repo: **Fusion_Flow_V3_QAS** — GitHub org **Synovia-Flow**, primary branch
  `Master`; local working tree runs on `dev`. (The org **Synovia-Digital** is a
  different, older org — reference only.)
- Working copy lives on a UNC share: `\\pl-az-sdf-plint\Fusion_Production\Scratch\Fusion_Flow_V3_QAS`.
  Git works but use `git -C "<UNC path>" …`; `cd` + UNC breaks cmd/npx (copy files to a
  local temp dir to run esbuild/node tooling).
- V2 reference implementation (working prod flow for BKD): the separate checkout at
  `E:\Desktop\dev\Fusion_Flow_V2_BKD` (branch `dev02`) — has its own CLAUDE.md. Use it
  to answer "how does the live flow do X", never as a pattern to copy blindly into V3.
  A stale 89 MB copy used to sit under `.codex_tmp/`; it was removed as redundant, so
  this path is machine-local and not everyone will have it.

## Hard rules for Claude in this workspace

1. **Never touch the database.** No DDL/DML, no `deploy.py` runs, no SQL execution.
   Propose SQL as files/docs only.
2. **Never commit.** Leave all changes in the working tree; the team commits.
3. Role: support — help Codex with the frontend (`Portal/`), write
   documentation, review and fix code per-module.

## Core design principles (user-enforced)

- **Total traceability.** Every value sent to TSS must walk back to an `ING` row with
  full lineage (`RowHash`, `SourceTable`/`SourceID`, `ImportedAt`, ExecutionID).
  **Masterdata included**: partners/products are *ingested data* — target tables
  `ING.Customer_Master` / `ING.Product_Master` landed from Microsoft Graph via the same
  `ING_01 → ING_03` path as transactional data. The current engine reads
  `CFG.Partner_Master`/`CFG.Product_Master` and hardcodes constants in
  `mapping.BKD_QAS_CONSTANTS` — known gaps, not the design. Constants belong in
  `CFG.Application_Parameters`.
- **Everything per module.** Each field mapping/write is owned by exactly one Modules
  pipeline step (see below). No ad-hoc writes, no side-loaded data.
- **Reuse the existing V3 model** unless a real schema gap is proven and approved
  (see repo README working notes).

## Pipeline (Modules/)

`acquire → load raw → transform+validate → promote → submit → mirror`, every step in
the `EXC` execution spine, every TSS call logged to `API.Call`.

| Step | Script | Writes |
| --- | --- | --- |
| Acquire | `Modules/Ingestion/ING_01_acquire_email.py` (Microsoft Graph) | attachments |
| Parse ENS | `ING_02_parse_ens.py` | ENS headers CSV |
| Load raw | `ING_03_load_raw.py` | `ING.BKD_Raw_ENS`, `ING.BKD_Raw_Sales_Orders.PayloadJson` |
| NORMALISE/ENRICH/VALIDATE | `Modules/Processing/PRS_01_engine.py` via `mapping.py` + `process_data.py` | `PRS.*` |
| Reprocess | `PRS_02_reprocess.py` | rejected rows re-run |
| Promote | `Modules/Submission/SUB_01_promote.py` | `STG.*` |
| Submit | `SUB_02_submit.py` (+ `tss_client.py`) | POST to TSS, `API.Call` |
| Mirror / fetch | `SUB_03_mirror.py`, `SUB_06_fetch_json.py` | `Tss_Status`, response JSON |
| Update / cancel | `SUB_04_update.py`, `SUB_05_cancel.py` | full-replacement update (Rule 16), cancel |
| Reference | `Global/REF_01_choice_values.py`, `REF_02_commodity_codes.py` | `CFG.Choice_Value_Cache` |

Schemas: `CFG` config, `CHG` deploy audit, `EXC` execution spine + `EXC.Job_Queue`,
`ING` raw, `PRS` canonical, `STG` submission-ready, `API` call log, `LOG` technical.

## Key files for mapping work

- `Modules/Processing/mapping.py` — `ENS_CSV_TO_HEADER` (l.48), `SALES_ORDER_TO_GOODS`
  (l.71), `SALES_ORDER_TO_CONSIGNMENT` (l.171-274), `BKD_QAS_CONSTANTS` (l.284-289),
  `CONDITIONAL_RULES` (l.388-469).
- `Modules/Processing/process_data.py` — NORMALISE consignment loop (~l.693), partner
  enrichment (~l.1045-1154), ENRICH defaults (~l.1206-1259), masterdata fetch
  (l.819-949).
- DDL: `Configuration/SQL/` is the live queue; applied sets archived under `Archive/<timestamp>/`
  (e.g. `010_prs_tables.sql` PRS.Consignment, `014/016` choice field map).
- `Documentation/DB_Schema.md`, `Documentation/Solution_Design/`.
- `Documentation/Solution_Design/Control_Tower_Traceability_Proposal.md` — verified DB
  lineage inventory (DPE, API.Call payloads, stage ExecutionID stamps, unused views) +
  Control Tower upgrade plan. Key fact: `EXC.Data_Processing_Enhancement` = per-field
  audit (EntityRef `MK=…`, OldValue→NewValue, RuleApplied).

## TSS API references

- Postman collections: in the V2 checkout, `docs/api/v2.9.4/` and `v2.9.5/`
  (`TSS-Declaration-API-*.postman_collection.json`). Consignment endpoints under folder
  "2. Consignment"; SFD folder 5; IMMI folder 10; choice downloads folder 12.
- The operator supplies process-overlay CSVs per declaration type (field name, type,
  max len, mandatory, description + source hints). Received so far:
  - **ENS Consignment** header mapping → filled result:
    `Documentation/Solution_Design/Cons_Header_Mapping_Filled.md` (field-by-field
    sources organised by module step + gaps list). Keep this doc updated as mappings land.
  - **Full Frontier Declaration (FFD)** — `POST /tss_api/full_frontier_declarations`,
    ref prefix FFD, H1/H2/H3/H4 via `declaration_choice` (choice field
    `ffd_declaration_choice`), `declaration_category` IMZ/IMD/IMA. Notable: `icr`
    (string 25) required if `mode_of_transport=4`, FK → MaritimeICR;
    `maritime_inventory` required if `mode_of_transport=1`; `exporter_eori` mandatory
    H1/H3/H4, N/A H2; `customs_warehouse_identifier` mandatory H2. Not yet mapped —
    next candidate for a `FFD_Mapping_Filled.md`.
- Business terms: **ICR** = the ENS "transport document number / ICR number" from the
  ENS email (`ING.BKD_Raw_ENS.transport_document_number` → `PRS.ENS_Header`); it IS the
  consignment-level `transport_document_number` in TSS terms (`mapping.py:54-56`).
  The engine currently derives it from the SO document ref — known defect (gap 5 in the
  mapping doc).

## Known gaps (tracked in Cons_Header_Mapping_Filled.md)

1. ENRICH reads `CFG.Partner_Master`/`CFG.Product_Master`, silently skips if missing →
   must read `ING.Customer_Master`/`ING.Product_Master`, missing = validation exception.
2. Ingestion doesn't acquire masterdata from Graph yet.
3. BKD constants hardcoded in `mapping.py` → `CFG.Application_Parameters`.
4. EORI fallback `XI379692092000` literal → resolve from Birkdale partner row.
5. Consignment `transport_document_number` derived from SO doc ref → must pull down the
   ENS header ICR.
6. R1 engine never writes: `no_sfd_reason`, `align_ukims`, `use_importer_sde`,
   `declaration_choice`, `generate_SD`, `ducr`, `supervising_customs_office`,
   `customs_warehouse_identifier`, `importer_parent_organisation_eori`.

## Portal (two stacks)

- **Stack A — `liveWeb/`**: the operations portal (Flask, single-page + small API).
  Primary. See `liveWeb/README.md`.
- **Stack B — `Portal/`**: `fusion_api` (FastAPI) + `fusion_portal`
  (Vite/React). This is where Codex works; Claude assists here. Status vocabulary
  contract + modal behaviour documented in
  `Portal/fusion_portal/README.md`. Frontend checks: copy the file to
  a local temp dir and run esbuild there (UNC breaks npx).

## Safety

Run behaviour is data in `CFG.Application_Parameters` (`SUBMISSION_ENV`,
`SUBMISSION_DRY_RUN`, `SUBMISSION_MAX_ROWS`, `PROCESSING_MODE`). Portal buttons run
REAL jobs — safety is DB config, not a UI toggle. Rule 4: past `arrival_date_time` is
auto-corrected to tomorrow, never rejected.
