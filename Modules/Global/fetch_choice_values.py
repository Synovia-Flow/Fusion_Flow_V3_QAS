#!/usr/bin/env python3
"""Fusion Flow V3 QAS - refresh TSS choice-value reference sets into CFG.

For every active field in CFG.Choice_Field_Registry, calls
    GET <base_url>/choice_values/<field>
and refreshes CFG.Choice_Value_Cache. Designed for an initial load AND regular
refreshes: every value is classified relative to what is already cached -

    NEW       - first time we have seen this value
    CHANGED   - the value's name/metadata changed
    UNCHANGED - identical to the cached row
    REMOVED   - cached + active, but no longer returned by TSS (deactivated)

So changes are trivial to find: filter ChangeStatus, or query
CFG.vw_Choice_Value_Changes / CFG.vw_Choice_Sync_Summary.

Logging (per the EXC spine): each run is one EXC.Execution (SYNCING -> COMPLETED),
each field is one EXC.Transaction, and any failure is an EXC.Error.

Config-driven, NO CLI. Controls from CFG.Application_Parameters:
    CHOICE_VALUES_ENV     TSS environment (CFG.TSS_Environment)         - default TST
    CHOICE_VALUES_CLIENT  credential to authenticate (CFG.TSS_Credential) - default BKD
    CHOICE_VALUES_DRY_RUN 1/true = fetch + classify + report, write nothing

DB connection from Configuration/Fusion_Flow_QAS.ini ([database]); TSS password
from CFG.TSS_Credential (falls back to the gitignored tss_credentials.json if the
stored value is the placeholder).
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
CRED_FILE = REPO_ROOT / "Configuration" / "tss_credentials.json"

MODULE = "REFERENCE_DATA"
PROCESS = "SYNCING"
PLACEHOLDER_PWD = "<SET_IN_DB>"
RATE_LIMIT_SECONDS = 0.25                    # Rule 14
TIMEOUT = 60


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def load_db_config(ini_path: Path) -> dict[str, str]:
    if not ini_path.exists():
        raise SystemExit(f"Missing connection file: {ini_path}")
    cp = configparser.ConfigParser(); cp.read(ini_path, encoding="utf-8")
    if "database" not in cp:
        raise SystemExit(f"No [database] section in {ini_path}")
    return {k.lower(): v for k, v in cp["database"].items()}


def conn_str(db: dict[str, str]) -> str:
    yes = lambda v: str(v).lower() in ("yes", "true", "1")
    parts = [f"Driver={db.get('driver', '{ODBC Driver 17 for SQL Server}')}",
             f"Server={db['server']}", f"Database={db['database']}"]
    parts += ([f"Uid={db['user']}", f"Pwd={db.get('password','')}"] if db.get("user")
              else ["Trusted_Connection=yes"])
    parts.append(f"Encrypt={'yes' if yes(db.get('encrypt','yes')) else 'no'}")
    parts.append(f"TrustServerCertificate={'yes' if yes(db.get('trust_server_certificate','no')) else 'no'}")
    return ";".join(parts) + ";"


def q(cur, sql: str, *params: Any) -> list[dict[str, Any]]:
    cur.execute(sql, *params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def param(cur, key: str, default: str = "") -> str:
    rows = q(cur, "SELECT ParameterValue FROM CFG.Application_Parameters "
                  "WHERE ParameterKey = ? AND IsActive = 1", key)
    return rows[0]["ParameterValue"] if rows and rows[0]["ParameterValue"] is not None else default


# --------------------------------------------------------------------------- #
# Credentials: prefer the DB; fall back to the gitignored JSON for the password
# --------------------------------------------------------------------------- #
def resolve_endpoint(cur, env_code: str, client_code: str) -> tuple[str, str, str]:
    envs = q(cur, "SELECT BaseUrl FROM CFG.TSS_Environment WHERE EnvCode = ?", env_code)
    if not envs:
        raise SystemExit(f"No CFG.TSS_Environment row for EnvCode={env_code}")
    base_url = (envs[0]["BaseUrl"] or "").rstrip("/")
    creds = q(cur, "SELECT TssUsername, TssPassword FROM CFG.TSS_Credential "
                   "WHERE ClientCode = ? AND EnvCode = ?", client_code, env_code)
    if not creds:
        raise SystemExit(f"No CFG.TSS_Credential row for {client_code}/{env_code}")
    user, pwd = creds[0]["TssUsername"], creds[0]["TssPassword"]
    if not pwd or pwd == PLACEHOLDER_PWD:
        pwd = _password_from_json(client_code, env_code)
    if not pwd:
        raise SystemExit(f"No usable TSS password for {client_code}/{env_code} "
                         f"(set it in CFG.TSS_Credential or {CRED_FILE}).")
    return base_url, user, pwd


def _password_from_json(client_code: str, env_code: str) -> str | None:
    if not CRED_FILE.exists():
        return None
    data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    rows = data.get("credentials", data if isinstance(data, list) else [])
    for c in rows:
        if (str(c.get("client_code")).upper() == client_code.upper()
                and str(c.get("env_code")).upper() == env_code.upper()):
            return c.get("password")
    return None


# --------------------------------------------------------------------------- #
# Response parsing - tolerant of TSS shape variations
# --------------------------------------------------------------------------- #
def parse_items(payload: Any, field: str) -> list[tuple[str, str | None, dict]]:
    if isinstance(payload, dict):
        items = (payload.get("result") or payload.get("data") or payload.get("values")
                 or payload.get("items") or payload.get(field) or [])
        if isinstance(items, dict):
            items = [items]
    else:
        items = payload
    out: list[tuple[str, str | None, dict]] = []
    for it in items or []:
        if isinstance(it, dict):
            value = (it.get("value") or it.get("code") or it.get("id")
                     or it.get(field) or it.get(f"{field}_code"))
            name = it.get("name") or it.get("description") or it.get("label") or it.get("text")
            raw = it
        else:
            value, name, raw = str(it), None, {"value": it}
        if value is not None and str(value).strip():
            out.append((str(value).strip(), (str(name).strip() if name else None), raw))
    return out


def row_hash(value: str, name: str | None, extra: str) -> str:
    return hashlib.sha256(f"{value}|{name or ''}|{extra}".encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# EXC spine (Execution / Transaction / Error) - columns per file 004
# --------------------------------------------------------------------------- #
def open_execution(cur, env_code: str, client_code: str) -> tuple[int, str]:
    cur.execute(
        "INSERT INTO EXC.Execution (EnvCode, ClientCode, ModuleName, ProcessName, RunMode, Status) "
        "OUTPUT INSERTED.ExecutionID, INSERTED.TransactionID VALUES (?, ?, ?, ?, ?, ?)",
        env_code, client_code, MODULE, PROCESS, "scheduled", "SYNCING")
    row = cur.fetchone()
    return int(row[0]), str(row[1])


def finish_execution(cur, eid: int, status: str, found: int, processed: int, failed: int, err: str = "") -> None:
    cur.execute(
        "UPDATE EXC.Execution SET EndedAt = SYSUTCDATETIME(), Status = ?, "
        "ItemsFound = ?, ItemsProcessed = ?, ItemsFailed = ?, ErrorMessage = ? WHERE ExecutionID = ?",
        status[:30], found, processed, failed, (err or None), eid)


def transaction(cur, eid: int, txn: str, entity_ref: str, status: str) -> None:
    cur.execute(
        "INSERT INTO EXC.[Transaction] (ExecutionID, TransactionID, ClientCode, "
        "EntityType, EntityRef, ProcessName, Status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        eid, txn, None, "CHOICE_FIELD", entity_ref[:100], PROCESS, status[:30])


def exc_error(cur, eid: int, txn: str, code: str, message: str, context: str = "") -> None:
    cur.execute(
        "INSERT INTO EXC.Error (ExecutionID, TransactionID, ClientCode, Severity, ErrorCode, Message, Context) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        eid, txn, None, "ERROR", code[:50], message[:2000], (context or None))


# --------------------------------------------------------------------------- #
# Per-field sync with NEW / CHANGED / UNCHANGED / REMOVED classification
# --------------------------------------------------------------------------- #
def sync_field(cur, field: str, items: list[tuple[str, str | None, dict]], eid: int) -> dict[str, int]:
    existing = {r["ChoiceValue"]: r for r in q(
        cur, "SELECT ChoiceValue, RowHash, IsActive FROM CFG.Choice_Value_Cache WHERE ChoiceField = ?", field)}
    counts = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0}
    seen: set[str] = set()

    for value, name, raw in items:
        seen.add(value)
        extra = json.dumps(raw, default=str)[:8000]
        h = row_hash(value, name, extra)
        ex = existing.get(value)
        if ex is None:
            counts["new"] += 1
            cur.execute(
                "INSERT INTO CFG.Choice_Value_Cache (ChoiceField, ChoiceValue, ChoiceName, ExtraJson, "
                "RowHash, ChangeStatus, IsActive, FirstSeenAt, LastSyncedAt, LastSyncExecutionID, RetrievedAt) "
                "VALUES (?, ?, ?, ?, ?, 'NEW', 1, SYSUTCDATETIME(), SYSUTCDATETIME(), ?, SYSUTCDATETIME())",
                field, value, name, extra, h, eid)
        elif ex["RowHash"] != h or not ex["IsActive"]:
            counts["changed"] += 1
            cur.execute(
                "UPDATE CFG.Choice_Value_Cache SET ChoiceName = ?, ExtraJson = ?, RowHash = ?, "
                "ChangeStatus = 'CHANGED', IsActive = 1, LastSyncedAt = SYSUTCDATETIME(), "
                "LastSyncExecutionID = ?, RetrievedAt = SYSUTCDATETIME() "
                "WHERE ChoiceField = ? AND ChoiceValue = ?",
                name, extra, h, eid, field, value)
        else:
            counts["unchanged"] += 1
            cur.execute(
                "UPDATE CFG.Choice_Value_Cache SET ChangeStatus = 'UNCHANGED', "
                "LastSyncedAt = SYSUTCDATETIME(), LastSyncExecutionID = ? "
                "WHERE ChoiceField = ? AND ChoiceValue = ?", eid, field, value)

    for value, ex in existing.items():
        if value not in seen and ex["IsActive"]:
            counts["removed"] += 1
            cur.execute(
                "UPDATE CFG.Choice_Value_Cache SET IsActive = 0, ChangeStatus = 'REMOVED', "
                "LastSyncedAt = SYSUTCDATETIME(), LastSyncExecutionID = ? "
                "WHERE ChoiceField = ? AND ChoiceValue = ?", eid, field, value)
    return counts


# --------------------------------------------------------------------------- #
def run(ini_path: Path = DEFAULT_INI) -> int:
    import pyodbc
    conn = pyodbc.connect(conn_str(load_db_config(ini_path)), autocommit=False)
    cur = conn.cursor()

    env_code = (param(cur, "CHOICE_VALUES_ENV", "TST") or "TST").strip().upper()
    client_code = (param(cur, "CHOICE_VALUES_CLIENT", "BKD") or "BKD").strip().upper()
    dry_run = (param(cur, "CHOICE_VALUES_DRY_RUN", "0") or "0").strip().lower() in ("1", "true", "yes", "on")

    base_url, user, pwd = resolve_endpoint(cur, env_code, client_code)
    fields = [r["ChoiceField"] for r in q(cur,
              "SELECT ChoiceField FROM CFG.Choice_Field_Registry WHERE IsActive = 1 ORDER BY ChoiceField")]

    eid, txn = (None, None)
    if not dry_run:
        eid, txn = open_execution(cur, env_code, client_code); conn.commit()

    print(f"Choice-values sync: env={env_code} via {client_code}; {len(fields)} field(s); "
          f"base={base_url}{' [dry-run]' if dry_run else ''}  exec={eid}")

    session = requests.Session()
    session.auth = HTTPBasicAuth(user, pwd)
    session.headers.update({"Accept": "application/json"})

    tot = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0}
    ok_fields = failed_fields = 0
    for i, field in enumerate(fields):
        if i:
            time.sleep(RATE_LIMIT_SECONDS)
        url = f"{base_url}/choice_values/{field}"
        try:
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code != 200:
                failed_fields += 1
                msg = f"HTTP {resp.status_code} {resp.text[:140].replace(chr(10), ' ')}"
                print(f"  [FAIL] {field}: {msg}")
                if not dry_run:
                    exc_error(cur, eid, txn, "CHOICE_HTTP", f"{field}: {msg}", url)
                    transaction(cur, eid, txn, field, "SYNC_FAILED"); conn.commit()
                continue
            items = parse_items(resp.json(), field)
            if dry_run:
                ok_fields += 1
                print(f"  [dry]  {field}: {len(items)} value(s) (no write)")
                continue
            c = sync_field(cur, field, items, eid)
            for k in tot:
                tot[k] += c[k]
            ok_fields += 1
            transaction(cur, eid, txn, field, "SYNCED"); conn.commit()
            print(f"  [ok]   {field}: {len(items)} value(s)  new={c['new']} changed={c['changed']} "
                  f"removed={c['removed']} unchanged={c['unchanged']}")
        except (requests.RequestException, ValueError) as error:
            failed_fields += 1
            msg = f"{type(error).__name__}: {str(error)[:160]}"
            print(f"  [FAIL] {field}: {msg}")
            if not dry_run:
                exc_error(cur, eid, txn, "CHOICE_FETCH", f"{field}: {msg}", url)
                transaction(cur, eid, txn, field, "SYNC_FAILED"); conn.commit()

    summary = (f"fields ok={ok_fields} failed={failed_fields}; "
               f"values new={tot['new']} changed={tot['changed']} removed={tot['removed']} "
               f"unchanged={tot['unchanged']}")
    print(f"\n{summary}")
    if not dry_run:
        status = "COMPLETED" if failed_fields == 0 else "COMPLETED_WITH_WARNINGS"
        finish_execution(cur, eid, status, len(fields), ok_fields, failed_fields,
                         "" if failed_fields == 0 else summary)
        conn.commit()
    conn.close()
    return 1 if failed_fields and ok_fields == 0 else 0


def main() -> int:
    return run(Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI))))


if __name__ == "__main__":
    raise SystemExit(main())
