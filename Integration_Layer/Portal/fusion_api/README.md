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
- `POST /api/auth/login` - resolves the portal tenant from username/password using existing `CFG.TSS_Credential` rows, with `FLOW_V1_USER` fallback for local app login. Secrets are never returned.`r`n- `GET /api/session?client_code=PLE` or `client_code=CW`
- `GET /api/dashboard?client_code=PLE` or `client_code=CW`
- `GET /api/consignments?client_code=PLE&status=ALL&q=&limit=100` or `client_code=CW`
- `GET /api/consignments/{consignment_row_id}`
- `GET /api/ingestion/files?client_code=PLE&limit=50` or `client_code=CW`
- `POST /api/uploads/consignments/preview`

`preview` intentionally does not write to DB yet. It accepts one or more `files`, selects the portal-required attachment ordinal for the current client, hashes only that selected file, inspects CSV/XLSX headers, proposes safe target mappings for review, and returns the target landing path (`ING.Inbound_File` / `ING.Raw_Record`) so the write path can be added deliberately.

## Portal/TSS Prepared Routes

- `GET /api/portal/profiles`
- `GET /api/file-profiles?client_code=PLE`
- `GET /api/file-profiles?client_code=CW`
- `GET /api/tss/connections` — includes TSS credential status, route, and `fileSelection` for PLE/CW
- `GET /api/tss/route-plan?client_code=PLE`
- `GET /api/tss/route-plan?client_code=CW`
- `POST /api/tss/connections/test?client_code=PLE`
- `POST /api/tss/consignments/{consignment_row_id}/update-ens-plan?client_code=PLE` or `client_code=CW`
- `POST /api/tss/consignments/{consignment_row_id}/submit?client_code=PLE&dry_run=true` or `client_code=CW`

Current portal bridge contract:

- `PLE` maps to data client `PLE`, TSS credential client `PLE`, preferred env `PRD`, and selects attached file #1.
- `CW` maps to data client `CWD`, TSS credential client `CWF`, preferred env `TST`, and selects attached file #2.

The bridge does not create CFG tables; client rows and credential state are read from the existing `CFG.Clients`, `CFG.TSS_Credential`, and `CFG.TSS_Environment` tables. TSS submission keeps the route invariant: `UPDATE_CONSIGNMENT_WITH_ENS` must happen before `SUBMIT_CONSIGNMENT`.

`submit` returns the payload plan by default (`dry_run=true`). A live TSS write is blocked unless `dry_run=false&confirm_live=true`, the credential is active, the ENS/declaration number is present, the consignment has goods rows, and required TSS fields are mapped.

## Operational Checks

```powershell
cd Integration_Layer\Portal\fusion_api
python tools\check_portal_bridge.py`r`npython tools\check_portal_end_to_end_readiness.py`r`npython tools\check_portal_end_to_end_readiness.py --strict
```

This check verifies the PLE/CW portal bridge against the live database without printing secrets: data client, TSS credential client/env, active password presence, required file ordinal, and absence of the removed portal CFG profile tables.


The readiness check validates login-to-tenant mapping, TSS credential selection, required attachment ordinal, and reports whether real PRS.Consignment data exists for a dry-run ENS update plus submit plan. Use --strict when missing PRS data should fail the check.
