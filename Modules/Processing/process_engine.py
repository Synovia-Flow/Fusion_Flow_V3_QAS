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
def _norm_name(s: str) -> str:
    """Normalise a choice name/value for matching: lower-case, treat brackets as
    spaces (so 'RoRo Accompanied [ICS2]' == 'RoRo Accompanied ICS2' and
    'Belfast Port (GBAUBELBELBEL)' starts with 'Belfast Port'), collapse whitespace."""
    s = (s or "").lower()
    s = re.sub(r"[()\[\]{}]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class ChoiceResolver:
    def __init__(self, db: ProcessingDb, fields: set[str]):
        self.by_value: dict[str, set[str]] = {}
        self.norm_name: dict[str, dict[str, str]] = {}            # norm(name) -> value
        self.norm_list: dict[str, list[tuple[str, str]]] = {}     # longest-first for prefix match
        for cf in fields:
            rows = db._query(
                "SELECT ChoiceValue, ChoiceName FROM CFG.Choice_Value_Cache "
                "WHERE ChoiceField = ? AND IsActive = 1", cf)
            self.by_value[cf] = {(r["ChoiceValue"] or "").strip() for r in rows}
            nm: dict[str, str] = {}
            for r in rows:
                if r["ChoiceName"]:
                    nm[_norm_name(r["ChoiceName"])] = (r["ChoiceValue"] or "").strip()
            self.norm_name[cf] = nm
            self.norm_list[cf] = sorted(nm.items(), key=lambda kv: len(kv[0]), reverse=True)

    def resolve(self, cf: str, incoming: str) -> tuple[str, bool]:
        """Return (resolved_code, matched). Unmatched -> input unchanged.

        Matching cascade: exact code -> exact normalised name -> token-boundary
        prefix in either direction (handles the cache's bracketed annotations,
        e.g. incoming 'Belfast Port' vs cached 'Belfast Port (GBAUBELBELBEL)')."""
        s = (incoming or "").strip()
        if not s:
            return s, False
        if s in self.by_value.get(cf, set()):
            return s, True                                       # already a code
        ni = _norm_name(s)
        if not ni:
            return s, False
        nm = self.norm_name.get(cf, {})
        if ni in nm:
            return nm[ni], True                                  # exact (bracket-insensitive) name
        for nname, val in self.norm_list.get(cf, []):            # longest names first
            if len(ni) < 3 or len(nname) < 3:
                continue
            if nname.startswith(ni) and (len(nname) == len(ni) or nname[len(ni)] == " "):
                return val, True                                 # cache name begins with the input
            if ni.startswith(nname) and (len(ni) == len(nname) or ni[len(nname)] == " "):
                return val, True                                 # input begins with cache name
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


def _upsert(db: ProcessingDb, schema: str, table: str, obj: dict,
            key_cols: list[str], pk_col: str) -> int | None:
    """UPDATE the row matching key_cols (in place) else INSERT. Returns the pk.
    Binds only real columns (Rule 9). NULLs in obj are written (clears stale data)."""
    if db.dry_run or not db.execution_id:
        return None
    real = set(db.introspect_columns(schema, table))
    cur = db.conn.cursor()
    where = " AND ".join(f"[{k}] = ?" for k in key_cols)
    existing = db._query(f"SELECT {pk_col} AS pk FROM {schema}.{table} WHERE {where}",
                         *[obj.get(k) for k in key_cols])
    if existing:
        pk = existing[0]["pk"]
        setc = [c for c in obj if c in real and c != pk_col and c not in key_cols]
        if setc:
            assign = ", ".join(f"[{c}] = ?" for c in setc)
            if "UpdatedAt" in real:
                assign += ", [UpdatedAt] = SYSUTCDATETIME()"
            cur.execute(f"UPDATE {schema}.{table} SET {assign} WHERE {pk_col} = ?",
                        *[obj[c] for c in setc], pk)
        return int(pk)
    cols = [c for c in obj if c in real and c != pk_col]
    cur.execute(f"INSERT INTO {schema}.{table} ({', '.join('['+c+']' for c in cols)}) "
                f"OUTPUT INSERTED.{pk_col} VALUES ({', '.join('?' for _ in cols)})",
                *[obj[c] for c in cols])
    return int(cur.fetchone()[0])


def process_row(db: ProcessingDb, raw: dict, profile: dict, fmap: list[dict],
                resolver: ChoiceResolver, carrier_for, run_date: datetime,
                reprocess: bool = False) -> str:
    client = profile["ClientCode"]
    mk = (raw.get(profile["SourceKeyColumn"]) or "").strip()
    eref = f"MK={mk}"
    details_date = (raw.get("DetailsDate") or "").strip() or None
    icr = mk.split("|", 1)[1] if "|" in mk else None
    proc = "REPROCESSING" if reprocess else "STAGING"
    sch, ttab, stab = profile["TargetSchema"], profile["TrackingTable"], profile["TargetTable"]

    # 1) tracking row (control/source spine) - upsert so reprocess updates in place
    tracking = {
        "ClientCode": client, "MovementKey": mk, "EntityType": profile["EntityKind"],
        "SourceChannel": "EMAIL", "SourceEnsLoadID": raw.get(profile["SourceIdColumn"]),
        "SourceFile": raw.get("SourceFile") or raw.get("SourceCsv"),
        "SourceReceivedUtc": raw.get("SourceReceivedUtc"), "DetailsDate": details_date, "ICR": icr,
        "Fusion_Status": "STAGED", "StagedAt": datetime.now(timezone.utc),
        "LastExecutionID": db.execution_id, "LastTransactionID": db.transaction_id,
    }
    tracking_id = _upsert(db, sch, ttab, tracking, ["ClientCode", "MovementKey"], "TrackingID")
    db.log_transition(profile["EntityKind"], eref, proc, "STAGED")

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
        db.log_enhancement(sch, stab, fm["TargetField"], eref, old, new, rule)
        if new is not None:
            rec[fm["TargetField"]] = new

    arr_utc = mapping.parse_arrival_to_utc(rec.get("arrival_date_time"), now_utc=run_date)
    rec["arrival_date_time_utc"] = arr_utc.replace(tzinfo=None) if arr_utc is not None else None
    db.log_transition(profile["EntityKind"], eref, "ENRICHING", "ENRICHED")

    # 3) validate
    reasons = validate(rec, fmap, resolver, arr_utc, run_date)
    if unmatched:
        reasons.append("unresolved choice value(s): " + ", ".join(unmatched))
    status = "VALIDATED" if not reasons else "REJECTED"
    reason_text = "; ".join(reasons)[:2000] or None
    rec["Fusion_Status"] = status
    rec["Fusion_Status_Reason"] = reason_text          # None clears a prior rejection
    submission_id = _upsert(db, sch, stab, rec, ["ClientCode", "MovementKey"], "SubmissionID")

    # 4) update tracking; on reprocess bump the count and CLOSE OFF resolved errors
    if not db.dry_run and db.execution_id and tracking_id:
        resolved = reprocess and status == "VALIDATED"
        sets = ["SubmissionID = ?", "Fusion_Status = ?", "RejectReason = ?",
                "ValidatedAt = SYSUTCDATETIME()", "LastExecutionID = ?", "UpdatedAt = SYSUTCDATETIME()"]
        vals: list[Any] = [submission_id, status, reason_text, db.execution_id]
        if reprocess:
            sets.append("ReprocessCount = ISNULL(ReprocessCount, 0) + 1")
        if resolved:
            sets += ["ResolvedAt = SYSUTCDATETIME()", "ResolvedByExecutionID = ?"]
            vals.append(db.execution_id)
        db.conn.cursor().execute(
            f"UPDATE {sch}.{ttab} SET {', '.join(sets)} WHERE TrackingID = ?", *vals, tracking_id)

    db.log_transition(profile["EntityKind"], eref, "VALIDATING", status)
    if status == "REJECTED":
        db.log_error("VALIDATE", f"{eref} REJECTED: {'; '.join(reasons)}", "VALIDATION")
    elif reprocess:
        db.log("REPROCESS", f"{eref} resolved -> VALIDATED (prior errors closed)", "OK")
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


def run(ini_path: Path = DEFAULT_INI, mode: str | None = None) -> int:
    db = ProcessingDb.connect(load_db_config(ini_path), dry_run=False)
    client = (db.fetch_parameter("PROCESSING_CLIENT", "BKD") or "BKD").strip().upper()
    entity = (db.fetch_parameter("PROCESSING_ENTITY", "ENS_HEADER") or "ENS_HEADER").strip().upper()
    db.dry_run = (db.fetch_parameter("PROCESSING_DRY_RUN", "0") or "0").strip().lower() in ("1", "true", "yes", "on")
    # NEW (default) processes untracked rows; REPROCESS re-runs already-tracked rows.
    mode = (mode or db.fetch_parameter("PROCESSING_MODE", "NEW") or "NEW").strip().upper()
    reprocess = mode == "REPROCESS"
    scope = (db.fetch_parameter("PROCESSING_REPROCESS_SCOPE", "REJECTED") or "REJECTED").strip().upper()
    target_mk = (db.fetch_parameter("PROCESSING_MOVEMENT_KEY", "") or "").strip()
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

        proc_name = "REPROCESSING" if reprocess else "PROCESSING"
        db.open_execution(MODULE, proc_name, client, "dry-run" if db.dry_run else "scheduled")
        db._client_code = client
        db.advance_execution(proc_name, "RUNNING")
        run_date = datetime.now(timezone.utc)

        raw_rows = db._query(f"SELECT * FROM {profile['SourceSchema']}.{profile['SourceTable']} "
                             f"ORDER BY {profile['SourceIdColumn']}")
        idcol = profile["SourceIdColumn"]
        if reprocess:
            # Re-run rows already tracked, selected by scope (REJECTED|ALL) and optional MovementKey.
            tsql = (f"SELECT SourceEnsLoadID, MovementKey FROM {profile['TargetSchema']}.{profile['TrackingTable']} "
                    f"WHERE ClientCode = ?")
            tparams: list[Any] = [client]
            if scope != "ALL":
                tsql += " AND Fusion_Status = 'REJECTED'"
            if target_mk:
                tsql += " AND MovementKey = ?"; tparams.append(target_mk)
            trk = db._query(tsql, *tparams)
            load_ids = {r["SourceEnsLoadID"] for r in trk if r["SourceEnsLoadID"] is not None}
            pending = [r for r in raw_rows if r.get(idcol) in load_ids]
            db.log("START", f"Reprocessing {client}/{entity} scope={scope}"
                   + (f" MK={target_mk}" if target_mk else "") + f" (dry_run={db.dry_run})")
            db.log("SOURCE", f"{len(trk)} tracked match(es); {len(pending)} source rows to reprocess.")
        else:
            staged = ({r["SourceEnsLoadID"] for r in db._query(
                f"SELECT SourceEnsLoadID FROM {profile['TargetSchema']}.{profile['TrackingTable']} "
                f"WHERE ClientCode = ? AND SourceEnsLoadID IS NOT NULL", client)} if not db.dry_run else set())
            pending = [r for r in raw_rows if r.get(idcol) not in staged]
            db.log("START", f"Processing {client}/{entity} via {profile['SourceSchema']}.{profile['SourceTable']} "
                   f"(dry_run={db.dry_run})")
            db.log("SOURCE", f"{len(raw_rows)} source rows; {len(pending)} to process.")
        found = len(pending)

        validated = rejected = 0
        for raw in pending:
            mk = (raw.get(profile["SourceKeyColumn"]) or "").strip()
            try:
                st = process_row(db, raw, profile, fmap, resolver, carrier_for, run_date, reprocess=reprocess)
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
        print(f"{'Reprocess' if reprocess else 'Process'} {client}/{entity}: found={found} "
              f"validated={validated} rejected={rejected} failed={failed} "
              f"resolved={validated if reprocess else 0} enhancements={db.enhancement_count}")
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
