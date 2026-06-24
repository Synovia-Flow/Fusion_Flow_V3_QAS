# Phase 2 - Validation And Consolidation Requirements

## Purpose

This document defines the Phase 2 requirements for validation and consolidation in the Fusion Flow V3 QAS project.

Phase 1 confirmed the API-first approach and the initial consolidation rule: the 99 limit applies to distinct goods item rows per consignment, not quantities.

Phase 2 turns that discovery into detailed requirements that can be reviewed, designed, built, and tested.

## Functional Mode

API / Data Processing

## Source References

| Reference | Purpose |
| --- | --- |
| Flow Review of Discovery Findings | Phase 1 findings used to confirm the operating model, CSV dependency, TDN grouping, 99-line split, validation, monitoring, reporting, and automation opportunities. |
| Flow V1 to Flow V2 FRD v1.5 | Functional requirements used to align FR/RF coverage. |
| Fusion_Flow_V2_BKD prod docs README | Reference context for production automation, data ownership, TSS source of truth, and execution order. |
| Fusion_Flow_V2_BKD docs/additional/INGEST_EXCEL_WORKFLOW.md | Reference context for Excel/template ingestion, review gates, ENS-only path, and local staging expectations. |
| Fusion_Flow_V2_BKD docs/additional/PRD_DATA_MODEL_CUTOVER.md | Reference context for ING/STG/TSS/BKD ownership, natural keys, and SDI goods separation. |
| Fusion_Flow_V2_BKD docs/ENS_USER_GUIDE_MAY_2026_NOTES.md | Reference context for TSS-led status, declaration chain, RawJson capture, TDN limit, and SDI/SUP operational notes. |
| Fusion_Flow_V2_BKD docs/api/API_CODE_CROSS_REFERENCE.md | Reference context for endpoint coverage and v2.9.5 endpoint parity. |
| TSS API v2.9.5 DataModel | Source of required, optional and conditional API fields. |
| TSS API v2.9.5 Postman collection | Source of practical payload examples. |
| `Integration_Layer/Graph/config/customers/*.yml` | Current customer routing and destination rules. |
| `Configuration_Layer/SQL/001_create_schemas_and_tables.sql` -> `002_add_constraints_and_indexes.sql` -> `003_seed_qas_config.sql` | Current QAS database starting point and tenant seed config. |

## Validation Requirements

| ID | Requirement | Rule | Owner / Confirmation | Acceptance Criteria |
| --- | --- | --- | --- | --- |
| VAL-001 | API-first validation | The system must not block locally any request that TSS can officially validate. Local checks should only protect required execution basics such as missing file path, missing operation, or invalid JSON. | Alvaro / Rey | Invalid TSS payloads return official TSS errors in terminal or logs rather than being silently blocked by local code. |
| VAL-002 | Required field source | Required and conditional API fields must be taken from the TSS v2.9.5 DataModel, not hard-coded from memory. | Alvaro / Rey | Design/build references the DataModel sheets for ENS Header, Consignment, and Goods Item. |
| VAL-003 | Payload template source | Example payloads must come from the v2.9.5 Postman collection where possible. | Alvaro / Rey | Operators/developers can view or reuse known-good JSON templates. |
| VAL-004 | TSS response visibility | Every API request must expose the TSS response body, including error details where returned. | Alvaro / Rey | Success and error responses show enough detail to confirm created references or diagnose validation failure. |
| VAL-005 | Environment separation | TEST/QAS API execution must be clearly separated from PROD. | Aidan / Alvaro | Operator can choose TEST or PROD; dry-run remains available before sending real requests. |
| VAL-006 | Credentials handling | API credentials must not be committed to Git. | Aidan / Alvaro | Credentials come from environment variables, secure config, or terminal prompt. |
| VAL-007 | actAs handling | `actAs` must not be sent automatically. It should only be sent when explicitly supplied and valid for the agent workflow. | Alvaro / Rey | Requests without explicit actAs do not include actAs. |
| VAL-008 | Database-driven validation input | The MVP should be ready to validate requests against database-driven inputs once Aidan confirms the database/data mapping. | Aidan | Database fields can be mapped to ENS, Consignment, and Goods payloads without changing the API request model. |
| VAL-009 | Auditability | Validation results should be traceable back to the source file/message/customer. | Aidan / Alvaro | TSS references and error responses can be linked to an inbound message/file row in the future database flow. |
| VAL-010 | Controlled goods verification | Controlled goods flags, controlled goods type, and related fields must be mapped from the source data and validated using the TSS DataModel/API rules. | Business / Aidan / TSS | Controlled goods examples either pass TSS validation or return a clear TSS error that can be reviewed and corrected. |
| VAL-011 | TSS source of truth | Official ENS, DEC, SFD, SUP, goods IDs, MRNs, deadlines and statuses must come from TSS, not be invented locally. | Aidan / TSS | Local records store or mirror official TSS values after API response or sync. |
| VAL-012 | Raw API payload retention | Create, update, read and sync responses should be retained where the database scope allows it. | Aidan / Engineering | API exchanges or mirror records store enough response detail to diagnose requests and future fields. |
| VAL-013 | TSS choice values | Choice/dropdown values should come from TSS `choice_values` endpoints or `TSS.CV_*` mirrors where available. | Aidan / Engineering | Local validation and payload mapping use values that TSS accepts. |
| VAL-014 | Live submit gates | Automated live submit paths must stay gated by environment/config and support dry-run before production execution. | Aidan / Support | QAS can run without production submission; PROD submit requires explicit enablement. |
## Consolidation Requirements

| ID | Requirement | Rule | Owner / Confirmation | Acceptance Criteria |
| --- | --- | --- | --- | --- |
| CONS-001 | ENS grouping | The process must define what creates one ENS Header. | Business / Aidan | Confirmed grouping keys exist, such as movement, arrival date/time, port, transport identity, trailer/container, or customer-specific movement reference. |
| CONS-002 | Consignment grouping | Source rows must be grouped into consignments according to confirmed customer/business rules. | Business / Aidan | Each source row can be assigned to exactly one consignment group. |
| CONS-003 | Goods item creation | Each distinct source item row should normally become one TSS goods item. | Alvaro / Rey | A source file with N distinct rows creates N goods items unless a confirmed aggregation rule applies. |
| CONS-004 | Quantity handling | Quantities do not count toward the 99 goods item limit. | Alvaro / Rey | One source row with quantity 500 is treated as one goods item. |
| CONS-005 | 99 item split | A consignment must contain no more than 99 distinct goods item rows. | Alvaro / Rey | 100 distinct items split into two consignments: 99 + 1. |
| CONS-006 | Split continuation | When splitting due to the 99 item limit, the new consignments should remain under the same ENS where business rules allow. | Aidan / Business | Split consignments preserve required header/consignment data and can be linked back to the original logical group. |
| CONS-007 | Split reference convention | Split consignments must have a clear reference convention. | Business / Support | Example convention agreed, such as original reference plus sequence suffix `-001`, `-002`, etc. |
| CONS-008 | No unconfirmed aggregation | The process must not merge item rows unless the customer/business confirms aggregation is valid. | Business | Commodity, origin, documents, value and audit trail remain correct after any aggregation. |
| CONS-009 | Partial failure handling | The process must define what happens when one consignment or goods item fails TSS validation. | Business / Support / Aidan | Decision recorded: stop entire file, continue valid rows, or retry failed rows only. |
| CONS-010 | Normal SFD/SD flow | For normal ENS flow, SFD/SD should be generated or linked by TSS after consignment submission where applicable. | Alvaro / Rey | Guided flow does not require manually creating SFD/SD for standard ENS processing. |
| CONS-011 | Standalone SFD/SD exception | Standalone SFD/SD operations remain available where the API and business process support them. | Business / TSS | Standalone creation is not the default guided flow but can be executed through known/generic API operations. |
| CONS-012 | Source traceability | Consolidated and split rows must remain traceable back to source file/message/row. | Aidan / Alvaro | Future database records store enough keys to trace source -> ENS -> Consignment -> Goods -> TSS response. |
| CONS-013 | Data ownership layers | Source intake, working state, API mirror data, and tenant masterdata should have separate ownership. | Aidan / Engineering | QAS either follows the ING/STG/TSS/BKD pattern or documents a simpler equivalent before build. |
| CONS-014 | TDN default grouping | Transport Document Number should be the default consignment grouping candidate and must respect the TSS 35-character limit. | Business / Aidan | Rows sharing the same TDN group together unless a customer-specific rule overrides it; over-length TDNs are flagged before automated submit. |
| CONS-015 | TSS natural keys | Idempotency should use official TSS references and source row links, not generated display names. | Aidan / Engineering | Reprocessing the same source does not create duplicate ENS, DEC, goods or SDI rows when natural keys already exist. |
| CONS-016 | ENS goods vs SDI goods | ENS/source goods and SDI/SUP goods must remain separate but linkable by source row. | Aidan / Engineering | SDI/SUP goods store their own TSS goods IDs and link back to source goods. |
| CONS-017 | ENS-only / no-SFD path | ENS-only or `no_sfd_reason` should not auto-create the normal SFD/SDI path unless TSS later returns an SFD/SUP relationship. | Business / TSS | No SFD/SDI is invented locally for ENS-only movements. |
| CONS-018 | Duplicate SDI prevention | Active or non-cancelled SUP/SDI duplicates for the same TDN should be detected before staging or submitting a new SDI. | Aidan / Support | Duplicate check uses TDN plus active TSS/local status. |
## 99 Item Handling Examples

| Distinct source item rows | Quantity matters for split? | Expected consignments | Distribution |
| ---: | --- | ---: | --- |
| 1 | No | 1 | 1 |
| 99 | No | 1 | 99 |
| 100 | No | 2 | 99 + 1 |
| 198 | No | 2 | 99 + 99 |
| 199 | No | 3 | 99 + 99 + 1 |
| 250 | No | 3 | 99 + 99 + 52 |
| 1 row with quantity 500 | No | 1 | 1 goods item |

## Client File Intake Proposal

The PLE and CW example files should be treated as **ENS-linked consignment and goods uploads**, not as full ENS creation files.

Proposed MVP flow:

1. Customer creates the ENS in TSS.
2. User enters the TSS ENS reference in Fusion.
3. User uploads the customer Excel file.
4. Fusion validates the ENS reference with TSS.
5. Fusion reads each Excel row as one goods item.
6. Fusion groups goods rows into consignments by `transport_document_number`.
7. Fusion applies the 99 goods-row split where needed.
8. Fusion sends consignments/goods to TSS and shows the full TSS response.

| Client | Example file | Observed data | Proposed handling |
| --- | --- | --- | --- |
| PLE / Primeline | Lisburn manifest | 48 goods rows, 1 TDN, no split required. | Create 1 consignment under the supplied ENS. |
| CW / Country Wide | WOS file | 160 goods rows, 1 TDN, split required. | Create 2 consignments under the supplied ENS: 99 + 61. |

Technical proposal:

| Area | Proposal |
| --- | --- |
| ENS creation | Not part of this file flow for MVP. ENS is created first in TSS. |
| Consignment grouping | Use `transport_document_number` as the default grouping key. |
| Goods creation | Treat each source row as one goods item. Quantities do not affect the 99-row limit. |
| 99-row split | Preserve the original TDN and store an internal split sequence, for example `001`, `002`. |
| Customer mappings | Keep PLE and CW mapping rules separate from shared logic. |
| Validation | Do not block locally beyond basic readiness checks; show official TSS errors clearly. |

Brief questions to confirm:

| Question | Owner |
| --- | --- |
| Should the original TDN remain unchanged when a file is split into multiple consignments? | Business / TSS |
| Can Fusion store an internal split sequence instead of changing the customer TDN? | Aidan / Business |
| What default values should be used when PLE does not provide net mass, invoice values or document references? | Business / Aidan |
| Should CW package text such as `Boxes` be normalised to TSS package codes such as `BX` before submission? | Business / Aidan |
| Are 6-digit commodity codes acceptable for the intended declaration path, or must they be enriched before TSS submit? | Business / TSS |
## Required Confirmations

| ID | Confirmation Needed | Owner |
| --- | --- | --- |
| CONF-001 | Database/data mapping from source records into ENS Header, Consignment and Goods payloads. | Aidan |
| CONF-002 | Customer-specific ENS grouping keys. | Business / Aidan |
| CONF-003 | Customer-specific consignment grouping keys. | Business / Aidan |
| CONF-004 | Split reference convention for consignments over 99 items. | Business / Support |
| CONF-005 | Partial failure behaviour. | Business / Support / Aidan |
| CONF-006 | Any customer exceptions to the default one-row-to-one-goods-item rule. | Business |
| CONF-007 | QAS database ownership contract: ING/STG/TSS/BKD or a simpler documented equivalent. | Aidan |
| CONF-008 | TDN default grouping and 35-character handling. | Business / Aidan |
| CONF-009 | ENS-only / no-SFD customer usage and expected downstream behaviour. | Business / TSS |
| CONF-010 | Whether SDI/SUP automation is Phase 2 scope or future enhancement for QAS. | Aidan / Business |
| CONF-011 | Source and refresh process for TSS choice values. | Aidan / Engineering |
| CONF-012 | Live submit gates and dry-run rules for any automated API submit path. | Aidan / Support |
| CONF-013 | Confirm PLE/CW files are MVP consignment + goods uploads, not ENS creation files. | Business / Aidan |
| CONF-014 | Confirm user must enter an existing TSS ENS reference before uploading PLE/CW files. | Business / Aidan |
| CONF-015 | Confirm original TDN handling when 99-row split creates multiple consignments. | Business / TSS |
| CONF-016 | Confirm customer-specific defaults for missing PLE/CW fields. | Business / Aidan |
| CONF-017 | Confirm normalisation rules for package codes, commodity codes and numeric formats. | Business / TSS |

## Flow V2 BKD Context Alignment

This section uses the existing `Synovia-Digital/Fusion_Flow_V2_BKD` prod documentation as reference context. It does not make QAS a copy of BKD production; it captures reusable decisions that affect validation and consolidation design.

| Source | Confirmed Context | Phase 2 Impact |
| --- | --- | --- |
| Production automation handbook | TSS is the source of truth for official references, statuses, MRNs and deadlines. | Reinforces VAL-011 and VAL-012. |
| Data ownership model | `ING` is audit, `STG` is working state, `TSS` is API mirror, and tenant schema is config/masterdata. | Reinforces CONS-013 and Ready For Build database confirmation. |
| PRD data model cutover | ENS/source goods and SDI/SUP goods use separate tables and separate TSS goods IDs. | Reinforces CONS-016 and avoids mixing SDI goods into ENS goods. |
| Ingest Excel workflow | Clean auto-create is gated; preview/review is used when blockers or warnings exist. | Reinforces VAL-014 and CONS-009 without bypassing TSS validation. |
| Ingest Excel workflow | ENS-only and no-SFD are explicit paths; SDI should not be invented locally. | Reinforces CONS-017. |
| ENS user guide notes | Status decisions should be TSS-led and RawJson should be retained for diagnostics and future automation. | Reinforces VAL-011, VAL-012 and CONS-012. |
| ENS user guide notes | Transport Document Number has a 35-character limit and matters for duplicate SDI prevention. | Reinforces CONS-014 and CONS-018. |
| API code cross-reference | v2.9.4 and v2.9.5 have the same endpoint set; v2.9.5 changes are field-level. | Reinforces VAL-002, VAL-003 and endpoint MVP coverage. |
| API v2.9.5 notes | `addition_deduction_currency` and compact `taric_code` formatting are v2.9.5-specific field rules. | Keep these in field mapping/testing rather than hard-coding unsupported assumptions. |
## Discovery Findings Alignment

This section maps the Phase 1 Discovery findings into the Phase 2 validation and consolidation requirements.

| Finding | Discovery Meaning | Phase 2 Coverage |
| --- | --- | --- |
| KDF-001 | Flow V1 does not initiate the customs journey; the normal replicated flow is ENS-linked consignment processing after the ENS exists in TSS. | Covered by CONS-010 and FRD alignment RF-001. ENS creation remains available as an API MVP/future capability, but not assumed as the default Flow V1 replication path. |
| KDF-002 | The CSV template is a critical business component and must remain stable for customers. | Covered by VAL-008, VAL-009, CONS-003 and CONS-012. Source-to-payload mapping must preserve customer CSV meaning. |
| KDF-003 | Transport Document Number drives consignment grouping in Flow V1. | Covered by CONS-002 and CONF-003. TDN should be treated as the default grouping candidate unless a customer-specific rule overrides it. |
| KDF-004 | Automated 99-line segmentation provides high operational value. | Covered by CONS-004, CONS-005, CONS-006, CONS-007 and the 99 Item Handling Examples. |
| KDF-005 | Validation is a core capability, including mandatory fields, commodity codes, EORI, country, customs codes and controlled goods. | Covered by VAL-001, VAL-002, VAL-004 and VAL-010. The API-first MVP must expose official TSS validation responses clearly. |
| KDF-006 | Monitoring supports operational control across consignment lifecycle and processing outcomes. | Covered by VAL-004, VAL-009 and CONS-012 as the data foundation for status visibility. |
| KDF-008 | Reporting exists at internal and customer levels. | Covered by VAL-009 and CONS-012 as the traceability foundation for later reporting. |
| KDF-009 | User interaction is mainly around upload, validation, correction, controlled goods, submission and monitoring. | Covered by VAL-004, VAL-010 and CONS-009. Later UI/workflow decisions depend on database and operational design. |
| KDF-010 | Future automation opportunities include ENS creation, automated progression/submission, retry logic, notifications and dashboarding. | Covered by the FRD RF alignment table as future enhancement scope, not mandatory Phase 2 parity build. |
## FRD Alignment

This section shows how the Phase 2 validation and consolidation requirements support the Flow V1 to Flow V2 FRD v1.5 requirements.

| FRD ID | FRD Area | Phase 2 Coverage |
| --- | --- | --- |
| FR-001 | Consignment Data Import | Covered by CONS-002, CONS-003, VAL-008 and VAL-009. |
| FR-002 | ENS-Linked Consignment Processing | Covered by CONS-001, CONS-006, CONS-010 and VAL-008. |
| FR-003 | Consignment Record Generation | Covered by CONS-002, CONS-003, CONS-005 and CONS-006. |
| FR-004 | Transport Document Number Grouping | Covered by CONS-002 and CONF-003. The exact grouping key must be confirmed per customer. |
| FR-005 | 99-Line Segmentation | Covered by CONS-004, CONS-005, CONS-006, CONS-007 and the 99 Item Handling Examples. |
| FR-006 | Validation Rules | Covered by VAL-001, VAL-002, VAL-003, VAL-004 and VAL-005. |
| FR-007 | Controlled Goods Verification | Covered by VAL-010. |
| FR-008 | User Review and Amendment | Covered by VAL-004, VAL-008 and CONS-009. Future UI/database workflow still needs confirmation. |
| FR-009 | Consignment Deletion | Covered by CONS-009 and the API-first operation approach. |
| FR-010 | Submission to TSS | Covered by VAL-004, VAL-005, VAL-006, VAL-007 and CONS-010. |
| FR-011 | Monitoring and Status Visibility | Covered by VAL-004, VAL-009 and CONS-012. |
| FR-012 | Reporting | Covered by VAL-009 and CONS-012 as the data foundation for reporting. |
| FR-013 | Audit Trail | Covered by VAL-009 and CONS-012. |

| FRD ID | Future Enhancement Area | Phase 2 Position |
| --- | --- | --- |
| RF-001 | ENS Creation Within Flow V2 | Supported by the API MVP; database mapping to be confirmed by Aidan. |
| RF-002 | Automated Consignment Processing | Supported by CONS-002, CONS-003, CONS-005 and CONS-006 after grouping rules are confirmed. |
| RF-003 | Automated Submission to TSS | Supported by VAL-004, VAL-005, VAL-006 and CONS-010; automation timing remains a later decision. |
| RF-004 | Exception-Based User Intervention | Supported by VAL-004 and CONS-009 once partial failure behaviour is agreed. |
| RF-005 | User Notifications | Not in Phase 2 build scope yet; depends on monitoring/status data. |
| RF-006 | Operational Dashboard | Not in Phase 2 build scope yet; depends on reporting/audit data. |
| RF-007 | Automated Retry Logic | Not in Phase 2 build scope yet; depends on partial failure and retry rules. |
| RF-008 | Enhanced Audit and Traceability | Supported by VAL-009 and CONS-012. |

## TSS Transition API Test Evidence

This test confirms the new TSS transition API can be reached with OAuth2 client credentials. Credentials were provided at runtime only and must not be stored in source control or documentation.

| Item | Value |
| --- | --- |
| Test date | 2026-06-23 |
| Environment | TSS transition test API |
| Token flow | OAuth2 `client_credentials` |
| Token URL | `https://auth.tt.nc-tss.uk/realms/TSS_Portal/protocol/openid-connect/token` |
| Base URL | `https://tt.nc-tss.uk/api/v1/trader_api/transition` |
| Safety rule | Controlled GET and transition-test create only; no production write, submit, update or cancel action was executed. |
| actAs | Not sent. It must only be used when `can_act_as` is confirmed. |

### Connectivity Check

A non-destructive GET against `choice_values/declaration_category` returned HTTP `200` and confirmed the API token and base URL are valid. The response returned declaration categories `IMY` and `IMZ`.

### ENS Draft Create Check

A controlled `POST /headers` was executed in the transition test API to confirm ENS header creation works with OAuth2. The payload used sanitized test values and did not copy production ENS references, status values, MRNs or audit fields.

| Check | Endpoint | Result |
| --- | --- | --- |
| Create ENS header | `POST /headers` | HTTP `200`; response `status=created`, `process_message=success`, reference `ENS1782234071313`. |
| Read created ENS header | `GET /headers?reference=ENS1782234071313&fields=...` | HTTP `200`; status `Draft`; movement type `1a`; arrival `25/06/2026 11:00:00`; port `GBAUBELBELBEL`. |

Conclusion: the new transition API can create and read an ENS draft through the API. For the MVP, this confirms the ENS creation path is technically viable once business rules and field mapping are agreed.

### Consignment Read Check

Requested consignment reference: `DEC000000017182156`.

| Attempt | Endpoint | Result |
| --- | --- | --- |
| Legacy-style path | `/tss_api/consignments?reference=DEC000000017182156` | HTTP `404 Not Found`. |
| Transition-style path | `/consignments?reference=DEC000000017182156` | HTTP `400` with `ERROR 100002: The consignment was not found.` |

Conclusion: the transition API is reachable, and the transition-style consignment endpoint is active without the legacy `/tss_api` prefix. The supplied `DEC000000017182156` reference was not found in the transition test environment. This likely means the reference belongs to the current/legacy environment or has not been migrated into transition test data.

### Current Production Read Evidence

The same consignment reference was checked against the current production TSS API using Basic Auth credentials loaded from the local `.env` file at runtime. No credentials were written into source control or documentation.

| Check | Endpoint | Result |
| --- | --- | --- |
| Consignment read without explicit fields | `/tss_api/consignments?reference=DEC000000017182156` | HTTP `200`, but TSS returned `Cannot map object`. |
| Consignment read with explicit fields | `/tss_api/consignments?reference=DEC000000017182156&fields=...` | Successful response. Status: `Authorised for Movement`. Parent ENS: `ENS000000002680586`. MRN: `26XI05000T9636LAT0`. |
| ENS read without explicit fields | `/tss_api/headers?reference=ENS000000002680586` | HTTP `200`, but TSS returned `Cannot map object`. |
| ENS read with explicit fields | `/tss_api/headers?reference=ENS000000002680586&fields=...` | Successful response. Status: `Authorised for Movement`. Arrival: `24/06/2026 06:30:00`. Port: `GBAUBELBELBEL`. |

Conclusion: the current production API can read the DEC and parent ENS when the request asks for a controlled field list. For the MVP, read/sync jobs should request explicit fields instead of relying on the default response, because the default response can fail inside TSS mapping even when the HTTP status is `200`.

No transition-test POST was executed with production data. If production data is reused for transition testing later, the payload must be reviewed first and production-only values such as original references, statuses, MRNs and audit fields must be removed or replaced.

## Ready For Build When

- Aidan confirms the database/data mapping.
- Aidan confirms the QAS database ownership contract or simpler documented equivalent.
- Business confirms grouping keys for ENS and consignments.
- TDN grouping and 35-character handling are agreed.
- Support/business confirms split reference convention.
- Partial failure handling is agreed.
- TSS API TEST credentials and endpoint access are available for QAS testing.
- TSS choice value source and refresh process are confirmed.
- PLE/CW intake proposal is agreed: existing ENS reference first, then consignment/goods upload.
