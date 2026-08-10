# Fusion Portal API

FastAPI backend for the Fusion Flow portal. It exposes read-only routes over the current Release 1 database model and keeps DB credentials outside the frontend.

## Local Run

```powershell
cd Portal\fusion_api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

The API reads the database connection from `DB_CONN_STR` in the process environment or repo `.env` first. If that is not available, it falls back to `Configuration/Fusion_Flow_QAS.ini`. Override the `.ini` path with:

```powershell
$env:FUSION_FLOW_INI='Z:\Scratch\Fusion_Flow_V3_QAS\Configuration\Fusion_Flow_QAS.ini'
```

## Routes

- `GET /api/health?check_db=true`
- `POST /api/auth/login` - resolves the portal tenant from username/password using existing `CFG.TSS_Credential` rows, with `FLOW_V1_USER` fallback for local app login. Secrets are never returned.
- `POST /api/auth/logout` - closes the audit trail for a session. The portal holds its session client-side, so there is nothing server-side to clear.
- `GET /api/session?client_code=PLE` or `client_code=CWD`
- `GET /api/dashboard?client_code=PLE` or `client_code=CWD`
- `GET /api/declarations?client_code=PLE&page=1&page_size=100&q=&api_date_range=all` - ENS headers from the tenant mirror, paged on `declaration_number`
- `GET /api/consignments?client_code=PLE&status=ALL&q=&limit=100&page=1&page_size=100&api_date_range=all` - reads the tenant mirror; `client_code` may be `BKD`, `PLE`, `CWD` or `CRS`
- `GET /api/consignments/{reference}?client_code=PLE` - keyed by the TSS DEC reference
- `GET /api/ingestion/files?client_code=PLE&limit=50` or `client_code=CWD`
- `POST /api/uploads/consignments/preview`

## Two databases

The portal reads two databases, because neither holds the other's objects.

| Connection | Database | Serves |
| --- | --- | --- |
| `DB_CONN_STR` | `Fusion_Flow_V3_QAS` | Release 1 model: `CFG` (clients, credentials, parameters, status vocabulary), `PRS`, `STG`, `API`, `ING`. Login, settings, uploads, and the ingestion figures. |
| `DB_CONN_STR_MIRROR` | `Fusion_Flow_V3` | Per-tenant TSS mirror. Declarations, consignments, goods, SFD/SDI, and the dashboard totals. |

**Why the read screens moved.** The portal used to answer "how many consignments" from `PRS` while the operator was looking at TSS references — 16 rows in `PRS` against 6,167 in the mirror for the same tenant, and a `DEC Ref` column showing a sales-order reference the engine had derived. `/api/declarations`, `/api/consignments` and the dashboard totals now all read the mirror, so they agree with each other and with TSS. `/api/dashboard` still reports the ingestion figures from `ING.*`, because "what did Fusion pull in" is a different question and only the Release 1 database knows it; the two are returned as separate blocks with a `sources` map, never summed.

In the mirror **the tenant is the schema**: `BKD`, `PLE`, `CWF` and `CRS` each own `Consignments`, `ENS_Headers`, `GoodsItems`, `ApiCallLog`, and - except `CRS` - `SFD_Declarations` and `SDI_Declarations`. There is no `ClientCode` column to filter on. `app/tenant_schema.py` maps a portal client code to its schema (`CWD` → `CWF`, matching the TSS credential bridge) from an allowlist, because the schema name is part of the object name and has to be interpolated rather than bound.

The schemas are near-identical but not identical, and the code asks rather than assumes:

- `CWF` has no `consignment_id` and no `loaded_at`; it has `created_at`
- `CRS` has no `goods_checked_at`, and no SFD/SDI tables at all

`Consignments.consignment_number` is the **TSS DEC reference** (`DEC000000017496719`) and is the primary key in all four schemas - the only consignment key every tenant carries. `declaration_number` is the parent ENS (`ENS000000002730908`). That is why `/api/consignments/{reference}` takes a reference rather than a row id; a numeric value is still accepted for the tenants that keep one.

### Consignment list paging (technique ported from V2 dev02 `3b95601`)

The list hydrates every row it returns - a goods count per row, the SFD and SDI lookups, the header join - so the cost has to track the page, not the tenant. `page`/`page_size` are cut in SQL by a cheap reference-pick over the tenant's `Consignments` alone; only those rows are hydrated. `OFFSET` on the big query is not enough on its own, because SQL Server still evaluates the per-row work for everything before the offset and then discards it.

The page is ordered by `consignment_number DESC`, which has a unique index in every schema, so SQL takes the page straight off that index with no sort. Measured against the live database: page 1 and page 500 of PLE's 100,000 consignments both come back in ~450 ms.

Every filter reaches SQL, so the totals are exact:

- `status` and `q` are predicates on `Consignments`
- `api_date_range` (`all`, `this_week`, `this_month`, `last_6_months`, `last_12_months`, `over_12_months`) binds against `ENS_Headers.arrival_date_time_utc`, a real `datetime2` that sits alongside the `dd/MM/yyyy` string. The window comes from `tss_api_date_bounds` so the SQL path and the in-Python predicate in `app/tss_api_dates.py` cannot drift apart.

```json
"pagination": { "page": 1, "pageSize": 100, "totalPages": 5000,
                "filteredTotal": 100000, "sqlPaged": true }
```

`statusCounts` honours the search and the date range but ignores the active status tab, so the tab counts do not collapse to the current page.

The list reports `ValidationSummary: null`. Fusion's validation summary is computed from `PRS` rows in the Release 1 database; the mirror holds what TSS holds, and nothing is fabricated from it. For the same reason `Status` and `TssStatus` are both the TSS status - the mirror keeps no separate local lifecycle.

### Login audit (ported from V2 dev02 `11bc217`)

`/api/auth/login` and `/api/auth/logout` record every attempt in `AUTH.LoginAudit`, paired by `CorrelationId`. **The `AUTH` schema does not exist in the V3 QAS database**; the proposed DDL is `Configuration/SQL/036_auth_login_audit.sql` and has not been applied. `app/login_audit.py` checks for the table before every write and records nothing while it is absent, so login behaviour is unchanged until the team deploys it. The audit is best-effort by design: it can never be the reason a login fails.

`preview` intentionally does not write to DB yet. It accepts one or more `files`, selects the portal-required attachment ordinal for the current client, hashes only that selected file, inspects CSV/XLSX headers, proposes safe target mappings for review, and returns the target landing path (`ING.Inbound_File` / `ING.Raw_Record`) so the write path can be added deliberately.
### Optional Scanned PDF OCR

Native-text PDFs are parsed locally with `pypdf`. Scanned/image-only PDFs are not sent anywhere unless OCR is explicitly configured. To enable optional OCR for scanned invoices, set Azure Document Intelligence credentials in the runtime environment:

```powershell
$env:PDF_OCR_ENABLED='true'
$env:PDF_OCR_AZURE_ENDPOINT='https://<resource>.cognitiveservices.azure.com'
$env:PDF_OCR_AZURE_KEY='<document-intelligence-key>'
```

Supported aliases are `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` and `AZURE_DOCUMENT_INTELLIGENCE_KEY`. Optional tuning keys are `PDF_OCR_AZURE_MODEL_ID` (default `prebuilt-layout`), `PDF_OCR_AZURE_API_VERSION` (default `2024-11-30`), `PDF_OCR_AZURE_FEATURES`, `PDF_OCR_REQUEST_TIMEOUT_SECONDS`, `PDF_OCR_POLL_TIMEOUT_SECONDS`, and `PDF_OCR_POLL_INTERVAL_SECONDS`.

If OCR is not configured, scanned PDFs still return detected filename/metadata references such as ENS, SUP, or S-ORD numbers plus safe PDF metadata (title, author, producer, dates where available), but no PRS consignment/goods rows are fabricated from image-only content.

## Portal/TSS Prepared Routes

- `GET /api/portal/profiles`
- `GET /api/file-profiles?client_code=PLE`
- `GET /api/file-profiles?client_code=CWD`
- `GET /api/tss/connections` - includes TSS credential status, route, and `fileSelection` for PLE/CWD
- `GET /api/tss/readiness` or `?client_code=PLE` - reports login/connection/file-rule state and whether a real PRS consignment can run the ENS-before-submit dry-run.
- `GET /api/tss/route-plan?client_code=PLE`
- `GET /api/tss/route-plan?client_code=CWD`
- `GET /api/tss/connections/test?client_code=PLE`
- `POST /api/tss/consignments/{consignment_row_id}/update-ens-plan?client_code=PLE` or `client_code=CWD`
- `POST /api/tss/consignments/{consignment_row_id}/submit?client_code=PLE&dry_run=true` or `client_code=CWD`

Current portal bridge contract:

- `PLE` maps to data client `PLE`, TSS credential client `PLE`, preferred env `PRD`, and selects attached file #1.
- `CWD` maps to data client `CWD`, TSS credential client `CWF`, preferred env `TST`, and selects attached file #2.

The bridge does not create CFG tables; client rows and credential state are read from the existing `CFG.Clients`, `CFG.TSS_Credential`, and `CFG.TSS_Environment` tables. TSS submission keeps the route invariant: `UPDATE_CONSIGNMENT_WITH_ENS` must happen before `SUBMIT_CONSIGNMENT`.

`submit` returns the payload plan by default (`dry_run=true`). A live TSS write is blocked unless `dry_run=false&confirm_live=true`, the credential is active, the ENS/declaration number is present, the consignment has goods rows, and required TSS fields are mapped.

## Operational Checks

```powershell
cd Portal\fusion_api
python tools\check_portal_bridge.py
python tools\check_portal_end_to_end_readiness.py
python tools\check_portal_end_to_end_readiness.py --strict
```

This check verifies the PLE/CWD portal bridge against the live database without printing secrets: data client, TSS credential client/env, active password presence, required file ordinal, and absence of the removed portal CFG profile tables.

The readiness check validates login-to-tenant mapping, TSS credential selection, required attachment ordinal, and reports whether real PRS.Consignment data exists for a dry-run ENS update plus submit plan. Use --strict when missing PRS data should fail the check.
