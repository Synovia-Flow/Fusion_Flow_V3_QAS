#!/usr/bin/env python3
"""Fusion Flow V3 QAS - LIVE DB link for the portal.

Connects to the database and regenerates liveWeb/blueprint.json from the real
tables, so the portal reflects live state:

    CFG.Clients                -> clients
    CFG.Job (per client)       -> data.<CC>.jobs
    CFG.Application_Parameters -> data.<CC>.params  (secrets masked)
    PRS.<CC>_ENS_Header_Tracking / _Submission, STG/TSS, views, EXC/LOG
                               -> kpis / statusMix / submissions / rejectionReasons
                                  / throughput / activity   (for clients that have them)

Connection resolves from liveWeb/.env (generate it with make_env.py) if present,
otherwise from Configuration/Fusion_Flow_QAS.ini [database]. Every DB read is
guarded, so inactive clients (no ENS tables) simply get an onboarding placeholder.

    python liveWeb/tools/make_env.py          # once, to create liveWeb/.env
    python liveWeb/tools/export_blueprint.py   # regenerate liveWeb/blueprint.json
"""

from __future__ import annotations

import configparser
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
ENVF = REPO_ROOT / "liveWeb" / ".env"
OUT = REPO_ROOT / "liveWeb" / "blueprint.json"

NAV = ["Dashboard", "Jobs", "Analytics", "Clients", "Submissions", "Admin"]
MODULE_LABEL = {"INGESTION": "Ingestion", "DATA_PROCESSING": "Processing", "SUBMISSION": "Submission",
                "REFERENCE_DATA": "Reference", "CONFIG": "Reference", "REPORTING": "Reporting"}
STATUS_TONE = {"VALIDATED": "good", "REJECTED": "bad", "SUBMITTED": "sky", "RECONCILED": "flow",
               "STAGED": "muted", "STG_MATERIALISED": "muted", "READY": "muted", "CANCELLED": "muted",
               "Draft": "sky", "Submitted": "sky"}
SECRET = re.compile(r"(password|secret|pwd|token|apikey|api_key|clientsecret)", re.I)
PARAM_KEYS = ["SUBMISSION_ENV", "SUBMISSION_DRY_RUN", "SUBMISSION_MAX_ROWS", "PROCESSING_MODE",
              "PROCESSING_REPROCESS_SCOPE", "DEFAULT_ENV", "API_RATE_LIMIT_SECONDS", "ARRIVAL_MAX_FUTURE_DAYS"]


# --------------------------------------------------------------------------- #
def load_conn() -> dict[str, str]:
    """Prefer liveWeb/.env, else the .ini [database]."""
    if ENVF.exists():
        env: dict[str, str] = {}
        for line in ENVF.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        if env.get("DB_SERVER"):
            return {"server": env.get("DB_SERVER", ""), "database": env.get("DB_NAME", ""),
                    "user": env.get("DB_USER", ""), "password": env.get("DB_PASSWORD", ""),
                    "driver": env.get("DB_DRIVER", "{ODBC Driver 17 for SQL Server}"),
                    "encrypt": env.get("DB_ENCRYPT", "yes"), "trust_server_certificate": env.get("DB_TRUST", "no")}
    if not INI.exists():
        raise SystemExit(f"No liveWeb/.env and no {INI}. Run make_env.py or create the .ini.")
    cp = configparser.ConfigParser(); cp.read(INI, encoding="utf-8")
    return {k.lower(): v for k, v in cp["database"].items()}


def conn_str(db: dict[str, str]) -> str:
    parts = [f"Driver={db.get('driver', '{ODBC Driver 17 for SQL Server}')}",
             f"Server={db['server']}", f"Database={db['database']}"]
    if db.get("user"):
        parts += [f"Uid={db['user']}", f"Pwd={db.get('password', '')}"]
    else:
        parts.append("Trusted_Connection=yes")
    yes = lambda v: str(v).lower() in ("yes", "true", "1")
    parts.append(f"Encrypt={'yes' if yes(db.get('encrypt', 'yes')) else 'no'}")
    parts.append(f"TrustServerCertificate={'yes' if yes(db.get('trust_server_certificate', 'no')) else 'no'}")
    return ";".join(parts) + ";"


def q(cur, sql: str, *p: Any) -> list[dict]:
    """Guarded query - returns [] on any error (missing table/view etc.)."""
    try:
        cur.execute(sql, *p)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def tone_for(status: str) -> str:
    return STATUS_TONE.get(status, "muted")


# --------------------------------------------------------------------------- #
def build_client(cur, c: dict) -> dict:
    cc = c["ClientCode"].strip()
    trk = f"PRS.{cc}_ENS_Header_Tracking"
    oid = q(cur, "SELECT OBJECT_ID(?,'U') AS o", trk)
    has_trk = bool(oid and oid[0]["o"])

    jobs = [{"code": r["JobCode"], "name": r["JobName"],
             "module": MODULE_LABEL.get((r["ModuleName"] or "").upper(), r["ModuleName"] or "—"),
             "schedule": r.get("Schedule") or "—", "active": bool(r["IsActive"])}
            for r in q(cur, "SELECT JobCode,JobName,ModuleName,Schedule,IsActive FROM CFG.Job "
                            "WHERE (ClientCode = ? OR ClientCode IS NULL) ORDER BY ModuleName, StepNo, JobCode", cc)]
    if not jobs:
        jobs = [{"code": "—", "name": "No jobs registered", "module": "—", "schedule": "—", "active": False}]

    params = [{"key": r["ParameterKey"], "value": ("***" if SECRET.search(r["ParameterKey"]) else r["ParameterValue"])}
              for r in q(cur, "SELECT ParameterKey,ParameterValue FROM CFG.Application_Parameters "
                              "WHERE IsActive = 1 ORDER BY ParameterKey")
              if r["ParameterKey"] in PARAM_KEYS]

    if not has_trk:
        return {"kpis": [{"label": "Movements (30d)", "value": 0, "delta": "0", "tone": "muted"},
                         {"label": "Onboarding", "value": "Inactive" if not c["IsActive"] else "Active",
                          "delta": "", "tone": "fusion"}],
                "throughput": [], "statusMix": [], "jobsByModule": [], "rejectionReasons": [],
                "activity": [{"time": "—", "text": "No ENS tables yet — client not activated.", "tone": "muted"}],
                "submissions": [], "jobs": jobs, "params": params}

    mix = q(cur, f"SELECT Fusion_Status s, COUNT(*) n FROM {trk} WHERE ClientCode = ? GROUP BY Fusion_Status", cc)
    by = {r["s"]: r["n"] for r in mix}
    total = sum(by.values())
    val = sum(by.get(s, 0) for s in ("VALIDATED", "STG_MATERIALISED", "READY", "SUBMITTED", "RECONCILED"))
    sub = sum(by.get(s, 0) for s in ("SUBMITTED", "RECONCILED"))
    inflight = sum(by.get(s, 0) for s in ("STAGED", "STG_MATERIALISED", "READY", "SUBMITTING"))
    kpis = [{"label": "Movements (30d)", "value": total, "delta": "", "tone": "flow"},
            {"label": "Validated", "value": val, "delta": "", "tone": "good"},
            {"label": "Submitted to TSS", "value": sub, "delta": "", "tone": "sky"},
            {"label": "Rejected", "value": by.get("REJECTED", 0), "delta": "", "tone": "bad"},
            {"label": "In-flight", "value": inflight, "delta": "", "tone": "fusion"}]
    status_mix = [{"label": s, "value": n, "tone": tone_for(s)} for s, n in sorted(by.items(), key=lambda kv: -kv[1])]

    jobs_by_mod: dict[str, int] = {}
    for j in jobs:
        if j["active"] and j["module"] != "—":
            jobs_by_mod[j["module"]] = jobs_by_mod.get(j["module"], 0) + 1
    jobsByModule = [{"label": k, "value": v} for k, v in jobs_by_mod.items()]

    reasons = [{"label": (r.get("Reason") or "")[:38], "value": r["n"]}
               for r in q(cur, f"SELECT Reason, COUNT(*) n FROM PRS.vw_{cc}_ENS_Header_Reasons "
                               f"GROUP BY Reason ORDER BY n DESC", )][:6]

    thr = q(cur, f"SELECT CONVERT(varchar(10), COALESCE(SubmittedAt, StagedAt, CreatedAt), 103) d, COUNT(*) v "
                 f"FROM {trk} WHERE ClientCode = ? GROUP BY CONVERT(varchar(10), COALESCE(SubmittedAt, StagedAt, CreatedAt), 103) "
                 f"ORDER BY MIN(COALESCE(SubmittedAt, StagedAt, CreatedAt))", cc)
    throughput = [{"d": (r["d"] or "")[:5], "v": r["v"]} for r in thr]

    subq = q(cur, f"SELECT TOP 20 MovementKey, Declaration_Number, Fusion_Status, Tss_Status "
                  f"FROM {trk} WHERE ClientCode = ? ORDER BY UpdatedAt DESC", cc)
    submissions = [{"mk": r["MovementKey"], "decl": r.get("Declaration_Number") or "—",
                    "status": r.get("Tss_Status") or r["Fusion_Status"], "arrival": "—", "port": "—"}
                   for r in subq]

    act = q(cur, "SELECT TOP 6 CONVERT(varchar(5), CreatedAt, 108) t, StepName, Message, LogLevel "
                 "FROM LOG.Process_Log WHERE ClientCode = ? ORDER BY CreatedAt DESC", cc)
    activity = [{"time": r["t"] or "—", "text": (r.get("Message") or r.get("StepName") or "")[:90],
                 "tone": "bad" if (r.get("LogLevel") or "").upper() in ("ERROR", "WARN") else "flow"} for r in act]

    return {"kpis": kpis, "throughput": throughput, "statusMix": status_mix, "jobsByModule": jobsByModule,
            "rejectionReasons": reasons, "activity": activity or [{"time": "—", "text": "No recent activity.", "tone": "muted"}],
            "submissions": submissions, "jobs": jobs, "params": params}


def build(cur) -> dict:
    clients_raw = q(cur, "SELECT ClientCode, ClientName, IsActive, DefaultRoute FROM CFG.Clients ORDER BY IsActive DESC, ClientCode")
    ch = {r["ClientCode"]: r["Channel"] for r in q(cur, "SELECT ClientCode, Channel FROM CFG.Ingestion_Source WHERE IsActive = 1")}
    clients = [{"code": c["ClientCode"].strip(), "name": c["ClientName"], "active": bool(c["IsActive"]),
                "route": c.get("DefaultRoute") or "A", "channel": ch.get(c["ClientCode"], "—"), "admin": "—"}
               for c in clients_raw]
    data = {c["code"]: build_client(cur, {"ClientCode": c["code"], "IsActive": c["active"]}) for c in clients}
    return {"product": {"name": "Synovia Flow", "version": "3", "tagline": "Next-Generation Integration", "poweredBy": "Fusion"},
            "generatedAt": date.today().isoformat(), "nav": NAV, "clients": clients, "data": data}


def main() -> int:
    import pyodbc
    conn = pyodbc.connect(conn_str(load_conn()), autocommit=True)
    try:
        bp = build(conn.cursor())
        OUT.write_text(json.dumps(bp, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        nclients = len(bp["clients"])
        rich = sum(1 for c in bp["clients"] if bp["data"][c["code"]]["submissions"])
        print(f"Wrote {OUT}  ({nclients} clients, {rich} with live movements)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
