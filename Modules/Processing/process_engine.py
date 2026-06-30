#!/usr/bin/env python3
"""Fusion Flow V3 QAS - config-driven processing engine.

ONE engine processes every principal. What differs per client (which ING table,
which columns, which transforms, which rules) is configuration, not code:

    CFG.Processing_Profile    -> source ING table + target PRS tables + keys
    CFG.Processing_Field_Map  -> per target field: source column + transform type
                                 + choice set + mandatory rule + condition + max len
    CFG.Choice_Value_Cache    -> resolves CHOICE fields (name -> code)
    CFG.Carrier_Master        -> MASTER_ENRICH of the carrier block (by EORI)

For each ING row the engine: maps + transforms each field (logging every change to
EXC.Data_Processing_Enhancement), validates (mandatory/conditional incl. the 3a set,
the Rule-4 arrival window, choice membership, max length), writes the canonical row
to the client PRS submission table + a parallel tracking row, and sets Fusion_Status
STAGED -> VALIDATED / REJECTED. One EXC.Execution per run; one EXC.Transaction per
record; failures to EXC.Error.

No CLI. Controls from CFG.Application_Parameters: PROCESSING_CLIENT (default BKD),
PROCESSING_ENTITY (default ENS_HEADER), PROCESSING_DRY_RUN. Connection from the .ini.
To onboard another client: seed its profile + field map - no code change.
"""

from __future__ import annotations

import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import mapping
    from .process_data import ProcessingDb, load_db_config, DEFAULT_INI
except Exception:  # pragma: no cover - script context
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mapping  # type: ignore
    from process_data import ProcessingDb, load_db_config, DEFAULT_INI  # type: ignore

MODULE = "DATA_PROCESSING"
ARRIVAL_MAX_FUTURE_DAYS = 14


# --------------------------------------------------------------------------- #
# Choice-value resolver (name/value -> TSS code), built from the cache.
# --------------------------------------------------------------------------- #
class ChoiceResolver:
    def __init__(self, db: ProcessingDb, fields: set[str]):
        self.by_value: dict[str, set[str]] = {}
        self.by_name: dict[str, dict[str, str]] = {}
        self.names: dict[str, list[tuple[str, str]]] = {}   # (name_lower, value) longest-first
        for cf in fields:
            rows = db._query(
                "SELECT ChoiceValue, ChoiceName FROM CFG.Choice_Value_Cache "
                "WHERE ChoiceField = ? AND IsActive = 1", cf)
            self.by_value[cf] = {(r["ChoiceValue"] or "").strip() for r in rows}
            nm = {}
            for r in rows:
                if r["ChoiceName"]:
                    nm[r["ChoiceName"].strip().lower()] = (r["ChoiceValue"] or "").strip()
            self.by_name[cf] = nm
            self.names[cf] = sorted(nm.items(), key=lambda kv: len(kv[0]), reverse=True)

    def resolve(self, cf: str, incoming: str) -> tuple[str, bool]:
        """Return (resolved_code, matched). If unmatched, returns the input unchanged."""
        s = (incoming or "").strip()
        if not s:
            return s, False
        if s in self.by_value.get(cf, set()):
            return s, True                                   # already a code
        low = s.lower()
        if low in self.by_name.get(cf, {}):
            return self.by_name[cf][low], True               # exact name
        for nm_low, val in self.names.get(cf, []):           # name is a prefix of the input
            if low.startswith(nm_low) and len(nm_low) >= 3:
                return val, True
        return s, False

    def is_member(self, cf: str, value: str) -> bool:
        return (value or "").strip() in self.by_value.get(cf, set())


# --------------------------------------------------------------------------- #
# Condition evaluation for COND fields:  "field=value"  or  "field IN (a,b,c)"
# --------------------------------------------------------------------------- #
def cond_holds(expr: str | None, rec: dict[str, Any]) -> bool:
    if not expr:
        return True
    m = re.match(r"\s*([A-Za-z_]+)\s+IN\s*\((.*)\)\s*$", expr, re.I)
    if m:
        field, vals = m.group(1), m.group(2)
        allowed = {v.strip().lower() for v in vals.split(",")}
        return str(rec.get(field) or "").strip().lower() in allowed
    if "=" in expr:
        field, val = expr.split("=", 1)
        return str(rec.get(field.strip()) or "").strip().lower() == val.strip().lower()
    return True


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# --------------------------------------------------------------------------- #
def _transform(fm: dict, raw: dict, rec: dict, resolver: ChoiceResolver,
               carrier_for, run_date: datetime) -> tuple[Any, bool]:
    """Apply one field map row. Returns (new_value, choice_unmatched_flag)."""
    kind = fm["TransformType"]
    src = fm["SourceColumn"]
    incoming = raw.get(src) if src else None

    if kind in ("READONLY", "API_RETURN"):
        return None, False
    if kind == "CONST" or kind == "QAS":
        return fm["ConstValue"], False
    if kind == "PASSTHROUGH":
        return mapping.normalise_text(incoming), False
    if kind == "CODE":
        return mapping.normalise_code(incoming), False
    if kind == "YESNO":
        return mapping.to_yes_no(incoming), False
    if kind == "DATE_UTC":
        return mapping.normalise_datetime(incoming, now_utc=run_date), False
    if kind == "CHOICE":
        val, matched = resolver.resolve(fm["ChoiceField"], mapping.normalise_text(incoming) or "")
        return (val or None), (bool(val) and not matched)
    if kind == "MASTER_ENRICH":
        key_field = fm["LookupKey"]            # e.g. carrier_eori (already in rec)
        master_col = fm["ConstValue"]          # the master column name to pull
        row = carrier_for(rec.get(key_field))
        return (row.get(master_col) if row else None), False
    if kind == "DERIVE":
        return mapping.normalise_text(incoming), False
    return mapping.normalise_text(incoming), False


def _insert(db: ProcessingDb, schema: str, table: str, obj: dict, pk_col: str) -> int | None:
    if db.dry_run or not db.execution_id:
        return None
    real = set(db.introspect_columns(schema, table))
    cols = [c for c in obj if c in real and c != pk_col]
    cur = db.conn.cursor()
    cur.execute(f"INSERT INTO {schema}.{table} ({', '.join('['+c+']' for c in cols)}) "
                f"OUTPUT INSERTED.{pk_col} VALUES ({', '.join('?' for _ in cols)})",
                *[obj[c] for c in cols])
    return int(cur.fetchone()[0])


def process_row(db: ProcessingDb, raw: dict, profile: dict, fmap: list[dict],
                resolver: ChoiceResolver, carrier_for, run_date: datetime) -> str:
    client = profile["ClientCode"]
    mk = (raw.get(profile["SourceKeyColumn"]) or "").strip()
    eref = f"MK={mk}"
    details_date = (raw.get("DetailsDate") or "").strip() or None
    icr = mk.split("|", 1)[1] if "|" in mk else None

    # 1) tracking row (control/source spine)
    tracking = {
        "ClientCode": client, "MovementKey": mk, "EntityType": profile["EntityKind"],
        "SourceChannel": "EMAIL", "SourceEnsLoadID": raw.get(profile["SourceIdColumn"]),
        "SourceFile": raw.get("SourceFile") or raw.get("SourceCsv"),
        "SourceReceivedUtc": raw.get("SourceReceivedUtc"), "DetailsDate": details_date, "ICR": icr,
        "Fusion_Status": "STAGED", "StagedAt": datetime.now(timezone.utc),
        "LastExecutionID": db.execution_id, "LastTransactionID": db.transaction_id,
    }
    tracking_id = _insert(db, profile["TargetSchema"], profile["TrackingTable"], tracking, "TrackingID")
    db.log_transition(profile["EntityKind"], eref, "STAGING", "STAGED")

    # 2) transform every mapped field, logging old -> new to EXC.DPE
    rec: dict[str, Any] = {"ClientCode": client, "MovementKey": mk,
                           "SourceEnsLoadID": raw.get(profile["SourceIdColumn"]),
                           "TrackingID": tracking_id,
                           "ExecutionID": db.execution_id, "TransactionID": db.transaction_id}
    unmatched: list[str] = []
    for fm in fmap:                              # already ordered by StepNo
        old = raw.get(fm["SourceColumn"]) if fm["SourceColumn"] else None
        new, bad_choice = _transform(fm, raw, rec, resolver, carrier_for, run_date)
        if bad_choice:
            unmatched.append(fm["TargetField"])
        rule = f"{fm['TransformType']}" + (f" ({fm['RuleRef']})" if fm.get("RuleRef") else "")
        db.log_enhancement(profile["TargetSchema"], profile["TargetTable"], fm["TargetField"],
                           eref, old, new, rule)
        if new is not None:
            rec[fm["TargetField"]] = new

    # derived UTC for the arrival bounds check
    arr_utc = mapping.parse_arrival_to_utc(rec.get("arrival_date_time"), now_utc=run_date)
    if arr_utc is not None:
        rec["arrival_date_time_utc"] = arr_utc.replace(tzinfo=None)
    db.log_transition(profile["EntityKind"], eref, "ENRICHING", "ENRICHED")

    # 3) validate
    reasons = validate(rec, fmap, resolver, arr_utc, run_date)
    status = "VALIDATED" if not reasons else "REJECTED"
    if unmatched:
        reasons.append("unresolved choice value(s): " + ", ".join(unmatched))
        status = "REJECTED"
    rec["Fusion_Status"] = status
    if reasons:
        rec["Fusion_Status_Reason"] = "; ".join(reasons)[:2000]

    submission_id = _insert(db, profile["TargetSchema"], profile["TargetTable"], rec, "SubmissionID")
    if not db.dry_run and db.execution_id and tracking_id and submission_id:
        cur = db.conn.cursor()
        cur.execute(f"UPDATE {profile['TargetSchema']}.{profile['TrackingTable']} "
                    f"SET SubmissionID = ?, Fusion_Status = ?, RejectReason = ?, "
                    f"ValidatedAt = SYSUTCDATETIME(), UpdatedAt = SYSUTCDATETIME() WHERE TrackingID = ?",
                    submission_id, status, ("; ".join(reasons)[:2000] or None), tracking_id)

    db.log_transition(profile["EntityKind"], eref, "VALIDATING", status)
    if status == "REJECTED":
        db.log_error("VALIDATE", f"{eref} REJECTED: {'; '.join(reasons)}", "VALIDATION")
    if not db.dry_run:
        db.conn.commit()
    return status


def validate(rec: dict, fmap: list[dict], resolver: ChoiceResolver,
             arr_utc: datetime | None, run_date: datetime) -> list[str]:
    reasons: list[str] = []
    for fm in fmap:
        fld, val = fm["TargetField"], rec.get(fm["TargetField"])
        mand = fm["Mandatory"]
        required = mand == "YES" or (mand == "COND" and cond_holds(fm.get("CondExpression"), rec))
        if required and _empty(val):
            reasons.append(f"{fld} required" + (f" ({fm['CondExpression']})" if mand == "COND" else ""))
        if val and fm.get("MaxLen") and len(str(val)) > fm["MaxLen"]:
            reasons.append(f"{fld} exceeds max length {fm['MaxLen']}")
        if fm["TransformType"] == "CHOICE" and val and not resolver.is_member(fm["ChoiceField"], val):
            reasons.append(f"{fld} '{val}' not in choice set {fm['ChoiceField']}")
    # Rule 4: arrival not in the past, <= 14 days ahead
    if rec.get("arrival_date_time") and arr_utc is None:
        reasons.append("arrival_date_time unparseable (Rule 4)")
    elif arr_utc is not None:
        now = run_date if run_date.tzinfo else run_date.replace(tzinfo=timezone.utc)
        days = (arr_utc - now).total_seconds() / 86400.0
        if days < 0:
            reasons.append("arrival_date_time is in the past (Rule 4)")
        elif days > ARRIVAL_MAX_FUTURE_DAYS:
            reasons.append(f"arrival_date_time more than {ARRIVAL_MAX_FUTURE_DAYS} days ahead (Rule 4)")
    return reasons


def run(ini_path: Path = DEFAULT_INI) -> int:
    db = ProcessingDb.connect(load_db_config(ini_path), dry_run=False)
    client = (db.fetch_parameter("PROCESSING_CLIENT", "BKD") or "BKD").strip().upper()
    entity = (db.fetch_parameter("PROCESSING_ENTITY", "ENS_HEADER") or "ENS_HEADER").strip().upper()
    db.dry_run = (db.fetch_parameter("PROCESSING_DRY_RUN", "0") or "0").strip().lower() in ("1", "true", "yes", "on")
    db._client_code = client

    found = done = failed = 0
    try:
        prof = db._query("SELECT * FROM CFG.Processing_Profile WHERE ClientCode = ? AND EntityKind = ? AND IsActive = 1",
                         client, entity)
        if not prof:
            print(f"[ERROR] No CFG.Processing_Profile for {client}/{entity}."); return 2
        profile = prof[0]
        fmap = db._query("SELECT * FROM CFG.Processing_Field_Map WHERE ClientCode = ? AND EntityKind = ? "
                         "AND IsActive = 1 ORDER BY StepNo", client, entity)
        if not fmap:
            print(f"[ERROR] No CFG.Processing_Field_Map rows for {client}/{entity}."); return 2

        choice_fields = {fm["ChoiceField"] for fm in fmap if fm["TransformType"] == "CHOICE" and fm["ChoiceField"]}
        resolver = ChoiceResolver(db, choice_fields)
        carrier_cache: dict[str, dict | None] = {}

        def carrier_for(eori):
            eori = (eori or "").strip()
            if not eori:
                return None
            if eori not in carrier_cache:
                rows = db._query("SELECT * FROM CFG.Carrier_Master WHERE Eori = ? AND IsActive = 1", eori)
                carrier_cache[eori] = rows[0] if rows else None
            return carrier_cache[eori]

        db.open_execution(MODULE, "PROCESSING", client, "dry-run" if db.dry_run else "scheduled")
        db._client_code = client
        db.advance_execution("PROCESSING", "RUNNING")
        db.log("START", f"Processing {client}/{entity} via {profile['SourceSchema']}.{profile['SourceTable']} "
               f"(dry_run={db.dry_run})")

        run_date = datetime.now(timezone.utc)
        staged = ({r["SourceEnsLoadID"] for r in db._query(
            f"SELECT SourceEnsLoadID FROM {profile['TargetSchema']}.{profile['TrackingTable']} "
            f"WHERE ClientCode = ? AND SourceEnsLoadID IS NOT NULL", client)} if not db.dry_run else set())

        raw_rows = db._query(f"SELECT * FROM {profile['SourceSchema']}.{profile['SourceTable']} "
                             f"ORDER BY {profile['SourceIdColumn']}")
        pending = [r for r in raw_rows if r.get(profile["SourceIdColumn"]) not in staged]
        found = len(pending)
        db.log("SOURCE", f"{len(raw_rows)} source rows; {found} to process.")

        validated = rejected = 0
        for raw in pending:
            mk = (raw.get(profile["SourceKeyColumn"]) or "").strip()
            try:
                st = process_row(db, raw, profile, fmap, resolver, carrier_for, run_date)
                done += 1
                validated += (st == "VALIDATED"); rejected += (st == "REJECTED")
            except Exception as error:  # noqa: BLE001
                failed += 1
                try:
                    db.conn.rollback()
                except Exception:
                    pass
                db.log_error("PROCESS_ROW", f"MK={mk}: {error}", type(error).__name__, traceback.format_exc())

        final = "COMPLETED" if failed == 0 else "COMPLETED_WITH_WARNINGS"
        db.finish_execution(final, found, done, failed)
        db.log("FINISH", f"found={found} processed={done} validated={validated} rejected={rejected} "
               f"failed={failed} enhancements={db.enhancement_count}", "OK")
        print(f"Process {client}/{entity}: found={found} validated={validated} rejected={rejected} "
              f"failed={failed} enhancements={db.enhancement_count}")
        return 0 if failed == 0 else 1
    except Exception as error:  # noqa: BLE001
        db.log_error("RUN", str(error), type(error).__name__, traceback.format_exc())
        db.finish_execution("ERROR", found, done, max(failed, 1), str(error))
        raise
    finally:
        db.close()


def main() -> int:
    return run(Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI))))


if __name__ == "__main__":
    raise SystemExit(main())
