# Scripts

Operational scripts live here. Run them from the repository root unless a script
explicitly says otherwise.

## Related References

- [Production automation handbook](../docs/README.md) maps the production email automation order.
- [TSS API code cross-reference](../docs/api/API_CODE_CROSS_REFERENCE.md) maps `app/tss_api.py` wrappers to documented endpoints.
- [Graph mail ingestion archive](../docs/additional/GRAPH_MAIL_INGESTION.md) documents `pull_inbound_email.py` and mailbox traceability.
- [Production migration catalogue](../migrations/README.md) documents database prerequisites for script/runtime expectations.

## Primary Pipeline

- `validate_pipeline.py` validates cargo pipeline staging rows.
- `submit_pipeline.py` submits consignments, goods and SDI payloads to TSS.
- `sync_pipeline.py` syncs TSS status and references back into local tables.
- `run_pipeline.py` runs validate, submit and sync phases in sequence.
- `run_tenant_syncs.py` runs the PRD-safe ENS/SFD status sync plus the
  SDI/SupDec discovery worker for configured tenants. It is used by the Render
  automatic sync cron and intentionally avoids legacy `BKD.Staging*` paths.
  When `SDI_AUTO.SUBMIT_ENABLED=true`, this runner launches the SDI worker with
  live TSS update+submit enabled.
- `sdi_autosubmit.py` runs the PRD-safe one-step SDI/SupDec worker. It defaults
  to dry-run and only submits when called with `--submit --no-dry-run` and
  `SDI_AUTO.SUBMIT_ENABLED=true`.

## TSS And GVMS Utilities

- `submit_declarations.py`, `sync_statuses.py` handle legacy ENS declaration rows.
- `submit_gmr.py`, `sync_gmr.py`, `stage_ready_gmrs.py` support Route A / GVMS.
- `sync_tss_tables.py`, `sync_choice_values.py`, `backfill_from_tss.py` mirror TSS data.
- `legacy/cancel_sdi.py` is retained only as a historical, NOT-FOR-PRD
  reference. PRD SDI cancellation must use the portal/API path backed by
  `STG.BKD_SDI_Headers`, `TSS.BKD_SDI_Headers`, and `cancel_sdi()` logging.

## Operator Tools

- `import_missing_consignments_from_fusion_tss.py` imports missing BKD
  consignments from `Fusion_TSS` into `Fusion_TSS_Automation_PRD` STG tables,
  then runs the normal PRD general sync to refresh ENS, goods, SFD and SDI from
  TSS. It defaults to dry-run; use `--execute` to apply.
- `standalone_submit_consignment_goods.py` is the portable submitter for users
  outside this repo.
- `run_submit_pipeline_prompt.ps1` prompts for environment values and then runs
  the repository pipeline.
- `staged_audit.py`, `db_audit.py`, `validate_declarations.py` are diagnostic
  and audit tools.

## Legacy

- `legacy/process_queue.py` and `legacy/poll_statuses.py` remain callable from
  the web orchestrator, but new work should prefer the primary pipeline scripts.
