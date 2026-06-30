#!/usr/bin/env python3
"""Fusion Flow V3 QAS - download TSS choice-value reference sets into CFG.

For every active field in CFG.Choice_Field_Registry, calls
    GET <base_url>/choice_values/<field>
and caches the returned {name, value} pairs into CFG.Choice_Value_Cache. The
companion CFG.Choice_Field_Map (file 014) maps each reference set to the schema
columns it governs (e.g. CV 'mode_of_transport' -> our column movement_type).

Config-driven, NO CLI (the scheduler just runs it). Controls come from
CFG.Application_Parameters:
    CHOICE_VALUES_ENV     TSS environment to query   (CFG.TSS_Environment) - default TST
    CHOICE_VALUES_CLIENT  credential to authenticate (CFG.TSS_Credential)  - default BKD
    CHOICE_VALUES_DRY_RUN 1/true = fetch + report only, write nothing
Reference data is client-agnostic; any active credential authenticates the read.

The DB connection comes from Configuration/Fusion_Flow_QAS.ini ([database]).
TSS passwords live in CFG.TSS_Credential (by design); if the stored password is
the placeholder, the gitignored Configuration/tss_credentials.json is used.

Every run opens one EXC.Execution and logs progress / errors to LOG.
"""

from __future__ import annotations

import configparser
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
CRED_FILE = REPO_ROOT / "Configuration" / "tss_credentials.json"

MODULE = "REFERENCE_DATA"
PROCESS = "CHOICE_VALUES_BOOTSTRAP"
PLACEHOLDER_PWD = "<SET_IN_DB>"
RATE_LIMIT_SECONDS = 0.25                    # Rule 14
TIMEOUT = 60


# --------------------------------------------------------------------------- #
# Connection (same shape as the other Global scripts)
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
    user = creds[0]["TssUsername"]
    pwd = creds[0]["TssPassword"]

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
    """Return [(value, name, raw_item), ...] from a /choice_values response."""
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


def upsert_value(cur, field: str, value: str, name: str | None, raw: dict) -> None:
    extra = json.dumps(raw, default=str)[:8000]
    cur.execute(
        "UPDATE CFG.Choice_Value_Cache SET ChoiceName = ?, ExtraJson = ?, IsActive = 1, "
        "RetrievedAt = SYSUTCDATETIME() WHERE ChoiceField = ? AND ChoiceValue = ?",
        name, extra, field, value)
    if cur.rowcount == 0:
        cur.execute(
            "INSERT INTO CFG.Choice_Value_Cache (ChoiceField, ChoiceValue, ChoiceName, ExtraJson) "
            "VALUES (?, ?, ?, ?)", field, value, name, extra)


# --------------------------------------------------------------------------- #
# EXC spine (minimal, matches 004 columns)
# --------------------------------------------------------------------------- #
def open_execution(cur, env_code: str, client_code: str) -> tuple[int, str]:
    cur.execute(
        "INSERT INTO EXC.Execution (EnvCode, ClientCode, ModuleName, ProcessName, RunMode, Status) "
        "OUTPUT INSERTED.ExecutionID, INSERTED.TransactionID VALUES (?, ?, ?, ?, ?, ?)",
        env_code, client_code, MODULE, PROCESS, "scheduled", "RUNNING")
    row = cur.fetchone()
    return int(row[0]), str(row[1])


def log(cur, execution_id: int, txn: str, client: str, step: str, msg: str, level: str = "INFO") -> None:
    cur.execute(
        "INSERT INTO LOG.Process_Log (ExecutionID, TransactionID, ClientCode, ModuleName, "
        "StepName, LogLevel, Message) VALUES (?, ?, ?, ?, ?, ?, ?)",
        execution_id, txn, client, MODULE, step[:100], level[:20], msg[:2000])


# --------------------------------------------------------------------------- #
# Main run
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

    execution_id, txn = (None, "00000000-0000-0000-0000-000000000000")
    if not dry_run:
        execution_id, txn = open_execution(cur, env_code, client_code); conn.commit()

    print(f"Choice-values download: env={env_code} via {client_code} creds; {len(fields)} field(s); "
          f"base={base_url}{' [dry-run]' if dry_run else ''}")
    if not dry_run:
        log(cur, execution_id, txn, client_code, "START",
            f"Downloading {len(fields)} choice fields from {base_url} ({env_code})"); conn.commit()

    session = requests.Session()
    session.auth = HTTPBasicAuth(user, pwd)
    session.headers.update({"Accept": "application/json"})

    total_values = ok_fields = failed_fields = 0
    for i, field in enumerate(fields):
        if i:
            time.sleep(RATE_LIMIT_SECONDS)
        url = f"{base_url}/choice_values/{field}"
        try:
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code != 200:
                failed_fields += 1
                msg = f"{field}: HTTP {resp.status_code} {resp.text[:120].replace(chr(10), ' ')}"
                print(f"  [FAIL] {msg}")
                if not dry_run:
                    log(cur, execution_id, txn, client_code, "FETCH", msg, "WARN"); conn.commit()
                continue
            items = parse_items(resp.json(), field)
            if not dry_run:
                for value, name, raw in items:
                    upsert_value(cur, field, value, name, raw)
                conn.commit()
            total_values += len(items); ok_fields += 1
            print(f"  [ok]   {field}: {len(items)} value(s)")
        except (requests.RequestException, ValueError) as error:
            failed_fields += 1
            msg = f"{field}: {type(error).__name__}: {str(error)[:140]}"
            print(f"  [FAIL] {msg}")
            if not dry_run:
                log(cur, execution_id, txn, client_code, "FETCH", msg, "ERROR"); conn.commit()

    summary = (f"fields ok={ok_fields} failed={failed_fields}; values cached={total_values}")
    print(f"\n{summary}")
    if not dry_run:
        status = "COMPLETED" if failed_fields == 0 else "COMPLETED_WITH_WARNINGS"
        cur.execute(
            "UPDATE EXC.Execution SET EndedAt = SYSUTCDATETIME(), Status = ?, "
            "ItemsFound = ?, ItemsProcessed = ?, ItemsFailed = ? WHERE ExecutionID = ?",
            status[:30], len(fields), ok_fields, failed_fields, execution_id)
        log(cur, execution_id, txn, client_code, "FINISH", summary, "OK")
        conn.commit()
    conn.close()
    return 1 if failed_fields and ok_fields == 0 else 0


def main() -> int:
    import os
    return run(Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI))))


if __name__ == "__main__":
    raise SystemExit(main())
