# Consignment (Cons_Header_Mapping) — field sources by module

Filled version of `COnsignments(Cons_Header_Mapping).csv`, organised by the V3
`Modules/` pipeline. Every consignment field is owned by exactly one module step:

| Step | Module script | What it does to consignment fields |
| --- | --- | --- |
| 1. Acquire + load | `Modules/Ingestion/ING_01_acquire_email.py` → `ING_03_load_raw.py` | Lands the Sales Orders workbook row verbatim into `ING.BKD_Raw_Sales_Orders.PayloadJson` (snake_cased keys). No field mapping happens here. |
| 2. NORMALISE | `Modules/Processing/PRS_01_engine.py` via `mapping.SALES_ORDER_TO_CONSIGNMENT` (`mapping.py:171-274`) | Maps SO JSON keys (with aliases) → `PRS.Consignment` columns. Rule `DP-FR-01:MAP_SO_CONSIGNMENT`. |
| 3. ENRICH | `PRS_01_engine.py` via `process_data.py:1045-1259` | Partner-master lookups (`CFG.Partner_Master`), BKD constants (`mapping.BKD_QAS_CONSTANTS`), assumption defaults. Logged as `MASTERDATA:*` / `ASSUMPTION:*`. |
| 4. VALIDATE | `PRS_01_engine.py` + `check_choice.py` | Mandatory/conditional checks; choice-value membership via `CFG.Choice_Field_Map` + `CFG.Choice_Value_Cache` (refreshed by `Global/REF_01_choice_values.py`). |
| 5. Promote | `Modules/Submission/SUB_01_promote.py` | Validated `PRS.Consignment` rows → `STG`. No value changes. |
| 6. Submit | `SUB_02_submit.py` + `tss_client.py` | Builds the `POST /consignments` payload; payload-time rules (drop empties, omit buyer/seller blocks, GB-consignee rule). |
| 7. Mirror | `SUB_03_mirror.py` / `SUB_06_fetch_json.py` | Reads TSS responses back (READ-only fields); logs to `API.Call`. |

Cross-checked against the working V2 BKD prod flow
(`.codex_tmp/Fusion_Flow_V2_BKD_prod/scripts/submit_pipeline.py` `build_consignment_payload`,
staging in `app/ingestion/sales_orders_stage.py`) and TSS Declaration API v2.9.5 Postman.

## Traceability principle

**Every value that reaches TSS must be traceable to an ING row with full lineage.**
That includes masterdata: partners and products are *ingested data*, not configuration.
The target model (matching the mapping workbook) is:

- `ING.Customer_Master` — partner/customer rows landed from Graph, with lineage columns
  `SourceDatabase, SourceSchema, SourceTable, SourceID, PartnerType, EORI, EORIGB, …,
  RowHash, SourceCreatedAt, SourceUpdatedAt, SourceLoadedAt, ImportedAt, UpdatedAt, IsActive`.
- `ING.Product_Master` — SKU rows landed from Graph, same lineage shape
  (`SKU, ProductCode, GoodsDescription, CommodityCode, ControlledGoods, GrossWeightKg, …`).
- Acquisition through the same channel as transactional data:
  `ING_01_acquire_email.py` (Microsoft Graph) → `ING_03_load_raw.py`, one `EXC` execution
  per load, `RowHash` for change detection.
- ENRICH reads the **latest active ING masterdata row** and tags the write with
  `MASTERDATA:*` + the ExecutionID of the run — so any TSS payload value can be walked
  back to the exact source row and the email/file it arrived in.
- Environment constants (Rule 10 `D`, Rule 13 EORI fallback, defaults `GB`/`0`) belong in
  `CFG.Application_Parameters` — data, per-environment, auditable — not in Python.

### Current state vs target (gaps to close)

| # | Current (R1 engine) | Target | Where |
| --- | --- | --- | --- |
| 1 | ENRICH reads `CFG.Partner_Master` / `CFG.Product_Master`; if the table is missing it silently skips enrichment | Read `ING.Customer_Master` / `ING.Product_Master` (Graph-landed, lineage-carrying); missing masterdata = validation exception, not silent skip | `process_data.py:819-949` (`fetch_product_master`, `fetch_partner_master`) |
| 2 | Ingestion cycle only acquires ENS + sales orders | Acquire masterdata attachments from Graph too and land them in `ING.*_Master` via the same `ING_01 → ING_03` path | `Modules/Ingestion/ING_00_run_cycle.py`, `CFG.Job` |
| 3 | BKD constants hardcoded in `mapping.BKD_QAS_CONSTANTS` (`D`, `XI379692092000`, `GBAUBELBELBEL`, `Y`) and ENRICH defaults (`GB`, `0`, `no`) in code | Rows in `CFG.Application_Parameters`, read per run | `mapping.py:284-289`, `process_data.py:1206-1259` |
| 4 | EORI fallback `XI379692092000` is a literal | Resolve from the Birkdale partner row in `ING.Customer_Master` (PartnerType/EORI), constant only as last-resort CFG param | `mapping.py` Rule 13 |
| 5 | Consignment `transport_document_number` derived from the SO document ref (`DP-FR-01:DERIVE_TRANSPORT_DOCUMENT_FROM_SALES_ORDER`) | Pull down the **ENS header ICR**: `ING.BKD_Raw_ENS.transport_document_number` → `PRS.ENS_Header.transport_document_number` → consignment. The ENS "transport document number / ICR number" IS the consignment-level transport document in TSS terms (`mapping.py:54-56`) | `process_data.py:707-709` |

Until these land, the ENRICH sources documented below describe the **current** engine
(`CFG.Partner_Master` / `CFG.Product_Master`); the workbook's `ING.Customer_Master` /
`ING.Product_Master` columns are the target lineage-bearing equivalents.

---

## Step 2 — NORMALISE (ING → PRS.Consignment)

Source is always `ING.BKD_Raw_Sales_Orders.PayloadJson`; "SO key" = accepted alias keys.

| Field | Mand. | SO key aliases | Notes |
| --- | --- | --- | --- |
| goods_description | YES | `consignment_description`, `consignment_desc` | Usually empty in SO → built at ENRICH (see below). |
| trader_reference | NO | `trader_reference`, `trader_ref`, `customer_reference`, `manifest_reference` | Fallback: SO `Document No.` (S-ORD…). >99-goods split adds `-NN` suffix. Max 100. |
| transport_document_number | YES | **ENS header ICR** — `ING.BKD_Raw_ENS.transport_document_number` (the ENS "transport document number / ICR number", parsed from the ENS email by `ING_02_parse_ens`) → `PRS.ENS_Header.transport_document_number` → pulled down to the consignment | Max 35. ⚠ Current engine instead derives it from the SO document ref (`DP-FR-01:DERIVE_TRANSPORT_DOCUMENT_FROM_SALES_ORDER`, `process_data.py:707`) — wrong source, see gap 5. |
| controlled_goods | YES | `controlled_goods`, `is_controlled_goods`, `controlled` | `to_yes_no()`. Empty → ENRICH roll-up. |
| goods_domestic_status | COND | `goods_domestic_status`, `domestic_status` | Overwritten at ENRICH regardless (constant `D`). |
| destination_country | NO | `destination_country`, `dest_country`, `country_of_destination` | Country name → ISO2. **Not** derived from consignee EORI prefix. |
| container_indicator | COND | `container_indicator`, `containerised`, `container` | Empty → ENRICH default `0`. |
| consignor_* / consignee_* / importer_* / exporter_* | see below | role-prefixed aliases (`shipper_*`, `sender_*`, `receiver_*`, `sales_header_ship_to_*`) | Party values from SO row if present; enrichment fills gaps. |
| buyer_same_as_importer / seller_same_as_exporter | NO | direct + `buyer_importer_same` / `seller_exporter_same` | `to_yes_no()`, default `yes`. |
| buyer_* / seller_* | NO | direct aliases (`buyer_address` → `buyer_street_and_number`) | Raw only, no enrichment. PRS columns: `buyer_street_and_number`, `seller_street_and_number`. |
| consignment_number | — | — | Derived `doc_ref[:40]` (`DP-FR-01:DERIVE_CONSIGNMENT_NUMBER_FROM_SALES_ORDER`) as internal key; TSS assigns the real DEC ref. |

## Step 3 — ENRICH (constants, master data, roll-ups)

Party resolution: lookup key = SO `Sell-to Customer No.` (e.g. C003956) →
`Partner_Master` role match (`_best_partner_match`; `CFG.Partner_Master` today →
`ING.Customer_Master` target), fallback = Birkdale partner row.
(V2 equivalent: `Partners.account_ref` / `DATA.BKD_Customers.CustomerNo`.)

| Field | Filled with | Rule tag |
| --- | --- | --- |
| goods_description | Per-goods-line descriptions joined `" \| "` — line `goods_description`, else `Product_Master.GoodsDescription`/`ProductName` (CFG today → `ING.Product_Master` target), else SKU. Dedup, truncate 254. | `ASSUMPTION:CONSIGNMENT_DESCRIPTION_FROM_DOCUMENT_REF` when nothing at all |
| controlled_goods | Roll-up: `yes` if ANY goods line `Product_Master.ControlledGoods = 1` (CFG today → ING target), else `no` | `MASTERDATA:*CONTROLLED_GOODS_ROLLUP` / `ASSUMPTION:BKD_DEFAULT_CONTROLLED_GOODS_NO` |
| goods_domestic_status | Constant `D` | `ASSUMPTION:BKD_QAS_CONSTANT` (Rule 10) |
| destination_country | `GB` if empty | `ASSUMPTION:BKD_DEFAULT_DESTINATION_COUNTRY_GB` |
| container_indicator | `0` if empty | `ASSUMPTION:BKD_DEFAULT_NOT_CONTAINERISED` |
| importer_eori | `XI379692092000` (Birkdale XI EORI) if empty — **also sets consignor_eori** | `ASSUMPTION:BKD_IMPORTER_FALLBACK` (Rule 13) |
| importer_name/street/city/postcode/country | `CFG.Partner_Master` role=Importer (fallback Birkdale row = Birkdale Sales Ltd own details) | `MASTERDATA:BKD_PARTNER_MASTER_IMPORTER_*` |
| consignor_name/street/city/postcode/country | Partner role=Consignor, fallback Birkdale | `MASTERDATA:BKD_PARTNER_MASTER_CONSIGNOR_*` |
| consignee_eori | Customer master EORI via Sell-to lookup | validate: mandatory OR full address |
| consignee_name/street/city/postcode/country | Partner role=Consignee (`replace_source=True` — master wins over SO `Ship-to *` values); country default `GB`; postcode never defaulted | `MASTERDATA:BKD_PARTNER_MASTER_CONSIGNEE_*` |
| exporter_eori | `XI379692092000` if empty | `ASSUMPTION:BKD_EXPORTER_EORI_FALLBACK` |
| exporter_name/street/city/postcode/country | Partner role=Exporter, fallback Birkdale | `MASTERDATA:BKD_PARTNER_MASTER_EXPORTER_*` |

Text fields truncated to 35 (`PRS_CONSIGNMENT_TEXT_MAX`); postcodes/countries via `normalise_code`.

## Step 4 — VALIDATE (choice fields)

`CFG.Choice_Field_Map` rows scoped to `PRS.Consignment` (seeded `014_cfg_choice_field_map.sql`,
aligned `016`): `goods_domestic_status`, `no_sfd_reason`, `sfd_declaration_choice`
(→ column `declaration_choice`). Membership checked against `CFG.Choice_Value_Cache`
(refreshed by `REF_01_choice_values.py`). `controlled_goods` / `container_indicator` are
deliberately not choice-registered (no `/choice_values` endpoint).

## Step 6 — SUBMIT (payload-time rules in SUB_02)

| Rule | Behaviour |
| --- | --- |
| op_type | Literal `create` / `update` / `submit` / `cancel` — set by the job. |
| declaration_number | From `PRS.ENS_Header.declaration_number` (ENS ref returned by TSS at header create). Required on create. |
| consignment_number | `''` on create; TSS returns DEC ref → stored, required on update/cancel (SUB_04/SUB_05). |
| Empty fields | Dropped from payload (`v not in (None,'')`). |
| consignee_eori GB rule | If `consignee_eori` starts `GB` AND full name+street+city+postcode+country present → EORI **removed** from payload. |
| buyer/seller blocks | Omitted entirely when `buyer_same_as_importer` / `seller_same_as_exporter` = `yes`. |
| use_importer_sde / generate_SD | `normalise_yes_no` on emit; `generate_SD` forced `no` when `no_sfd_reason` present. |

## Step 7 — MIRROR (READ-only, TSS → local)

`status`, `movement_reference_number`, `eori_for_eidr`, `error_code`, `error_message`,
`total_packages`, `gross_mass_kg`, `control_status` — from `GET /consignments`
(SUB_03_mirror / SUB_06_fetch_json) → `Tss_Status` / `RejectReason`; every call logged
to `API.Call`.

## Arrays — not in this payload

`header_previous_document` and `holder_of_authorisation` are **not** part of the ENS
consignment create payload. They belong to the Supplementary Declaration (SDI) path only
(V2: `app/sdi_payloads.py`, stored as `*_json` on SDI header tables). No V3 module sends them.

## Gaps — columns with no module owner yet

`no_sfd_reason`, `align_ukims`, `use_importer_sde`, `declaration_choice`, `generate_SD`,
`ducr`, `supervising_customs_office`, `customs_warehouse_identifier`,
`importer_parent_organisation_eori` — exist in `PRS.Consignment` DDL and pass through
SUB_02 if populated, but **no NORMALISE alias and no ENRICH rule writes them** in the R1
engine (V2 staged them raw-through). To enable the SFD opt-out / SDE flows, add their
mappings to `mapping.SALES_ORDER_TO_CONSIGNMENT` and any defaults to the ENRICH step —
per module, no ad-hoc writes.
