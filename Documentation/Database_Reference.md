# Fusion Flow V3 — Database Reference (dev branch)

Multi-tenant, modular SQL Server database for customs declaration processing.
Registered clients: **BKD** (Birkdale), **CWD** (CountryWide), **PLE** (Primeline Express).
Currently in **Release 1** — 37 tables across 13 schemas.

---

## Schema Map

```
┌─────────────────────────────────────────────────────────────────┐
│  CHG   Change Management  (deployment log + change audit)        │
│  CFG   Configuration      (clients, creds, routes, jobs, cache)  │
│  ING   Ingestion          (raw landing — verbatim, no transform) │
│  EXC   Execution Spine    (master audit + field-change audit)    │
│  LOG   Logging            (process, error, API trace)            │
│  PRS   Processing         (canonical TSS objects: ENS hierarchy) │
├─────────────────────────────────────────────────────────────────┤
│  STG  API  CTL  ARC  SRV  — provisioned, empty (R2/Modules 3-5) │
│  BKD  CWD  PLE            — per-client schemas, empty in R1      │
└─────────────────────────────────────────────────────────────────┘
```

Every table (except CHG and CFG) carries `ExecutionID`, `TransactionID`, and `ClientCode`
so every row is traceable back to the batch run that produced it.

---

## CHG — Change Management

Tracks database deployments. Written by `deploy.py` — not touched by application code.

### `CHG.Deployment`
One row per `deploy.py` run. Records who deployed, when, how many scripts, and the final outcome.

| Column | Type | Notes |
|---|---|---|
| DeploymentID | bigint PK | Auto-increment |
| RunStamp | varchar(20) | `yyyyMMdd-HHmmss` |
| Description | nvarchar(1000) | Operator-supplied text |
| ServerName / DatabaseName / AppliedBy | nvarchar | Context metadata |
| ScriptCount / SucceededCount / FailedCount | int | |
| Status | varchar(20) | `RUNNING` → `SUCCESS / FAILED / PARTIAL` |
| StartedAt / EndedAt | datetime2(3) | UTC |

### `CHG.Change_Log`
One row per DDL script within a deployment. Stores the SHA-256 hash of every script and the archive path after successful execution.

| Column | Type | Notes |
|---|---|---|
| ChangeID | bigint PK | |
| DeploymentID | bigint FK → CHG.Deployment | |
| ScriptName | nvarchar(500) | e.g. `002_cfg_tables.sql` |
| ScriptHash | char(64) | SHA-256 for integrity check |
| BatchCount | int | Number of `GO`-separated batches |
| Status | varchar(20) | `SUCCESS / FAILED / SKIPPED` |
| ErrorMessage | nvarchar(max) | If `FAILED` |
| ArchivePath | nvarchar(1000) | Where the script was moved after apply |
| AppliedAt | datetime2(3) | UTC |

---

## CFG — Configuration

All runtime configuration lives here. Adding a client, credential, API route, or scheduled job is a data operation, not a code change.

### `CFG.Application_Parameters`
Global key-value store for runtime settings. Every non-connection config value lives here: Graph credentials, folder roots, processing flags, rate limits.

| Column | Type | Notes |
|---|---|---|
| ParameterID | int PK | |
| ParameterKey | varchar(100) UNIQUE | e.g. `GRAPH_TENANT_ID`, `PROCESSING_CLIENT`, `INGESTION_DRY_RUN` |
| ParameterValue | nvarchar(1000) | Null = unset |
| ValueType | varchar(20) | `STRING / INT / BOOL / DATE / GUID / DECIMAL / SECRET / SECRET_REF` |
| Description | nvarchar(500) | |
| IsActive | bit | |
| UpdatedAt | datetime2(3) | UTC |

Seeded keys include: `GRAPH_APP_NAME`, `GRAPH_TENANT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_MAILBOX`, `DEFAULT_ENV`, `API_RATE_LIMIT_SECONDS`, `GMR_READ_WAIT_SECONDS`, `PROCESSING_CLIENT`, `PROCESSING_DRY_RUN`, `INGESTION_CLIENT`, `INGESTION_DRY_RUN`.

### `CFG.Clients`
Principal registry. One row per registered client. Controls schema name, staging prefix, default API route, and whether the client acts as an agent on behalf of another.

| Column | Type | Notes |
|---|---|---|
| ClientID | int PK | |
| ClientCode | char(3) UNIQUE | `BKD`, `CWD`, `PLE` |
| ClientName | nvarchar(100) | Full name |
| SchemaName | sysname | Mirrors `ClientCode` in R1 |
| StgTablePrefix | varchar(10) | e.g. `BKD_` |
| DefaultRoute | char(1) | `A/B/C/D` — BKD uses Route A |
| IsAgent | bit | `1` if acting on behalf of another customer |
| ActAsSysId | varchar(64) | `customer_account_sys_id` when `IsAgent=1` |
| IsActive | bit | Only BKD is active in R1 |
| Notes | nvarchar(1000) | |

Seeded: BKD (active), CWD (inactive), PLE (inactive).

### `CFG.Credentials`
TSS API authentication per client + environment. Stores only a **reference** to the secret (e.g. Key Vault path), never the plaintext password.

| Column | Type | Notes |
|---|---|---|
| CredentialID | int PK | |
| ClientCode | char(3) FK → CFG.Clients | |
| EnvCode | varchar(10) | `TEST` or `PROD` |
| AuthType | varchar(20) | `BASIC` (all R1 clients) |
| ApiUsername | varchar(64) | Format: `API.TSSnnnnnnn` |
| SecretRef | nvarchar(256) | Key Vault reference — NOT the secret |
| IsActive | bit | |
| Notes | nvarchar(500) | |

UQ on `(ClientCode, EnvCode)`. Seeded: BKD-TEST, BKD-PROD, CWD-TEST, PLE-TEST.

### `CFG.Folder_Paths`
Per-client operational folder paths (Windows UNC). Types: `INBOUND`, `PROCESS`, `FAIL`, `ARCHIVE`, `ENS_SOURCE`.

| Column | Type | Notes |
|---|---|---|
| PathID | int PK | |
| ClientCode | char(3) FK → CFG.Clients | |
| PathType | varchar(30) | `INBOUND / PROCESS / FAIL / ARCHIVE / ENS_SOURCE` |
| PathValue | nvarchar(1000) | Full UNC path |
| IsActive | bit | |

UQ on `(ClientCode, PathType)`. 12 rows seeded (3 clients × 4 types; BKD also has `ENS_SOURCE`).

### `CFG.Email_Rules`
Mailbox acquisition rules per client. Defines which sender domain or address triggers ingestion, and what file types are allowed.

| Column | Type | Notes |
|---|---|---|
| RuleID | int PK | |
| ClientCode | char(3) FK | |
| Mailbox | nvarchar(320) | e.g. `nexus@synoviaflow.cloud` |
| SenderRuleType | varchar(20) | `DOMAIN` or `ADDRESS` |
| SenderRule | nvarchar(500) | e.g. `birkdalesales.com` |
| AllowedFileTypes | nvarchar(200) | Semicolon-separated: `.xlsx,.csv` |
| IsActive | bit | Only BKD active in R1 |

### `CFG.Ingestion_Source`
Registry of acquisition channels per client. Controls which channel (`EMAIL`, `SFTP`, `AS2`, `API`, `FILE_DROP`) is active and holds channel-specific config as JSON.

| Column | Type | Notes |
|---|---|---|
| SourceID | int PK | |
| ClientCode | char(3) FK | |
| Channel | varchar(20) | `EMAIL / SFTP / AS2 / API / FILE_DROP` |
| IsActive | bit | BKD EMAIL=active; rest inactive |
| ProcessedSubfolder | varchar(100) | Move destination after processing |
| ConfigJson | nvarchar(max) | Host, port, endpoint, etc. |

UQ on `(ClientCode, Channel)`.

### `CFG.API_Version`
Controls which TSS API version (`NEW`/`OLD`) to call per client, and the base URLs for TEST and PROD environments.

| Column | Type | Notes |
|---|---|---|
| VersionID | int PK | |
| ClientCode | char(3) FK | |
| ResourceName | varchar(50) | `*` = all resources, or specific (e.g. `Declaration Header`) |
| ApiVersion | varchar(10) | `NEW` or `OLD` |
| BaseUrlTest | nvarchar(200) | TSS test environment base URL |
| BaseUrlProd | nvarchar(200) | TSS production base URL |
| IsActive | bit | |

UQ on `(ClientCode, ResourceName)`. All three clients seeded with NEW API.

### `CFG.API_Process_Map`
Ordered sequence of TSS API operations per client + route. Defines exactly what HTTP call to make at each step, including inter-step wait times (e.g. 90 s after GMR submit).

| Column | Type | Notes |
|---|---|---|
| MapID | int PK | |
| ClientCode | char(3) FK | |
| RouteCode | char(1) | `A/B/C/D` |
| StepNo | int | Execution order |
| ResourceName | varchar(50) | TSS resource |
| Endpoint | varchar(100) | e.g. `/headers`, `/consignments`, `/goods` |
| HttpMethod | varchar(10) | `GET / POST / PUT / DELETE` |
| OpType | varchar(20) | `create / read / update / submit / lookup / cancel` |
| WaitSeconds | int | Pause after this step |
| Notes | nvarchar(500) | Spec rule references |

UQ on `(ClientCode, RouteCode, StepNo)`.

**BKD Route A (13 steps):**
Permission Grant (0) → Declaration Header (1) → Consignments (2) → Goods Items (3) → Consignment submit (4) → SFD lookup (5) → GVMS GMR create (6) → GMR submit (7, +90 s wait) → GMR read (8) → Supplementary Declaration lookup (9) → SDI update (10) → SDI read (11) → SDI submit (12).

### `CFG.Choice_Field_Registry`
Bootstrap list of ~35 TSS reference datasets that need to be cached locally from `GET /choice_values/<field>`.

| Column | Type | Notes |
|---|---|---|
| FieldID | int PK | |
| ChoiceField | varchar(80) UNIQUE | e.g. `movement_type`, `country`, `procedure_code` |
| Description | nvarchar(300) | |
| ApiPath | varchar(150) | e.g. `/choice_values/movement_type` |
| IsActive | bit | |

### `CFG.Choice_Value_Cache`
Cached results of TSS `GET /choice_values/<field>` calls. Used during the ENRICH phase to validate and resolve codes.

| Column | Type | Notes |
|---|---|---|
| ChoiceID | bigint PK | |
| ChoiceField | varchar(80) | e.g. `movement_type` |
| ChoiceValue | nvarchar(100) | Code sent to TSS (e.g. `0010`) |
| ChoiceName | nvarchar(400) | Display name |
| ExtraJson | nvarchar(max) | `ens_allowed`, `ffd_allowed`, effective dates, etc. |
| EffectiveFrom / EffectiveTo | date | Validity window |
| RetrievedAt | datetime2(3) | Cache refresh timestamp (UTC) |

UQ on `(ChoiceField, ChoiceValue)`.

### `CFG.Status_Vocabulary`
Single shared vocabulary for all lifecycle statuses across every module and table. Prevents status string divergence.

| Column | Type | Notes |
|---|---|---|
| VocabID | int PK | |
| ProcessName | varchar(30) | Phase that produces this status (e.g. `VALIDATING`) |
| ResultStatus | varchar(30) UNIQUE | The status string (e.g. `VALIDATED`) |
| Meaning | nvarchar(300) | Human explanation |
| SortOrder | int | Display order |
| IsTerminal | bit | `1` = end state (ARCHIVED, CANCELLED) |
| IsException | bit | `1` = error/exception (REJECTED, ERROR, MISMATCH) |

Seeded statuses: `INGESTED → NORMALISED → ENRICHED → CONSTRUCTED → VALIDATED → STAGED → READY → LINKED → SUBMITTING → SUBMITTED → ACKNOWLEDGED → IN_PROGRESS → RECONCILED → ARCHIVED` plus exception states `REJECTED / MISMATCH / ERROR / CANCELLED / ON_HOLD`.

### `CFG.Job`
Canonical registry of every scheduled job in the platform. The ingestion runner reads this table to drive the cycle — no CLI arguments needed. Adding or disabling a job is a `UPDATE CFG.Job SET IsActive = 0`.

| Column | Type | Notes |
|---|---|---|
| JobID | int PK | |
| JobCode | varchar(50) UNIQUE | e.g. `ING_BKD_CYCLE`, `PRS_PROCESS_BKD` |
| JobName | nvarchar(120) | Friendly name |
| ModuleName | varchar(40) | `INGESTION / DATA_PROCESSING / SUBMISSION / MONITORING / REPORTING` |
| ClientCode | char(3) | NULL = cross-client |
| Channel | varchar(20) | `EMAIL / SFTP / AS2 / API / FILE_DROP` (or NULL) |
| JobType | varchar(20) | `ORCHESTRATOR / STEP / ACQUIRE / TASK` |
| StepNo | int | Order within orchestrator |
| ParentJobCode | varchar(50) | Soft FK to parent orchestrator |
| Purpose | nvarchar(600) | What the job does |
| EntryPoint | nvarchar(200) | `module:function` (e.g. `run_ingestion:main`) |
| InputSource / OutputTarget | nvarchar(300) | Human description |
| Schedule | nvarchar(120) | e.g. `Every 30 min, 06:00–20:00 UK business days` |
| IsActive | bit | Operator-owned; MERGE never overwrites if row exists |

**Seeded jobs (11):**

| JobCode | Type | Description |
|---|---|---|
| `ING_BKD_CYCLE` | ORCHESTRATOR | Birkdale 3-step ingestion cycle |
| `ING_BKD_ACQUIRE_EMAIL` | STEP 1 | Acquire attachments via Microsoft Graph |
| `ING_BKD_PARSE_ENS` | STEP 2 | Parse "Details for \<date\>" emails → ENS CSV |
| `ING_BKD_LOAD_RAW` | STEP 3 | Load ENS CSV → `ING.BKD_Raw_ENS`; Sales Orders → `ING.BKD_Raw_Sales_Orders` |
| `ING_ACQUIRE_FILE_DROP` | ACQUIRE | FILE_DROP stub (inactive) |
| `ING_ACQUIRE_SFTP` | ACQUIRE | SFTP stub (inactive) |
| `ING_ACQUIRE_AS2` | ACQUIRE | AS2 stub (inactive) |
| `ING_ACQUIRE_API` | ACQUIRE | REST API stub (inactive) |
| `PRS_PROCESS_BKD` | TASK | Data processing for BKD: normalise → enrich → construct → validate |

---

## ING — Ingestion (Raw Landing)

Receives files and rows verbatim. No transformation happens here. Everything is preserved exactly as received so Module 2 (Data Processing) has a clean, auditable source.

### `ING.Inbound_File`
One row per inbound file (any channel). De-duplicates on `(ClientCode, FileHash)` — re-sending the same file is a no-op.

| Column | Type | Notes |
|---|---|---|
| FileID | bigint PK | |
| ExecutionID | bigint FK → EXC.Execution | |
| TransactionID | uniqueidentifier | Thread ID |
| ClientCode | char(3) FK | |
| SourceChannel | varchar(20) | `FILE_DROP / EMAIL / SFTP / REST / MANUAL` |
| SourceName | nvarchar(500) | Original filename |
| SourcePath | nvarchar(1000) | Full source path |
| Mailbox / Sender | nvarchar | If EMAIL channel |
| ReceivedUtc | datetime2(3) | |
| FileHash | char(64) | SHA-256 — natural dedup key |
| SizeBytes | bigint | |
| ContentType | nvarchar(255) | MIME type |
| RowsLanded | int | Records loaded downstream |
| Status | varchar(30) | `INGESTED / QUARANTINED / FAILED / DUPLICATE` |
| FailReason | nvarchar(2000) | If failed |

UQ on `(ClientCode, FileHash)`.

### `ING.Raw_Record`
One row per source row from any inbound file. Stores the entire row as JSON — no parsing, no mapping.

| Column | Type | Notes |
|---|---|---|
| RawID | bigint PK | |
| FileID | bigint FK → ING.Inbound_File | |
| ClientCode | char(3) | |
| RowOrdinal | int | 1-based position in source file |
| RowHash | char(64) | SHA-256 of the row's natural key |
| PayloadJson | nvarchar(max) | Verbatim row as JSON |
| Status | varchar(30) | `INGESTED / PROCESSED / FAILED / SKIPPED` |

### `ING.Source_Email`
Email-channel provenance. Captures full Graph metadata (messageID, internetMessageID, sender, subject, body) for audit and forwarding detection.

| Column | Type | Notes |
|---|---|---|
| EmailID | bigint PK | |
| GraphMessageID | nvarchar(450) | Microsoft Graph message ID |
| InternetMessageID | nvarchar(1000) | RFC 5322 globally unique ID |
| Sender / SenderDomain | nvarchar | Used for CFG.Email_Rules filtering |
| Subject | nvarchar(998) | |
| ReceivedUtc | datetime2(3) | |
| HasAttachments | bit | |
| BodyText | nvarchar(max) | |
| Status | varchar(30) | `INGESTED / PROCESSED / FAILED / SKIPPED` |

### `ING.BKD_Raw_ENS`
Typed raw landing table for ENS CSV rows extracted from "Details for \<date\>" emails.
De-duplicates on `DedupKey = DetailsDate|ICR` so daily re-forwards don't create duplicates.

| Column | Type | Notes |
|---|---|---|
| LoadID | bigint PK | |
| DedupKey | varchar(100) UNIQUE | `DetailsDate\|ICR` |
| DetailsDate | varchar(20) | From email subject |
| SourceSender / SourceSubject | nvarchar | Email provenance |
| OriginalFrom / OriginalSent | nvarchar | Populated if forwarded |
| movement_type … transport_charges | nvarchar | All ENS header fields verbatim |
| ParseStatus | varchar(20) | Parse error tracking |
| LoadedAt | datetime2(3) | UTC |

All TSS ENS field columns are nullable — raw values, resolved in Module 2.

### `ING.BKD_Raw_Sales_Orders`
Verbatim row JSON from Sales Order workbooks. Keyed on `(SourceFile, RowNumber)` — re-loading the same file deletes and re-inserts.

| Column | Type | Notes |
|---|---|---|
| LoadID | bigint PK | |
| SourceFile | nvarchar(500) | Timestamped filename (`20260624_124739_Sales Orders...xlsx`) |
| FileDate | date | Extracted from filename prefix |
| SheetName | nvarchar(128) | Worksheet name |
| RowNumber | int | 1-based row ordinal |
| RowHash | char(64) | SHA-256 of the row payload (line-level dedup in M2) |
| PayloadJson | nvarchar(max) | Full row as JSON |
| Status | varchar(30) | `INGESTED / PROCESSED / FAILED / SKIPPED` |

UQ on `(SourceFile, RowNumber)`.

---

## EXC — Execution Spine & Audit

The backbone of the platform. Every batch operation opens a row in `EXC.Execution`; its `ExecutionID` is written to every other table touched during that run.

### `EXC.Execution`
Master execution/transaction record. One row per module run. The `TransactionID` (`NEWID()`) threads through every downstream table.

| Column | Type | Notes |
|---|---|---|
| ExecutionID | bigint PK | Primary join key for all other tables |
| TransactionID | uniqueidentifier | `NEWID()` — global transaction thread |
| EnvCode | varchar(10) | `TEST` or `PROD` |
| ClientCode | char(3) | NULL = system-level operation |
| ModuleName | varchar(40) | `INGESTION / DATA_PROCESSING / SUBMISSION / …` |
| ProcessName | varchar(30) | Phase within the module |
| RunMode | varchar(30) | `daily / historic / manual / dry-run` |
| StartedAt / EndedAt | datetime2(3) | UTC |
| Status | varchar(30) | Shared vocabulary status |
| ItemsFound / ItemsProcessed / ItemsFailed | int | Counters |
| ErrorMessage | nvarchar(2000) | Summary (full detail in LOG tables) |

### `EXC.Transaction`
Per-entity status transitions within an execution. One row per item (file, ENS header, consignment, goods item) processed, recording what entity changed and to what status.

| Column | Type | Notes |
|---|---|---|
| TransactionRowID | bigint PK | |
| ExecutionID | bigint FK | |
| EntityType | varchar(40) | `INBOUND_FILE / ENS_HEADER / CONSIGNMENT / GOODS_ITEM / …` |
| EntityRef | nvarchar(100) | e.g. filename, ICR, consignment_number |
| ProcessName | varchar(30) | Phase at transition time |
| Status | varchar(30) | Status after transition |

### `EXC.Error`
Execution-level errors with severity classification. Business error codes (e.g. `RULE_4_ARRIVAL_TOO_FAR_FUTURE`) and JSON context for debugging.

| Column | Type | Notes |
|---|---|---|
| ErrorID | bigint PK | |
| ExecutionID | bigint | Can be NULL for system errors |
| Severity | varchar(20) | `WARN / ERROR / CRITICAL` |
| ErrorCode | varchar(50) | Business rule code |
| Message | nvarchar(2000) | Human-readable description |
| Context | nvarchar(max) | JSON: failed row, constraint details, etc. |

No cascade deletes — audit rows must not vanish silently.

### `EXC.Data_Processing_Enhancement`
Field-level change audit written by Module 2 (Data Processing). One row per field transformation from raw (ING) to canonical (PRS), with the rule that was applied. Full transparency for data quality decisions.

| Column | Type | Notes |
|---|---|---|
| EnhancementID | bigint PK | |
| ExecutionID | bigint | |
| SchemaName / TableName / ColumnName | sysname | What was changed |
| EntityRef | nvarchar(100) | e.g. MovementKey |
| OldValue | nvarchar(max) | Value from ING |
| NewValue | nvarchar(max) | Value written to PRS |
| RuleApplied | nvarchar(200) | e.g. `arrival_date_time resolution`, `choice value lookup` |

---

## LOG — Logging

Three tables covering different verbosity levels. All linked to `ExecutionID`.

### `LOG.Process_Log`
Step-by-step lifecycle events. Standard application log — module start/end, item counts, timing, config used.

| Column | Type | Notes |
|---|---|---|
| LogID | bigint PK | |
| ExecutionID | bigint | |
| ModuleName / StepName | varchar | e.g. `INGESTION` / `acquire_bkd_email` |
| LogLevel | varchar(20) | `DEBUG / INFO / WARN / ERROR` |
| Message | nvarchar(2000) | |
| DetailJson | nvarchar(max) | Counts, timings, extra context |

### `LOG.Error_Log`
Deeper error log. Captures full stack traces and exception types for debugging — complements `EXC.Error` which is business-level.

| Column | Type | Notes |
|---|---|---|
| ErrorLogID | bigint PK | |
| ExecutionID | bigint | |
| ModuleName / StepName | varchar | |
| ErrorType | varchar(100) | Python exception class name |
| Message | nvarchar(2000) | |
| StackTrace | nvarchar(max) | Full traceback |

### `LOG.API_Trace`
Full TSS API request/response trace. Written by Module 3 (Submission). Every call to TSS is captured verbatim — request payload, response payload, HTTP status, and duration.

| Column | Type | Notes |
|---|---|---|
| TraceID | bigint PK | |
| ExecutionID | bigint | |
| ResourceName | varchar(50) | e.g. `Declaration Header`, `Goods Item` |
| Endpoint | varchar(100) | e.g. `/headers` |
| HttpMethod | varchar(10) | |
| RequestJson | nvarchar(max) | Full outbound payload |
| ResponseJson | nvarchar(max) | Full TSS response |
| StatusCode | int | HTTP status |
| DurationMs | int | Round-trip time |

---

## PRS — Processing (Canonical Objects)

Normalised, enriched, validated business objects ready for TSS submission. Mirrors the TSS hierarchy exactly:
`ENS_Header (1) → Consignment (many) → Goods_Item (many, ≤99 per consignment)`.
Ten child tables handle the nested arrays that TSS requires.

### `PRS.ENS_Header`
One row per logical movement (one ferry crossing). Top-level canonical object. Null `declaration_number` until after submission to TSS.

| Column | Type | Notes |
|---|---|---|
| EnsHeaderRowID | bigint PK | |
| ExecutionID | bigint FK | |
| ClientCode | char(3) | |
| Status | varchar(30) | Full lifecycle from `INGESTED` to `SUBMITTED` |
| RejectReason | nvarchar(2000) | If `REJECTED` |
| MovementKey | nvarchar(100) | Natural key — UQ with `ClientCode` |
| SourceEnsLoadID | bigint | Soft ref → `ING.BKD_Raw_ENS.LoadID` (no FK) |
| movement_type … transport_charges | nvarchar | All movement-level TSS fields |
| arrival_date_time | nvarchar(20) | Strict `DD/MM/YYYY HH:MM:SS` |
| arrival_date_time_utc | datetime2(0) | Resolved UTC datetime |
| declaration_number | nvarchar(40) | `ENS000…` assigned by TSS on submission |
| carrier_* / haulier_* fields | nvarchar | Party details |

UQ on `(ClientCode, MovementKey)`. Soft references to ING so PRS data survives raw-table pruning.

### `PRS.Consignment`
Child of `ENS_Header` (1→many). One row per consignment within a movement. Carries all consignment-level TSS fields and the six party blocks (consignor, consignee, importer, exporter, buyer, seller).

| Column | Type | Notes |
|---|---|---|
| ConsignmentRowID | bigint PK | |
| EnsHeaderRowID | bigint FK | |
| ConsignmentOrdinal | int | 1-based within the movement |
| MovementKey | nvarchar(100) | Propagated from header |
| consignment_number | nvarchar(40) | Assigned by TSS on submit |
| goods_description / trader_reference / transport_document_number | nvarchar | |
| ducr | nvarchar(35) | Declarant's Unique Consignment Reference |
| declaration_choice | nvarchar(2) | `FFD / SFD / FCCOM` |
| consignor_* / consignee_* / importer_* / exporter_* / buyer_* / seller_* | nvarchar | Six party blocks, each with EORI + address |
| align_ukims / use_importer_sde / generate_SD | varchar | UKIMS/SD flags |

UQ on `(EnsHeaderRowID, ConsignmentOrdinal)`.

### `PRS.Goods_Item`
Child of `Consignment` (1→many, ≥1 required, ≤99 per consignment — enforced at VALIDATE, not by constraint). One row per goods line. Carries all item-level TSS fields: packaging, weight, commodity code, procedure codes, valuation, tax.

| Column | Type | Notes |
|---|---|---|
| GoodsItemRowID | bigint PK | |
| ConsignmentRowID | bigint FK | |
| GoodsItemOrdinal | int | 1-based within the consignment |
| SourceSalesOrderLoadID | bigint | Soft ref → `ING.BKD_Raw_Sales_Orders.LoadID` |
| goods_id | nvarchar(32) | Assigned by TSS |
| type_of_packages / number_of_packages | nvarchar/int | Packaging |
| gross_mass_kg / net_mass_kg | decimal(15,2) | |
| commodity_code | nvarchar(10) | HS code |
| procedure_code / additional_procedure_code | nvarchar | Customs procedure |
| country_of_origin | char(2) | |
| item_invoice_amount / item_invoice_currency | nvarchar | Invoice value |
| valuation_method / valuation_indicator | nvarchar | |
| tax_type / tax_base_unit / payable_tax_amount | nvarchar | Tax fields |

UQ on `(ConsignmentRowID, GoodsItemOrdinal)`.

### Nested child tables

All 10 child tables follow the same pattern:
`PK (bigint)` + `ParentFK` + `Ordinal` + `ExecutionID` + `TransactionID` + `ClientCode` + `RowAction (create/update/delete)` + TSS fields + `CreatedAt / UpdatedAt`.
UQ on `(ParentRowID, Ordinal)`.

| Table | Parent | TSS fields |
|---|---|---|
| `PRS.Consignment_PreviousDocument` | Consignment | `previous_document_ref / _class / _type / _item_identifier` |
| `PRS.Consignment_HolderOfAuthorisation` | Consignment | `auth_role_id / _type / _code`, EORI + address block |
| `PRS.Goods_AdditionalProcedure` | Goods_Item | `additional_procedure_code` |
| `PRS.Goods_DocumentReference` | Goods_Item | `document_reference / _code / _status / _part / _reason`, validity dates, amount |
| `PRS.Goods_AdditionalInformation` | Goods_Item | `additional_info_code / _description` |
| `PRS.Goods_PreviousDocument` | Goods_Item | `previous_document_ref / _class / _type / _item_identifier` |
| `PRS.Goods_ItemAddDed` | Goods_Item | `item_add_ded_code / _value / _currency` (additions/deductions) |
| `PRS.Goods_NationalAdditionalCode` | Goods_Item | `national_additional_code` |
| `PRS.Goods_TaxBase` | Goods_Item | `tax_base_unit / _quantity`, `payable_tax_amount / _currency`, `tax_type` |
| `PRS.Goods_AdditionalParties` | Goods_Item | `auth_role_id / _code / _type`, EORI + address block (SD context, R1 Q3 — provisioned, unpopulated) |

---

## R2 / Future Schemas (provisioned, no tables yet)

| Schema | Module | Purpose |
|---|---|---|
| `STG` | Module 3 | Staging layer before TSS submission |
| `API` | Module 3 | Capture every raw TSS API request/response at rest |
| `CTL` | Module 4 | Control/monitoring views and status roll-ups |
| `ARC` | Module 5 | Archived, reconciled records (terminal state) |
| `SRV` | Module 5 | Reporting views and serve layer |
| `BKD` | Client | Per-client BKD objects (R2+) |
| `CWD` | Client | Per-client CountryWide objects (R2+) |
| `PLE` | Client | Per-client Primeline objects (R2+) |

---

## Table count

| Schema | Tables | Status |
|---|---|---|
| CHG | 2 | Complete |
| CFG | 10 | Complete |
| ING | 5 | Complete (3 generic + 2 BKD-specific) |
| EXC | 4 | Complete |
| LOG | 3 | Complete |
| PRS | 13 | Complete (3 core + 10 nested arrays) |
| STG / API / CTL / ARC / SRV | 0 | Provisioned (R2) |
| BKD / CWD / PLE | 0 | Provisioned (R2+) |
| **Total R1** | **37** | |

---

## Key design rules

1. **Execution threading** — every non-CHG/CFG table carries `ExecutionID + TransactionID + ClientCode`. Full traceability to the batch run that produced any row.
2. **No cascade deletes** — all foreign keys are `NO ACTION`. Audit rows must never vanish silently.
3. **Soft references in PRS** — `SourceEnsLoadID` and `SourceSalesOrderLoadID` are plain `bigint` columns (no FK) so PRS canonical objects survive raw ING pruning.
4. **Shared status vocabulary** — `CFG.Status_Vocabulary` is the single source of truth for every status string used across the platform.
5. **Idempotent ingestion** — `ING.Inbound_File` dedupes on `FileHash`; `ING.BKD_Raw_ENS` dedupes on `DedupKey`; `ING.BKD_Raw_Sales_Orders` dedupes on `(SourceFile, RowNumber)`.
6. **Configuration as data** — adding a client, credential, folder path, API route, or scheduled job is a data operation, not a code change.
7. **≤99 goods per consignment** — enforced by the Module 2 runner at VALIDATE, not by a `CHECK` constraint, to allow incremental mid-load inserts.
8. **Secrets never in DB** — `CFG.Credentials.SecretRef` holds a Key Vault reference path, not the plaintext secret.
