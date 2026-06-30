#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Stage BKD ENS Declaration Headers (PRS).

Reads the raw ENS rows landed by ingestion (ING.BKD_Raw_ENS) and STAGES each one
into the TSS-shaped submission table PRS.BKD_ENS_Header_Submission, in parallel
with a control/source row in PRS.BKD_ENS_Header_Tracking.

The model (no separate before/after table needed):
    ING.BKD_Raw_ENS                = the BEFORE (raw, verbatim)
    PRS.BKD_ENS_Header_Submission  = the AFTER  (final, TSS-shaped)
    EXC.Data_Processing_Enhancement = the per-field BEFORE -> AFTER ledger
                                      (old, new, rule, Transaction_ID)

Every field that is set, replaced by a lookup, normalised or reformatted is
logged to EXC.Data_Processing_Enhancement; each header is one EXC.Transaction
(STAGING -> STAGED); the run is one EXC.Execution. So "insert it all, change a
format" is fully auditable.

No CLI: the scheduler runs `python stage_bkd_ens_header.py`. Behaviour comes from
CFG.Application_Parameters (PROCESSING_DRY_RUN); connection from the .ini.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse the Module 2 DB adapter (EXC spine + DPE + logging) and the pure helpers.
try:  # package / script dual-mode import
    from . import mapping
    from .process_data import ProcessingDb, load_db_config, DEFAULT_INI
except Exception:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mapping  # type: ignore
    from process_data import ProcessingDb, load_db_config, DEFAULT_INI  # type: ignore

CLIENT = "BKD"
MODULE = "DATA_PROCESSING"
PROCESS = "STAGING"
SCHEMA = "PRS"
SUB_TABLE = "BKD_ENS_Header_Submission"
TRK_TABLE = "BKD_ENS_Header_Tracking"

# BKD QAS header constants (citing the empirically-confirmed Critical Rules).
BKD_ARRIVAL_PORT = "GBAUBELBELBEL"          # Rule 12
BKD_TRANSPORT_CHARGES = "Y"                 # Rule 11

# Field plan: (target submission column, source ING column | None, kind, rule label).
#   text   - trim                       code   - trim + upper-case
#   yesno  - normalise to yes/no        date   - TSS dd/mm/yyyy hh:mm:ss UTC (Rule 4)
#   choice - resolve against CFG.Choice_Value_Cache (kept normalised if absent)
#   const:VALUE - fixed BKD value (lookup/QAS), replaces the raw value
#   skip   - not sourced from raw ENS (left NULL; e.g. carrier address block)
HEADER_FIELDS: list[tuple[str, str | None, str, str]] = [
    ("op_type",                             None,                                  "const:create",            "STAGE:op_type=create"),
    ("declaration_number",                  None,                                  "skip",                    "STAGE:blank-on-create (Rule 17)"),
    ("movement_type",                       "movement_type",                       "choice:movement_type",    "ENRICH:choice movement_type"),
    ("type_of_passive_transport",           "type_of_passive_transport",           "choice:type_of_passive_transport", "ENRICH:choice type_of_passive_transport"),
    ("identity_no_of_transport",            "identity_no_of_transport",            "text",                    "NORMALISE:trim identity_no_of_transport"),
    ("nationality_of_transport",            "nationality_of_transport",            "code",                    "NORMALISE:code nationality_of_transport"),
    ("conveyance_ref",                      None,                                  "skip",                    "STAGE:not in ENS source"),
    ("arrival_date_time",                   "arrival_date_time",                   "date",                    "NORMALISE:arrival_date_time DD/MM/YYYY UTC (Rule 4)"),
    ("arrival_port",                        "arrival_port",                        "const:" + BKD_ARRIVAL_PORT, "QAS:BKD arrival_port (Rule 12)"),
    ("place_of_loading",                    "place_of_loading",                    "text",                    "NORMALISE:trim place_of_loading"),
    ("place_of_unloading",                  "place_of_unloading",                  "text",                    "NORMALISE:trim place_of_unloading"),
    ("place_of_acceptance_same_as_loading", "place_of_acceptance_same_as_loading", "yesno",                   "NORMALISE:yesno place_of_acceptance_same_as_loading"),
    ("place_of_acceptance",                 None,                                  "skip",                    "STAGE:not in ENS source"),
    ("place_of_delivery_same_as_unloading", "place_of_delivery_same_as_unloading", "yesno",                   "NORMALISE:yesno place_of_delivery_same_as_unloading"),
    ("place_of_delivery",                   None,                                  "skip",                    "STAGE:not in ENS source"),
    ("seal_number",                         None,                                  "skip",                    "STAGE:not in ENS source"),
    ("transport_charges",                   "transport_charges",                   "const:" + BKD_TRANSPORT_CHARGES, "QAS:BKD transport_charges (Rule 11)"),
    ("carrier_eori",                        "carrier_eori",                        "code",                    "NORMALISE:code carrier_eori"),
    ("carrier_name",                        None,                                  "skip",                    "STAGE:not in ENS source"),
    ("carrier_street_number",               None,                                  "skip",                    "STAGE:not in ENS source"),
    ("carrier_city",                        None,                                  "skip",                    "STAGE:not in ENS source"),
    ("carrier_postcode",                    None,                                  "skip",                    "STAGE:not in ENS source"),
    ("carrier_country",                     None,                                  "skip",                    "STAGE:not in ENS source"),
    ("haulier_eori",                        None,                                  "skip",                    "STAGE:not in ENS source"),
]


def _load_choice_cache(db: ProcessingDb) -> dict[str, set[str]]:
    """Resolve CFG.Choice_Value_Cache after introspecting its columns (Rule 9)."""
    cols = db.introspect_columns("CFG", "Choice_Value_Cache")
    if "ChoiceField" not in cols or "ChoiceValue" not in cols:
        return {}
    active = " AND IsActive = 1" if "IsActive" in cols else ""
    cache: dict[str, set[str]] = {}
    for r in db._query(f"SELECT ChoiceField AS f, ChoiceValue AS v FROM CFG.Choice_Value_Cache WHERE 1=1{active}"):
        cache.setdefault((r["f"] or "").strip(), set()).add((r["v"] or "").strip())
    return cache


def _transform(kind: str, raw: Any, choice_cache: dict[str, set[str]], run_date: datetime) -> Any:
    if kind == "skip":
        return None
    if kind == "text":
        return mapping.normalise_text(raw)
    if kind == "code":
        return mapping.normalise_code(raw)
    if kind == "yesno":
        return mapping.to_yes_no(raw)
    if kind == "date":
        return mapping.normalise_datetime(raw, now_utc=run_date)
    if kind.startswith("const:"):
        return kind.split(":", 1)[1]
    if kind.startswith("choice:"):
        # Choice codes are case-sensitive (e.g. movement_type '3a', not '3A') -
        # trim only, never upper-case. Membership is informational here.
        return mapping.normalise_text(raw)
    return mapping.normalise_text(raw)


def _insert(db: ProcessingDb, schema: str, table: str, obj: dict[str, Any], pk_col: str) -> int | None:
    """Insert obj (real columns only - Rule 9) and return the new identity."""
    if db.dry_run or not db.execution_id:
        return None
    real = set(db.introspect_columns(schema, table))
    cols = [c for c in obj if c in real and c != pk_col]
    cur = db.conn.cursor()
    collist = ", ".join(f"[{c}]" for c in cols)
    qs = ", ".join("?" for _ in cols)
    cur.execute(f"INSERT INTO {schema}.{table} ({collist}) OUTPUT INSERTED.{pk_col} VALUES ({qs})",
                *[obj[c] for c in cols])
    return int(cur.fetchone()[0])


def _stage_one(db: ProcessingDb, raw: dict[str, Any], choice_cache: dict[str, set[str]],
               run_date: datetime) -> None:
    movement_key = (raw.get("DedupKey") or "").strip()
    eref = f"MK={movement_key}"
    details_date = (raw.get("DetailsDate") or "").strip() or None
    icr = movement_key.split("|", 1)[1] if "|" in movement_key else None

    # 1) Tracking row first (the control/source spine) -> TrackingID.
    tracking = {
        "ClientCode": CLIENT, "MovementKey": movement_key, "EntityType": "ENS_HEADER",
        "SourceChannel": "EMAIL", "SourceEnsLoadID": raw.get("LoadID"),
        "SourceFile": raw.get("SourceFile") or raw.get("SourceCsv"),
        "SourceReceivedUtc": raw.get("SourceReceivedUtc"),
        "DetailsDate": details_date, "ICR": icr,
        "Fusion_Status": "STAGED", "StagedAt": datetime.now(timezone.utc),
        "LastExecutionID": db.execution_id, "LastTransactionID": db.transaction_id,
    }
    tracking_id = _insert(db, SCHEMA, TRK_TABLE, tracking, "TrackingID")

    # 2) Build the submission row, logging every field old -> new to EXC.DPE.
    sub: dict[str, Any] = {
        "ClientCode": CLIENT, "MovementKey": movement_key, "TrackingID": tracking_id,
        "SourceEnsLoadID": raw.get("LoadID"),
        "ExecutionID": db.execution_id, "TransactionID": db.transaction_id,
    }
    for target, source, kind, rule in HEADER_FIELDS:
        old = raw.get(source) if source else None
        new = _transform(kind, old, choice_cache, run_date)
        # Log the change (skip pure None->None no-ops; handled inside log_enhancement).
        db.log_enhancement(SCHEMA, SUB_TABLE, target, eref, old, new, rule)
        if new is not None:
            sub[target] = new

    # Derived UTC datetime for the Rule-4 bounds check (also logged).
    arr_utc = mapping.parse_arrival_to_utc(sub.get("arrival_date_time"), now_utc=run_date)
    if arr_utc is not None:
        db.log_enhancement(SCHEMA, SUB_TABLE, "arrival_date_time_utc", eref,
                           None, arr_utc.isoformat(), "DERIVE:arrival_date_time_utc (Rule 4)")
        sub["arrival_date_time_utc"] = arr_utc.replace(tzinfo=None)

    sub["Fusion_Status"] = "STAGED"
    submission_id = _insert(db, SCHEMA, SUB_TABLE, sub, "SubmissionID")

    # 3) Link the tracking row to its submission row.
    if not db.dry_run and db.execution_id and tracking_id and submission_id:
        cur = db.conn.cursor()
        cur.execute(f"UPDATE {SCHEMA}.{TRK_TABLE} SET SubmissionID = ?, UpdatedAt = SYSUTCDATETIME() "
                    f"WHERE TrackingID = ?", submission_id, tracking_id)

    db.log_transition("ENS_HEADER", eref, PROCESS, "STAGED")
    if not db.dry_run:
        db.conn.commit()


def run(ini_path: Path = DEFAULT_INI, dry_run: bool | None = None) -> int:
    db = ProcessingDb.connect(load_db_config(ini_path), dry_run=False)
    if dry_run is None:
        dry_run = (db.fetch_parameter("PROCESSING_DRY_RUN", "0") or "0").strip().lower() in ("1", "true", "yes", "on")
    db.dry_run = dry_run
    db._client_code = CLIENT

    found = staged = failed = 0
    try:
        db.open_execution(MODULE, PROCESS, CLIENT, "dry-run" if dry_run else "scheduled")
        db._client_code = CLIENT
        db.advance_execution(PROCESS, "RUNNING")
        db.log("START", f"Stage BKD ENS headers (Transaction_ID={db.transaction_id}, dry_run={dry_run})")

        run_date = datetime.now(timezone.utc)
        choice_cache = _load_choice_cache(db)

        # Already-staged raw rows (idempotent: one tracking row per ENS LoadID).
        done = {r["SourceEnsLoadID"] for r in db._query(
            f"SELECT SourceEnsLoadID FROM {SCHEMA}.{TRK_TABLE} "
            f"WHERE ClientCode = ? AND SourceEnsLoadID IS NOT NULL", CLIENT)} if not dry_run else set()

        raw_rows = db._query("SELECT * FROM ING.BKD_Raw_ENS ORDER BY LoadID")
        pending = [r for r in raw_rows if r.get("LoadID") not in done]
        found = len(pending)
        db.log("SOURCE", f"{len(raw_rows)} raw ENS rows; {found} not yet staged.")

        for raw in pending:
            mk = (raw.get("DedupKey") or "").strip()
            try:
                _stage_one(db, raw, choice_cache, run_date)
                staged += 1
            except Exception as error:  # noqa: BLE001
                failed += 1
                db.log_error("STAGE", f"MK={mk}: {error}", type(error).__name__, traceback.format_exc())

        final = "STAGED" if failed == 0 else "ERROR"
        db.finish_execution(final, found, staged, failed)
        db.log("FINISH", f"found={found} staged={staged} failed={failed} "
               f"enhancements={db.enhancement_count}", "OK")
        print(f"Stage BKD ENS: found={found} staged={staged} failed={failed} "
              f"enhancements={db.enhancement_count} status={final}")
        return 0 if failed == 0 else 1
    except Exception as error:  # noqa: BLE001
        db.log_error("RUN", str(error), type(error).__name__, traceback.format_exc())
        db.finish_execution("ERROR", found, staged, max(failed, 1), str(error))
        raise
    finally:
        db.close()


def main() -> int:
    ini_path = Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI)))
    return run(ini_path)


if __name__ == "__main__":
    raise SystemExit(main())
