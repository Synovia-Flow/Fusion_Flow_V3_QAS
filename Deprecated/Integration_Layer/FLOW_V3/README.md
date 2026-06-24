# FLOW V3 QAS Orchestration

This folder defines the compact five-step execution shape for Fusion Flow V3 QAS.
The repo is being rebuilt from zero, so only Step 01 is operational today.
Steps 02-05 are local placeholders until their small QAS services are implemented
under `Integration_Layer/App/app/services/`.

| Script | Current state | Future service |
| --- | --- | --- |
| `01_graph_email_ing.py` | Operational Graph email fetch, sender classification and file capture. | `Integration_Layer/App/app/services/graph_ingestion.py` if/when moved into app. |
| `02_ens_details_auto_submit.py` | Placeholder. | `Integration_Layer/App/app/services/ens_details.py` |
| `03_sales_orders_cargo_submit.py` | Placeholder. | `Integration_Layer/App/app/services/sales_orders_cargo.py` |
| `04_status_watcher_notify.py` | Placeholder. | `Integration_Layer/App/app/services/status_watcher.py` |
| `05_sdi_autosubmit.py` | Placeholder. | `Integration_Layer/App/app/services/sdi_autosubmit.py` |

## Design Rule

Do not import V2 through `FUSION_FLOW_APP_ROOT`. Use V2 only as a reference when
building small local QAS services.

## How To Run

```powershell
python Integration_Layer\FLOW_V3\01_graph_email_ing.py --run-mode daily
python Integration_Layer\FLOW_V3\02_ens_details_auto_submit.py --tenant-code BKD --limit 10
python Integration_Layer\FLOW_V3\03_sales_orders_cargo_submit.py --tenant-code BKD --limit 10
python Integration_Layer\FLOW_V3\04_status_watcher_notify.py --tenant-code BKD --limit 10
python Integration_Layer\FLOW_V3\05_sdi_autosubmit.py --tenant-code BKD
```

Steps 02-05 currently return exit code `2` to show they are not implemented yet.
