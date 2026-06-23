"""Read-source selectors for the ING/STG/TSS production model.

These helpers keep table names centralized. New production tables are the
default; legacy/compatibility sources are explicit overrides.
"""

from __future__ import annotations

from app.data_model import data_model_read_mode
from app.tenant import qualified_table


_STG_OBJECTS = {
    "ENS_HEADERS": {
        "legacy": "StagingEnsHeaders",
        "compat": "[STG].[vw_BKD_Legacy_ENS_Headers]",
        "new": "[STG].[BKD_ENS_Headers]",
    },
    "CONSIGNMENTS": {
        "legacy": "StagingConsignments",
        "compat": "[STG].[vw_BKD_Legacy_ENS_Consignments]",
        "new": "[STG].[BKD_ENS_Consignments]",
    },
    "GOODS": {
        "legacy": "StagingGoodsItems",
        "compat": "[STG].[vw_BKD_Legacy_GoodsItems]",
        "new": "[STG].[BKD_GoodsItems]",
    },
    "SDI": {
        "legacy": "StagingSupDecHeaders",
        "compat": "[STG].[vw_BKD_Legacy_SDI_Headers]",
        "new": "[STG].[BKD_SDI_Headers]",
    },
    "GMR": {
        "legacy": "StagingGmrs",
        "compat": "[TSS].[vw_BKD_Legacy_GMR_Movements]",
        "new": "[STG].[BKD_GMR_Movements]",
    },
    "SFD": {
        "legacy": "Sfds",
        "compat": "[TSS].[vw_BKD_Legacy_SFD]",
        "new": "[TSS].[BKD_SFD]",
    },
}

_TSS_OBJECTS = {
    "API_LOGS": {
        "legacy": "ApiCallLog",
        "compat": "[TSS].[vw_BKD_Legacy_API_Exchanges]",
        "new": "[TSS].[BKD_API_Exchanges]",
    },
    "OUTBOX": {
        "legacy": "MessageOutbox",
        "compat": "[TSS].[vw_BKD_Legacy_API_Outbox]",
        "new": "[TSS].[BKD_API_Outbox]",
    },
    "JOB_LOGS": {
        "legacy": "JobRunLog",
        "compat": "[TSS].[vw_BKD_Legacy_JobRuns]",
        "new": "[TSS].[BKD_JobRuns]",
    },
    "TSS_ENS_HEADERS": {
        "legacy": "EnsHeaders",
        "compat": "[TSS].[vw_BKD_Legacy_ENS_Headers]",
        "new": "[TSS].[BKD_ENS_Headers]",
    },
    "TSS_ENS_CONSIGNMENTS": {
        "legacy": "EnsConsignments",
        "compat": "[TSS].[vw_BKD_Legacy_ENS_Consignments]",
        "new": "[TSS].[BKD_ENS_Consignments]",
    },
}

_STATUS_COLUMNS = {
    "ENS_HEADERS": {"legacy": "status", "compat": "sub_status", "new": "sub_status"},
    "CONSIGNMENTS": {"legacy": "status", "compat": "sub_status", "new": "sub_status"},
    "GOODS": {"legacy": "status", "compat": "sub_status", "new": "sub_status"},
    "SDI": {"legacy": "status", "compat": "sub_status", "new": "sub_status"},
    "GMR": {"legacy": "status", "compat": "LocalStatus", "new": "sub_status"},
    "SFD": {"legacy": "tss_status", "compat": "TssStatus", "new": "TssStatus"},
}


def _normalise_flow(flow: str) -> str:
    return str(flow or "").strip().upper()


def source_for(flow: str, *, mode: str | None = None, schema_name: str | None = None) -> str:
    """Return the SQL object to read for a named flow."""

    flow_name = _normalise_flow(flow)
    sources = _STG_OBJECTS.get(flow_name) or _TSS_OBJECTS.get(flow_name)
    if not sources:
        raise KeyError(f"Unknown data-model flow: {flow}")

    selected_mode = mode or data_model_read_mode(flow_name)
    if selected_mode not in {"legacy", "compat", "new"}:
        selected_mode = "new"
    # compat views were dropped by migration 081; fall through to new tables
    if selected_mode == "compat":
        selected_mode = "new"

    source = sources[selected_mode]
    if selected_mode == "legacy":
        return qualified_table(source, schema_name)
    return source


def status_column_for(flow: str, *, mode: str | None = None) -> str:
    """Return the status column for a flow in the selected read mode."""

    flow_name = _normalise_flow(flow)
    selected_mode = mode or data_model_read_mode(flow_name)
    if selected_mode not in {"legacy", "compat", "new"}:
        selected_mode = "new"
    return _STATUS_COLUMNS.get(flow_name, {}).get(selected_mode, "status")


def all_repository_flows() -> tuple[str, ...]:
    return tuple(sorted({*_STG_OBJECTS.keys(), *_TSS_OBJECTS.keys()}))
