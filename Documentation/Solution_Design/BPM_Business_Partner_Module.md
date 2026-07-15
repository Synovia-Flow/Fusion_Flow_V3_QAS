# BPM — Business Partner Module (Release 4)

_Integration of the Business Partner Master proposal into Fusion Flow V3 QAS.
Detailed column-level spec: [`BPM_BusinessPartner_Schema_Design.xlsx`](BPM_BusinessPartner_Schema_Design.xlsx)
(sheets: Overview, BusinessPartner, Role/Auth, Ref\_\*, TSS Field Mapping, Critical Rules).
DDL: [`Configuration/SQL/036_bpm_business_partner.sql`](../../Configuration/SQL/036_bpm_business_partner.sql)._

## Purpose

A single governed source of truth for every trading party used on TSS customs
declarations — one record per physical partner, resolving up to six+ party roles
per consignment (consignor / consignee / importer / exporter / buyer / seller,
plus Decl-Header carrier and holder-of-authorisation), with validated EORIs and
length-correct addresses. Global schema, shared across client tenants (BKD, PLE, …).

## Design (two layers)

```
Customer Master xlsx (D365 BC [Customer], BT* postcodes)
        │  land raw, all NVARCHAR + lineage
        ▼
BPM.Stg_Customer_Master        ← ExecutionID / TransactionID (EXC spine) + Ingest_Batch_Id
        │  BPM.vw_BusinessPartner_Load  (normalise / derive)
        ▼
BPM.BusinessPartner            ← typed master: scheme-typed EORIs, county canon,
        │                         TSS 35/9-char shadow columns + *_Exceeds flags
        ├── BPM.BusinessPartnerRole            (m:n roles, optionally client-scoped)
        └── BPM.BusinessPartnerAuthorisation   (UKIMS / AEO)
Reference: BPM.Ref_Role (12 TSS party roles) · BPM.Ref_Client (mirror of CFG.Clients)
           · BPM.Ref_County_Normalisation (28 variants)
```

Key DQ rules carried in the model (empirically confirmed against TSS API v2.9.5):

| Rule | Consequence in the schema |
|---|---|
| Scheme acceptance differs by role — `consignor_eori` rejects GB (XI/EU only); importer accepts XI/EU/GB | `EORI_XI` / `EORI_GB` / `EORI_EU` scheme-typed columns + `Has_Valid_*` persisted flags, not one EORI column |
| TSS party name/street/city cap at 35 chars, postcode at 9 | `*_TSS` shadow columns + `*_Exceeds` flags catch truncation **before** submit |
| Importer EORI must be TSS-registered (Rule 6) or SFD/SDI silently never generate | `TSS_Registered` + `TSS_Registered_Checked_UTC` |
| No importer EORI → Birkdale (XI379692092000) becomes importer + consignor (Rule 13) | `EORI_Unknown = 1` triggers the fallback |
| Update is full replacement (Rule 16/24) | The master is the retained source; Fusion re-sends every party field |

## How it was adapted to platform conventions

The proposal arrived as a standalone `deploy_bpm_schema.py` + `BPM_Deploy.sql`.
It was integrated as canonical migration **036** with these changes:

1. **Deployment goes through the standard pipeline** (`stage_queue.py` →
   `deploy.py` → `Archive/` + `CHG` audit + `DB_Schema.md` regeneration) — the
   standalone deploy script is **not** used. Note its CONFIG targeted database
   `Synovia_Flow_Quality`; the platform's single core database is
   `Fusion_Flow_V3_QAS` (see `ARCHITECTURE.md`), which the pipeline targets.
2. **`BPM.Ref_Client` seeds from `CFG.Clients`** (MERGE with name/active sync)
   instead of a hardcoded BKD/PLE list — the proposal's static seed was missing
   CWD and would drift from the platform client registry.
3. **`BPM.Stg_Customer_Master` carries `ExecutionID` / `TransactionID`** so
   loads join the EXC execution spine like every other landing table
   (added via `COL_LENGTH` guards in case the proposal script already ran somewhere).
4. **The loader job is registered in `CFG.Job`** as `BPM_LOAD_CUSTOMER_MASTER`
   (module `MASTER_DATA`, **inactive**) so the registry stays authoritative;
   activate it when the loader script is built.

## Deploy

```powershell
cd Development\Deploy
python stage_queue.py --list        # should show 035 + 036 pending
python stage_queue.py
python deploy.py --description "035 API response docs + 036 BPM Business Partner Master"
```

(035 `API.Response_Document` is also pending — they deploy together in order.)

## Open items / next build steps

1. **Loader script** (`BPM_01_load_customer_master.py` or similar): land the
   Customer Master xlsx → `Stg_Customer_Master` (EXC execution + logging, drop
   the `Total` footer row), then upsert `BusinessPartner` from
   `vw_BusinessPartner_Load` (match on `PartnerCode`, stamp `Modified_At_UTC`).
   Wire the file pick-up into the ingestion pattern (`INBOUND` drop or a
   dedicated `CFG.Folder_Paths` type), then activate `BPM_LOAD_CUSTOMER_MASTER`.
2. **Per-client participation flags** (`Client_BKD` / `Client_PLE` bit columns)
   are deployed as proposed, but they require a schema change per new client —
   which cuts against the platform principle that onboarding a client is
   seed-data only. Consider migrating to a `BusinessPartnerClient` bridge (or
   deriving participation from `BusinessPartnerRole.ClientCode`) before a third
   client onboards.
3. **TSS_Registered validation job**: periodic check of importer EORIs via
   Permission Grant S0, stamping `TSS_Registered_Checked_UTC`.
4. **Processing-engine hookup**: enrich `PRS`/consignment party blocks from
   `BPM.BusinessPartner` (a `MASTER_ENRICH`-style transform, like
   `CFG.Carrier_Master`) so declarations consume the governed master rather
   than raw file values.
