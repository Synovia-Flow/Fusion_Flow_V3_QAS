# FLOW V3 Operating Model

## Purpose

This document is the single reference for the FLOW V3 QAS automation model. It
combines the FRD requirements, Discovery findings, notification design and code
map into one readable document so the team does not have to jump across several
files.

The target operating model is:

```text
Graph email/file intake
-> ING trace
-> ENS header creation/submission
-> Sales Orders consignments/goods submission
-> TSS status watcher + notifications
-> SDI/SupDec sync and autosubmit
```


## Confirmed Architecture Decisions

| Decision | Contract |
| --- | --- |
| Layer separation | `Documentation_Layer` explains the why/what, `Configuration_Layer` owns SQL/config contracts, and `Integration_Layer` owns executable code and runtime files. |
| FLOW_V3 role | `Integration_Layer/FLOW_V3` is the thin operational console for scripts 01-05. Shared logic belongs under `Integration_Layer/App`. |
| DB-first configuration | Jobs should read `CFG.*` first. YAML files are fallback/dev overrides during transition, not the long-term source of truth. |
| Graph vs filesystem | Microsoft Graph is the source of inbound email truth. The filesystem is operational staging, and every saved path must be recorded in `ING.ProcessFile`. |
| BKD slice first | The locked DB contract should be proven with one vertical BKD happy path before broad CWH/PLE buildout. |
| Pack rules in CFG | `ENS_PACK` and `DEC_PACK` behavior belongs in `CFG.IngestionPackRule`, not tenant-specific Python branches. |
| Runtime gates in CFG | TSS submit/dry-run switches belong in `CFG.TenantSetting`; secrets remain in environment or secure config. |

## Immediate Build Priority

The next build target is not general ingestion for every tenant. It is a BKD vertical slice:

```text
01 fetch BKD DETAILS email
-> generate ENS_PACK from the email body
-> save source/process artifacts
-> write EXC/ING trace
-> load process rows for later STG validation
-> keep TSS submit disabled unless CFG explicitly enables it
```

CWH and PLE stay configured but inactive until sender rules and templates are confirmed.

BKD operational artefacts:

| Artefact | Location | Purpose |
| --- | --- | --- |
| Original ENS source text | `Integration_Layer/BKD/Inbound/ENS_Source/ENS_Source_{dd.MM.yyyy}.txt` | Keeps the received email body as traceable ENS source evidence, trimmed after the last `customsadmin@primelineexpress.co.uk` marker to remove forwarded-chain noise. |
| Original sales order attachment | `Integration_Layer/BKD/Inbound/Sales_Order_files/` | Keeps the customer Excel exactly as received. |
| Generated API pack | `Integration_Layer/BKD/Processed/BKD_API_PACK_{dd.MM.yyyy}.xlsx` | Combines `ENS PACK` from the first ENS body email and `DEC PACK` from the later Excel attachment email for review/mapping before TSS API processing. |

## Source Material

| Source | Why it matters |
| --- | --- |
| `V1_to_V2_FRD_DocVer1.5.pdf` | Functional requirements: import, ENS-linked processing, grouping, validation, submission, monitoring, reporting and audit. |
| `Flow_Review_of_Discovery_Findings.pdf` | Discovery findings: customer files are critical, TDN drives grouping, 99-line split, validation, monitoring and exception-led operation. |
| Existing Fusion V2 BKD production code | Proven implementation for Graph ingestion, ENS submit, cargo submit, watcher, notifications and SDI/SupDec automation. |
| QAS Graph layer | Current QAS starting point for mailbox download, customer routing and file capture. |

## Five-Step FLOW V3 Shape

| Step | Script | What it does | Output |
| --- | --- | --- | --- |
| 01 | `Integration_Layer/FLOW_V3/01_graph_email_ing.py` | Fetches Graph emails, classifies by customer/sender, stores source file/body trace and generates the BKD API pack when the ENS body email and DEC attachment email are paired. | ING trace / saved originals / generated process pack. |
| 02 | `Integration_Layer/FLOW_V3/02_ens_details_auto_submit.py` | Converts `DETAILS FOR...` email data into ENS header and submits to TSS. | Official `ENS000...`. |
| 03 | `Integration_Layer/FLOW_V3/03_sales_orders_cargo_submit.py` | Converts Sales Orders Excel into consignments/goods and submits cargo to TSS. | Official `DEC000...` and TSS goods IDs. |
| 04 | `Integration_Layer/FLOW_V3/04_status_watcher_notify.py` | Reads TSS statuses, syncs DEC/SFD/MRN/goods and sends movement notification when ready. | Updated mirrors and `movement_notified_at`. |
| 05 | `Integration_Layer/FLOW_V3/05_sdi_autosubmit.py` | Discovers SUP/SDI from TSS, enriches, updates goods/header and submits when enabled. | SUP submitted or official TSS review reason. |

## Validation Ownership

FLOW V3 should not duplicate validation logic. Each step delegates validation to
the module that owns the business object.

| Step | Validation location | What is validated |
| --- | --- | --- |
| 01 | `Integration_Layer/Graph/graph_mail_customer_downloader.py` | Mailbox access, sender/domain rules, allowed file types, duplicate file handling. |
| 02 | `app.blueprints.ingest.routes._auto_validate_and_submit_stg_ens_header` | DETAILS body parse, ENS payload, mandatory fields, TSS choice values, official TSS response. |
| 03 | `app.blueprints.declarations.routes._submit_prd_cargo_for_header` | TDN grouping, consignment payload, goods payload, goods completion and TSS submit response. |
| 04 | `app.ingestion.ens_status_watcher.sync_ens_status_once` and `app.ingestion.automation_notify` | TSS status readback, mirror updates, notification gate and SMTP result. |
| 05 | `app.ingestion.sdi_autosubmit.run_sdi_autosubmit` | SUP discovery, duplicate guard, source/masterdata enrichment, goods/header update, submit and readback. |

Important rule: local checks should protect execution basics only, such as no
file, no tenant, missing TSS reference, invalid JSON, or missing operation
context. Customs blockers should come from official TSS responses wherever
possible.

## Requirements Coverage

| Requirement area | FLOW V3 handling |
| --- | --- |
| Import and traceability | Step 01 keeps the original inbound email/file and links it to ING/Integration Layer trace. |
| ENS-linked processing | Step 02 creates/submits ENS where enabled; Step 03 submits consignments/goods under the ENS. |
| Consignment generation | Step 03 stages consignments and goods from Sales Orders rows. |
| TDN grouping | Transport Document Number is the default grouping key unless a customer-specific rule overrides it. |
| 99-line segmentation | Split by distinct goods item rows, not quantity. Keep source trace for every split. |
| Validation | Parser/readiness locally; official customs validation through TSS response. |
| Controlled goods | Values come from source/masterdata and TSS DataModel rules, not assumptions. |
| Submission to TSS | Steps 02, 03 and 05 submit to TSS through existing app logic. |
| Monitoring/status | Step 04 keeps local mirrors aligned with TSS statuses. |
| Reporting/audit | ING/STG/TSS/EXC traces should support dashboards, technical logs and customer reports. |
| Automation | Clean records progress automatically; humans intervene only on exceptions. |

## Notifications And Exceptions

Notifications are core FLOW V3 scope, not a nice-to-have. Discovery and the FRD
both point toward exception-led operation.

| Event | Trigger | Behaviour |
| --- | --- | --- |
| Pipeline error | Missing DETAILS email, parse failure, staging exception, or no consignments. | Notify operators with the exact failure reason. |
| Staging failure | Sales Orders staging finds blockers or failed consignments. | Notify and stamp `staging_failures_notified_at` only if SMTP succeeds. |
| Cargo submitted | Consignments/goods have been submitted to TSS. | Log/send summary where configured. |
| TSS status attention | TSS returns a status requiring action, such as Trader Input Required. | Surface official TSS message; repair only known safe cases. |
| Movement authorised | ENS and all active consignments pass the final gate. | Send final movement pack and stamp `movement_notified_at` only after send success. |
| SDI autosubmit issue | SUP/SDI update/submit fails or TSS returns review status. | Capture official TSS response and notify support. |

Movement notification gate:

1. ENS header has a TSS reference.
2. `movement_notified_at` is blank.
3. Active consignments are authorised/arrived according to TSS-led status.
4. Goods have no error messages.
5. SMTP send succeeds.

If SMTP fails, the stamp must not happen so the watcher can retry later.

## Data Ownership Direction

```text
CFG = configuration and routing
EXC = execution runs and technical outcomes
ING = immutable inbound source trace
STG = working business state ready for validation/submission
TSS = official API references, statuses, raw responses and mirrors
Tenant/masterdata = defaults, product/customer rules and enrichment data
```

Do not collapse these layers just to make the first prototype shorter. The value
of V3 is being able to trust the automation and investigate it when something
fails.

## Non-Negotiable Design Rules

1. TSS is the source of truth for ENS, DEC, SFD, SUP, goods IDs, MRN, statuses,
   deadlines and validation outcomes.
2. Original customer files must remain traceable exactly as received.
3. Masterdata can enrich missing values only when the source is confirmed.
4. Do not invent item values, document references or customs facts.
5. Do not create duplicate declarations when an active/closed natural key already
   exists.
6. Duplicate SUP/SDI cancellation requires TDN plus goods fingerprint, not TDN
   alone.
7. Live submit paths must be gated by `CFG.TenantSetting` and environment/config.
8. Technical logs must include enough TSS response detail to explain failures.

## Running The Flow

All five steps are intended to run from this QAS repository. Step 01 uses the QAS Graph layer today. Steps 02-05 must be rebuilt as small local QAS services under `Integration_Layer/App/app/services/`; V2 is a reference only, not a runtime dependency.

Commands:

```powershell
python Integration_Layer\FLOW_V3\01_graph_email_ing.py --run-mode daily
python Integration_Layer\FLOW_V3\02_ens_details_auto_submit.py --tenant-code BKD --limit 10
python Integration_Layer\FLOW_V3\03_sales_orders_cargo_submit.py --tenant-code BKD --limit 10
python Integration_Layer\FLOW_V3\04_status_watcher_notify.py --tenant-code BKD --limit 10
python Integration_Layer\FLOW_V3\05_sdi_autosubmit.py --tenant-code BKD
```

## Open Items

| Item | Why it matters |
| --- | --- |
| STG object shape | Avoid a permanently flat `STG.SalesOrder` model; validate the ENS header -> consignment -> goods item chain before buildout. |
| TSS submission typing | `TSS.Submission` must identify submit type, such as `ENS`, `DEC`, `SDI_GOODS`, or `SDI_HEADER`, before multi-flow automation. |
| Migration proof | Run migrations in QAS and insert a dummy chain through `CFG.Tenant -> CFG.Graph -> ING.Graph -> ING.ProcessFile -> ING.LoadRow`. |
| Customer-specific grouping exceptions | Same TDN can be legitimate in more than one context, so duplicate logic must check goods/context too. |
| Missing item values | TSS can reject SDIs where item values are blank; values must come from customer files or confirmed masterdata. |
| Missing document references | Historical closed declarations can help, but only where the mapping is reliable. |
| QAS schema expansion | Current QAS SQL is Graph-first and minimal; extend only around confirmed V3 object contracts. |
| Production cutover | OAuth/new TSS API credentials and endpoint changes need separate environment/config handling. |
