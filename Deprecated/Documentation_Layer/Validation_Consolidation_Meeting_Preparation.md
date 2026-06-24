# Define Detailed Validation Requirements

## Document Purpose

| Item | Description |
| --- | --- |
| Workstream | Define Detailed Validation Requirements and Define Consolidation Requirements |
| Scope | Goods/items Excel validation and consolidation rules before API processing |
| Output Type | Meeting preparation and requirements discussion document |
| Status | Working document - API mandatory fields are treated as fixed requirements; unresolved business/default rules remain Pending to confirm |

## 1. Purpose of the Work

The objective of this work is not to build the final application. The objective is to define the technical and business rules that an Excel file of goods/items must satisfy before the data can be processed against an existing consignment or ENS reference.

The current focus is limited to two areas:

- Validation Requirements: what must be checked before processing.
- Consolidation Requirements: how goods rows may be grouped, split or aggregated.

Important principle: fields marked as mandatory by the TSS API contract must not be presented as open questions. The open question is only how the value will be obtained: from the customer Excel, master data, a tenant default, TSS, or operator input.

## 2. Current Understanding

| Area | Current understanding | Status |
| --- | --- | --- |
| Consignment / ENS reference | The user may enter an existing TSS reference such as `ENS00...` before uploading goods. | Pending to confirm exact reference type/format |
| Customer file | The customer may provide an Excel file containing goods/items. | Confirmed as discussion basis |
| ENS creation | For the MVP discussion, the ENS is expected to exist before goods are uploaded. | Pending business confirmation |
| Goods rows | Each Excel row is treated as one goods item unless a confirmed consolidation rule says otherwise. | Working assumption |
| API mandatory fields | Mandatory fields from the TSS DataModel are treated as fixed requirements, not optional business questions. | Confirmed principle |
| Validation | The Excel should be validated before processing to API or database flow. | Confirmed as required |
| Consolidation | Goods may need grouping or splitting, especially around the 99 goods row limit. | Pending final grouping rules |
| Rule ownership | API contract rules belong to TSS/API. Source/default decisions belong to business/client/team. | Confirmed principle |

## 3. API Contract Baseline - Not Open Questions

These fields should be treated as required because they are marked as mandatory in the local TSS API v2.9.5 DataModel. The team should not ask whether they are mandatory. The team should ask where the value will come from and how missing values will be handled.

### Consignment Contract Fields

| Field | Contract status | Meaning for discussion | Question we should ask |
| --- | --- | --- | --- |
| `op_type` | API mandatory | Operation type must be supplied by the integration. | Which operation is being executed: create, update, submit or cancel? |
| `goods_description` | API mandatory | Consignment description is required by the API. | Which source column should provide it when the customer file has both consignment and goods descriptions? |
| `transport_document_number` | API mandatory | Transport document number is required by the API. | Is this also the business grouping key for PLE/CW? |
| `controlled_goods` | API mandatory | Controlled goods flag is required. | Does the customer supply it, or can master data/defaults determine yes/no? |
| `consignor_eori` | API mandatory | Consignor EORI is required. | Is it always supplied, or must it come from master data? |
| `consignee_eori` | API mandatory | Consignee EORI is required. | Is it always supplied, or must it come from master data? |
| `importer_eori` | API mandatory | Importer EORI is required. | Is it always supplied, or tenant-specific? |
| `exporter_eori` | API mandatory | Exporter EORI is required. | Is it always supplied, or must it come from master data? |

### Goods Item Contract Fields

| Field | Contract status | Meaning for discussion | Question we should ask |
| --- | --- | --- | --- |
| `op_type` | API mandatory | Operation type must be supplied by the integration. | Which operation is being executed: create, update or delete? |
| `type_of_packages` | API mandatory | Package type is required and should be a valid TSS choice value. | What mapping converts customer values such as `Boxes` into accepted package values? |
| `number_of_packages` | API mandatory | Package count is required; min 1, max 99999. | Which source column gives package count and how are invalid values corrected? |
| `package_marks` | API mandatory | Package marks are required; API notes say use `ADDR` if unknown. | Can we apply `ADDR` automatically when blank? |
| `gross_mass_kg` | API mandatory | Gross mass is required; max 13 digits and 2 decimals. | Which source or master data provides it and how should rounding be handled? |
| `goods_description` | API mandatory | Goods description is required. | Which source column is authoritative and what length/text sanitisation is needed? |

### Conditional API Fields

| Field | Contract status | Condition / rule | Question we should ask |
| --- | --- | --- | --- |
| `declaration_number` | Conditional | Required on consignment create. | Does the user always supply an existing ENS reference for this MVP flow? |
| `consignment_number` | Conditional | Required on consignment update/submit/cancel and goods create parent link. | How do we store/use returned DEC references after creation? |
| `no_sfd_reason` | Conditional | Required when importer EORI is unregistered or SCDP Restricted. | Does this customer use ENS-only/no-SFD scenarios? |
| `goods_domestic_status` | Conditional | Required if `controlled_goods=yes`. | What source/default provides this when controlled goods are present? |
| `container_indicator` | Conditional | Required for Maritime/RoRo. | Is the movement always Maritime/RoRo and where is the value sourced? |
| `controlled_goods_type` | Conditional | Required when controlled goods rules apply. | What source or product master data supplies controlled type? |
| `commodity_code` | Conditional | Min 8 digits, or 6 if APC `1SG`; mandatory for SD/FFD/IMMI. | Which declaration path applies, and where do we enrich short codes? |
| `country_of_origin` | Conditional | Required when preference is 100-199. | Is origin always expected from source/product master data? |
| `procedure_code` | Conditional | Mandatory for SD/FFD. | Is SDI/FFD in scope for this flow, and what default/source applies? |
| `valuation_method`, `invoice_number`, `nature_of_transaction`, `item_add_ded` | Conditional | Required under SD/FFD valuation paths. | Are these part of MVP or future SDI processing? |

## 4. Proposed Minimal Prototype for Discussion

A simple HTML prototype can be used to make the discussion concrete. It is not the final system and does not include login, backend, database, authentication, deployment or final architecture.

The prototype only simulates this flow:

1. User enters a consignment or ENS reference.
2. User uploads an Excel file.
3. The browser reads the goods rows.
4. The screen shows the raw rows in a table.
5. The screen classifies validation results as errors, warnings or pending items.
6. The screen shows a possible consolidation view.
7. The team uses the output to discuss rules and decisions.

The prototype is useful because it shows structure without implying that the final solution has already been built.

## 5. Detailed Validation Areas

| Validation area | What should be validated | Why it matters | Correct discussion question | Pending to confirm |
| --- | --- | --- | --- | --- |
| Consignment reference validation | Required value, no leading/trailing spaces, allowed characters, expected TSS reference pattern. | Links uploaded goods to the correct TSS declaration flow. | Which TSS reference type is valid for this MVP: existing ENS only, or other parent references too? | Exact reference type, format and lookup flow. |
| Excel file validation | File exists, `.xlsx` or `.xls`, readable workbook, size limit. | Prevents processing invalid or unsupported files. | Which file formats and size limit should we support operationally? | Maximum file size and accepted formats. |
| Sheet and header validation | Correct worksheet, detectable header row, named columns. | The system needs a stable way to map Excel columns to API fields. | Is the template fixed, or do PLE/CW need separate mappings? | Final template structure and customer mapping. |
| API mandatory column availability | Contract fields must be available directly or derivable before API submission. | API-mandatory fields cannot be treated as optional. | For each API-mandatory field, is the source Excel, master data, tenant default, TSS response, or operator input? | Source of each mandatory value. |
| Row-level validation | Empty rows, required row values, one goods item per row, row traceability. | Prevents incomplete goods being processed silently. | Should file-level processing stop when any API-mandatory value is missing? | Partial failure behaviour. |
| Data type validation | Numeric weights, integer packages, ISO country/currency codes, yes/no flags. | Prevents avoidable API validation errors. | Which values should be auto-normalised before sending to TSS? | Normalisation rules and approved transforms. |
| Cross-field validation | Net mass cannot exceed gross mass, controlled goods trigger conditional fields, containerised movement may need equipment number. | Some fields become required because of another field. | Which conditional API paths are in MVP scope? | Declaration path and conditional field contract. |
| Duplicate detection | Duplicate item numbers, repeated source rows, repeated consolidation keys. | Helps identify accidental duplicates without blocking valid consolidation. | What defines a duplicate source goods row for audit and reprocessing? | Duplicate handling rule. |
| Totals validation | Totals for gross mass, net mass, packages and invoice value. | Supports reconciliation and confidence before submission. | Are source totals provided, and should calculated totals be compared? | Source of declared totals and rounding. |
| Business rule validation | 99 goods row split, customer-specific defaults, controlled goods source/default handling. | Business rules decide how required values are derived. | Which defaults are approved and who owns them? | Rule owner and approval process. |
| Error and warning classification | API missing mandatory fields should be errors; business defaults pending approval should be pending; review-only issues can be warnings. | Avoids false optionality and inconsistent decisions. | Which non-contract checks should block processing? | Severity model for non-contract rules. |

## 6. Consolidation Areas

| Consolidation criterion | Meaning | Totals to calculate | Risks / doubts | Correct question for the team | Status |
| --- | --- | --- | --- | --- | --- |
| Consignment reference | Group rows under the entered ENS/consignment reference. | Row count, gross mass, packages, invoice value. | Wrong reference could attach goods to the wrong declaration. | Is one uploaded file always linked to one existing ENS in MVP? | Pending to confirm |
| Transport document number | Use TDN as default consignment grouping candidate. | Row count per TDN, goods split count. | TDN may exceed API length or may not be unique enough. | Is TDN the business grouping key for PLE/CW? | Working assumption |
| Commodity / HS code | Keep goods code available for validation/consolidation. | Gross mass, net mass, packages, invoice value. | Commodity code has API conditions and SD/FFD/IMMI rules. | Should commodity code be part of the grouping key, beyond being a required/conditional API field? | Pending to confirm |
| Description | Group goods with the same description only if approved. | Same as above. | Text equality can under- or over-consolidate. | Must descriptions match exactly, or should normalised text be used? | Pending to confirm |
| Country of origin | Keep origin-specific totals separate when origin is relevant. | Gross/net mass, packages, value by origin. | Merging different origins can break declaration accuracy. | Should origin always be part of the consolidation key? | Recommended pending confirmation |
| Package type | Keep package type-specific totals separate when grouping. | Package count by type, mass, value. | Text such as `Boxes` must map to TSS values. | What package type mapping is approved per customer? | Pending to confirm |
| Goods row count | Split groups over 99 goods rows into additional consignments. | Number of consignments, rows per split. | Reference convention for split consignments must be agreed. | Should original TDN remain unchanged when split occurs? | Working assumption |
| Invoice value | Sum item invoice amounts where available and relevant. | Total invoice value by group. | Value may be mandatory in SDI/controlled flows but not base ENS goods create. | Which declaration path requires invoice value in MVP? | Pending to confirm |

## 7. Questions to Ask in the Meeting

### API Contract and Source of Mandatory Data

- For each API-mandatory consignment field, what is the source: Excel, master data, tenant default, TSS response, or operator input?
- For each API-mandatory goods field, what is the source: Excel, master data, tenant default, or operator input?
- Which API-mandatory fields are already present in the PLE and CW files?
- Which API-mandatory fields must be derived from master data or defaults?
- Who owns approval of defaults such as `package_marks=ADDR`?

### Excel Template and Input

- What is the expected Excel template for each customer?
- Are column names fixed or can they vary by customer?
- Can the header row appear below a title row?
- Should PLE and CW use the same template or customer-specific mappings?
- Should unknown columns be ignored, mapped, or retained only as source/audit data?

### Consignment / ENS Reference

- What is the exact TSS parent reference expected for this MVP flow?
- Does the user always supply an existing ENS reference before upload?
- Should the system validate the reference with TSS before processing the file?
- How do we store returned DEC references for later goods creation, updates, submit and cancel?

### Goods Validation

- Which fields identify a unique source goods row for audit and retry?
- Which validations should block processing beyond API-mandatory missing/invalid fields?
- Which validations should only generate warnings?
- For commodity code, which declaration path applies: base ENS/SFD, SD, FFD or IMMI?
- If a customer supplies a 6-digit code, is APC `1SG` applicable or must the code be enriched?
- Net mass cannot be greater than gross mass; what source should be used when net mass is blank?
- Package count and package type are API mandatory; how do we correct invalid package data?
- Controlled goods is API mandatory; what source/default determines yes/no?
- When `controlled_goods=yes`, where do conditional values such as goods domestic status and controlled goods type come from?
- Invoice value/currency may be declaration-path dependent; is SDI/controlled goods processing in MVP scope?

### Consolidation

- Which fields are used for consolidation?
- Should consolidation group by commodity code only or by multiple fields?
- Should descriptions be exactly equal to consolidate goods?
- Should country of origin always be part of the grouping key?
- Should package type always be part of the grouping key?
- What should happen when one group has more than 99 goods rows?
- Should the original TDN remain unchanged after a 99-row split?
- What totals should be calculated after consolidation?
- Should totals be rounded? If yes, how many decimals?

### Error Handling and Ownership

- What should happen with invalid rows during consolidation?
- Should the user be allowed to continue if there are warnings but no API-mandatory errors?
- What output is expected after consolidation?
- Who owns each non-contract decision: business, technical, client or TSS?
- Which rules can be confirmed now, and which must remain Pending to confirm?

## 8. What to Show as Progress

Progress should show structure and control, not a finished product.

Suitable progress evidence:

- API contract baseline table.
- Validation matrix.
- Consolidation matrix.
- List of open questions focused on data source and defaults, not whether API-mandatory fields are mandatory.
- Simple local HTML prototype for discussion.
- Sample Excel mapping from source columns to API fields.
- Summary of assumptions.
- Pending decisions log.
- Proposed next-step plan.

Message to use: the team is not being shown a final application; the team is being shown a structured way to agree the rules before implementation.

## 9. Validation Matrix Template

| Area | Field / Rule | Description | Severity | Blocking | Owner | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| API Contract | Consignment `goods_description` | API mandatory. | Error | Yes | TSS/API | Confirmed | Need source column decision only. |
| API Contract | Consignment `transport_document_number` | API mandatory. | Error | Yes | TSS/API | Confirmed | Also candidate grouping key. |
| API Contract | Consignment `controlled_goods` | API mandatory yes/no. | Error | Yes | TSS/API | Confirmed | Ask source/default, not mandatory status. |
| API Contract | Party EORIs | `consignor_eori`, `consignee_eori`, `importer_eori`, `exporter_eori` are API mandatory for consignment. | Error | Yes | TSS/API | Confirmed | Need master data/default strategy if blank. |
| API Contract | Goods `type_of_packages` | API mandatory choice value. | Error | Yes | TSS/API | Confirmed | Customer text values need approved mapping. |
| API Contract | Goods `number_of_packages` | API mandatory, min 1 max 99999. | Error | Yes | TSS/API | Confirmed | Validate integer and range. |
| API Contract | Goods `package_marks` | API mandatory; use `ADDR` if unknown. | Error | Yes | TSS/API | Confirmed | Business approves automatic fallback. |
| API Contract | Goods `gross_mass_kg` | API mandatory, max 13 digits and 2 decimals. | Error | Yes | TSS/API | Confirmed | Validate numeric and precision. |
| API Contract | Goods `goods_description` | API mandatory. | Error | Yes | TSS/API | Confirmed | Validate text and source column. |
| Conditional API | `commodity_code` | Conditional: min 8 digits, or 6 if APC `1SG`; mandatory for SD/FFD/IMMI. | Error when condition applies | Conditional | TSS/API | Confirmed condition | Need declaration path and enrichment source. |
| Conditional API | `country_of_origin` | Conditional when preference is 100-199. | Error when condition applies | Conditional | TSS/API | Confirmed condition | Usually product/source data. |
| Conditional API | `controlled_goods_type` | Conditional under controlled goods rules. | Error when condition applies | Conditional | TSS/API / Business | Confirmed condition | Need source/master data. |
| Conditional API | `procedure_code` | Mandatory for SD/FFD. | Error when condition applies | Conditional | TSS/API | Confirmed condition | Need SD/FFD scope decision. |
| Data Quality | Net mass greater than gross | Net mass cannot exceed gross when supplied. | Error | Yes | Technical | Confirmed rule | Not a business question. |
| File | Excel file required | User must upload a workbook. | Error | Yes | Technical | Confirmed | Basic readiness check. |
| File | Header row detection | System must identify named columns even if a title row exists. | Error | Yes | Technical | Assumption | Based on customer/template examples. |
| Source Mapping | Unknown source columns | Unknown columns should be listed for mapping/audit review. | Info / Pending | No | Technical / Business | Pending | Do not label useful party fields as unknown once mapped. |
| Business Default | Missing mandatory value source | Mandatory API values can come from Excel, master data, default or operator input. | Error until sourced | Yes | Business / Technical | Pending | Decision is source/fallback, not mandatory status. |
| Totals | Calculated totals | Calculate gross, net, packages and invoice totals. | Info | No | Business | Pending | Compare to source totals only if source provides totals. |

## 10. Consolidation Matrix Template

| Consolidation field | Used for grouping | Aggregated values | Rule description | Example | Risk / ambiguity | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Consignment / ENS reference | Yes | Row count, gross, packages, value | All uploaded goods attach to the entered parent reference. | `ENS00...` | Wrong reference creates wrong association. | Pending reference-type confirmation |
| Transport document number | Pending / likely yes | Row count, split count, totals | Rows with the same TDN may form one consignment group. | `GVT1606Test3` | TDN may not be unique enough. | Working assumption |
| HS / commodity code | Pending | Gross, net, packages, value | Commodity may be part of the grouping key if business approves. | `8708299000` | Same code may cover different descriptions; API condition depends on declaration path. | Pending to confirm |
| Description | Pending | Gross, net, packages, value | Same descriptions may be grouped. | `Car parts` | Exact text matching can be unreliable. | Pending to confirm |
| Country of origin | Recommended | Gross, net, packages, value by origin | Different origins should usually remain separate. | `GB` vs `CN` | Mixing origins can break declaration accuracy. | Pending to confirm |
| Package type | Pending | Package count by type | Package types may need separate groups. | `BX`, `PK`, `Boxes` | Text-to-code mapping needed. | Pending to confirm |
| Gross mass | No | Sum | Gross mass should be summed, not used alone as a grouping key. | `100 + 50 = 150` | Rounding and decimal precision. | Confirmed aggregation approach |
| Net mass | No | Sum | Net mass should be summed where available. | `90 + 45 = 135` | Missing values may affect SDI quality. | Pending source/fallback decision |
| Number of packages | No | Sum | Packages should be summed across compatible package types. | `10 + 5 = 15` | Package type must be compatible. | Confirmed aggregation approach |
| Invoice value | No | Sum | Invoice values should be summed where relevant. | `500 + 250 = 750` | Declaration path determines requirement. | Pending scope decision |
| Goods row count | Yes for split logic | Count rows per group | More than 99 goods rows should create additional consignments. | `160 -> 99 + 61` | Split reference convention needed. | Working assumption |

## 11. Decision Log

| Decision ID | Topic | Decision needed | Proposed option | Owner | Deadline | Status |
| --- | --- | --- | --- | --- | --- | --- |
| DEC-001 | Excel template | Confirm fixed template or customer-specific mappings. | Support PLE/CW mappings separately, keep shared validation logic. | Business / Technical | TBD | Pending |
| DEC-002 | TSS parent reference | Confirm whether existing ENS reference is required before upload. | MVP requires existing ENS reference. | Business / TSS | TBD | Pending |
| DEC-003 | Mandatory field sources | Confirm source for each API-mandatory field. | Map as Excel / master data / tenant default / operator input. | Business / Technical | TBD | Pending |
| DEC-004 | Party EORI source | Confirm source for mandatory party EORIs. | Use Excel when present; otherwise master data/tenant config. | Business / Client | TBD | Pending |
| DEC-005 | Commodity code condition | Confirm declaration path and enrichment source. | Enrich short codes where required before SD/FFD/IMMI. | Business / TSS | TBD | Pending |
| DEC-006 | Package type mapping | Confirm mapping for customer text values. | Map common values such as Boxes to approved TSS package value per tenant. | Business / Technical | TBD | Pending |
| DEC-007 | Missing package marks | Confirm operational use of API fallback. | Use `ADDR` when package marks are missing. | Business | TBD | Pending |
| DEC-008 | Controlled goods source | Confirm how yes/no is determined. | Use Excel or product master data; avoid silent default until approved. | Business / Client | TBD | Pending |
| DEC-009 | Net mass fallback | Confirm whether blank net mass can default to gross. | Allow fallback only when approved and not conflicting with declaration path. | Business / TSS | TBD | Pending |
| DEC-010 | Consolidation key | Confirm final grouping fields. | Start with TDN, commodity, description, origin and package type. | Business / Technical | TBD | Pending |
| DEC-011 | 99 goods split | Confirm split rule and references. | Auto-split after 99 rows while preserving original source TDN. | Business / TSS | TBD | Pending |
| DEC-012 | Invalid row handling | Confirm processing behaviour. | Block file if API-mandatory errors exist; allow review if only warnings/pending items. | Business / Support | TBD | Pending |
| DEC-013 | Rule ownership | Confirm owner for each non-contract rule. | Track owners as Business, Technical, Client or TSS. | Project team | TBD | Pending |

## 12. Recommended Path Forward

1. Confirm the expected Excel structure for PLE and CW.
2. Map each API-mandatory field to a source: Excel, master data, default, TSS response or operator input.
3. Define which conditional API paths are in MVP scope.
4. Classify validations as Error, Warning or Info.
5. Confirm which non-contract rules block processing.
6. Define consolidation grouping logic.
7. Validate the proposed rules with sample files.
8. Record decisions in the decision log.
9. Convert confirmed rules into final documentation.
10. Only after that, consider implementation.

## 13. Meeting Narrative

Use this script as a calm summary in the meeting:

"The objective here is not to build the final system alone or to make assumptions too early. I have structured the work into two areas: validation requirements and consolidation requirements.

One correction I want to make clear is that API-mandatory fields are not open questions. If the TSS DataModel marks a field as mandatory, we treat it as mandatory. The question for the team is where that value comes from: the customer Excel, master data, tenant defaults, TSS response, or operator input.

To make the discussion easier, I prepared a minimal prototype approach. It lets us simulate entering a parent reference, uploading an Excel file, reading goods rows, showing validation results and showing a possible consolidation view. This is only a discussion tool, not the final application.

The important output is the rule set: API contract fields, conditional fields, business defaults, validation severity and consolidation keys.

My proposed next step is to map every mandatory API field to a source, confirm the conditional declaration paths, agree the consolidation logic, and record ownership for business/default decisions. The aim of this meeting is to reduce ambiguity and avoid asking questions that the API contract already answers."

## Closing Note

This document should remain controlled and practical. Confirmed API contract requirements must not be downgraded to assumptions. Any rule that depends on customer behaviour, source data, default values, declaration path or operational policy should stay marked as Pending to confirm until the rule owner approves it.