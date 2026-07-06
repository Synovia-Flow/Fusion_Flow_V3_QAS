#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Synovia Flow 3 portal, live edition.

One small Flask service that BOTH serves the static portal (index.html, assets,
blueprint.json) AND exposes the live blueprint straight from the database:

    GET /                -> the portal
    GET /<file>          -> static assets
    GET /api/blueprint   -> live blueprint JSON (built from the DB, 30s cache)
    GET /api/health      -> liveness probe

The blueprint is built by reusing liveWeb/tools/export_blueprint.py (same queries,
same shape). The DB connection resolves from env (DB_SERVER / DB_NAME / DB_USER /
DB_PASSWORD / DB_DRIVER / DB_ENCRYPT / DB_TRUST) via liveWeb/.env, else the .ini.

If the DB can't be reached, /api/blueprint falls back to the committed static
blueprint.json (so the portal still loads) and reports the error in the JSON header.

Run locally:   python liveWeb/app.py        (or: gunicorn app:app  from liveWeb/)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import importlib
import os

from flask import Flask, Response, abort, jsonify, request, send_from_directory

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE / "tools"))
for _d in ("Modules/Submission", "Modules/Processing"):
    sys.path.insert(0, str(REPO / _d))
import export_blueprint as xb  # reuse load_conn / conn_str / build  # noqa: E402

app = Flask(__name__, static_folder=None)
CACHE_TTL = 30  # seconds
_cache = {"ts": 0.0, "data": None}

# verb -> (runner module, the param that scopes it to one movement, extra per-run overrides)
# Passed to run(overrides=...) as an in-memory scope the runner reads INSTEAD of the
# shared CFG.Application_Parameters, so concurrent portal actions (and a scheduled batch
# running at the same time) can't clobber each other's scope.
VERB = {
    "promote":   ("SUB_01_promote",   "SUBMISSION_MOVEMENT_KEY", {"SUBMISSION_MAX_ROWS": "1"}),
    "submit":    ("SUB_02_submit",    "SUBMISSION_MOVEMENT_KEY", {"SUBMISSION_MAX_ROWS": "1"}),
    "mirror":    ("SUB_03_mirror",    "SUBMISSION_MOVEMENT_KEY", {"SUBMISSION_MAX_ROWS": "1"}),
    "update":    ("SUB_04_update",    "SUBMISSION_MOVEMENT_KEY", {"SUBMISSION_MAX_ROWS": "1"}),
    "cancel":    ("SUB_05_cancel",    "SUBMISSION_MOVEMENT_KEY", {"SUBMISSION_MAX_ROWS": "1"}),
    # reprocess runs the processing engine in REPROCESS mode for the one movement.
    "reprocess": ("PRS_02_reprocess", "PROCESSING_MOVEMENT_KEY", {"PROCESSING_MODE": "REPROCESS"}),
}
EDITABLE = {"movement_type", "type_of_passive_transport", "identity_no_of_transport",
            "nationality_of_transport", "conveyance_ref", "arrival_date_time", "arrival_port",
            "place_of_loading", "place_of_unloading", "seal_number", "transport_charges",
            "carrier_eori", "carrier_name", "carrier_country", "haulier_eori"}


def live_blueprint() -> dict:
    import pyodbc
    conn = pyodbc.connect(xb.conn_str(xb.load_conn()), autocommit=True)
    try:
        return xb.build(conn.cursor())
    finally:
        conn.close()


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "service": "synovia-flow-3", "region": "frankfurt"})


@app.route("/api/health/db")
def health_db():
    """Read-only DB connectivity diagnostic for the Connectivity tab. Attempts a real
    connection + SELECT 1, reports where the config came from, the ODBC drivers the
    image has, latency, a couple of table counts, and the exact error on failure.
    Password is never returned. Always available (no PORTAL_ACTIONS_ENABLED gate)."""
    try:
        cfg = xb.load_conn()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "stage": "config", "error": str(e)}), 200
    source = "env (DB_*)" if os.environ.get("DB_SERVER") else "ini"
    info = {
        "source": source,
        "server": cfg.get("server"), "database": cfg.get("database"),
        "user": cfg.get("user") or "(integrated)", "driver": cfg.get("driver"),
        "encrypt": cfg.get("encrypt", "yes"), "trust": cfg.get("trust_server_certificate", "no"),
    }
    try:
        import pyodbc
        info["odbc_drivers"] = pyodbc.drivers()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, **info, "stage": "import-pyodbc", "error": str(e)}), 200
    if not cfg.get("server"):
        return jsonify({"ok": False, **info, "stage": "config",
                        "error": "No DB_SERVER/.ini connection configured."}), 200
    t0 = time.time()
    try:
        conn = pyodbc.connect(xb.conn_str(cfg), autocommit=True, timeout=8)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        info["elapsed_ms"] = int((time.time() - t0) * 1000)
        try:
            row = cur.execute("SELECT @@VERSION").fetchone()
            info["server_version"] = (row[0].splitlines()[0] if row and row[0] else "")[:120]
        except Exception:
            pass
        checks = {}
        for t in ("CFG.Clients", "CFG.Job", "PRS.BKD_ENS_Header_Tracking", "STG.BKD_ENS_Header", "TSS.BKD_ENS_Header"):
            try:
                checks[t] = int(cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            except Exception:
                checks[t] = None
        info["checks"] = checks
        conn.close()
        return jsonify({"ok": True, **info})
    except Exception as e:  # noqa: BLE001
        info["elapsed_ms"] = int((time.time() - t0) * 1000)
        return jsonify({"ok": False, **info, "stage": "connect", "error": str(e)}), 200


@app.route("/api/blueprint")
def api_blueprint():
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return jsonify(_cache["data"])
    try:
        bp = live_blueprint()
        bp["source"] = "live-db"
        _cache.update(ts=now, data=bp)
        return jsonify(bp)
    except Exception as e:  # DB unreachable -> serve the committed static blueprint
        static = HERE / "blueprint.json"
        if static.exists():
            body = static.read_text(encoding="utf-8").rstrip()
            if body.endswith("}"):
                body = body[:-1] + f',"source":"static-fallback","dbError":{_json(str(e))}}}'
            return Response(body, mimetype="application/json")
        return jsonify({"error": "blueprint unavailable", "detail": str(e)}), 503


def _connect():
    import pyodbc
    return pyodbc.connect(xb.conn_str(xb.load_conn()), autocommit=True)


@app.route("/api/action/<verb>", methods=["POST"])
def api_action(verb):
    """Run a real job scoped to one movement (dry-run still governed by SUBMISSION_DRY_RUN
    and SUBMISSION_ENV). Everything the runner does is tracked in EXC / API.Call / LOG."""
    if verb not in VERB:
        abort(404)
    mk = (request.args.get("mk") or (request.get_json(silent=True) or {}).get("mk") or "").strip()
    if not mk:
        return jsonify({"ok": False, "error": "mk (MovementKey) required"}), 400
    module_name, mk_param, extra = VERB[verb]
    # Per-run scope passed straight into the runner — NO shared CFG mutation, so two
    # concurrent actions (or a scheduled batch) can't overwrite each other's movement key.
    overrides = {mk_param: mk, **extra}
    conn = None
    try:
        conn = _connect(); cur = conn.cursor()
        mod = importlib.import_module(module_name)
        code = mod.run(overrides=overrides)
        row = cur.execute("SELECT Fusion_Status, Tss_Status, declaration_number "
                          "FROM STG.BKD_ENS_Header WHERE MovementKey=?", mk).fetchone()
        status = {"fusion": row[0], "tss": row[1], "decl": row[2]} if row else None
        return jsonify({"ok": code == 0, "verb": verb, "job": module_name, "mk": mk, "status": status})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "verb": verb, "mk": mk, "error": str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/api/enqueue/<verb>", methods=["POST"])
def api_enqueue(verb):
    """Queue a job for the background worker instead of running it in the web process.
    Writes a PENDING row to EXC.Job_Queue; Modules/Global/job_worker.py polls it, runs
    the same runner with per-run scope, and records the outcome. Use this for batches or
    long runs so the request returns immediately (202)."""
    if verb not in VERB:
        abort(404)
    mk = (request.args.get("mk") or (request.get_json(silent=True) or {}).get("mk") or "").strip()
    if not mk:
        return jsonify({"ok": False, "error": "mk (MovementKey) required"}), 400
    conn = None
    try:
        conn = _connect(); cur = conn.cursor()
        row = cur.execute(
            "INSERT INTO EXC.Job_Queue (Verb, MovementKey, Status, RequestedBy) "
            "OUTPUT INSERTED.QueueID VALUES (?, ?, 'PENDING', ?)",
            verb, mk, (request.headers.get("X-Forwarded-For") or "portal")[:100]).fetchone()
        return jsonify({"ok": True, "queued": True, "queueId": int(row[0]), "verb": verb, "mk": mk}), 202
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/api/edit", methods=["POST"])
def api_edit():
    """Edit STG payload fields for a movement (whitelisted). The next Update/Submit
    pushes them to TSS."""
    body = request.get_json(silent=True) or {}
    mk = (body.get("mk") or "").strip()
    fields = {k: v for k, v in (body.get("fields") or {}).items() if k in EDITABLE}
    if not mk or not fields:
        return jsonify({"ok": False, "error": "mk and at least one editable field required"}), 400
    conn = None
    try:
        conn = _connect(); cur = conn.cursor()
        sets = ", ".join(f"[{k}]=?" for k in fields) + ", UpdatedAt=SYSUTCDATETIME()"
        cur.execute(f"UPDATE STG.BKD_ENS_Header SET {sets} WHERE MovementKey=?", *fields.values(), mk)
        return jsonify({"ok": True, "mk": mk, "updated": list(fields)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/<path:p>")
def static_files(p):
    if p.startswith("api/") or p == ".env":
        abort(404)
    target = (HERE / p)
    if not target.exists() or target.is_dir():
        abort(404)
    return send_from_directory(HERE, p)


def _json(s: str) -> str:
    import json
    return json.dumps(s)


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
