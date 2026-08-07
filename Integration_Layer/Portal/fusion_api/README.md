# Fusion Portal API

FastAPI backend for the Fusion Flow portal. It exposes read-only routes over the current Release 1 database model and keeps DB credentials outside the frontend.

## Local Run

```powershell
cd Integration_Layer\Portal\fusion_api
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
- `GET /api/consignments?client_code=PLE&status=ALL&q=&limit=100&page=1&page_size=100&api_date_range=all` or `client_code=CWD`
- `GET /api/consignments/{consignment_row_id}`
- `GET /api/ingestion/files?client_code=PLE&limit=50` or `client_code=CWD`
- `POST /api/uploads/consignments/preview`

### Consignment list paging (ported from V2 dev02 `3b95601`)

The list hydrates every row it returns - two `OUTER APPLY` blocks, a goods `GROUP BY`, a second goods query and a per-row validation summary - so the cost has to track the page, not the tenant. `page`/`page_size` are cut in SQL by a cheap id-pick over `PRS.Consignment` alone; only those ids are hydrated. `OFFSET` on the big query alone is not enough, because SQL Server still evaluates the per-row work for everything before the offset.

`status` and `q` are SQL predicates on `PRS.Consignment`, so they page safely. `api_date_range` (`all`, `this_week`, `this_month`, `last_6_months`, `last_12_months`, `over_12_months`, from `app/tss_api_dates.py`) filters on the TSS arrival date/time in Python and therefore leaves the SQL-paged path: that request scans up to `CONSIGNMENT_DATE_FILTER_SCAN_CAP` rows instead. The response says which path ran:

```json
"pagination": { "page": 1, "pageSize": 100, "totalPages": 4, "filteredTotal": 340,
                "sqlPaged": true, "scanCap": null, "scanTruncated": false }
```

`scanTruncated: true` means the date-filtered result is incomplete - it is reported rather than passed off as a full result. `statusCounts` honours the search but ignores the active status tab, so the tab counts do not collapse to the current page.

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
cd Integration_Layer\Portal\fusion_api
python tools\check_portal_bridge.py
python tools\check_portal_end_to_end_readiness.py
python tools\check_portal_end_to_end_readiness.py --strict
```

This check verifies the PLE/CWD portal bridge against the live database without printing secrets: data client, TSS credential client/env, active password presence, required file ordinal, and absence of the removed portal CFG profile tables.

The readiness check validates login-to-tenant mapping, TSS credential selection, required attachment ordinal, and reports whether real PRS.Consignment data exists for a dry-run ENS update plus submit plan. Use --strict when missing PRS data should fail the check.
