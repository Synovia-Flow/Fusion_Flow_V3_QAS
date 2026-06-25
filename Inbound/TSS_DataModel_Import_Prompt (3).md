# Import Prompt — TSS API Data Model → Solution Ingestion

Use this prompt to drive an import of `TSS_DataModel_Spec.xlsx` into your solution
(data catalogue, schema registry, validation engine, or declaration-builder). Paste it
to your ingestion agent / LLM, attach the workbook, and fill in the two bracketed
placeholders at the top.

---

## ROLE

You are a data-model ingestion agent. You will read the attached workbook
`TSS_DataModel_Spec.xlsx` and load its contents into **[TARGET SOLUTION NAME]** via
**[TARGET MECHANISM: e.g. REST API / SQL inserts / CSV staging / config files]**.
Do not invent fields. Every record you create must trace back to a row in the workbook.

## SOURCE OF TRUTH

The workbook is authoritative. It is derived from the TSS Declaration API v2.9.5 model.
Tabs:

| Tab | Contents | Use for |
|---|---|---|
| `0 · README` | Status vocabulary + tab index | Orientation only — do not import |
| `R · Routes Overview` | The 4 routes (A–D): trigger, entities in order, references produced | Route registry |
| `R2 · Route × Entity` | Route × entity coverage (Required / Auto / Optional / Ref / Not used) | Route-to-entity membership |
| `RA`–`RD · …` | Per-route step sequence: step, phase, entity/tab, endpoint·op_type, reference returned, key data elements, timing | RouteStep table |
| `1 · Data Dictionary` | 253 distinct data elements, each with a stable code `DE0001`… | Master element table |
| `2 · Field × API Matrix` | Element × declaration-type with status text | Element-to-type membership |
| `3 · API Returns & Refs` | Each API, presence, reference series returned | Endpoint / reference registry |
| `4 · Allowed Values` | Per-element validation + choice endpoint | Validation rules |
| `5 · Choice Catalogue` | 35 choice-value reference lists | Lookup/code-list registry |
| `6 · Relationships & Keys` | Primary keys, foreign keys, cardinality, nested arrays | Relational model |
| `7 · Critical Rules` | 26 empirically-confirmed override rules | Validation overrides / warnings |
| `8 · …` through `18 · …` | One tab per declaration type | Per-type field specifications |

## STATUS VOCABULARY (canonical — map exactly, do not collapse)

- **Mandatory** — required in the create payload for that declaration type.
- **Conditional** — required only when a condition holds (see Description / Critical Rules). Store the condition text.
- **Nullable** — optional. Flag separately that `op_type=update` is full-replacement (omitting a value clears it).
- **Read-only** — system-populated / returned by GET; never sent on create.
- **Not present** — element does not exist on that type; create NO membership row.

## TARGET SCHEMA TO POPULATE

Create (or map to) these entities. Use the suggested keys.

1. **DataElement** — `code` (PK, e.g. DE0001), `name`, `data_type`, `max_length`,
   `has_validation` (bool), `validation_rule` (text), `choice_endpoint` (nullable),
   `description`. Source: tab 1 (+ tab 4 for validation detail).
2. **DeclarationType** — `code` (DH, CN, GI, SH, SC, SD, FF, IM, GM, MI, SO),
   `name`, `endpoint`, `reference_series` (e.g. ENS000…), `present` (bool).
   Source: tab 3.
3. **ElementUsage** (join) — `declaration_type_code` (FK), `element_code` (FK),
   `status` (one of the vocabulary above), `conditional_rule` (nullable text).
   Source: per-type tabs 8–18 (preferred — they carry the condition text) cross-checked
   against tab 2. One row per (type, element) where status ≠ Not present.
4. **ChoiceList** — `name` (PK), `description`, `used_by`, `api_call`. Source: tab 5.
5. **Relationship** — `parent_entity`, `parent_key`, `child_entity`, `child_key`,
   `cardinality`, `kind` (`primary` | `nested_array`), `notes`. Source: tab 6.
6. **PrimaryKey** — `entity`, `key_field`, `reference_series`. Source: tab 6 (top block).
7. **CriticalRule** — `id` (PK int), `topic`, `rule`, `rationale`,
   `severity` (derive: `error` if the rule text implies rejection/404/400/silent-fail,
   else `warning`). Source: tab 7.
8. **Route** — `code` (A|B|C|D, PK), `name`, `applies_to`, `entities_in_order` (text),
   `references_produced`. Source: tab `R · Routes Overview`.
9. **RouteEntity** (join) — `route_code` (FK), `entity` (FK→DeclarationType.name),
   `usage` (`Required` | `Auto` | `Optional` | `Referenced` | `Not used`),
   `step_refs` (text, e.g. "Steps 2,4"). Source: tab `R2 · Route × Entity`
   (derive `usage` from the cell colour/text key). Skip `Not used` cells.
10. **RouteStep** — `route_code` (FK), `step_no` (text — may be `0`,`✦`,`⏳`,`X`),
   `phase`, `action`, `entity` (links to DeclarationType / tab), `endpoint`, `op_type`,
   `reference_returned`, `key_element_names` (text list — split and resolve each to a
   DataElement code where it matches), `timing`. Source: tabs `RA`–`RD`.
   Order RouteSteps by their appearance order in the tab (the `✦`/`⏳` rows are
   system/physical events — store with `is_api_call=false`).

## PROCEDURE

1. Read tab 1; upsert **DataElement** keyed on `code`. Enrich `validation_rule` /
   `has_validation` / `choice_endpoint` from tab 4 (join on `code`).
2. Read tab 3; upsert **DeclarationType** and the 3 auxiliary APIs
   (Permission Grant, Agent Relationships, Choice Values) with `present=true`,
   `reference_series=null`.
3. For each per-type tab (8–18): for every row, upsert **ElementUsage**
   (type_code from the tab's prefix, element_code from column `Code`,
   `status` from column `Status`, `conditional_rule` from `Description` when status=Conditional).
   Skip any row whose status would be "Not present" (these tabs only list present fields).
4. Read tab 5; upsert **ChoiceList**. Link any **DataElement.choice_endpoint** to the
   matching ChoiceList where the element name equals the choice field name.
5. Read tab 6: load **PrimaryKey** from the top block; load **Relationship** from the
   Foreign-Key block (`kind=primary`) and the Nested-Array block (`kind=nested_array`).
   For nested arrays, `child_key` = the array field name.
6. Read tab 7; upsert **CriticalRule**; set `severity` per the derivation above.
7. Read tab `R · Routes Overview`; upsert **Route** (4 rows: A,B,C,D).
8. Read tab `R2 · Route × Entity`; upsert **RouteEntity** for every cell whose usage
   is not `Not used`. Resolve `entity` to a DeclarationType.
9. Read tabs `RA`–`RD`; upsert **RouteStep** in tab order. For each step's
   `key_element_names`, split on commas and resolve each token to a **DataElement.code**
   (`op_type=submit` → `op_type`; unresolved tokens like "importer/exporter parties"
   are kept as free text and flagged, not dropped). This is the tie-back: every
   RouteStep points at the entity/per-type tab, the reference it emits, and the
   DataElement codes it reads or writes.
10. Cross-validate before commit (see below). Only commit if all checks pass.

## VALIDATION / ACCEPTANCE CHECKS (must all pass)

- Exactly **253** DataElement rows; codes are contiguous `DE0001`…`DE0253`, no gaps/dupes.
- Every **ElementUsage.element_code** resolves to a DataElement; every
  **declaration_type_code** resolves to a DeclarationType.
- For every element, the set of types in ElementUsage equals the non-blank cells for that
  element in tab 2 (matrix and per-type tabs must agree). Report any mismatch; do not auto-resolve.
- Every DataElement with a non-null `choice_endpoint` has a matching ChoiceList.
- Every Relationship `parent_entity`/`child_entity` resolves to a known DeclarationType or sub-object.
- **11** primary entity reference series present (ENS/DEC/goods_id/SFD/DEC/SUP/FFD/GLR/GMR/ICR + Shared=none).
- **26** CriticalRule rows.
- Exactly **4** Route rows (A,B,C,D). Every RouteEntity.entity and RouteStep.entity
  resolves to a known DeclarationType. Every RouteStep.reference_returned matches a
  known reference series (ENS/DEC/goods_id/SFD/SUP/FFD/GLR/GMR/ICR) or is a status/—.
- Route A lists Permission Grant, Declaration Header, Consignment, Goods Item, SFD
  Consignment (auto), GVMS GMR, Supplementary Declaration. Route C lists Full Frontier
  Declaration + Goods Item. Route D lists Maritime ICR. (Sanity check against R2.)

## RULES TO HARD-CODE AS VALIDATIONS (from tab 7 — high value)

Surface these as active constraints in the target, not just stored text:
- Endpoint is `/headers`, not `/declaration_headers` (rule 1).
- SFD lookup param is `consignment_number`, not `consignment_reference` (rule 2).
- For `movement_type=3a`, treat `type_of_passive_transport`, `transport_charges`,
  `carrier_name`, `place_of_acceptance_same_as_loading`,
  `place_of_delivery_same_as_unloading` as **Mandatory** even though base status is Conditional (rule 3).
- `arrival_date_time`: `DD/MM/YYYY HH:MM:SS` UTC, not past, ≤14 days future (rule 4).
- `goods_domestic_status` = single char `D`, not `NIDOM` (rule 10).
- `op_type=update` is full replacement — re-send all retained fields (rules 16, 24).
- On update, omit `declaration_number` / `consignment_number` (rule 17).
- After first consignment submit, Declaration Header is locked except
  `identity_no_of_transport` (rule 18).
- `goods_id` differs between ENS and SDI context — re-fetch in SDI context (rule 15).

## OUTPUT

Produce: (a) a load summary (counts per entity, inserted vs updated),
(b) the validation-check results table (pass/fail per check),
(c) a list of any rows skipped or flagged with the reason.
If any acceptance check fails, **roll back / do not commit** and return the discrepancies.
