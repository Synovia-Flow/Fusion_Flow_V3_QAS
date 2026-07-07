#!/usr/bin/env python3
"""Fusion Flow V3 QAS - refresh the commodity-code reference set into CFG.

commodity_code is the largest TSS choice set (~35k codes, with effective dates),
so it lives in its own table CFG.Commodity_Code_Cache and is refreshed by this
dedicated script - keeping the general choice refresh (fetch_choice_values.py)
fast. Same model: initial load + recurring refresh with NEW / CHANGED /
UNCHANGED / REMOVED classification, and the EXC spine
(EXC.Execution SYNCING -> COMPLETED, EXC.Transaction, EXC.Error).

Config-driven, NO CLI. Reuses the CHOICE_VALUES_* controls
(CHOICE_VALUES_ENV / CHOICE_VALUES_CLIENT / CHOICE_VALUES_PATH /
CHOICE_VALUES_DRY_RUN) from CFG.Application_Parameters. DB connection from the .ini.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, date
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

# Reuse the shared helpers from the general downloader (same Modules/Global dir).
try:
    from fetch_choice_values import (
        DEFAULT_INI, conn_str, load_db_config, q, param, resolve_endpoint,
        col_len, _fit, parse_items, row_hash,
    )
except Exception:  # pragma: no cover - script-dir import fallback
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fetch_choice_values import (  # type: ignore
        DEFAULT_INI, conn_str, load_db_config, q, param, resolve_endpoint,
        col_len, _fit, parse_items, row_hash,
    )

FIELD = "commodity_code"
MODULE = "REFERENCE_DATA"
PROCESS = "SYNCING"
RATE_LIMIT_SECONDS = 0.25
TIMEOUT = 180                      # large payload
COMMIT_EVERY = 1000               # batch commits across ~35k rows

# Tolerant date extraction from the raw item metadata.
DATE_FROM_KEYS = ("effective_from", "valid_from", "start_date", "validity_start_date", "date_from")
DATE_TO_KEYS = ("effective_to", "valid_to", "end_date", "validity_end_date", "date_to")


def _date_from(raw: dict, keys) -> date | None:
    """First parseable date among `keys` as a real date (pyodbc-bindable), else None.
    Returns None for non-date / unparseable values so a bad string can never cause a
    SQL date-conversion error."""
    for k in keys:
        v = raw.get(k)
        if v in (None, ""):
            continue
        s = str(v).strip()
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
    return None


def open_execution(cur, env_code: str, client_code: str) -> tuple[int, str]:
    cur.execute(
        "INSERT INTO EXC.Execution (EnvCode, ClientCode, ModuleName, ProcessName, RunMode, Status) "
        "OUTPUT INSERTED.ExecutionID, INSERTED.TransactionID VALUES (?, ?, ?, ?, ?, ?)",
        env_code, client_code, MODULE, "COMMODITY_SYNC", "scheduled", "SYNCING")
    r = cur.fetchone()
    return int(r[0]), str(r[1])


def finish_execution(cur, eid, status, found, processed, failed, err="") -> None:
    cur.execute(
        "UPDATE EXC.Execution SET EndedAt = SYSUTCDATETIME(), Status = ?, "
        "ItemsFound = ?, ItemsProcessed = ?, ItemsFailed = ?, ErrorMessage = ? WHERE ExecutionID = ?",
        status[:30], found, processed, failed, (err or None), eid)


def transaction(cur, eid, txn, status, ref="commodity_code") -> None:
    cur.execute(
        "INSERT INTO EXC.[Transaction] (ExecutionID, TransactionID, ClientCode, EntityType, EntityRef, "
        "ProcessName, Status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        eid, txn, None, "COMMODITY_CODE", ref[:100], PROCESS, status[:30])


def exc_error(cur, eid, txn, code, message, context="") -> None:
    cur.execute(
        "INSERT INTO EXC.Error (ExecutionID, TransactionID, ClientCode, Severity, ErrorCode, Message, Context) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        eid, txn, None, "ERROR", code[:50], message[:2000], (context or None))


def run(ini_path: Path = DEFAULT_INI) -> int:
    import pyodbc
    conn = pyodbc.connect(conn_str(load_db_config(ini_path)), autocommit=False)
    cur = conn.cursor()

    env_code = (param(cur, "CHOICE_VALUES_ENV", "PRD") or "PRD").strip().upper()
    client_code = (param(cur, "CHOICE_VALUES_CLIENT", "BKD") or "BKD").strip().upper()
    default_path = "/x_fhmrc_tss_api/v1/choice_values"
    path_prefix = "/" + (param(cur, "CHOICE_VALUES_PATH", default_path) or default_path).strip().strip("/")
    dry_run = (param(cur, "CHOICE_VALUES_DRY_RUN", "0") or "0").strip().lower() in ("1", "true", "yes", "on")

    base_url, user, pwd = resolve_endpoint(cur, env_code, client_code)
    code_max = col_len(cur, "Commodity_Code_Cache", "CommodityCode")    # truncate to fit
    url = f"{base_url}{path_prefix}/{FIELD}"

    eid, txn = (None, None)
    if not dry_run:
        eid, txn = open_execution(cur, env_code, client_code); conn.commit()
    print(f"Commodity-code sync: env={env_code} via {client_code}; {url}{' [dry-run]' if dry_run else ''}  exec={eid}")

    session = requests.Session()
    session.auth = HTTPBasicAuth(user, pwd)
    session.headers.update({"Accept": "application/json"})

    try:
        resp = session.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            msg = f"HTTP {resp.status_code} {resp.text[:160].replace(chr(10), ' ')}"
            print(f"  [FAIL] {msg}")
            if not dry_run:
                exc_error(cur, eid, txn, "COMMODITY_HTTP", msg, url)
                transaction(cur, eid, txn, "SYNC_FAILED")
                finish_execution(cur, eid, "SYNC_FAILED", 0, 0, 1, msg); conn.commit()
            conn.close(); return 1
        items = parse_items(resp.json(), FIELD)
    except (requests.RequestException, ValueError) as error:
        msg = f"{type(error).__name__}: {str(error)[:160]}"
        print(f"  [FAIL] {msg}")
        if not dry_run:
            exc_error(cur, eid, txn, "COMMODITY_FETCH", msg, url)
            finish_execution(cur, eid, "SYNC_FAILED", 0, 0, 1, msg); conn.commit()
        conn.close(); return 1

    found = len(items)
    print(f"  fetched {found} commodity codes")
    if dry_run:
        print(f"\n[dry-run] {found} codes; no writes."); conn.close(); return 0

    existing = {r["CommodityCode"]: r for r in q(
        cur, "SELECT CommodityCode, RowHash, IsActive FROM CFG.Commodity_Code_Cache")}
    counts = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0}
    seen: set[str] = set()
    n = 0
    for value, name, raw in items:
        value = _fit(value, code_max)
        if not value or value in seen:
            continue
        seen.add(value)
        eff_from = _date_from(raw, DATE_FROM_KEYS)
        eff_to = _date_from(raw, DATE_TO_KEYS)
        extra = json.dumps(raw, default=str)[:8000]
        h = row_hash(value, name, extra)
        ex = existing.get(value)
        if ex is None:
            counts["new"] += 1
            cur.execute(
                "INSERT INTO CFG.Commodity_Code_Cache (CommodityCode, Description, EffectiveFrom, EffectiveTo, "
                "ExtraJson, RowHash, ChangeStatus, IsActive, FirstSeenAt, LastSyncedAt, LastSyncExecutionID, RetrievedAt) "
                "VALUES (?, ?, ?, ?, ?, ?, 'NEW', 1, SYSUTCDATETIME(), SYSUTCDATETIME(), ?, SYSUTCDATETIME())",
                value, name, eff_from, eff_to, extra, h, eid)
        elif ex["RowHash"] != h or not ex["IsActive"]:
            counts["changed"] += 1
            cur.execute(
                "UPDATE CFG.Commodity_Code_Cache SET Description = ?, EffectiveFrom = ?, EffectiveTo = ?, "
                "ExtraJson = ?, RowHash = ?, ChangeStatus = 'CHANGED', IsActive = 1, "
                "LastSyncedAt = SYSUTCDATETIME(), LastSyncExecutionID = ?, RetrievedAt = SYSUTCDATETIME() "
                "WHERE CommodityCode = ?", name, eff_from, eff_to, extra, h, eid, value)
        else:
            counts["unchanged"] += 1
            cur.execute(
                "UPDATE CFG.Commodity_Code_Cache SET ChangeStatus = 'UNCHANGED', "
                "LastSyncedAt = SYSUTCDATETIME(), LastSyncExecutionID = ? WHERE CommodityCode = ?", eid, value)
        n += 1
        if n % COMMIT_EVERY == 0:
            conn.commit(); print(f"    …{n} processed")

    for value, ex in existing.items():
        if value not in seen and ex["IsActive"]:
            counts["removed"] += 1
            cur.execute(
                "UPDATE CFG.Commodity_Code_Cache SET IsActive = 0, ChangeStatus = 'REMOVED', "
                "LastSyncedAt = SYSUTCDATETIME(), LastSyncExecutionID = ? WHERE CommodityCode = ?", eid, value)
    conn.commit()

    transaction(cur, eid, txn, "SYNCED")
    finish_execution(cur, eid, "COMPLETED", found, counts["new"] + counts["changed"] + counts["unchanged"], 0)
    conn.commit()
    print(f"\ncommodity_code: {found} fetched  new={counts['new']} changed={counts['changed']} "
          f"removed={counts['removed']} unchanged={counts['unchanged']}")
    conn.close()
    return 0


def main() -> int:
    return run(Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI))))


if __name__ == "__main__":
    raise SystemExit(main())
