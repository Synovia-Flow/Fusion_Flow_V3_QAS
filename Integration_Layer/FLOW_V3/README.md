# FLOW V3 QAS Orchestration

This folder defines the compact end-to-end execution shape for Fusion Flow V3.
It is intentionally five entrypoints instead of many scattered operational
scripts.

| Script | Covers | Main output |
| --- | --- | --- |
| `01_graph_email_ing.py` | Graph email fetch, sender classification, attachment capture, inbound trace | Original file/body trace in ING / Integration Layer |
| `02_ens_details_auto_submit.py` | `DETAILS FOR...` email data -> ENS header -> TSS ENS submit | `ENS000...` |
| `03_sales_orders_cargo_submit.py` | Sales Orders Excel -> consignments/goods -> TSS cargo submit | `DEC000...` plus TSS goods IDs |
| `04_status_watcher_notify.py` | TSS status watcher, DEC/SFD/MRN/goods sync, notification gate | `movement_notified_at` after Authorised for Movement email |
| `05_sdi_autosubmit.py` | SDI/SupDec discovery, enrichment, update and submit | SUP/SDI submitted or official TSS review status |

## Execution Mode

Step 01 is QAS-native and runs `Graph/graph_mail_customer_downloader.py` from
this repository.

Steps 02-05 now use the local `app/` and `scripts/` folders copied into this QAS repository. The goal is for QAS to run as its own app, without depending on the V2 repo path.

## Validation Ownership

V3 does not create a second validation system. Each step delegates validation to
the production module that owns the business object.

| Step | Validation location | What is validated |
| --- | --- | --- |
| 01 | `Graph/graph_mail_customer_downloader.py` and future ING persistence | Mailbox access, sender/customer routing, allowed file extension, duplicate filename behaviour, run log. |
| 02 | `app.blueprints.ingest.routes._auto_validate_and_submit_stg_ens_header` | DETAILS email parse, ENS header payload, mandatory header fields, TSS choice values, safe auto-fixes, official TSS response. |
| 03 | `app.blueprints.declarations.routes._submit_prd_cargo_for_header` | TDN grouping, consignment payload, goods payload, TSS consignment create, goods create, goods completion, submit outcome. |
| 04 | `app.ingestion.ens_status_watcher.sync_ens_status_once` and `app.ingestion.automation_notify.check_and_notify_ens_authorised` | Live TSS status read, local mirror upsert, DEC/SFD/MRN/goods sync, movement-authorised gate, SMTP send result before stamping. |
| 05 | `app.ingestion.sdi_autosubmit.run_sdi_autosubmit` | TSS SUP discovery, duplicate prevention, source/masterdata enrichment, API-attempt payload build, TSS goods update, TSS header update, TSS submit, post-submit readback. |

## Design Rule

Only execution basics should block locally: missing file, missing tenant, missing
TSS reference, invalid JSON, or missing operation context. Customs validation
must come from the official TSS response wherever possible.

## How To Run

```powershell
python Integration_Layer\FLOW_V3\01_graph_email_ing.py --run-mode daily
python Integration_Layer\FLOW_V3\02_ens_details_auto_submit.py --tenant-code BKD --limit 10
python Integration_Layer\FLOW_V3\03_sales_orders_cargo_submit.py --tenant-code BKD --limit 10
python Integration_Layer\FLOW_V3\04_status_watcher_notify.py --tenant-code BKD --limit 10
python Integration_Layer\FLOW_V3\05_sdi_autosubmit.py --tenant-code BKD
```
